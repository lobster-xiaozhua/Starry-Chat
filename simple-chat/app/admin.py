"""管理端点（PR-3 改动 2）：成本账单聚合。

权限模型（local-first，不强制）：
- 未配置 ADMIN_API_KEY → 整个端点返回 501 Not Implemented（管理员可能不需要）。
- 配置了 ADMIN_API_KEY → 请求须带 X-Admin-Key 头（常量时间比较），不匹配 401。

聚合规则：
- 直接 SQL SUM + GROUP BY，绝不拉全表到 Python。
- token 归属：message.role='user' → prompt_tokens；role='assistant' → completion_tokens。
  （发给模型的 prompt 总量与用户消息 token 有 build_context 差异，此处按落库口径
  统计，成本估算精度足够；精确账单以模型返回的 usage 为准，误差 <3% 见验收。）
- requests = assistant 消息数（每次成功的对话请求恰好落一条 assistant 行）。
- 时间窗口：默认最近 7 天，最大跨度 90 天，超限 422。
- 缓存：内存 dict + TTL 60s，key=(from,to,user_id)，避免被刷。
- 价格：settings.model_price[settings.llm_model] 缺失 → cost_usd=0，不报错。
"""

from __future__ import annotations

import hmac
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app import metrics
from app.config import settings
from app.db import get_db
from app.errors import AppError, ErrorCode

router = APIRouter(prefix="/api/admin", tags=["admin"])

# 简单内存缓存：key -> (过期时刻 monotonic, payload)
_cost_cache: dict[tuple[str, str, str], tuple[float, dict]] = {}
COST_CACHE_TTL = 60.0


def _parse_bound(raw: str | None, *, is_end: bool) -> tuple[str, datetime]:
    """解析 from/to 边界：YYYY-MM-DD 或完整 ISO8601（Z 结尾可）。

    返回 (用于 SQL 字符串比较的 ISO 串, 用于跨度校验的 datetime)。
    仅日期时：from 补 T00:00:00.000Z、to 补 T23:59:59.999Z（含端点当天）。
    """
    now = datetime.now(timezone.utc)
    if raw is None or not raw.strip():
        if is_end:
            return now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", now
        start = now - timedelta(days=7)
        return start.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", start

    value = raw.strip()
    if len(value) == 10:  # YYYY-MM-DD
        try:
            day = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            raise AppError(ErrorCode.VALIDATION_ERROR, f"时间格式不合法: {value}") from None
        if is_end:
            end_of_day = day + timedelta(hours=23, minutes=59, seconds=59, milliseconds=999)
            return end_of_day.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", day
        return day.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", day

    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        raise AppError(ErrorCode.VALIDATION_ERROR, f"时间格式不合法: {value}") from None
    return value, dt


@router.get("/cost", summary="按用户聚合 token 用量与成本（管理端点）")
async def cost(
    request: Request,
    from_: str | None = Query(None, alias="from", description="起始时间（含），YYYY-MM-DD 或 ISO8601"),
    to: str | None = Query(None, alias="to", description="结束时间（含）"),
    user_id: str | None = Query(None, description="过滤指定用户"),
):
    # 1) 权限：未配置密钥 → 501（不能强制 local-first 管理员使用此接口）
    if not settings.admin_api_key.strip():
        return JSONResponse(
            status_code=501,
            content={
                "error": {
                    "code": "NOT_IMPLEMENTED",
                    "message": "成本端点未启用：请配置 ADMIN_API_KEY 环境变量",
                }
            },
        )
    given = request.headers.get("X-Admin-Key", "")
    if not hmac.compare_digest(given, settings.admin_api_key):
        # 注意：ErrorCode.AUTH_ERROR 的 HTTP 映射是 500（上游模型鉴权失败语义），
        # 管理端点的密钥错误语义是 401，故直接构造响应
        return JSONResponse(
            status_code=401,
            content={"error": {"code": "AUTH_ERROR", "message": "管理密钥不正确"}},
        )

    # 2) 时间窗口
    from_iso, from_dt = _parse_bound(from_, is_end=False)
    to_iso, to_dt = _parse_bound(to, is_end=True)
    if (to_dt - from_dt).total_seconds() > 90 * 24 * 3600:
        raise AppError(ErrorCode.VALIDATION_ERROR, "时间跨度不能超过 90 天")
    if to_dt < from_dt:
        raise AppError(ErrorCode.VALIDATION_ERROR, "to 不能早于 from")

    # 3) 缓存
    cache_key = (from_iso, to_iso, user_id or "")
    hit = _cost_cache.get(cache_key)
    now_mono = time.monotonic()
    if hit is not None and hit[0] > now_mono:
        return hit[1]
    # 顺手清理过期项，防 dict 无限增长
    for k in [k for k, v in _cost_cache.items() if v[0] <= now_mono]:
        _cost_cache.pop(k, None)

    # 4) SQL 聚合（SUM + GROUP BY，不拉全表）
    sql = """
        SELECT c.user_id AS user_id,
               COALESCE(SUM(CASE WHEN m.role = 'user' THEN m.tokens END), 0)  AS prompt_tokens,
               COALESCE(SUM(CASE WHEN m.role = 'assistant' THEN m.tokens END), 0) AS completion_tokens,
               COUNT(CASE WHEN m.role = 'assistant' THEN 1 END)               AS requests
        FROM message m
        JOIN conversation c ON c.id = m.conversation_id
        WHERE m.created_at >= ?
          AND m.created_at <= ?
          AND m.role IN ('user', 'assistant')
          AND (? IS NULL OR c.user_id = ?)
        GROUP BY c.user_id
        ORDER BY prompt_tokens + completion_tokens DESC
    """
    with metrics.db_time("admin_cost_agg"):
        async with get_db() as conn:
            cur = await conn.execute(sql, (from_iso, to_iso, user_id, user_id))
            rows = [dict(r) for r in await cur.fetchall()]

    # 5) 价格（未配置 → 0，不报错）
    price = settings.model_price.get(settings.llm_model) or {}
    price_prompt = float(price.get("prompt", 0.0))
    price_completion = float(price.get("completion", 0.0))

    def _cost(p: int, c: int) -> float:
        return round(p / 1e6 * price_prompt + c / 1e6 * price_completion, 6)

    by_user = [
        {
            "user_id": r["user_id"],
            "prompt_tokens": r["prompt_tokens"],
            "completion_tokens": r["completion_tokens"],
            "cost_usd": _cost(r["prompt_tokens"], r["completion_tokens"]),
            "requests": r["requests"],
        }
        for r in rows
    ]
    total = {
        "prompt_tokens": sum(r["prompt_tokens"] for r in rows),
        "completion_tokens": sum(r["completion_tokens"] for r in rows),
        "cost_usd": round(sum(u["cost_usd"] for u in by_user), 6),
        "requests": sum(r["requests"] for r in rows),
    }
    payload = {
        "window": {"from": from_iso, "to": to_iso},
        "by_user": by_user,
        "total": total,
        "price_per_million": {"prompt": price_prompt, "completion": price_completion},
    }
    _cost_cache[cache_key] = (now_mono + COST_CACHE_TTL, payload)
    return payload


def clear_cost_cache() -> None:
    """清空成本缓存（测试用）。"""
    _cost_cache.clear()
