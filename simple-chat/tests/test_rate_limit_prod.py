"""T5（PR-2 改动 4）：生产环境 localhost 不再豁免限流 → 第 31 次必须 429。

WHITELIST（loopback）仅在 development 生效；生产环境一律按 IP 限流。
本测试以 APP_ENV=production 启动，从 localhost（ASGITransport 的 127.0.0.1）
连续发 31 个请求，断言第 31 个返回 429。
"""

from app.config import settings


async def test_rate_limit_whitelist_only_in_dev(client, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "rate_limit_enabled", None)  # 生产默认开
    assert settings.effective_rate_limit is True

    statuses = []
    for _ in range(31):
        res = await client.post("/api/chat", json={"message": "ping"})
        statuses.append(res.status_code)

    assert statuses[-1] == 429, statuses
    assert all(s == 200 for s in statuses[:-1]), statuses
    body = (await client.post("/api/chat", json={"message": "ping"})).json()
    assert body["error"]["code"] == "RATE_LIMITED"


async def test_dev_still_whitelists_loopback(client, monkeypatch):
    """反例：开发环境 loopback 豁免保持不变（超过 30 次仍 200）。"""
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "trusted_proxies", [])  # 不信 XFF → 直连 127.0.0.1

    for _ in range(35):
        res = await client.post("/api/chat", json={"message": "ping"})
        assert res.status_code == 200
