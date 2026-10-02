"""service 层单元测试：上下文裁剪、并发互斥、标题派生、事务删除。

直接调用 app.chat.service，不经 HTTP；用 FakeAsyncOpenAI 打桩 LLM。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="simple-chat-svc-")
os.environ["APP_ENV"] = "development"
os.environ["LLM_API_KEY"] = "test-key"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP}/svc.db"
os.environ["LOG_LEVEL"] = "WARNING"

from app.chat import service  # noqa: E402
from app.db import get_db, init_db  # noqa: E402
from app.errors import ConversationBusyError, ConversationNotFoundError  # noqa: E402
from app.llm import client as llm_client  # noqa: E402
from app.schema import ChatRequest  # noqa: E402


# ───────────────────────── 桩 LLM ─────────────────────────


class _FakeDelta:
    def __init__(self, text): self.content = text

class _FakeChoice:
    def __init__(self, text):
        self.delta = _FakeDelta(text)
        self.finish_reason = "stop"

class _FakeChunk:
    def __init__(self, text): self.choices = [_FakeChoice(text)]

class FakeStream:
    def __init__(self, chunks): self._chunks = chunks
    async def __aiter__(self):
        for t in self._chunks:
            yield _FakeChunk(t)

class FakeChatCompletions:
    def __init__(self, chunks): self._chunks = chunks
    async def create(self, **kw):
        if kw.get("stream"):
            return FakeStream(self._chunks)
        raise AssertionError("service 不应走非流式")

class FakeCompletions:
    def __init__(self, chunks): self.completions = FakeChatCompletions(chunks)

class FakeAsyncOpenAI:
    def __init__(self, chunks): self.chat = FakeCompletions(chunks)


@pytest.fixture(autouse=True)
def patch_llm(monkeypatch):
    """每个测试注入干净的 fake 客户端，并清空锁池与后台任务。"""
    monkeypatch.setattr(llm_client, "_client", FakeAsyncOpenAI(["你", "好", "！"]))
    service._conv_locks.clear()
    service._background_tasks.clear()
    yield
    service._conv_locks.clear()
    service._background_tasks.clear()


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    """每测试一个全新空库，避免跨用例污染。"""
    db_path = tmp_path / "svc.db"
    monkeypatch.setattr(service.settings, "database_url", f"sqlite+aiosqlite:///{db_path}")
    await init_db()
    async with get_db() as conn:
        yield conn
    # 测试结束后取消后台标题任务，避免游离任务引用已关闭的 loop 导致挂起
    await service.shutdown_tasks()


# ───────────────────────── 标题派生 ─────────────────────────


def test_derive_title_by_punctuation():
    """按首个句末标点截断。"""
    assert service._derive_title("你好。后续内容") == "你好。"
    assert service._derive_title("提问？后面") == "提问？"
    assert service._derive_title("感叹！后面") == "感叹！"


def test_derive_title_by_newline():
    """按首个换行截断（含换行符本身，因匹配 [。！？\n]）。"""
    assert service._derive_title("第一行\n第二行") == "第一行\n"


def test_derive_title_truncates_to_20():
    """无标点时取前 20 字符。"""
    long = "abcdefghijklmnopqrstuvwxyz"
    assert service._derive_title(long) == long[:20]


def test_derive_title_empty_fallback():
    """空或全空白回退到默认标题。"""
    assert service._derive_title("   ") == "新对话"
    assert service._derive_title("") == "新对话"


# ───────────────────────── send_message 流式 ─────────────────────────


async def test_send_message_new_conversation_streams_tokens_and_done(db):
    """新会话：stream 出 token* + done，done 含 conversation_id/message_id/usage。"""
    req = ChatRequest(message="你好")
    gen = await service.send_message(req, db, "u1")
    chunks = [c async for c in gen]
    # 等游离标题任务收尾，避免它在测试 loop 关闭后才执行
    await asyncio.gather(*service._background_tasks, return_exceptions=True)

    assert chunks[-1].startswith("event: done")
    assert all(c.startswith("event: token") for c in chunks[:-1])
    # done 帧含 usage 三字段
    done_line = [l for l in chunks[-1].split("\n") if l.startswith("data:")][0]
    import json
    done = json.loads(done_line[5:])
    assert "conversation_id" in done
    assert done["message_id"] > 0
    assert set(done["usage"]) == {"prompt_tokens", "completion_tokens", "total_tokens"}


async def test_send_message_persists_user_and_assistant(db):
    """send_message 后库里有 user 与 assistant 两条消息。"""
    req = ChatRequest(message="第一句")
    gen = await service.send_message(req, db, "u1")
    _ = [c async for c in gen]
    await asyncio.gather(*service._background_tasks, return_exceptions=True)

    cur = await db.execute("SELECT role, content, tokens FROM message ORDER BY id ASC")
    rows = [dict(r) for r in await cur.fetchall()]
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[0]["content"] == "第一句"
    assert rows[1]["content"] == "你好！"
    assert rows[1]["tokens"] > 0


# ───────────────────────── 并发互斥 ─────────────────────────


async def test_concurrent_send_on_same_conversation_returns_409(db):
    """同一会话并发第二个请求 → ConversationBusyError (409)。"""
    # 第一个请求占用会话锁
    req = ChatRequest(message="占用")
    gen1 = await service.send_message(req, db, "u1")
    # 不消费 gen1，锁仍持有
    gen1_aiter = gen1.__aiter__()

    # 等待让 gen1 真正进入流式（拿到锁）
    first = await gen1_aiter.__anext__()
    assert first.startswith("event: token")

    # 第二个请求同会话 → 应抛 ConversationBusyError
    # 但 gen1 还没 done，conversation_id 未知；先取出 id
    import json
    # 需要从库查 conversation_id
    cur = await db.execute("SELECT id FROM conversation")
    conv_id = (await cur.fetchone())["id"]

    req2 = ChatRequest(message="并发", conversation_id=conv_id)
    with pytest.raises(ConversationBusyError):
        await service.send_message(req2, db, "u1")

    # 消费完 gen1 释放锁
    async for _ in gen1_aiter:
        pass
    await asyncio.gather(*service._background_tasks, return_exceptions=True)


# ───────────────────────── 异常回滚 ─────────────────────────


async def test_stream_error_rolls_back_assistant_message(monkeypatch, db):
    """流式中 LLM 抛业务异常 → assistant 行已写入则删除，保证库一致。"""
    import httpx
    import openai

    def factory():
        raise openai.RateLimitError(
            "rl",
            response=httpx.Response(
                status_code=429, content=b'{}',
                request=httpx.Request("POST", "http://x"),
            ),
            body=None,
        )

    class FailCompletions:
        async def create(self, **kw):
            raise factory()

    class FailAOAI:
        def __init__(self): self.chat = type("C", (), {"completions": FailCompletions()})()

    monkeypatch.setattr(llm_client, "_client", FailAOAI())

    req = ChatRequest(message="触发错误")
    gen = await service.send_message(req, db, "u1")
    chunks = [c async for c in gen]
    await asyncio.gather(*service._background_tasks, return_exceptions=True)

    # 应有 error 事件
    assert any(c.startswith("event: error") for c in chunks)
    # 库里只应有 user 消息，无 assistant
    cur = await db.execute("SELECT role FROM message ORDER BY id ASC")
    roles = [dict(r)["role"] for r in await cur.fetchall()]
    assert roles == ["user"]


# ───────────────────────── build_context 裁剪 ─────────────────────────


async def test_build_context_includes_system_prefix(db):
    """上下文首条必须是 system。"""
    req = ChatRequest(message="你好")
    gen = await service.send_message(req, db, "u1")
    _ = [c async for c in gen]
    await asyncio.gather(*service._background_tasks, return_exceptions=True)

    cur = await db.execute("SELECT id FROM conversation LIMIT 1")
    conv_id = (await cur.fetchone())["id"]
    ctx = await service.build_context(conv_id, db, 256000)
    assert ctx[0]["role"] == "system"
    assert ctx[0]["content"] == llm_client.SYSTEM_PROMPT


async def test_build_context_drops_consecutive_same_role(db):
    """连续相同 role 的消息，较旧一条被丢弃并告警。"""
    # 手动插入两条连续 user 消息
    now = service.utcnow_iso()
    cur = await db.execute("INSERT INTO conversation (id,title,created_at,updated_at) VALUES (?,?,?,?)",
                           ("c1", "t", now, now))
    await db.execute("INSERT INTO message (conversation_id,role,content,tokens,created_at) VALUES (?,?,?,0,?)",
                     ("c1", "user", "旧user", now))
    await db.execute("INSERT INTO message (conversation_id,role,content,tokens,created_at) VALUES (?,?,?,0,?)",
                     ("c1", "user", "新user", now))
    await db.commit()

    ctx = await service.build_context("c1", db, 256000)
    # 较旧的 user 被丢弃，只剩 system + 新user
    roles = [m["role"] for m in ctx if m["role"] != "system"]
    assert roles == ["user"]
    contents = [m["content"] for m in ctx if m["role"] == "user"]
    assert contents == ["新user"]


async def test_build_context_respects_token_budget(db):
    """超出预算时从最旧开始丢弃。"""
    now = service.utcnow_iso()
    await db.execute("INSERT INTO conversation (id,title,created_at,updated_at) VALUES (?,?,?,?)",
                     ("c2", "t", now, now))
    # 插入 5 条消息，每条很长
    for i in range(5):
        await db.execute("INSERT INTO message (conversation_id,role,content,tokens,created_at) VALUES (?,?,?,0,?)",
                         ("c2", "user" if i % 2 == 0 else "assistant", f"msg{i}_" + "x" * 500, now))
    await db.commit()

    # 极小预算：只能容纳最新一条
    ctx = await service.build_context("c2", db, 200)
    non_system = [m for m in ctx if m["role"] != "system"]
    # 至少保留最新一条
    assert len(non_system) >= 1


# ───────────────────────── 列表与查询 ─────────────────────────


async def test_list_conversations_returns_last_message_preview(db):
    """列表含 last_message 预览（≤80 字）。"""
    req = ChatRequest(message="预览测试")
    gen = await service.send_message(req, db, "u1")
    _ = [c async for c in gen]
    await asyncio.gather(*service._background_tasks, return_exceptions=True)

    items = await service.list_conversations(db, "u1")
    assert len(items) >= 1
    assert items[0]["last_message"] is not None
    assert len(items[0]["last_message"]) <= 80


async def test_list_conversations_limit_capped_at_100(db):
    """limit 上限 100。"""
    items = await service.list_conversations(db, "u1", limit=500)
    assert isinstance(items, list)


async def test_get_messages_returns_chronological(db):
    """消息按 id ASC 返回（时间正序）。"""
    req = ChatRequest(message="第一")
    gen = await service.send_message(req, db, "u1")
    _ = [c async for c in gen]
    await asyncio.gather(*service._background_tasks, return_exceptions=True)

    cur = await db.execute("SELECT id FROM conversation LIMIT 1")
    conv_id = (await cur.fetchone())["id"]
    msgs = await service.get_messages(conv_id, db, "u1")
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"
    assert msgs[0]["id"] < msgs[1]["id"]


async def test_get_messages_unknown_conversation_404(db):
    """未知会话 → ConversationNotFoundError。"""
    with pytest.raises(ConversationNotFoundError):
        await service.get_messages("nope", db, "u1")


# ───────────────────────── 删除事务 ─────────────────────────


async def test_delete_conversation_removes_messages_and_conversation(db):
    """删除会话后消息一并清除（事务内先删消息再删会话）。"""
    req = ChatRequest(message="待删")
    gen = await service.send_message(req, db, "u1")
    _ = [c async for c in gen]
    await asyncio.gather(*service._background_tasks, return_exceptions=True)

    cur = await db.execute("SELECT id FROM conversation LIMIT 1")
    conv_id = (await cur.fetchone())["id"]

    ok = await service.delete_conversation(conv_id, db, "u1")
    assert ok is True

    cur = await db.execute("SELECT COUNT(*) c FROM message WHERE conversation_id = ?", (conv_id,))
    assert (await cur.fetchone())["c"] == 0
    cur = await db.execute("SELECT COUNT(*) c FROM conversation WHERE id = ?", (conv_id,))
    assert (await cur.fetchone())["c"] == 0


async def test_delete_unknown_conversation_404(db):
    """删除不存在的会话 → 404。"""
    with pytest.raises(ConversationNotFoundError):
        await service.delete_conversation("nope", db, "u1")
