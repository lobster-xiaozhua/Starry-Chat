"""登录/注册独立限流桶：按 (桶, IP, 用户名) 的滑动窗口失败计数。

为什么独立于全局 RateLimitMiddleware（app/middleware/rate_limit.py）：
- 全局中间件按 user_id + IP 限 30 次/60s，且开发环境豁免 loopback；
  登录爆破必须更严格，且不受 loopback 白名单与 AUTH_ENABLED 开关影响。
- 只在失败时计数：正常登录不受影响；成功后清零该 key。

单实例、纯内存，与全局限流同一基线（不引入 Redis）。多 worker 场景下
每进程独立计数，属已知技术债（见 ROADMAP 技术债表）。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from app.config import settings
from app.errors import RateLimitedError

# key -> 失败时间戳队列（monotonic 秒）
_failures: dict[str, deque] = defaultdict(deque)


def _key(bucket: str, ip: str, username: str) -> str:
    return f"{bucket}:{ip}:{(username or '').strip().lower()}"


def _prune(dq: deque, now: float, window: int) -> None:
    cutoff = now - window
    while dq and dq[0] <= cutoff:
        dq.popleft()


def check_allowed(bucket: str, ip: str, username: str) -> None:
    """进入认证端点前的闸门；已超限抛 429 RATE_LIMITED（带 Retry-After）。"""
    if not settings.auth_rate_limit_enabled:
        return
    window = settings.auth_rate_limit_window_seconds
    now = time.monotonic()
    dq = _failures[_key(bucket, ip, username)]
    _prune(dq, now, window)
    if len(dq) >= settings.auth_rate_limit_max:
        retry_after = max(1, int(window - (now - dq[0])))
        raise RateLimitedError(
            f"登录尝试过于频繁，请 {retry_after} 秒后再试",
            retry_after=retry_after,
        )


def record_failure(bucket: str, ip: str, username: str) -> None:
    """记录一次失败；窗口外的旧记录顺带清理。"""
    if not settings.auth_rate_limit_enabled:
        return
    window = settings.auth_rate_limit_window_seconds
    now = time.monotonic()
    dq = _failures[_key(bucket, ip, username)]
    _prune(dq, now, window)
    dq.append(now)


def reset(bucket: str, ip: str, username: str) -> None:
    """成功后清零该 key 的失败计数。"""
    _failures.pop(_key(bucket, ip, username), None)


def clear() -> None:
    """清空全部计数（测试用）。"""
    _failures.clear()


__all__ = ["check_allowed", "record_failure", "reset", "clear"]
