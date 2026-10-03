"""v0.2 认证验收测试：注册/登录/登出、密码哈希、Cookie 伪造、A/B 隔离、限流。

覆盖 ROADMAP v0.2 验收标准：
- 密码不落明文、响应不含 password_hash
- 伪造/篡改/过期 Cookie → 401
- A/B 会话隔离（B 读/删 A 的会话 → 404）
- 登出后 Cookie 失效
- AUTH_ENABLED=false 回归兼容（X-User-Id 路径保持）
- 登录失败独立限流（第 N+1 次 429）

风格与现有测试一致：client 夹具走 ASGI，db 夹具直连同一 SQLite 文件。
"""

import json

import pytest

from app.auth import ratelimit, service as auth_service
from app.auth.session import (
    SESSION_COOKIE,
    create_session_value,
    verify_session_value,
)
from app.config import settings

USER_A = {"username": "alice", "password": "alice-password-1"}
USER_B = {"username": "bob", "password": "bob-password-2"}


async def _register(client, user):
    return await client.post("/api/auth/register", json=user)


async def _login(client, user):
    return await client.post("/api/auth/login", json=user)


def _cookie(client) -> str:
    return client.cookies.get(SESSION_COOKIE)


async def _send(client, message="hello", conversation_id=None):
    """发一条消息并返回 (status, done_payload 或 error_payload)。"""
    payload = {"message": message, "stream": True}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    async with client.stream("POST", "/api/chat", json=payload) as res:
        raw = ""
        async for chunk in res.aiter_text():
            raw += chunk
    for frame in raw.split("\n\n"):
        if frame.startswith("event: done"):
            for line in frame.split("\n"):
                if line.startswith("data:"):
                    return res.status_code, json.loads(line[5:].strip())
        if frame.startswith("event: error"):
            for line in frame.split("\n"):
                if line.startswith("data:"):
                    return res.status_code, json.loads(line[5:].strip())
    return res.status_code, None


@pytest.fixture
def enable_auth(monkeypatch):
    """打开认证（显式开关，不依赖 app_env 推导）。"""
    monkeypatch.setattr(settings, "auth_enabled", True)


# ───────────────────────── 注册 / 登录 / 登出 ─────────────────────────


async def test_register_login_logout_flow(client, db, enable_auth):
    # 注册：201/200 + Cookie 下发，响应体绝不含 password_hash
    res = await _register(client, USER_A)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["user"]["username"] == "alice"
    assert "password_hash" not in json.dumps(body)
    assert body["user"]["id"]
    assert _cookie(client)

    # Cookie 属性：HttpOnly + SameSite=Strict（Secure 生产才置位，见下方独立用例）
    set_cookie = res.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie.lower() or "samesite=strict" in set_cookie.lower()

    # /me 可用
    me = await client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "alice"

    # 登出：204 + Cookie 清除，之后 /me → 401
    out = await client.post("/api/auth/logout")
    assert out.status_code == 204
    me = await client.get("/api/auth/me")
    assert me.status_code == 401
    assert me.json()["error"]["code"] == "UNAUTHORIZED"

    # 重新登录（同一账号）
    res = await _login(client, USER_A)
    assert res.status_code == 200
    assert _cookie(client)
    # 重复注册 → 409 CONFLICT
    res = await _register(client, USER_A)
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "CONFLICT"


async def test_password_is_scrypt_hashed_not_plaintext(client, db, enable_auth):
    await _register(client, USER_A)
    cur = await db.execute("SELECT username, password_hash FROM user")
    row = await cur.fetchone()
    assert row is not None
    stored = row["password_hash"]
    assert USER_A["password"] not in stored
    assert stored.startswith("scrypt$")
    parts = stored.split("$")
    assert len(parts) == 6 and parts[1] == str(settings.scrypt_n)
    assert auth_service.verify_password(USER_A["password"], stored)
    assert not auth_service.verify_password("wrong-password", stored)


async def test_register_rejects_weak_password_and_bad_username(client, enable_auth):
    res = await _register(client, {"username": "carol", "password": "short"})
    assert res.status_code == 422
    res = await _register(client, {"username": "a", "password": "long-enough-pass"})
    assert res.status_code == 422


async def test_login_failure_is_uniform(client, enable_auth):
    await _register(client, USER_A)
    await client.post("/api/auth/logout")

    wrong_pw = await _login(client, {"username": "alice", "password": "wrong-password"})
    unknown = await _login(client, {"username": "nobody", "password": "whatever-123"})
    assert wrong_pw.status_code == 401
    assert unknown.status_code == 401
    # 统一文案：不能通过响应区分“用户不存在”和“密码错误”
    # （request_id 会随请求变化，故只比较 code/message）
    assert wrong_pw.json()["error"]["code"] == unknown.json()["error"]["code"]
    assert wrong_pw.json()["error"]["message"] == unknown.json()["error"]["message"]


# ───────────────────────── Cookie 伪造 / 篡改 / 过期 ─────────────────────────


async def test_forged_and_tampered_cookie_rejected(client, enable_auth):
    # 完全伪造
    client.cookies.set(SESSION_COOKIE, "not-a-real-token")
    res = await client.get("/api/chat/conversations")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHORIZED"

    # 用测试密钥签发合法令牌后篡改签名
    valid = create_session_value("some-user-id")
    client.cookies.set(SESSION_COOKIE, valid[:-4] + "AAAA")
    assert (await client.get("/api/chat/conversations")).status_code == 401

    # 过期令牌
    expired = create_session_value("some-user-id", ttl_seconds=-1)
    client.cookies.set(SESSION_COOKIE, expired)
    assert (await client.get("/api/chat/conversations")).status_code == 401

    # 无 Cookie → 401（认证开启时不回退 X-User-Id）
    client.cookies.clear()
    res = await client.get(
        "/api/chat/conversations", headers={"X-User-Id": "alice"}
    )
    assert res.status_code == 401


async def test_cookie_signed_with_other_secret_rejected(client, enable_auth):
    forged = create_session_value("alice-id", secret="attacker-secret-0123456789abcdefgh")
    client.cookies.set(SESSION_COOKIE, forged)
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_deleted_user_session_rejected(client, db, enable_auth):
    await _register(client, USER_A)
    assert (await client.get("/api/auth/me")).status_code == 200
    await db.execute("DELETE FROM user")
    await db.commit()
    res = await client.get("/api/auth/me")
    assert res.status_code == 401


async def test_logout_invalidates_cookie(client, enable_auth):
    await _register(client, USER_A)
    assert (await client.get("/api/chat/conversations")).status_code == 200
    await client.post("/api/auth/logout")
    res = await client.get("/api/chat/conversations")
    assert res.status_code == 401


def test_session_value_helper_roundtrip():
    value = create_session_value("u-1")
    assert verify_session_value(value) == "u-1"
    assert verify_session_value(value, secret="another-secret") is None
    assert verify_session_value(None) is None
    assert verify_session_value("") is None
    assert verify_session_value("a.b.c") is None


# ───────────────────────── A/B 会话隔离 ─────────────────────────


async def test_ab_conversation_isolation(client, db, enable_auth):
    import httpx

    # 注册时用独立的 cookie jar，避免覆盖 client 自身会话；
    # 复用同一个 ASGITransport（同一 app、同一数据库）。
    transport = client._transport
    jar_a = httpx.AsyncClient(transport=transport, base_url="http://test")
    jar_b = httpx.AsyncClient(transport=transport, base_url="http://test")
    try:
        assert (await _register(jar_a, USER_A)).status_code == 200
        assert (await _register(jar_b, USER_B)).status_code == 200

        # A 发一条消息 → 得到 A 的会话
        status, done = await _send(jar_a, "A 的私密消息")
        assert status == 200 and done and done["conversation_id"]
        conv_a = done["conversation_id"]

        # A 能看到自己的会话
        assert (await jar_a.get(f"/api/chat/conversations/{conv_a}/messages")).status_code == 200

        # B 读取 A 的会话 → 404（不泄露“存在但非本人”）
        res = await jar_b.get(f"/api/chat/conversations/{conv_a}/messages")
        assert res.status_code == 404
        # B 删除 A 的会话 → 404
        res = await jar_b.request("DELETE", f"/api/chat/conversations/{conv_a}")
        assert res.status_code == 404
        # B 的列表为空，A 的列表有 1 条
        assert (await jar_b.get("/api/chat/conversations")).json()["conversations"] == []
        assert len((await jar_a.get("/api/chat/conversations")).json()["conversations"]) == 1

        # 会话归属列为 A 的用户 id（来自 Cookie，而非 header）
        cur = await db.execute(
            "SELECT user_id FROM conversation WHERE id = ?", (conv_a,)
        )
        row = await cur.fetchone()
        cur = await db.execute("SELECT id FROM user WHERE username = 'alice'")
        alice = await cur.fetchone()
        assert row["user_id"] == alice["id"]
    finally:
        await jar_a.aclose()
        await jar_b.aclose()


async def test_chat_requires_login_when_auth_enabled(client, enable_auth):
    res = await client.post("/api/chat", json={"message": "hi"})
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHORIZED"


# ───────────────────────── 登录限流 ─────────────────────────


async def test_login_rate_limited_after_failures(client, enable_auth):
    await _register(client, USER_A)
    await client.post("/api/auth/logout")

    for _ in range(settings.auth_rate_limit_max):
        res = await _login(client, {"username": "alice", "password": "wrong-password"})
        assert res.status_code == 401

    # 第 N+1 次：独立登录桶拦截（429），即使密码正确也被拦截
    res = await _login(client, {"username": "alice", "password": "wrong-password"})
    assert res.status_code == 429
    assert res.json()["error"]["code"] == "RATE_LIMITED"
    res = await _login(client, USER_A)
    assert res.status_code == 429

    # 另一个用户名不受影响（按 IP+用户名分桶）
    res = await _login(client, {"username": "someone-else", "password": "whatever-123"})
    assert res.status_code == 401


async def test_successful_login_resets_failure_bucket(client, enable_auth, monkeypatch):
    monkeypatch.setattr(settings, "auth_rate_limit_max", 3)
    await _register(client, USER_A)
    await client.post("/api/auth/logout")

    for _ in range(2):
        assert (await _login(client, {"username": "alice", "password": "nope-nope"})).status_code == 401
    # 成功登录清零该桶
    assert (await _login(client, USER_A)).status_code == 200
    await client.post("/api/auth/logout")
    for _ in range(2):
        assert (await _login(client, {"username": "alice", "password": "nope-nope"})).status_code == 401


async def test_register_rate_limited_on_repeated_conflicts(client, enable_auth, monkeypatch):
    monkeypatch.setattr(settings, "auth_rate_limit_max", 3)
    await _register(client, USER_A)
    for _ in range(3):
        res = await _register(client, USER_A)
        assert res.status_code == 409
    res = await _register(client, USER_A)
    assert res.status_code == 429


async def test_rate_limit_bucket_isolated_from_global(client, enable_auth, monkeypatch):
    """登录限流与全局限流互不影响：清空登录桶后仍可正常登录。"""
    await _register(client, USER_A)
    await client.post("/api/auth/logout")
    for _ in range(settings.auth_rate_limit_max):
        await _login(client, {"username": "alice", "password": "bad-password"})
    assert (await _login(client, USER_A)).status_code == 429
    ratelimit.clear()
    assert (await _login(client, USER_A)).status_code == 200


# ───────────────────────── AUTH_ENABLED=false 回归兼容 ─────────────────────────


async def test_auth_disabled_keeps_x_user_id_path(client, monkeypatch):
    """关闭认证：保留 v0.1 的 X-User-Id 兼容路径，无需 Cookie。"""
    monkeypatch.setattr(settings, "auth_enabled", False)
    async with client.stream(
        "POST", "/api/chat", json={"message": "hi"}, headers={"X-User-Id": "legacy"}
    ) as res:
        raw = ""
        async for chunk in res.aiter_text():
            raw += chunk
    assert res.status_code == 200
    assert "event: done" in raw

    res = await client.get("/api/chat/conversations", headers={"X-User-Id": "legacy"})
    assert res.status_code == 200
    assert len(res.json()["conversations"]) == 1
    # 另一个 legacy 用户看不到
    res = await client.get("/api/chat/conversations", headers={"X-User-Id": "other"})
    assert res.json()["conversations"] == []
    # 缺少 header 时回退 anonymous（不 401）
    res = await client.get("/api/chat/conversations")
    assert res.status_code == 200


async def test_auth_disabled_ignores_auth_endpoints_gracefully(client, monkeypatch):
    """关闭认证时认证端点仍可用（注册不写 Cookie 也可登录），但 chat 不校验 Cookie。"""
    monkeypatch.setattr(settings, "auth_enabled", False)
    res = await _register(client, USER_A)
    assert res.status_code == 200
    res = await client.post("/api/chat", json={"message": "hi"}, headers={"X-User-Id": "x"})
    assert res.status_code == 200
