"""v0.3 每用户日 token 预算护栏。

设计约束（对齐 ROADMAP v0.3 MVI）：
- 路由前检查当日该用户累计 token（从 message 表 SUM），超
  BUDGET_DAILY_TOKENS_PER_USER → 429 RATE_LIMITED（文案“今日额度已用尽”）。
- 次日按 UTC 日期边界自然恢复（无需重置计数器，按日期过滤即可）。
- 预算检查与全局限流独立：BUDGET_ENABLED 关闭时不检查，即便限流开启。
- 上限为 0 时即便开启也不限制（便于“不限制但保留接入位”的部署）。
- 与路由开关解耦：可在 ROUTING_ENABLED=false 时仍生效。

注意：token 口径沿用 message.tokens（落库口径，非模型返回的精确 usage）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite

from app.config import settings
from app.errors import RateLimitedError


def _utc_day_start_iso(now: datetime | None = None) -> str:
    """UTC 当日起点（00:00:00.000Z）的 ISO 串，用于 SQL 字符串比较。

    created_at 落库格式为 `utcnow_iso()`（UTC ISO 毫秒 + Z），字典序与时间序
    一致；故只需比较 created_at >= 当日 00:00:00.000Z 即可。
    """
    cur = now or datetime.now(timezone.utc)
    return cur.strftime("%Y-%m-%dT00:00:00.000") + "Z"


async def daily_user_tokens(conn: aiosqlite.Connection, user_id: str) -> int:
    """统计该用户当日（UTC）累计 token。

    按 conversation.user_id 过滤，SUM(message.tokens) 覆盖 prompt + completion
    （user/assistant 消息都带 tokens，落库时均已写入）。
    """
    since = _utc_day_start_iso()
    cur = await conn.execute(
        """
        SELECT COALESCE(SUM(m.tokens), 0) AS total
        FROM message m
        JOIN conversation c ON c.id = m.conversation_id
        WHERE c.user_id = ? AND m.created_at >= ?
        """,
        (user_id, since),
    )
    row = await cur.fetchone()
    return int(row["total"] if row else 0)


async def enforce_budget(
    conn: aiosqlite.Connection,
    user_id: str,
) -> None:
    """路由前预算护栏：超额抛 RateLimitedError（429）。

    关闭时直接返回；上限为 0 时也直接返回（不限制）。
    """
    if not settings.effective_budget_enabled:
        return
    limit = settings.effective_budget_daily_tokens
    if limit <= 0:
        return
    used = await daily_user_tokens(conn, user_id)
    if used >= limit:
        raise RateLimitedError("今日额度已用尽")


__all__ = ["daily_user_tokens", "enforce_budget"]
