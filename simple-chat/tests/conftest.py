"""pytest 公共夹具：隔离数据库与打桩 LLM，测试不触网、无需真实 API Key。

设计要点（对齐需求）：
- 每个测试使用 tmp_path 下的独立 SQLite 文件，避免跨用例污染。
- 覆盖环境变量 LLM_API_KEY / DATABASE_URL（DATABASE_URL 通过 monkeypatch 指向 tmp 文件）。
- mock LLM：用 monkeypatch 把 app.llm.client._client 替换为 FakeAsyncOpenAI，
  默认产出确定性分片；需要错误/阻塞行为时由测试再覆盖。
- 提供夹具：client（ASGITransport）、db（独立连接）、sample_conversation。
"""

import asyncio
import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 在导入 app 之前固定与测试相关的最小环境变量。
os.environ.setdefault("APP_ENV", "development")
os.environ["LLM_API_KEY"] = "test-key"
os.environ["LOG_LEVEL"] = "WARNING"

from app.config import settings  # noqa: E402
from app.llm import client as llm_client  # noqa: E402

# aiosqlite.Connection 是 threading.Thread 的子类且默认为非守护线程。
# 若某个连接未在事件循环关闭前被 await 关闭，其工作线程会阻塞在队列上，
# 导致 pytest 进程退出时挂起。测试环境下强制为守护线程，避免 CI 卡死
# （仅影响测试；生产代码已确保所有连接都被正常关闭）。
import aiosqlite.core as _aiosqlite_core

_original_conn_init = _aiosqlite_core.Connection.__init__


def _daemon_conn_init(self, *args, **kwargs):
    _original_conn_init(self, *args, **kwargs)
    self.daemon = True


_aiosqlite_core.Connection.__init__ = _daemon_conn_init


# ───────────────────────── LLM 桩 ─────────────────────────
# 与业务无关的纯内存桩，覆盖流式 / 非流式 / 抛异常 / 阻塞。


class _FakeDelta:
    def __init__(self, text: str) -> None:
        self.content = text


class _FakeChoice:
    def __init__(self, text: str) -> None:
        self.delta = _FakeDelta(text)
        self.finish_reason = "stop"


class _FakeChunk:
    def __init__(self, text: str) -> None:
        self.choices = [_FakeChoice(text)]


class _FakeStream:
    """假流式响应：先可选阻塞 delay 秒，再逐片产出。"""

    def __init__(self, chunks, delay: float = 0.0):
        self._chunks = chunks
        self._delay = delay

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        if self._delay:
            await asyncio.sleep(self._delay)
        for text in self._chunks:
            yield _FakeChunk(text)


class _FakeNonStream:
    def __init__(self, chunks):
        content = "".join(chunks)
        self.choices = [
            type(
                "C",
                (),
                {
                    "message": type("M", (), {"content": content})(),
                    "finish_reason": "stop",
                },
            )()
        ]
        self.usage = type(
            "U", (), {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        )()


class _FakeCompletions:
    def __init__(self, responder, delay: float = 0.0):
        self._responder = responder
        self._delay = delay

    async def create(self, **kwargs):
        result = self._responder(kwargs.get("messages", []))
        if isinstance(result, Exception):
            raise result
        if kwargs.get("stream"):
            return _FakeStream(result, self._delay)
        return _FakeNonStream(result)


class FakeAsyncOpenAI:
    """可注入的假 OpenAI 客户端。

    responder(messages) -> list[str]（分片）或 Exception（直接抛出）。
    delay 仅用于并发测试，模拟首个 token 之前的阻塞。
    """

    def __init__(self, responder, delay: float = 0.0):
        self._responder = responder
        self._delay = delay
        self.chat = type("Chat", (), {"completions": _FakeCompletions(responder, delay)})()


def _default_responder(messages: list) -> list:
    """上下文无关但能识别关键字的确定性应答。

    - 上下文含“小明”时回答包含“小明”（供多轮记忆测试）。
    - 否则返回固定分片，拼接为“你好，这是测试回复。”。
    """
    blob = " ".join(str(m.get("content", "")) for m in messages)
    if "小明" in blob:
        return ["你叫", "小明。"]
    return ["你好", "，", "这是", "测试回复", "。"]


def make_default_fake():
    return FakeAsyncOpenAI(_default_responder)


def make_slow_fake(delay: float = 0.5):
    """首个 token 之前阻塞 delay 秒，用于并发互斥测试。"""
    return FakeAsyncOpenAI(_default_responder, delay=delay)


def make_error_fake(exc):
    """create 直接抛出给定异常，用于错误映射测试。"""
    return FakeAsyncOpenAI(lambda messages: exc)


# ───────────────────────── 环境变量 / LLM 重置 ─────────────────────────


@pytest.fixture(autouse=True)
def _reset_llm(monkeypatch):
    """每个测试用干净的默认假客户端，避免跨用例污染。"""
    monkeypatch.setattr(llm_client, "_client", make_default_fake())
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    yield


@pytest.fixture(autouse=True)
def _reset_auth(monkeypatch):
    """认证相关状态复位：开发默认关认证；固定会话密钥；清空登录限流桶。

    - AUTH_ENABLED=None → 由 app_env(development) 推导为 false，保持既有测试
      的 X-User-Id 兼容路径不变。
    - SESSION_SECRET 固定值让 Cookie 伪造/篡改用例可复现；开发环境不强制非空。
    """
    monkeypatch.setattr(settings, "auth_enabled", None)
    monkeypatch.setattr(settings, "session_secret", "test-session-secret-0123456789abcdef")
    from app.auth import ratelimit

    ratelimit.clear()
    yield
    ratelimit.clear()


# ---- 数据库路径（会话级，提速） ----
# 性能优化：ASGI 客户端与 SQLite 在整会话内共享一个临时文件，lifespan 与建表只跑
# 一次；每个测试通过 _db_conn 舆具在自身连接内截断所有表（等价每测试全新库，
# 但省去 N x 建表 + N x 临时文件 + N x lifespan 的开销）。隔离是测试正确性的前提。


@pytest.fixture(scope="session")
def db_path(tmp_path_factory):
    """整会话共用一个 SQLite 文件，避免每个测试重复建表与创建临时文件。"""
    return tmp_path_factory.mktemp("sess") / "chat.db"


async def _truncate_tables(conn):
    """清空所有业务表，保持测试间隔离（等价每测试全新库）。"""
    for table in (
        "message",
        "conversation_memory",
        "chunk",
        "conversation",
        "user",
        "document",
        "chunks_fts",
    ):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()


@pytest.fixture(scope="session", autouse=True)
def _configure_session_db(db_path):
    """会话级只建一次表；database_url 指向共享文件，供应用与 db 舆具共用。"""
    import asyncio

    from app.db import init_db

    original = settings.database_url
    settings.database_url = f"sqlite+aiosqlite:///{db_path}"
    asyncio.run(init_db())
    yield
    settings.database_url = original


@pytest_asyncio.fixture(autouse=True)
async def _db_conn(db_path):
    """每个测试一个连接并截断所有表 -> 等价全新空库；db 舆具复用该连接。"""
    import aiosqlite

    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA journal_mode = WAL")
    await _truncate_tables(conn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture(autouse=True)
async def _drain_background_tasks():
    """每个测试结束后排空应用后台任务（标题生成等），避免跨测试/跨循环泄漏。"""
    yield
    from app.chat import service as _service

    pending = list(_service._background_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    _service._background_tasks.clear()


@pytest_asyncio.fixture
async def db(_db_conn, db_path, monkeypatch):
    """复用 _db_conn（已截断），返回可用于断言的连接。"""
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_path}")
    yield _db_conn


@pytest_asyncio.fixture(scope="session")
async def client(db_path):
    """会话级 ASGI 客户端：lifespan 与建表只跑一次（提速关键）。"""
    import httpx

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as ac:
            yield ac

# ───────────────────────── 样本数据夹具 ─────────────────────────


@pytest_asyncio.fixture
async def sample_conversation(db):
    """预置一个空会话，返回其 id，供需要已知会话的测试使用。"""
    from app.db import utcnow_iso

    conv_id = "sample-conv-1"
    now = utcnow_iso()
    await db.execute(
        "INSERT INTO conversation (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (conv_id, "样本会话", now, now),
    )
    await db.commit()
    return {"id": conv_id}
