"""PR-3 验收测试：/metrics 指标端点与 /api/admin/cost 成本端点。

覆盖验收清单：
- /metrics 返回合法 Prometheus 文本，含全部 10 个指标名，histogram 有
  _bucket/_sum/_count 三件套
- 连续 100 请求后 chat_requests_total 增量 == 100（/metrics 自身不计数）
- 未配置 ADMIN_API_KEY → 501（不是 500 / 403）
- 成本聚合结果与手工 SELECT SUM 一致（误差 0）
- 首 token 直方图在对话后被观测到
"""

from datetime import datetime, timedelta, timezone

import pytest

from app import metrics
from app.admin import clear_cost_cache
from app.config import settings
from app.db import utcnow_iso


REQUIRED_METRICS = [
    "chat_requests_total",
    "chat_first_token_seconds",
    "chat_duration_seconds",
    "chat_tokens_total",
    "chat_active_streams",
    "conversation_messages_total",
    "llm_retries_total",
    "db_query_seconds",
    "locks_contended_total",
    "context_truncated_total",
]


# ───────────────────────── /metrics 端点 ─────────────────────────


async def test_metrics_format_and_names(client):
    res = await client.post("/api/chat", json={"message": "hi", "stream": False})
    assert res.status_code == 200

    res = await client.get("/metrics")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain")
    assert "version=0.0.4" in res.headers["content-type"]
    body = res.text
    for name in REQUIRED_METRICS:
        assert name in body, f"missing metric {name}"
        assert f"# TYPE {name} " in body
        assert f"# HELP {name} " in body

    # histogram 三件套
    assert 'chat_first_token_seconds_bucket{model="sensenova-6.8-flash-lite",le="+Inf"}' in body
    assert "chat_first_token_seconds_sum{" in body
    assert "chat_first_token_seconds_count{" in body
    assert "db_query_seconds_bucket{op=" in body


async def test_metrics_endpoint_not_counted(client):
    """GET /metrics 本身不得改变 chat_requests_total（避免自增风暴）。"""
    metrics.reset()
    res = await client.post("/api/chat", json={"message": "hi", "stream": False})
    assert res.status_code == 200
    before = sum(metrics.chat_requests_total.values.values())
    assert before == 1

    for _ in range(5):
        await client.get("/metrics")

    after = sum(metrics.chat_requests_total.values.values())
    assert after == before


async def test_chat_requests_total_delta_100(client):
    """连续 100 请求 → chat_requests_total 增量 == 100。"""
    metrics.reset()
    for _ in range(100):
        res = await client.post(
            "/api/chat", json={"message": "hi", "stream": False}
        )
        assert res.status_code == 200

    total = sum(metrics.chat_requests_total.values.values())
    assert total == 100
    # 成功请求的标签：error_code 为空、status 200
    assert metrics.chat_requests_total.get(
        model="sensenova-6.8-flash-lite", error_code="", status="200"
    ) == 100


async def test_first_token_and_duration_observed(client):
    """一次流式对话后，首 token 直方图与耗时直方图都必须有观测值。"""
    metrics.reset()
    res = await client.post("/api/chat", json={"message": "hi"})  # stream 默认 True
    assert res.status_code == 200
    assert res.headers["x-accel-buffering"] == "no"

    key = ("sensenova-6.8-flash-lite",)
    ft = metrics.chat_first_token_seconds.data.get(key)
    assert ft is not None and ft["count"] == 1
    assert ft["sum"] >= 0.0
    assert metrics.chat_duration_seconds.data[key]["count"] == 1
    # token 计数：prompt + completion 各一条
    assert metrics.chat_tokens_total.get(
        model="sensenova-6.8-flash-lite", role="completion"
    ) > 0


async def test_409_counted_and_locks_contended(client, db, sample_conversation, monkeypatch):
    """同会话并发第二个请求：locks_contended_total 与 chat_requests_total(409)。"""
    import asyncio

    from tests.conftest import make_slow_fake
    from app.llm import client as llm_client

    monkeypatch.setattr(llm_client, "_client", make_slow_fake(delay=0.5))
    metrics.reset()

    async def _send():
        return await client.post(
            "/api/chat", json={"message": "hi", "conversation_id": "sample-conv-1"}
        )

    first, second = await asyncio.gather(_send(), _send())
    codes = {first.status_code, second.status_code}
    assert codes == {200, 409}
    assert metrics.locks_contended_total.get() == 1
    assert metrics.chat_requests_total.get(
        model="sensenova-6.8-flash-lite", error_code="CONVERSATION_BUSY", status="409"
    ) == 1


# ───────────────────────── /api/admin/cost ─────────────────────────


def _iso(days_ago: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


async def _seed(db, user_id: str, conv_id: str, rows: list[tuple[str, int]]):
    """rows: [(role, tokens)]，创建时间固定在 1 天前（确保落进默认 7 天窗口）。"""
    ts = _iso(1)
    await db.execute(
        "INSERT INTO conversation (id, title, user_id, created_at, updated_at)"
        " VALUES (?, 't', ?, ?, ?)",
        (conv_id, user_id, ts, ts),
    )
    for role, tokens in rows:
        await db.execute(
            "INSERT INTO message (conversation_id, role, content, tokens, created_at)"
            " VALUES (?, ?, 'c', ?, ?)",
            (conv_id, role, tokens, ts),
        )
    await db.commit()


async def test_admin_cost_missing_key_returns_501(client):
    monkey_key = settings.admin_api_key
    assert monkey_key == ""  # 默认未配置
    res = await client.get("/api/admin/cost")
    assert res.status_code == 501
    assert res.json()["error"]["code"] == "NOT_IMPLEMENTED"


async def test_admin_cost_auth(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "secret-key")
    clear_cost_cache()

    assert (await client.get("/api/admin/cost")).status_code == 401
    assert (
        await client.get("/api/admin/cost", headers={"X-Admin-Key": "wrong"})
    ).status_code == 401
    res = await client.get("/api/admin/cost", headers={"X-Admin-Key": "secret-key"})
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == {"window", "by_user", "total", "price_per_million"}


async def test_admin_cost_window_too_long(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "k")
    clear_cost_cache()
    res = await client.get(
        "/api/admin/cost",
        params={"from": "2020-01-01", "to": "2020-12-31"},
        headers={"X-Admin-Key": "k"},
    )
    assert res.status_code == 422


async def test_admin_cost_aggregation_matches_manual_sum(client, db, monkeypatch):
    """聚合结果与手工算术一致（误差 0），价格按配置计算。"""
    monkeypatch.setattr(settings, "admin_api_key", "k")
    monkeypatch.setattr(
        settings,
        "model_price",
        {"sensenova-6.8-flash-lite": {"prompt": 0.15, "completion": 0.60}},
    )
    clear_cost_cache()

    # alice：2 轮（每轮 user 10 + assistant 30）；bob：1 轮（user 5 + assistant 7）
    await _seed(db, "alice", "cost-a1", [("user", 10), ("assistant", 30)])
    await _seed(db, "alice", "cost-a2", [("user", 10), ("assistant", 30)])
    await _seed(db, "bob", "cost-b1", [("user", 5), ("assistant", 7)])
    # 窗口外的旧消息不得计入（100 天前）
    old = _iso(100)
    await db.execute(
        "INSERT INTO conversation (id, title, user_id, created_at, updated_at)"
        " VALUES ('cost-old', 't', 'alice', ?, ?)",
        (old, old),
    )
    await db.execute(
        "INSERT INTO message (conversation_id, role, content, tokens, created_at)"
        " VALUES ('cost-old', 'assistant', 'c', 99999, ?)",
        (old,),
    )
    await db.commit()

    res = await client.get(
        "/api/admin/cost", headers={"X-Admin-Key": "k"}, params={"user_id": "alice"}
    )
    assert res.status_code == 200
    body = res.json()

    assert body["price_per_million"] == {"prompt": 0.15, "completion": 0.60}
    assert len(body["by_user"]) == 1
    alice = body["by_user"][0]
    # 手工 SUM：prompt=20, completion=60, requests=2
    assert alice["prompt_tokens"] == 20
    assert alice["completion_tokens"] == 60
    assert alice["requests"] == 2
    assert alice["cost_usd"] == round(20 / 1e6 * 0.15 + 60 / 1e6 * 0.60, 6)
    assert body["total"]["prompt_tokens"] == 20
    assert body["total"]["requests"] == 2

    # 不带 user_id：alice + bob 全量
    res = await client.get("/api/admin/cost", headers={"X-Admin-Key": "k"})
    body = res.json()
    assert len(body["by_user"]) == 2
    assert body["total"]["prompt_tokens"] == 20 + 5
    assert body["total"]["completion_tokens"] == 60 + 7
    assert body["total"]["requests"] == 3
    assert body["total"]["cost_usd"] == round(
        25 / 1e6 * 0.15 + 67 / 1e6 * 0.60, 6
    )


async def test_admin_cost_no_price_configured(client, db, monkeypatch):
    """未配置价格 → cost_usd == 0，不报错。"""
    monkeypatch.setattr(settings, "admin_api_key", "k")
    monkeypatch.setattr(settings, "model_price", {})
    clear_cost_cache()
    await _seed(db, "carol", "cost-c1", [("user", 100), ("assistant", 200)])

    res = await client.get(
        "/api/admin/cost", headers={"X-Admin-Key": "k"}, params={"user_id": "carol"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["total"]["cost_usd"] == 0
    assert body["total"]["prompt_tokens"] == 100
    assert body["price_per_million"] == {"prompt": 0.0, "completion": 0.0}
