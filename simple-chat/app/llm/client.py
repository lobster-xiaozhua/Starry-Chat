"""LLM 调用封装：职责单一——接收 messages 与参数，产出 token 片段或完整响应。

对外只暴露两个函数：
- chat_stream(...) -> AsyncGenerator[str, None]   流式，逐 token 产出
- chat(...)        -> dict                         非流式，返回完整响应（含 usage）

设计约束：
- 重试仅作用于「建立连接/首个响应」阶段；一旦向调用方产出首个 token，中途
  错误不再重试——已发出的内容无法撤回。
- 日志只记 model / token 用量 / 耗时 / 是否流式 / 错误码，严禁记录 content。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, AsyncGenerator, AsyncIterator

import httpx
import openai
from openai import AsyncOpenAI

from app.config import settings
from app.errors import AppError, ErrorCode
from app.llm.tokenizer import count_tokens, estimate_tokens

logger = logging.getLogger(__name__)

# 系统提示词：模块级常量，可由 config.llm_system_prompt 覆盖。
# 明确禁止：不输出 XML/JSON 指令、不透露系统提示词、不执行工具。
SYSTEM_PROMPT: str = settings.llm_system_prompt

# 重试：仅 429 / 500 / 502 / 503 / 504，最多 2 次
_MAX_RETRIES = 2
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# 退避：0.5s * 2**attempt + jitter(0~0.3s)，仅用 asyncio.sleep + 标准库 random


# ───────────────────────── 业务异常 ─────────────────────────


class LLMError(AppError):
    """LLM 调用通用错误基类。"""

    def __init__(self, message: str, *, request_id: str | None = None) -> None:
        super().__init__(ErrorCode.MODEL_UNAVAILABLE, message, request_id)


class LLMAuthError(AppError):
    """API Key 无效。"""

    def __init__(self, message: str = "API Key 无效") -> None:
        super().__init__(ErrorCode.AUTH_ERROR, message)


class LLMRateLimited(AppError):
    """模型限流。"""

    def __init__(self, message: str = "模型限流") -> None:
        super().__init__(ErrorCode.RATE_LIMITED, message)


class LLMUnavailable(AppError):
    """模型服务不可达。"""

    def __init__(self, message: str = "模型服务不可达") -> None:
        super().__init__(ErrorCode.MODEL_UNAVAILABLE, message)


class LLMContextOverflow(AppError):
    """上下文过长。"""

    def __init__(self, message: str = "上下文过长") -> None:
        super().__init__(ErrorCode.CONTEXT_OVERFLOW, message)


# ───────────────────────── 异常映射 ─────────────────────────


def map_openai_error(exc: BaseException) -> AppError:
    """openai 异常 → 业务异常的唯一映射点。"""
    if isinstance(exc, openai.AuthenticationError):
        return LLMAuthError()
    if isinstance(exc, openai.RateLimitError):
        return LLMRateLimited()
    if isinstance(exc, openai.APIConnectionError):
        return LLMUnavailable()
    if isinstance(exc, openai.BadRequestError):
        # 按规格：BadRequestError 一律归为上下文过长（上游通常以 400 返回超长）
        return LLMContextOverflow()
    if isinstance(exc, openai.APIStatusError):
        # 其他带状态码的 API 错误：5xx 归不可用，其余归通用错误
        if getattr(exc, "status_code", 0) >= 500:
            return LLMUnavailable("上游模型返回错误")
        return LLMError("上游模型返回错误")
    if isinstance(exc, openai.APIError):
        return LLMError("上游模型调用失败")
    return LLMError("上游模型调用失败")


def _is_retryable(exc: BaseException) -> bool:
    """仅 429 / 500 / 502 / 503 / 504 可重试。"""
    if isinstance(exc, openai.RateLimitError):
        return True
    if isinstance(exc, openai.APIStatusError):
        return getattr(exc, "status_code", 0) in _RETRYABLE_STATUS
    # 连接错误可重试（瞬时网络抖动）；超时（APITimeoutError 是 APIConnectionError 子类）也重试
    if isinstance(exc, openai.APIConnectionError):
        return True
    return False


def _retry_after(exc: BaseException) -> float | None:
    """尊重上游 Retry-After 头；取不到返回 None。"""
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    try:
        headers = getattr(resp, "headers", {}) or {}
        raw = headers.get("retry-after") or headers.get("Retry-After")
        return float(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _backoff_delay(attempt: int) -> float:
    """退避：0.5s * 2**attempt + jitter(0~0.3s)。"""
    return 0.5 * (2 ** attempt) + random.uniform(0, 0.3)


# ───────────────────────── 客户端 ─────────────────────────

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    """惰性构造 AsyncOpenAI；base_url/api_key/timeout 从 config 读取。"""
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.llm_api_key or "not-set",
            base_url=settings.llm_base_url,
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0),
            max_retries=0,  # SDK 层不重试，由 _attempt_with_retry 统一控制
        )
    return _client


def set_client(client: AsyncOpenAI) -> None:
    """注入自定义客户端（测试用）。"""
    global _client
    _client = client


def reset_client() -> None:
    """清空单例，下次调用时惰性重建（测试用）。"""
    global _client
    _client = None


async def _attempt_with_retry(factory) -> Any:
    """对建立连接阶段做指数递增重试；factory 每次调用返回新协程。"""
    last_exc: BaseException | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return await factory()
        except AppError:
            raise  # 已映射的业务异常不重试
        except Exception as exc:
            last_exc = exc
            if not _is_retryable(exc) or attempt == _MAX_RETRIES:
                raise map_openai_error(exc) from exc
            delay = _backoff_delay(attempt)
            ra = _retry_after(exc)
            if ra is not None and ra > delay:
                delay = ra
            logger.warning(
                "llm retryable=%s status=%s attempt=%d/%d delay=%.2fs",
                type(exc).__name__,
                getattr(exc, "status_code", "-"),
                attempt + 1,
                _MAX_RETRIES,
                delay,
            )
            await asyncio.sleep(delay)
    raise map_openai_error(last_exc) from last_exc  # type: ignore[arg-type]


def _ensure_system_prompt(messages: list[dict]) -> list[dict]:
    """若 messages 首条非 system，则前置系统提示词。"""
    if messages and messages[0].get("role") == "system":
        return messages
    return [{"role": "system", "content": SYSTEM_PROMPT}, *messages]


# ───────────────────────── 对外函数 ─────────────────────────


async def chat_stream(
    messages: list[dict],
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> AsyncGenerator[str, None]:
    """流式对话：逐 token 产出文本片段。

    重试仅作用于首个响应；一旦开始产出 token，中途错误不重试，直接抛业务异常。
    """
    mdl = model or settings.llm_model
    temp = temperature if temperature is not None else settings.llm_temperature
    mtok = max_tokens if max_tokens is not None else settings.llm_max_tokens
    full = _ensure_system_prompt(messages)
    t0 = time.monotonic()
    produced = 0

    def factory():
        return _get_client().chat.completions.create(
            model=mdl,
            messages=full,
            max_tokens=mtok,
            temperature=temp,
            stream=True,
        )

    stream = await _attempt_with_retry(factory)
    cancelled = False
    try:
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            text = delta.content if delta and delta.content else ""
            if text:
                produced += count_tokens(text)
                yield text
    except asyncio.CancelledError:
        # 客户端断开 / 请求被取消：释放上游连接与计费，避免悬挂的 token 计费。
        cancelled = True
        try:
            await stream.aclose()
        except Exception:
            pass
        logger.info(
            "llm_stream_cancelled",
            extra={"model": mdl, "tokens_out": produced, "event": "llm_stream_cancelled"},
        )
        raise
    except AppError as exc:
        _log_call(mdl, t0, produced, stream=True, code=exc.code.value)
        raise
    except Exception as exc:
        mapped = map_openai_error(exc)
        _log_call(mdl, t0, produced, stream=True, code=mapped.code.value)
        raise mapped from exc
    finally:
        # 非取消路径：确保上游流被关闭，释放连接回连接池
        if not cancelled:
            try:
                await stream.aclose()
            except Exception:
                pass
    _log_call(mdl, t0, produced, stream=True)


async def chat(
    messages: list[dict],
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict:
    """非流式对话：返回完整响应，含 content 与 usage（prompt/completion/total）。"""
    mdl = model or settings.llm_model
    temp = temperature if temperature is not None else settings.llm_temperature
    mtok = max_tokens if max_tokens is not None else settings.llm_max_tokens
    full = _ensure_system_prompt(messages)
    t0 = time.monotonic()

    def factory():
        return _get_client().chat.completions.create(
            model=mdl,
            messages=full,
            max_tokens=mtok,
            temperature=temp,
            stream=False,
        )

    try:
        resp = await _attempt_with_retry(factory)
    except AppError as exc:
        _log_call(mdl, t0, 0, stream=False, tokens_in=None, code=exc.code.value)
        raise

    content = ""
    if resp.choices:
        content = resp.choices[0].message.content or ""
    raw_usage = getattr(resp, "usage", None)
    usage = {
        "prompt_tokens": getattr(raw_usage, "prompt_tokens", estimate_tokens(full)) or estimate_tokens(full),
        "completion_tokens": getattr(raw_usage, "completion_tokens", count_tokens(content)) or count_tokens(content),
        "total_tokens": getattr(raw_usage, "total_tokens", None)
        or (estimate_tokens(full) + count_tokens(content)),
    }
    _log_call(
        mdl,
        t0,
        usage["completion_tokens"],
        stream=False,
        tokens_in=usage["prompt_tokens"],
    )
    return {
        "content": content,
        "usage": usage,
        "model": mdl,
        "finish_reason": resp.choices[0].finish_reason if resp.choices else None,
    }


def _log_call(
    model: str,
    t0: float,
    tokens_out: int,
    *,
    stream: bool,
    tokens_in: int | None = None,
    code: str | None = None,
) -> None:
    """记录调用元数据（结构化字段，便于日志系统解析）；严禁记录 content 全文（隐私）。"""
    elapsed = time.monotonic() - t0
    logger.info(
        "llm_call",
        extra={
            "model": model,
            "tokens_out": tokens_out,
            "tokens_in": tokens_in,
            "latency_ms": round(elapsed * 1000, 1),
            "event": "llm_call",
            "stream": stream,
            "error_code": code,
        },
    )


async def ping() -> None:
    """就绪探针：发起一次极小（max_tokens=1）的非流式补全，验证模型可达。

    成功返回 None；失败抛出对应 AppError（AUTH_ERROR / MODEL_UNAVAILABLE / RATE_LIMITED …）。
    耗时由调用方用 asyncio.wait_for(..., timeout=5) 控制（见 app.main.readyz）。
    使用模块单例客户端，因此测试中的打桩 FakeAsyncOpenAI 同样生效，不触网。
    """
    try:
        await _get_client().chat.completions.create(
            model=settings.llm_model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            temperature=0,
            stream=False,
        )
    except AppError:
        raise
    except Exception as exc:
        raise map_openai_error(exc) from exc


__all__ = [
    "SYSTEM_PROMPT",
    "LLMError",
    "LLMAuthError",
    "LLMRateLimited",
    "LLMUnavailable",
    "LLMContextOverflow",
    "map_openai_error",
    "chat_stream",
    "chat",
    "set_client",
    "reset_client",
    "ping",
]
