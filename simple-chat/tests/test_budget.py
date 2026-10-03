"""v0.3 每用户日 token 预算护栏验收测试。

覆盖 ROADMAP v0.3 验收标准：
- 超日上限 → 429 RATE_LIMITED（文案“今日额度已用尽”）
- 次日按 UTC 日期边界自然恢复
- 预算独立于全局限流（BUDGET_ENABLED 关闭时不检查）

通过 conftest 的 db 夹具直连 SQLite 文件，调用 budget.enforce_budget。
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.budget import daily_user_tokens, enforce_budget
from app.config import settings
from app.db import utcnow_iso
from app.errors import RateLimitedError

USER = "alice"


async def _seed(conv_id: str, tokens: int, *, when: datetime, db):
    """落一条 assistant 消息，带指定 tokens 与时间。"""
    now = when.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    await db.execute(
        "INSERT INTO conversation (id, title, user_id, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (conv_id, "t", USER, now, now),
    )
    await db.execute(
        "INSERT INTO message (conversation_id, role, content, tokens, created_at)"
        " VALUES (?, 'assistant', ?, ?, ?)",
        (conv_id, "x", tokens, now),
    )
    await db.commit()


async def test_under_limit_passes(db, monkeypatch):
    monkeypatch.setattr(settings, "budget_enabled", True)
    monkeypatch.setattr(settings, "budget_daily_tokens_per_user", 1000)
    await _seed("c1", 100, when=datetime.now(timezone.utc), db=db)
    # 不应抛
    await enforce_budget(db, USER)
    assert await daily_user_tokens(db, USER) == 100


async def test_over_limit_raises_429(db, monkeypatch):
    monkeypatch.setattr(settings, "budget_enabled", True)
    monkeypatch.setattr(settings, "budget_daily_tokens_per_user", 100)
    await _seed("c1", 100, when=datetime.now(timezone.utc), db=db)
    with pytest.raises(RateLimitedError) as ei:
        await enforce_budget(db, USER)
    assert ei.value.status_code == 429
    assert "今日额度已用尽" in ei.value.message


async def test_next_day_resets(db, monkeypatch):
    """次日按 UTC 日期边界自然恢复：昨日 token 不计入今日预算。"""
    monkeypatch.setattr(settings, "budget_enabled", True)
    monkeypatch.setattr(settings, "budget_daily_tokens_per_user", 100)
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    await _seed("c-yesterday", 200, when=yesterday, db=db)
    # 昨日超额，但今日预算独立 → 通过
    await enforce_budget(db, USER)
    # daily_user_tokens 只统计今日
    assert await daily_user_tokens(db, USER) == 0


async def test_budget_disabled_no_check(db, monkeypatch):
    """BUDGET_ENABLED=false 时不检查，即便已超额。"""
    monkeypatch.setattr(settings, "budget_enabled", False)
    monkeypatch.setattr(settings, "budget_daily_tokens_per_user", 1)
    await _seed("c1", 999, when=datetime.now(timezone.utc), db=db)
    # 不应抛
    await enforce_budget(db, USER)


async def test_zero_limit_means_unlimited(db, monkeypatch):
    """上限为 0 即便开启也不限制（保留接入位）。"""
    monkeypatch.setattr(settings, "budget_enabled", True)
    monkeypatch.setattr(settings, "budget_daily_tokens_per_user", 0)
    await _seed("c1", 999, when=datetime.now(timezone.utc), db=db)
    await enforce_budget(db, USER)


async def test_budget_isolated_per_user(db, monkeypatch):
    """预算按用户隔离：alice 超额不影响 bob。"""
    monkeypatch.setattr(settings, "budget_enabled", True)
    monkeypatch.setattr(settings, "budget_daily_tokens_per_user", 100)
    now = datetime.now(timezone.utc)
    iso = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    # alice 超额
    await db.execute(
        "INSERT INTO conversation (id, title, user_id, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        ("c-a", "t", USER, iso, iso),
    )
    await db.execute(
        "INSERT INTO message (conversation_id, role, content, tokens, created_at)"
        " VALUES (?, 'assistant', ?, ?, ?)",
        ("c-a", "x", 100, iso),
    )
    await db.commit()
    with pytest.raises(RateLimitedError):
        await enforce_budget(db, USER)
    # bob 无任何记录 → 通过
    await enforce_budget(db, "bob")
    assert await daily_user_tokens(db, "bob") == 0


async def test_budget_independent_of_global_rate_limit(db, monkeypatch):
    """预算检查与全局限流互不影响：限流关闭时预算仍生效。"""
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    monkeypatch.setattr(settings, "budget_enabled", True)
    monkeypatch.setattr(settings, "budget_daily_tokens_per_user", 1)
    await _seed("c1", 10, when=datetime.now(timezone.utc), db=db)
    with pytest.raises(RateLimitedError):
        await enforce_budget(db, USER)
