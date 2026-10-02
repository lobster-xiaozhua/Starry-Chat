"""并发互斥与幽灵 assistant 防护（P0）单元测试。

覆盖：
  T4 同会话并发 10 请求 → 恰好 1 个 200 + 9 个 409
  T5 流式中模型错误 → DB 无 assistant 消息
  T6 流式中客户端断开 → DB 无空 assistant 消息
  T7 跑 100 轮后锁池无泄漏（len == 0）

经 conftest 的 client（ASGI）/ db 夹具驱动，LLM 已打桩，不触网。
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

from app.config import settings
from app.chat import service  # noqa: E402
from app.db import utcnow_iso  # noqa: E402
from app.llm import client as llm_client  # noqa: E402
from app.schema import ChatRequest  # noqa: E402

from conftest import make_slow_fake, _FakeChunk  # noqa: E402

import openai  # noqa: E402


async def _make_conv(db, conv_id):
    now = utcnow_iso()
    await db.execute(
        "INSERT INTO conversation (id,title,created_at,updated_at) VALUES (?,?,?,?)",
        (conv_id, "t", now, now),
    )
    await db.commit()


async def test_concurrent_same_conversation_returns_409(client, db, monkeypatch):
    """T4: 同会话 10 并发 → 恰好 1 个 200 + 9 个 409，无 500、无乱序。"""
    conv_id = "conv-409"
    await _make_conv(db, conv_id)
    monkeypatch.setattr(llm_client, "_backoff_delay", lambda attempt: 0.0)
    monkeypatch.setattr(llm_client.random, "uniform", lambda a, b: 0.0)
    llm_client._client = make_slow_fake(delay=2.0)

    async def _post():
        return await client.post(
            "/api/chat", json={"message": "并发", "conversation_id": conv_id}
        )

    results = await asyncio.gather(*[_post() for _ in range(10)])
    statuses = {r.status_code for r in results}
    counts = {}
    for r in results:
        counts[r.status_code] = counts.get(r.status_code, 0) + 1

    assert statuses == {200, 409}, counts
    assert counts[200] == 1, counts
    assert counts[409] == 9, counts


async def test_no_ghost_assistant_on_error(client, db, monkeypatch):
    """T5: 流式中 LLM 抛 APITimeoutError → DB 中 assistant 消息数 == 0。"""
    conv_id = "conv-err"
    await _make_conv(db, conv_id)
    monkeypatch.setattr(settings, "llm_max_retries", 0)
    monkeypatch.setattr(llm_client, "_backoff_delay", lambda attempt: 0.0)
    monkeypatch.setattr(llm_client.random, "uniform", lambda a, b: 0.0)

    async def fake_stream(*args, **kwargs):
        # 必须是 async generator（与真实 chat_stream 同构）：普通协程没有
        # aclose()，会让 _do_stream 的 finally 清理链抛 AttributeError
        raise openai.APITimeoutError("timeout")
        yield  # pragma: no cover -- 仅为把本函数标记为 async generator

    monkeypatch.setattr(llm_client, "chat_stream", fake_stream)

    res = await client.post(
        "/api/chat", json={"message": "hi", "conversation_id": conv_id}
    )
    assert res.status_code in (200, 500)

    cur = await db.execute(
        "SELECT COUNT(*) c FROM message WHERE conversation_id=? AND role='assistant'",
        (conv_id,),
    )
    assert (await cur.fetchone())["c"] == 0


async def test_no_ghost_assistant_on_cancel(db, monkeypatch):
    """T6: 流式中客户端断开（取消）→ DB 中不存在 assistant 消息。

    测试环境（httpx ASGITransport）下，客户端断开未必能可靠传播为服务端
    取消，故直接驱动生成器并以 task.cancel() 注入真实 CancelledError，
    精准覆盖 _do_stream 的 except asyncio.CancelledError 分支（同步回滚幽灵
    assistant 行 + 必须 re-raise）。幽灵消息防护的核心不变量是：assistant 行只在
    流式成功结束且内容非空时才落库，因此取消路径下 DB 中必无 assistant 记录。
    """
    conv_id = "conv-cancel"
    await _make_conv(db, conv_id)
    # 模型首个 token 前长时间阻塞，使生成器稳定停在"等待模型"的 await 点，
    # 便于在流式途中被取消。
    llm_client._client = make_slow_fake(delay=10.0)

    gen = await service.send_message(
        ChatRequest(message="hi", conversation_id=conv_id), db, "anonymous"
    )

    async def _drive() -> None:
        async for _ev in gen:
            pass

    task = asyncio.create_task(_drive())
    # 给流式任务一点时间进入"阻塞等待模型"状态
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # 确保生成器彻底关闭（释放会话锁、跑 finally）
    try:
        await gen.aclose()
    except Exception:
        pass

    # 取消传播后，DB 中不应有任何 assistant 消息（无幽灵、无空内容）
    cur = await db.execute(
        "SELECT COUNT(*) c FROM message WHERE conversation_id=? AND role='assistant'",
        (conv_id,),
    )
    assert (await cur.fetchone())["c"] == 0


async def test_lock_dict_no_leak(db, monkeypatch):
    """T7: 跑 100 轮后，锁池应被回收（len == 0）。"""
    for i in range(100):
        gen = await service.send_message(ChatRequest(message=f"m{i}"), db, "u1")
        async for _ in gen:
            pass
    assert len(service._conv_locks) == 0
    await asyncio.sleep(0.05)
    assert len(service._conv_locks) == 0
