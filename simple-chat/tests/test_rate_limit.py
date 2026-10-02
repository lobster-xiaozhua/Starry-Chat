"""限流中间件测试：60s 窗口 / 30 次上限 / 429 响应头；健康检查不受限。

说明：本测试通过 monkeypatch settings.rate_limit_enabled=True 打开限流；生产默认即开。
内存实现为单实例，key = user_id:ip。
"""

import pytest

from app.config import settings


# 用 X-Forwarded-For 模拟外部 IP，避免命中 127.0.0.1 白名单（开发本机调试才豁免）。
_FORWARDED_IP = "203.0.113.7"


async def _post(client, user=None, ip=_FORWARDED_IP):
    headers = {"X-Forwarded-For": ip}
    if user:
        headers["X-User-Id"] = user
    return await client.post(
        "/api/chat",
        json={"message": "ping"},
        headers=headers,
    )


async def test_rate_limit_returns_429_after_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)

    limited = False
    headers_seen = {}
    for i in range(35):
        res = await _post(client)
        if res.status_code == 429:
            limited = True
            assert res.headers.get("Retry-After") == "60"
            assert res.headers.get("X-RateLimit-Limit") == "30"
            assert res.headers.get("X-RateLimit-Remaining") == "0"
            err = res.json()["error"]
            assert err["code"] == "RATE_LIMITED"
            break
        # 成功响应应带剩余配额响应头
        headers_seen = res.headers
    else:
        pytest.fail("未触发限流（期望 30 次后 429）")
    assert limited
    # 成功响应也应包含 X-RateLimit-* 头
    assert headers_seen.get("X-RateLimit-Limit") == "30"
    assert "X-RateLimit-Remaining" in headers_seen


async def test_rate_limit_per_user_isolation(client, monkeypatch):
    """不同 user 各自独立计数：alice 被打满，bob 仍可用。"""
    monkeypatch.setattr(settings, "rate_limit_enabled", True)

    # alice 触发限流
    res = None
    for _ in range(31):
        res = await _post(client, user="alice")
        if res.status_code == 429:
            break
    assert res.status_code == 429

    # bob 同一瞬间仍可用
    res_bob = await _post(client, user="bob")
    assert res_bob.status_code == 200


async def test_health_endpoints_not_rate_limited(client, monkeypatch):
    """健康检查与静态资源不受限流影响。"""
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    for _ in range(35):
        res = await client.get("/healthz")
        assert res.status_code == 200


async def test_rate_limit_disabled_by_default_in_dev(client, monkeypatch):
    """开发环境（APP_ENV=development）默认关闭限流。"""
    monkeypatch.setattr(settings, "rate_limit_enabled", None)
    monkeypatch.setattr(settings, "app_env", "development")
    assert settings.effective_rate_limit is False
    for _ in range(35):
        res = await _post(client)
        assert res.status_code == 200
