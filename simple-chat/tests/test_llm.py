"""LLM 客户端单元测试：mock openai.AsyncOpenAI，覆盖流式解析 / 重试 / 异常映射。

不触网、不需要真实 API Key；通过 set_client 注入桩对象。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import openai
import pytest

from app.llm import client as llm_client
from app.llm import tokenizer
from app.llm.client import (
    LLMAuthError,
    LLMContextOverflow,
    LLMError,
    LLMRateLimited,
    LLMUnavailable,
    SYSTEM_PROMPT,
    chat,
    chat_stream,
    map_openai_error,
)


# ───────────────────────── 桩工具 ─────────────────────────


def _make_response(status_code: int, body: dict) -> httpx.Response:
    """构造一个带 Retry-After 头的 httpx.Response，用于复现上游错误。"""
    headers: dict[str, str] = {}
    if "retry_after" in body:
        headers["retry-after"] = str(body.pop("retry_after"))
    payload = json.dumps(body).encode()
    return httpx.Response(
        status_code=status_code,
        headers=headers,
        content=payload,
        request=httpx.Request("POST", "http://x/v1/chat/completions"),
    )


class StubStream:
    """可配置 chunk 序列与中途异常的假流式响应。"""

    def __init__(self, chunks: list[str], exc_at: int | None = None, exc: Exception | None = None):
        self._chunks = chunks
        self._exc_at = exc_at
        self._exc = exc

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for i, text in enumerate(self._chunks):
            if self._exc_at is not None and i == self._exc_at:
                raise self._exc  # type: ignore[misc]
            yield _Chunk(text)


class _Chunk:
    def __init__(self, text: str):
        self.choices = [_Choice(text)]


class _Choice:
    def __init__(self, text: str):
        self.delta = _Delta(text)
        self.finish_reason = "stop"


class _Delta:
    def __init__(self, text: str):
        self.content = text


class _Usage:
    def __init__(self, p=10, c=5, t=15):
        self.prompt_tokens = p
        self.completion_tokens = c
        self.total_tokens = t


class _NonStreamResp:
    def __init__(self, content: str = "完整回答"):
        self.choices = [MagicMock(message=MagicMock(content=content), finish_reason="stop")]
        self.usage = _Usage()


class StubCompletions:
    """记录调用次数与参数，按配置返回流式 / 非流式 / 抛异常。"""

    def __init__(self, *, stream_chunks=None, non_stream=None, exc=None):
        self.stream_chunks = stream_chunks or ["你", "好"]
        self.non_stream = non_stream or _NonStreamResp()
        self.exc = exc
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None and len(self.calls) == 1:
            # 仅首次抛异常，后续重试正常返回（用于重试测试）
            exc = self.exc
            self.exc = None
            raise exc
        if kwargs.get("stream"):
            return StubStream(self.stream_chunks)
        return self.non_stream


class StubChat:
    def __init__(self, completions: StubCompletions):
        self.completions = completions


class StubAsyncOpenAI:
    def __init__(self, completions: StubCompletions):
        self.chat = StubChat(completions)


@pytest.fixture(autouse=True)
def isolated_client():
    """每个测试独立客户端：结束后 reset，避免跨用例污染。"""
    yield
    llm_client.reset_client()


@pytest.fixture
def fast_retry(monkeypatch):
    """把退避延迟压到接近 0，避免重试测试等待真实秒数。"""
    monkeypatch.setattr(llm_client, "_backoff_delay", lambda attempt: 0.0)
    monkeypatch.setattr(llm_client.random, "uniform", lambda a, b: 0.0)


# ───────────────────────── 流式解析 ─────────────────────────


async def test_chat_stream_yields_tokens_in_order(monkeypatch):
    """chat_stream 按 chunk 顺序逐个产出文本片段，不拼接、不丢失。"""
    completions = StubCompletions(stream_chunks=["你好", "世界", "！"])
    llm_client.set_client(StubAsyncOpenAI(completions))

    parts = [p async for p in chat_stream([{"role": "user", "content": "hi"}])]

    assert parts == ["你好", "世界", "！"]
    assert completions.calls[0]["stream"] is True


async def test_chat_stream_injects_system_prompt(monkeypatch):
    """messages 首条非 system 时自动前置 SYSTEM_PROMPT。"""
    completions = StubCompletions(stream_chunks=["ok"])
    llm_client.set_client(StubAsyncOpenAI(completions))

    _ = [p async for p in chat_stream([{"role": "user", "content": "hi"}])]

    sent = completions.calls[0]["messages"]
    assert sent[0]["role"] == "system"
    assert sent[0]["content"] == SYSTEM_PROMPT
    assert sent[1]["role"] == "user"


async def test_chat_stream_preserves_existing_system_prompt(monkeypatch):
    """若 messages 已含 system，不重复注入。"""
    completions = StubCompletions(stream_chunks=["ok"])
    llm_client.set_client(StubAsyncOpenAI(completions))

    _ = [p async for p in chat_stream([
        {"role": "system", "content": "自定义"},
        {"role": "user", "content": "hi"},
    ])]

    sent = completions.calls[0]["messages"]
    assert sent[0]["content"] == "自定义"
    assert sum(1 for m in sent if m["role"] == "system") == 1


async def test_chat_stream_skips_empty_chunks(monkeypatch):
    """delta.content 为空或 None 的 chunk 不产出。"""
    completions = StubCompletions(stream_chunks=["a", "", "b"])
    llm_client.set_client(StubAsyncOpenAI(completions))

    parts = [p async for p in chat_stream([{"role": "user", "content": "x"}])]

    assert parts == ["a", "b"]


async def test_chat_stream_passes_params(monkeypatch):
    """model/temperature/max_tokens 正确透传，None 走默认值。"""
    completions = StubCompletions(stream_chunks=["ok"])
    llm_client.set_client(StubAsyncOpenAI(completions))

    _ = [p async for p in chat_stream(
        [{"role": "user", "content": "x"}],
        model="custom-model",
        temperature=0.1,
        max_tokens=128,
    )]

    call = completions.calls[0]
    assert call["model"] == "custom-model"
    assert call["temperature"] == 0.1
    assert call["max_tokens"] == 128


# ───────────────────────── 非流式 ─────────────────────────


async def test_chat_returns_full_response_with_usage(monkeypatch):
    """非流式返回 content + usage（prompt/completion/total）+ model + finish_reason。"""
    completions = StubCompletions(non_stream=_NonStreamResp(content="完整回答"))
    llm_client.set_client(StubAsyncOpenAI(completions))

    resp = await chat([{"role": "user", "content": "hi"}])

    assert resp["content"] == "完整回答"
    assert resp["model"]
    assert resp["finish_reason"] == "stop"
    assert resp["usage"]["prompt_tokens"] == 10
    assert resp["usage"]["completion_tokens"] == 5
    assert resp["usage"]["total_tokens"] == 15


async def test_chat_non_stream_not_injected_into_stream(monkeypatch):
    """非流式调用 stream=False。"""
    completions = StubCompletions()
    llm_client.set_client(StubAsyncOpenAI(completions))

    _ = await chat([{"role": "user", "content": "hi"}])

    assert completions.calls[0]["stream"] is False


# ───────────────────────── 重试 ─────────────────────────


async def test_retry_on_429_then_succeeds(fast_retry):
    """429 可重试，重试后成功产出 token。"""
    exc = openai.RateLimitError(
        "rate limited", response=_make_response(429, {"error": {"message": "rl"}}), body=None
    )
    completions = StubCompletions(stream_chunks=["ok"], exc=exc)
    llm_client.set_client(StubAsyncOpenAI(completions))

    parts = [p async for p in chat_stream([{"role": "user", "content": "x"}])]

    assert parts == ["ok"]
    assert len(completions.calls) == 2  # 首次失败 + 1 次重试


async def test_retry_on_503_then_succeeds(fast_retry):
    """503 可重试。"""
    exc = openai.APIStatusError(
        "unavailable", response=_make_response(503, {"error": {"message": "down"}}), body=None
    )
    completions = StubCompletions(stream_chunks=["ok"], exc=exc)
    llm_client.set_client(StubAsyncOpenAI(completions))

    parts = [p async for p in chat_stream([{"role": "user", "content": "x"}])]

    assert parts == ["ok"]
    assert len(completions.calls) == 2


async def test_retry_exhausted_raises_rate_limited(fast_retry):
    """连续 429 直到耗尽重试次数 → LLMRateLimited。"""
    exc = openai.RateLimitError(
        "rl", response=_make_response(429, {"error": {"message": "rl"}}), body=None
    )

    class AlwaysFail(StubCompletions):
        async def create(self, **kwargs):
            self.calls.append(kwargs)
            raise exc

    llm_client.set_client(StubAsyncOpenAI(AlwaysFail(stream_chunks=["ok"])))

    with pytest.raises(LLMRateLimited):
        _ = [p async for p in chat_stream([{"role": "user", "content": "x"}])]


async def test_retry_uses_exponential_backoff(monkeypatch):
    """退避序列为 0.5 * 2**attempt + jitter；记录每次 sleep 的延迟。"""
    delays: list[float] = []
    monkeypatch.setattr(
        llm_client.asyncio, "sleep", AsyncMock(side_effect=lambda d: _append(delays, d))
    )
    monkeypatch.setattr(llm_client.random, "uniform", lambda a, b: 0.0)

    exc = openai.RateLimitError(
        "rl", response=_make_response(429, {"error": {"message": "rl"}}), body=None
    )

    class AlwaysFail(StubCompletions):
        async def create(self, **kwargs):
            self.calls.append(kwargs)
            raise exc

    llm_client.set_client(StubAsyncOpenAI(AlwaysFail()))

    with pytest.raises(LLMRateLimited):
        _ = [p async for p in chat_stream([{"role": "user", "content": "x"}])]

    # 2 次重试 → 2 次 sleep，延迟应为 0.5 * 2**0 与 0.5 * 2**1
    assert len(delays) == 2
    assert delays[0] == pytest.approx(0.5)
    assert delays[1] == pytest.approx(1.0)


def _append(lst: list, val: Any) -> asyncio.Future:
    """配合 AsyncMock side_effect：返回已完成的 future，同时记录延迟。"""
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    fut.set_result(None)
    lst.append(val)
    return fut


async def test_no_retry_on_400_bad_request(fast_retry):
    """400 不可重试，首次即抛 LLMContextOverflow（规格：BadRequestError 一律归上下文过长）。"""
    exc = openai.BadRequestError(
        "bad", response=_make_response(400, {"error": {"message": "nope"}}), body=None
    )
    completions = StubCompletions(stream_chunks=["ok"], exc=exc)
    llm_client.set_client(StubAsyncOpenAI(completions))

    with pytest.raises(LLMContextOverflow):
        _ = [p async for p in chat_stream([{"role": "user", "content": "x"}])]

    assert len(completions.calls) == 1  # 未重试


# ───────────────────────── 异常映射 ─────────────────────────


async def test_auth_error_mapped(fast_retry):
    """AuthenticationError → LLMAuthError。"""
    exc = openai.AuthenticationError(
        "bad key", response=_make_response(401, {"error": {"message": "unauth"}}), body=None
    )
    completions = StubCompletions(stream_chunks=["ok"], exc=exc)
    llm_client.set_client(StubAsyncOpenAI(completions))

    with pytest.raises(LLMAuthError):
        _ = [p async for p in chat_stream([{"role": "user", "content": "x"}])]


async def test_rate_limit_error_mapped(fast_retry):
    """RateLimitError（重试耗尽）→ LLMRateLimited。"""
    exc = openai.RateLimitError(
        "rl", response=_make_response(429, {"error": {"message": "rl"}}), body=None
    )

    class AlwaysFail(StubCompletions):
        async def create(self, **kwargs):
            raise exc

    llm_client.set_client(StubAsyncOpenAI(AlwaysFail()))

    with pytest.raises(LLMRateLimited):
        _ = [p async for p in chat_stream([{"role": "user", "content": "x"}])]


async def test_connection_error_mapped_after_retry(fast_retry):
    """APIConnectionError 可重试，耗尽后 → LLMUnavailable。"""
    exc = openai.APIConnectionError(request=httpx.Request("POST", "http://x/v1"))

    class AlwaysFail(StubCompletions):
        async def create(self, **kwargs):
            raise exc

    llm_client.set_client(StubAsyncOpenAI(AlwaysFail()))

    with pytest.raises(LLMUnavailable):
        _ = [p async for p in chat_stream([{"role": "user", "content": "x"}])]


async def test_context_overflow_mapped(fast_retry):
    """BadRequestError 且内容含 context 关键词 → LLMContextOverflow。"""
    exc = openai.BadRequestError(
        "context length exceeded",
        response=_make_response(400, {"error": {"message": "context length exceeded"}}),
        body=None,
    )
    completions = StubCompletions(stream_chunks=["ok"], exc=exc)
    llm_client.set_client(StubAsyncOpenAI(completions))

    with pytest.raises(LLMContextOverflow):
        _ = [p async for p in chat_stream([{"role": "user", "content": "x"}])]


async def test_generic_api_error_mapped(fast_retry):
    """其他 APIError → LLMError。"""
    # APIError 需要 request 与 body 参数
    exc = openai.APIError("boom", request=httpx.Request("POST", "http://x/v1"), body=None)
    completions = StubCompletions(stream_chunks=["ok"], exc=exc)
    llm_client.set_client(StubAsyncOpenAI(completions))

    with pytest.raises(LLMError):
        _ = [p async for p in chat_stream([{"role": "user", "content": "x"}])]


def test_map_openai_error_unit():
    """map_openai_error 对各类异常的映射关系正确。"""
    req = httpx.Request("POST", "http://x/v1")
    assert isinstance(
        map_openai_error(openai.AuthenticationError("x", response=_make_response(401, {}), body=None)),
        LLMAuthError,
    )
    assert isinstance(
        map_openai_error(openai.RateLimitError("x", response=_make_response(429, {}), body=None)),
        LLMRateLimited,
    )
    assert isinstance(map_openai_error(openai.APIConnectionError(request=req)), LLMUnavailable)
    # 规格：BadRequestError 一律归为 LLMContextOverflow
    ctx_exc = openai.BadRequestError(
        "maximum context length", response=_make_response(400, {}), body=None
    )
    assert isinstance(map_openai_error(ctx_exc), LLMContextOverflow)
    plain_bad = openai.BadRequestError(
        "plain bad", response=_make_response(400, {"error": {"message": "nope"}}), body=None
    )
    assert isinstance(map_openai_error(plain_bad), LLMContextOverflow)
    # 5xx APIStatusError → LLMUnavailable
    server_err = openai.APIStatusError(
        "server boom", response=_make_response(502, {}), body=None
    )
    assert isinstance(map_openai_error(server_err), LLMUnavailable)
    # APIError 基类（非 status）→ LLMError
    assert isinstance(
        map_openai_error(openai.APIError("boom", request=req, body=None)), LLMError
    )


# ───────────────────────── tokenizer ─────────────────────────


def test_estimate_tokens_empty():
    """空消息列表仅含格式开销 2。"""
    assert tokenizer.estimate_tokens([]) == 2


def test_estimate_tokens_grows_with_content():
    """内容越多 token 越多。"""
    small = tokenizer.estimate_tokens([{"role": "user", "content": "hi"}])
    large = tokenizer.estimate_tokens([{"role": "user", "content": "x" * 300}])
    assert large > small > 2


def test_estimate_tokens_handles_missing_fields():
    """缺 role/content 的消息不报错。"""
    assert tokenizer.estimate_tokens([{}]) >= 2


def test_count_tokens_empty():
    assert tokenizer.count_tokens("") == 0


def test_count_tokens_nonempty():
    assert tokenizer.count_tokens("hello world") > 0


# ───────────────────────── 日志隐私 ─────────────────────────


async def test_log_does_not_leak_content(monkeypatch, caplog):
    """日志只记 model/tokens/耗时/是否流式/错误码，不记 content。"""
    import logging

    completions = StubCompletions(stream_chunks=["私密内容"])
    llm_client.set_client(StubAsyncOpenAI(completions))

    # app 日志默认不向 root 传播；临时开启以便 caplog 捕获（不影响生产行为）。
    app_log = logging.getLogger("app")
    prev_propagate = app_log.propagate
    app_log.propagate = True
    try:
        with caplog.at_level("INFO", logger="app.llm.client"):
            _ = [p async for p in chat_stream([{"role": "user", "content": "another-secret"}])]
    finally:
        app_log.propagate = prev_propagate

    blob = " ".join(r.getMessage() for r in caplog.records)
    assert "私密内容" not in blob
    assert "another-secret" not in blob
    # 日志消息本身为事件名 "llm_call"，不携带任何 content
    assert any(r.getMessage() == "llm_call" for r in caplog.records)
    # 流式标记通过结构化字段（extra.stream）传递，不出现在消息明文中
    assert any(getattr(r, "stream", None) is True for r in caplog.records)


# ───────────────────────── tokenizer 精度与回退 ─────────────────────────


def test_estimate_tokens_close_to_tiktoken_within_20pct():
    """tiktoken 可用时，estimate_tokens 估算值与真实编码值偏差 < 20%。"""
    tiktoken = pytest.importorskip("tiktoken")
    enc = tiktoken.get_encoding("cl100k_base")

    messages = [
        {"role": "user", "content": "Hello world, this is a tokenization test. " * 5},
        {
            "role": "assistant",
            "content": "Sure, here is a longer response about token counting. " * 5,
        },
    ]

    est = tokenizer.estimate_tokens(messages)

    # 用与 estimate_tokens 相同的公式（但使用真实编码器）计算“真实” token 数
    real = 2
    for m in messages:
        real += len(enc.encode(m["role"], disallowed_special=()))
        real += len(enc.encode(m["content"], disallowed_special=()))
        real += 4

    deviation = abs(est - real) / real
    assert deviation < 0.2, f"偏差 {deviation:.2%} 超过 20%"


def test_tokenizer_fallback_when_tiktoken_unavailable(monkeypatch):
    """tiktoken 不可用时，count_tokens / estimate_tokens 退化为字符近似。"""
    # 强制 _get_encoder 返回 None（模拟离线/无网络）
    monkeypatch.setattr(tokenizer, "_encoder", None)
    monkeypatch.setattr(tokenizer, "_tried", True)

    # count_tokens 回退：max(1, len // 3)
    assert tokenizer.count_tokens("") == 0
    assert tokenizer.count_tokens("abcdefghij") == 3  # 10 // 3 = 3
    assert tokenizer.count_tokens("a") == 1  # 1 // 3 = 0 -> max(1, 0) = 1

    # estimate_tokens 回退公式：2 + len(role) + len(content)//3 + 4
    est = tokenizer.estimate_tokens([{"role": "user", "content": "abc"}])
    assert est == 2 + len("user") + (len("abc") // 3) + 4  # 2 + 4 + 1 + 4 = 11
    assert tokenizer.estimate_tokens([]) == 2  # 仅格式开销
