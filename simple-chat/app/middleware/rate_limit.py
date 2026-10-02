"""限流中间件：基于 user_id + IP 的滑动窗口（窗口 60s，上限 30 次）。

⚠️ 单实例、纯内存实现。
- 多实例 / 多 worker 部署时，每个进程各自维护计数器，限流不再全局准确。
- 生产多实例请替换为 Redis + Lua 脚本（原子 INCR + 滑动窗口 / Sorted Set），
  否则只能做到“每实例 30 次/60s”的弱限流。

行为：
- 超过上限返回 429，并带响应头 Retry-After: 60 / X-RateLimit-Limit: 30 /
  X-RateLimit-Remaining: 0。
- 白名单：localhost / 127.0.0.1 / ::1 不限制（开发环境本机调试用）。
- 健康检查 / 静态资源跳过限流。
- 是否启用由 settings.effective_rate_limit 决定（开发默认关、生产默认开）。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.errors import ErrorCode

RATE_LIMIT = 30
WINDOW_SECONDS = 60
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _client_ip(request: Request) -> str:
    """取客户端 IP。

    ⚠️ X-Forwarded-For 仅在 config.trusted_proxies 非空时才采信：否则攻击者可伪造
    XFF 把自己伪装成受信代理后的任意 IP，从而绕过按 IP 的限流。直连 IP 始终可信。
    """
    if settings.trusted_proxies:
        fwd = request.headers.get("X-Forwarded-For")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# 向后兼容别名（如有外部引用）
get_client_ip = _client_ip


class RateLimitMiddleware(BaseHTTPMiddleware):
    """内存滑动窗口限流（单实例）。详见模块 docstring。"""

    def __init__(self, app) -> None:
        super().__init__(app)
        # key -> 命中时间戳（monotonic 秒）队列
        self._hits: dict[str, deque] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next):
        if not settings.effective_rate_limit:
            return await call_next(request)

        path = request.url.path
        # /metrics 豁免限流（PR-3 改动 1）：Prometheus 默认 15s 抓取一次，
        # 计入限流会在多抓取器/多实例场景下挤占业务配额
        if path in ("/healthz", "/readyz", "/metrics") or path.startswith("/static"):
            return await call_next(request)

        ip = _client_ip(request)
        # WHITELIST 仅在开发环境生效；生产环境一律按 IP 限流（含本机 localhost），
        # 否则本地压测/脚本可无限打满接口。
        if settings.app_env == "development" and ip in _LOOPBACK:
            return await call_next(request)

        user_id = request.headers.get("X-User-Id") or "anonymous"
        key = f"{user_id}:{ip}"

        now = time.monotonic()
        dq = self._hits[key]
        # 丢弃窗口外的旧记录
        cutoff = now - WINDOW_SECONDS
        while dq and dq[0] <= cutoff:
            dq.popleft()

        if len(dq) >= RATE_LIMIT:
            return JSONResponse(
                status_code=429,
                headers={
                    "Retry-After": "60",
                    "X-RateLimit-Limit": str(RATE_LIMIT),
                    "X-RateLimit-Remaining": "0",
                },
                content={
                    "error": {
                        "code": ErrorCode.RATE_LIMITED.value,
                        "message": ErrorCode.RATE_LIMITED.default_message,
                    }
                },
            )

        dq.append(now)
        remaining = RATE_LIMIT - len(dq)

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(RATE_LIMIT)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response


__all__ = ["RateLimitMiddleware", "get_client_ip"]
