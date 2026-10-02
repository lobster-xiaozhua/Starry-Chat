"""对话接口测试：覆盖流式顺序、多轮上下文、错误路径、并发互斥、级联删除、SSE 格式与错误映射。

通过 conftest 的 client / db 夹具驱动应用，LLM 已被打桩，不触网。
"""

import asyncio
import json
import os
import sys

import httpx
import openai
import pytest

from app.db import utcnow_iso
from app.errors import ErrorCode
from app.llm import client as llm_client
from app.llm.tokenizer import estimate_tokens
from app.config import settings
from app.chat import service

# conftest 提供打桩 LLM 的工厂；直接 import 该模块以复用 FakeAsyncOpenAI 等
sys.path.insert(0, os.path.dirname(__file__))
from conftest import make_error_fake, make_slow_fake  # noqa: E402


# ───────────────────────── SSE 解析工具 ─────────────────────────


def parse_sse(text: str) -> list:
    """按 \n\n 分帧，解析 (event, data)。"""
    events = []
    for frame in text.split("\n\n"):
        if not frame.strip() or frame.lstrip().startswith(":"):
            continue
        name = None
        data_parts = []
        for line in frame.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data_parts.append(line[5:].strip())
        if name and data_parts:
            events.append((name, json.loads("\n".join(data_parts))))
    return events


async def collect_stream(client, payload: dict) -> tuple:
    """发起一次流式请求，返回 (状态码, SSE事件列表, 原始文本)。"""
    raw = ""
    async with client.stream("POST", "/api/chat", json=payload) as res:
        status = res.status_code
        async for chunk in res.aiter_text():
            raw += chunk
    events = parse_sse(raw) if status == 200 else []
    return status, events, raw


# ───────────────────────── a. 流式基本顺序 ─────────────────────────


async def test_send_message_stream(client):
    """存在 token（delta 非空）、done（conversation_id 非空）、usage.total_tokens>0。"""
    status, events, _ = await collect_stream(client, {"message": "你好"})
    assert status == 200

    names = [e[0] for e in events]
    assert "token" in names
    assert names[-1] == "done"

    token_events = [d for n, d in events if n == "token"]
    assert token_events, "应有至少一个 token 事件"
    assert all(d["delta"] for d in token_events), "token 的 delta 不能为空"

    done = [d for n, d in events if n == "done"][-1]
    assert done["conversation_id"], "done 的 conversation_id 不能为空"
    assert done["usage"]["total_tokens"] > 0, "usage.total_tokens 应大于 0"


# ───────────────────────── b. 多轮上下文记忆 ─────────────────────────


async def test_multi_turn_context(client, db):
    """第二轮复用同一会话，回答包含“小明”，DB 共 4 条消息。"""
    _, events1, _ = await collect_stream(client, {"message": "我的名字是小明"})
    conv_id = events1[-1][1]["conversation_id"]

    _, events2, _ = await collect_stream(
        client, {"message": "我叫什么？", "conversation_id": conv_id}
    )
    answer = "".join(d["delta"] for n, d in events2 if n == "token")
    assert "小明" in answer, f"第二轮回答应包含“小明”，实际：{answer}"

    cur = await db.execute(
        "SELECT COUNT(*) c FROM message WHERE conversation_id = ?", (conv_id,)
    )
    assert (await cur.fetchone())["c"] == 4


# ───────────────────────── c. / d. 校验错误 ─────────────────────────


async def test_empty_message(client):
    """message 去空白后为空 → 422 VALIDATION_ERROR。"""
    res = await client.post("/api/chat", json={"message": "   "})
    assert res.status_code == 422
    assert res.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR.value


async def test_message_too_long(client):
    """message 超 4000 字符 → 422 VALIDATION_ERROR。"""
    res = await client.post("/api/chat", json={"message": "x" * 4001})
    assert res.status_code == 422
    assert res.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR.value


# ───────────────────────── e. 不存在的会话 ─────────────────────────


async def test_nonexistent_conversation(client):
    """请求体中不存在的 conversation_id → 404 NOT_FOUND。"""
    res = await client.post(
        "/api/chat", json={"message": "hi", "conversation_id": "does-not-exist"}
    )
    assert res.status_code == 404
    assert res.json()["error"]["code"] == ErrorCode.NOT_FOUND.value


# ───────────────────────── f. 并发同会话互斥 ─────────────────────────


async def test_concurrent_same_conversation(client, db):
    """同一会话并发第二个请求 → 409 CONVERSATION_BUSY。"""
    # 预置一个已知会话，让两个并发请求都指向它
    conv_id = "conv-concurrent"
    now = utcnow_iso()
    await db.execute(
        "INSERT INTO conversation (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (conv_id, "并发", now, now),
    )
    await db.commit()

    # 用阻塞型假客户端：首个 token 前 sleep，期间锁一直被占用
    llm_client._client = make_slow_fake(delay=0.5)

    task1 = asyncio.create_task(
        collect_stream(client, {"conversation_id": conv_id, "message": "占用"})
    )
    # 让请求 1 先取得会话锁并进入阻塞
    await asyncio.sleep(0.1)

    # 请求 2 同会话 → 应被判为忙
    res2 = await client.post(
        "/api/chat", json={"conversation_id": conv_id, "message": "并发"}
    )
    status1, _, _ = await task1

    assert res2.status_code == 409
    assert res2.json()["error"]["code"] == ErrorCode.CONVERSATION_BUSY.value
    assert status1 == 200


# ───────────────────────── g. 上下文裁剪 ─────────────────────────


async def test_context_truncation(client, db, monkeypatch):
    """20 条长消息：build_context 估算 token 数 < 预算，且最后一条为 user。"""
    conv_id = "conv-trunc"
    now = utcnow_iso()
    await db.execute(
        "INSERT INTO conversation (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (conv_id, "裁剪", now, now),
    )
    # 20 条交替角色的长消息（每条 ~500 字符）；最新一条（i=19）为 user
    for i in range(20):
        role = "user" if i % 2 == 1 else "assistant"
        await db.execute(
            "INSERT INTO message (conversation_id, role, content, tokens, created_at)"
            " VALUES (?, ?, ?, 0, ?)",
            (conv_id, role, f"消息{i}_" + "长" * 500, now),
        )
    await db.commit()

    # 缩小响应预算，确保裁剪真正发生
    monkeypatch.setattr(settings, "max_response_tokens", 0)
    budget = 2000

    ctx = await service.build_context(conv_id, db, budget)
    assert ctx[0]["role"] == "system"
    est = estimate_tokens(ctx)
    assert est < budget, f"裁剪后估算 {est} 应 < 预算 {budget}"

    non_system = [m for m in ctx if m["role"] != "system"]
    assert non_system, "裁剪后至少应保留最新一条"
    # 最新消息（id 最大，i=19 奇数 → user）应为 user
    assert non_system[-1]["role"] == "user"


# ───────────────────────── h. 级联删除 ─────────────────────────


async def test_delete_conversation_cascades(client, db):
    """删除会话后，message 表中对应记录数为 0，conversation 也为 0。"""
    _, events, _ = await collect_stream(client, {"message": "待删除"})
    conv_id = events[-1][1]["conversation_id"]

    res = await client.delete(f"/api/chat/conversations/{conv_id}")
    assert res.status_code == 204

    cur = await db.execute(
        "SELECT COUNT(*) c FROM message WHERE conversation_id = ?", (conv_id,)
    )
    assert (await cur.fetchone())["c"] == 0
    cur = await db.execute(
        "SELECT COUNT(*) c FROM conversation WHERE id = ?", (conv_id,)
    )
    assert (await cur.fetchone())["c"] == 0


# ───────────────────────── i. SSE 格式 ─────────────────────────


async def test_sse_format(client):
    """content-type 含 text/event-stream；每个 event 以 \n\n 结尾；JSON 合法。"""
    async with client.stream("POST", "/api/chat", json={"message": "你好"}) as res:
        ct = res.headers.get("content-type", "")
        raw = ""
        async for chunk in res.aiter_text():
            raw += chunk

    assert "text/event-stream" in ct
    # 以双换行结尾
    assert raw.endswith("\n\n"), "SSE 流应以 \n\n 结尾"

    frames = [f for f in raw.split("\n\n") if f.strip()]
    assert frames, "应至少存在一个事件帧"
    for frame in frames:
        assert frame.startswith("event:"), "每个帧应以 event: 开头"
        for line in frame.split("\n"):
            if line.startswith("data:"):
                # 断言 data 是合法 JSON（无 trailing comma 等问题）
                json.loads(line[5:].strip())


# ───────────────────────── j. 模型错误映射 ─────────────────────────

# 补充：覆盖 router 的非流式分支与列表/历史端点，以满足 router 覆盖率要求。


async def test_non_stream_response(client):
    """stream=False 时返回 JSON ChatResponse（含 message/usage）。"""
    res = await client.post("/api/chat", json={"message": "你好", "stream": False})
    assert res.status_code == 200
    body = res.json()
    assert body["conversation_id"]
    assert body["message"]["role"] == "assistant"
    assert body["message"]["content"]
    assert body["usage"]["total_tokens"] > 0


async def test_list_conversations_endpoint(client):
    """GET /conversations 返回会话列表，且包含 title。"""
    await collect_stream(client, {"message": "列表会话"})
    res = await client.get("/api/chat/conversations")
    assert res.status_code == 200
    items = res.json()["conversations"]
    assert items
    assert all("title" in c and c["title"] for c in items)


async def test_get_messages_endpoint(client):
    """GET /conversations/{id}/messages 按时间正序返回消息。"""
    _, events, _ = await collect_stream(client, {"message": "历史消息"})
    conv_id = events[-1][1]["conversation_id"]
    res = await client.get(f"/api/chat/conversations/{conv_id}/messages")
    assert res.status_code == 200
    msgs = res.json()["messages"]
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"


async def test_model_error_mapping(client, monkeypatch):
    """mock LLM 抛出各类 openai 异常 → 对应 SSE error 事件 code。"""
    # 重试路径（RateLimitError 可重试）会退避，压到 0 避免等待
    monkeypatch.setattr(llm_client, "_backoff_delay", lambda attempt: 0.0)
    monkeypatch.setattr(llm_client.random, "uniform", lambda a, b: 0.0)

    def _resp(status: int) -> httpx.Response:
        return httpx.Response(
            status_code=status,
            content=b"{}",
            request=httpx.Request("POST", "http://x/v1/chat/completions"),
        )

    cases = [
        (
            openai.RateLimitError("rl", response=_resp(429), body=None),
            ErrorCode.RATE_LIMITED,
        ),
        (
            openai.AuthenticationError("auth", response=_resp(401), body=None),
            ErrorCode.AUTH_ERROR,
        ),
        (
            # PR-2 改动 3：无关键词的 BadRequestError 归参数错误（VALIDATION_ERROR）
            openai.BadRequestError("bad", response=_resp(400), body=None),
            ErrorCode.VALIDATION_ERROR,
        ),
        (
            # 413 / context_length 关键词仍归上下文过长
            openai.BadRequestError(
                "ctx",
                response=_resp(413),
                body={"error": {"code": "context_length_exceeded", "message": "too long"}},
            ),
            ErrorCode.CONTEXT_OVERFLOW,
        ),
    ]

    for exc, expected_code in cases:
        llm_client._client = make_error_fake(exc)
        status, events, _ = await collect_stream(client, {"message": "触发错误"})
        # 错误在流开始之后以带内 error 事件返回，HTTP 仍为 200
        assert status == 200, f"{expected_code} 应经 SSE error 事件返回"
        errs = [d for n, d in events if n == "error"]
        assert errs, f"{expected_code} 应产生 error 事件"
        assert errs[-1]["code"] == expected_code.value

    # 规格中“429 / 500”对应 ErrorCode 的 http_status 语义校验
    assert ErrorCode.RATE_LIMITED.http_status == 429
    assert ErrorCode.AUTH_ERROR.http_status == 500
    # 注：BadRequestError 按 PR-2 改动 3 三级映射（413/关键词→CONTEXT_OVERFLOW，
    # 认证→AUTH_ERROR，model not found→MODEL_UNAVAILABLE，其余→VALIDATION_ERROR）。
