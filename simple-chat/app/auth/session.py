"""签名会话 Cookie：HMAC-SHA256 无状态令牌（不建 session 表）。

令牌结构（均为 base64url、去 padding）：
    <payload_b64>.<sig_b64>
    payload = {"uid": <user_id>, "exp": <unix 秒>}
    sig     = HMAC-SHA256(secret, payload_b64)

安全性质：
- 密钥取自 SESSION_SECRET；生产且认证开启时由 config 强制非空且 >= 32 字符。
- 开发环境允许 SESSION_SECRET 留空：此时用进程内随机密钥（每次重启失效），
  只服务本地调试，不会把可预测的默认密钥带进生产。
- 轮换 SESSION_SECRET 即让全部旧 Cookie 立即失效。
- 校验为常量时间比较，且先验签再解析 JSON。
- Cookie 属性：HttpOnly; SameSite=Strict; Path=/;（生产）Secure；
  登录成功用 Max-Age 下发，登出以 Max-Age=0 删除。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from fastapi import Response

from app.config import settings

SESSION_COOKIE = settings.session_cookie_name

# 开发环境（SESSION_SECRET 为空）下的进程内随机密钥缓存。
# _dev_secret_cache: dict[str, str]
_dev_secret_cache: dict[str, str] = {}


def _resolve_secret(secret: str) -> str:
    """返回实际用于签名的密钥；空密钥时按进程惰性生成随机密钥。"""
    if secret:
        return secret
    cached = _dev_secret_cache.get("")
    if cached is None:
        cached = secrets.token_urlsafe(32)
        _dev_secret_cache[""] = cached
    return cached


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(payload_b64: str, secret: str) -> str:
    digest = hmac.new(
        _resolve_secret(secret).encode("utf-8"),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return _b64e(digest)


def create_session_value(
    user_id: str,
    *,
    secret: str | None = None,
    ttl_seconds: int | None = None,
    now: int | None = None,
) -> str:
    """生成签名会话令牌；ttl_seconds<=0 时立即过期（用于测试）。"""
    secret = settings.session_secret if secret is None else secret
    ttl = settings.session_ttl_seconds if ttl_seconds is None else ttl_seconds
    issued = int(time.time()) if now is None else int(now)
    payload = json.dumps(
        {"uid": user_id, "exp": issued + int(ttl)},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    payload_b64 = _b64e(payload.encode("utf-8"))
    return f"{payload_b64}.{_sign(payload_b64, secret)}"


def verify_session_value(
    value: str | None,
    *,
    secret: str | None = None,
    now: int | None = None,
) -> str | None:
    """校验令牌；合法且未过期返回 user_id，否则返回 None（任何异常都视为无效）。"""
    if not value:
        return None
    secret = settings.session_secret if secret is None else secret
    parts = value.split(".")
    if len(parts) != 2:
        return None
    payload_b64, sig = parts
    if not payload_b64 or not sig:
        return None
    expected = _sign(payload_b64, secret)
    # 常量时间验签，且必须在解析 JSON 之前
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        payload = json.loads(_b64d(payload_b64).decode("utf-8"))
        uid = payload.get("uid")
        exp = int(payload.get("exp"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(uid, str) or not uid:
        return None
    current = int(time.time()) if now is None else int(now)
    if exp <= current:
        return None
    return uid


def set_session_cookie(
    response: Response,
    value: str,
    *,
    max_age: int | None = None,
) -> None:
    """写入会话 Cookie（HttpOnly; SameSite=Strict；生产 Secure）。"""
    response.set_cookie(
        key=SESSION_COOKIE,
        value=value,
        max_age=settings.session_ttl_seconds if max_age is None else max_age,
        httponly=True,
        samesite="strict",
        secure=settings.effective_cookie_secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """登出：以 max_age=0 删除 Cookie（属性与写入时一致）。"""
    response.delete_cookie(
        key=SESSION_COOKIE,
        httponly=True,
        samesite="strict",
        secure=settings.effective_cookie_secure,
        path="/",
    )


__all__ = [
    "SESSION_COOKIE",
    "create_session_value",
    "verify_session_value",
    "set_session_cookie",
    "clear_session_cookie",
]
