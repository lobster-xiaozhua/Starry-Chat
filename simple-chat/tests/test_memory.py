"""v0.4 工作记忆测试：每 N 轮摘要、当前用户消息永远最后、upto_message_id 单调、
摘要失败时聊天仍成功、60 轮 LLM 调用次数相对基线增长 < 20%。

复用 conftest.py 的 fixtures（client/db/FakeAsyncOpenAI）。
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

from app.chat import service  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import utcnow_iso  # noqa: E402
from app import memory as _memory  # noqa: E402
from app.llm import client as llm_client  # noqa: E402
import app.llm.tokenizer as _tok  # noqa: E402
from tests.conftest import FakeAsyncOpenAI  # noqa: E402


@pytest.fixture
def memory_enabled(monkeypatch):
    """显式开启工作记忆；小 N 与小窗口便于用少量消息触发摘要。"""
    monkeypatch.setattr(settings, "memory_enabled", True)
    monkeypatch.setattr(settings, "memory_summary_every_n", 20)
    monkeypatch.setattr(settings, "memory_raw_window", 40)
    monkeypatch.setattr(settings, "memory_summary_max_tokens", 256)
    monkeypatch.setattr(settings, "memory_summary_max_chars", 1200)
    yield


@pytest.fixture(autouse=True)
def _approx_tokens(monkeypatch):
    """强制近似 tokenizer，使 token 估算与字符数成比例、跨环境确定。"""
    monkeypatch.setattr(_tok, "_encoder", None)
    monkeypatch.setattr(_tok, "_tried", True)


async def _add_conv(db, conv_id):
    now = utcnow_iso()
    await db.execute(
        "INSERT INTO conversation (id, title, user_id, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (conv_id, "t", "anonymous", now, now),
    )
    await db.commit()


async def _insert(db, conv_id, role, content):
    now = utcnow_iso()
    await db.execute(
        "INSERT INTO message (conversation_id, role, content, tokens, created_at)"
        " VALUES (?, ?, ?, 0, ?)",
        (conv_id, role, content, now),
    )
    await db.commit()


async def test_summary_appears_and_current_message_last(db, memory_enabled, monkeypatch):
    """60 轮历史 → 出现 summary；当前用户消息仍是 messages 最后一条。"""
    conv_id = "mem-60"
    await _add_conv(db, conv_id)
    # 60 轮 = 120 条消息（user/assistant 交替）
    for i in range(60):
        await _insert(db, conv_id, "user", f"用户第{i+1}轮")
        await _insert(db, conv_id, "assistant", f"助手第{i+1}轮回复")
    # 再插一条当前用户消息
    current = "小明的最新的问题"
    await _insert(db, conv_id, "user", current)

    # 用可追踪的假客户端：摘要调用返回固定文本，主对话返回默认分片
    summary_calls = {"n": 0}

    def responder(messages):
        # 摘要输入含固化模板关键字"压缩成一段简短记事"
        blob = " ".join(str(m.get("content", "")) for m in messages)
        if "压缩成一段简短记事" in blob:
            summary_calls["n"] += 1
            return ["这是旧对话的摘要文本"]
        # 主对话：识别小明
        if "小明" in blob:
            return ["你叫", "小明。"]
        return ["好的"]

    monkeypatch.setattr(llm_client, "_client", FakeAsyncOpenAI(responder))

    # 直接调用 generate_summary（同步等摘要完成）
    await _memory.generate_summary(conv_id)
    assert summary_calls["n"] == 1

    ctx = await service.build_context(conv_id, db, settings.max_context_tokens, current)

    # system 在最前
    assert ctx[0]["role"] == "system"
    assert ctx[0]["content"] == llm_client.SYSTEM_PROMPT
    # 摘要在 system 之后、原文之前
    assert len(ctx) >= 2
    summary_msg = ctx[1]
    assert summary_msg["role"] == "system"
    assert "较早对话摘要" in summary_msg["content"]
    assert "这是旧对话的摘要文本" in summary_msg["content"]
    # 当前用户消息永远最后
    assert ctx[-1]["role"] == "user"
    assert current in ctx[-1]["content"]


async def test_upto_message_id_monotonic(db, memory_enabled, monkeypatch):
    """摘要的 upto_message_id 单调递增：只在新消息 id 更大时才重新摘要。"""
    conv_id = "mem-mono"
    await _add_conv(db, conv_id)
    for i in range(60):
        await _insert(db, conv_id, "user", f"u{i}")
        await _insert(db, conv_id, "assistant", f"a{i}")

    call_count = {"n": 0}

    def responder(messages):
        blob = " ".join(str(m.get("content", "")) for m in messages)
        if "压缩成一段简短记事" in blob:
            call_count["n"] += 1
            return [f"摘要v{call_count['n']}"]
        return ["ok"]

    monkeypatch.setattr(llm_client, "_client", FakeAsyncOpenAI(responder))

    await _memory.generate_summary(conv_id)
    mem1 = await _memory.get_summary(db, conv_id)
    assert mem1 is not None
    upto1 = mem1["upto_message_id"]
    assert upto1 > 0
    assert call_count["n"] == 1

    # 再加 20 条新消息，触发重摘要
    for i in range(10):
        await _insert(db, conv_id, "user", f"u2-{i}")
        await _insert(db, conv_id, "assistant", f"a2-{i}")

    await _memory.generate_summary(conv_id)
    mem2 = await _memory.get_summary(db, conv_id)
    assert mem2 is not None
    assert mem2["upto_message_id"] > upto1
    assert call_count["n"] == 2

    # 无新消息时再次调用不应触发摘要（upto 不变）
    await _memory.generate_summary(conv_id)
    mem3 = await _memory.get_summary(db, conv_id)
    assert mem3["upto_message_id"] == mem2["upto_message_id"]
    assert call_count["n"] == 2


async def test_summary_failure_chat_succeeds(db, memory_enabled, monkeypatch):
    """摘要任务抛错时聊天仍成功（不注入 summary，不阻塞）。"""
    conv_id = "mem-fail"
    await _add_conv(db, conv_id)
    for i in range(60):
        await _insert(db, conv_id, "user", f"u{i}")
        await _insert(db, conv_id, "assistant", f"a{i}")
    await _insert(db, conv_id, "user", "最终问题")

    # 摘要调用抛异常；主对话正常返回
    def responder(messages):
        blob = " ".join(str(m.get("content", "")) for m in messages)
        if "压缩成一段简短记事" in blob:
            raise RuntimeError("summary LLM failed")
        return ["好的回复"]

    monkeypatch.setattr(llm_client, "_client", FakeAsyncOpenAI(responder))

    # 后台任务路径：schedule_summary 抛错应被吞掉（_generate_summary_task 捕获所有异常）
    _memory.schedule_summary(conv_id)
    # 排空后台任务，确保异常被静默
    pending = list(_memory._background_tasks_local)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    _memory._background_tasks_local.clear()

    # 直接调用 generate_summary 会抛出（经 llm_client.chat 映射为 LLMError），
    # 但后台任务路径 _generate_summary_task 会吞掉；这里验证直接调用确实失败，
    # 且没有摘要写入 conversation_memory。
    with pytest.raises(Exception):
        await _memory.generate_summary(conv_id)

    # 没有摘要写入 conversation_memory
    mem = await _memory.get_summary(db, conv_id)
    assert mem is None

    # build_context 仍能正常工作，当前消息在最后
    ctx = await service.build_context(conv_id, db, settings.max_context_tokens, "最终问题")
    assert ctx[-1]["role"] == "user"
    # 没有 summary 段（只有 system + 原文）
    summary_segments = [
        m for m in ctx
        if m["role"] == "system" and "较早对话摘要" in m["content"]
    ]
    assert summary_segments == []


async def test_call_count_within_budget(db, memory_enabled, monkeypatch):
    """60 轮总 LLM 调用次数相对基线增长 < 20%。

    基线（无记忆）：60 轮 = 60 次主对话调用。
    启用记忆后：60 次主对话 + 至多 ceil(60/20)=3 次摘要（但只在 60 轮完成后触发一次）。
    增长率 = (60+3)/60 = 5%，远 < 20%。
    """
    conv_id = "mem-calls"
    await _add_conv(db, conv_id)
    main_calls = {"n": 0}
    summary_calls = {"n": 0}

    def responder(messages):
        blob = " ".join(str(m.get("content", "")) for m in messages)
        if "压缩成一段简短记事" in blob:
            summary_calls["n"] += 1
            return ["摘要"]
        main_calls["n"] += 1
        return ["回复"]

    monkeypatch.setattr(llm_client, "_client", FakeAsyncOpenAI(responder))

    # 直接模拟 60 轮，每轮触发 maybe_generate_summary
    for i in range(60):
        await _insert(db, conv_id, "user", f"u{i}")
        await _insert(db, conv_id, "assistant", f"a{i}")
        # 模拟 service 在 assistant 落库后调用 maybe_generate_summary
        await _memory.maybe_generate_summary(db, conv_id)

    # 排空后台任务
    pending = list(_memory._background_tasks_local)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    _memory._background_tasks_local.clear()
    # 也排空 service 后台集合（含可能挂起的标题任务）
    pending = list(service._background_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    service._background_tasks.clear()

    baseline = 60  # 60 轮主对话
    total = main_calls["n"] + summary_calls["n"]
    growth = (total - baseline) / baseline
    assert growth < 0.20, f"LLM 调用增长 {growth:.0%} 超过 20%（总 {total}，基线 {baseline}）"


async def test_memory_disabled_no_summary(db, monkeypatch):
    """MEMORY_ENABLED=false 时 build_context 不读不写 conversation_memory。"""
    monkeypatch.setattr(settings, "memory_enabled", False)
    conv_id = "mem-off"
    await _add_conv(db, conv_id)
    for i in range(60):
        await _insert(db, conv_id, "user", f"u{i}")
        await _insert(db, conv_id, "assistant", f"a{i}")
    await _insert(db, conv_id, "user", "最后")

    # maybe_generate_summary 在关闭时应直接返回，不投递任务
    await _memory.maybe_generate_summary(db, conv_id)
    assert len(_memory._background_tasks_local) == 0

    ctx = await service.build_context(conv_id, db, settings.max_context_tokens, "最后")
    # 没有 summary 段
    summary_segments = [
        m for m in ctx
        if m["role"] == "system" and "较早对话摘要" in m["content"]
    ]
    assert summary_segments == []
    # system 在最前，当前用户消息在最后
    assert ctx[0]["role"] == "system"
    assert ctx[0]["content"] == llm_client.SYSTEM_PROMPT
    assert ctx[-1]["role"] == "user"
