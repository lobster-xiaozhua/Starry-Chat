"""认证路由：注册 / 登录 / 登出 / 当前用户。

- POST /api/auth/register  创建本地账号并直接下发会话 Cookie
- POST /api/auth/login     校验口令并下发会话 Cookie（统一失败文案）
- POST /api/auth/logout    删除 Cookie（幂等，无需已登录）
- GET  /api/auth/me        返回当前 Cookie 对应的用户（401 表示未登录）

安全边界都由本文件与 app.auth.* 的服务端逻辑承担：
- 密码只存 scrypt 哈希（app/auth/service.py），响应体绝不含 password_hash。
- 登录/注册失败走独立限流桶（app/auth/ratelimit.py），且失败文案统一。
- Cookie 由 app/auth/session.py 统一设置 HttpOnly/SameSite=Strict/Secure。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response

from app.auth import ratelimit, service as auth_service
from app.auth.session import (
    clear_session_cookie,
    create_session_value,
    set_session_cookie,
)
from app.deps import DbDep, UserIdDep
from app.errors import UnauthorizedError
from app.middleware.rate_limit import get_client_ip
from app.schema import AuthCredentials, AuthUserOut, MeOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

LOGIN_BUCKET = "login"
REGISTER_BUCKET = "register"


def _audit(request: Request, event: str, user_id: str | None) -> None:
    """认证审计日志：记录事件与用户 id，绝不记录用户名/密码/请求体。"""
    logger.info(
        event,
        extra={
            "event": event,
            "user_id": user_id,
            "request_id": getattr(request.state, "request_id", None),
            "path": request.url.path,
            "method": request.method,
        },
    )


@router.post("/register", response_model=AuthUserOut, summary="注册本地账号")
async def register(
    body: AuthCredentials,
    conn: DbDep,
    request: Request,
    response: Response,
):
    ip = get_client_ip(request)
    ratelimit.check_allowed(REGISTER_BUCKET, ip, body.username)
    try:
        user = await auth_service.create_user(conn, body.username, body.password)
    except Exception:
        # 注册失败（重名/校验不通过）计入注册桶，防止脚本批量试探用户名
        ratelimit.record_failure(REGISTER_BUCKET, ip, body.username)
        raise
    set_session_cookie(response, create_session_value(user["id"]))
    _audit(request, "auth_register", user["id"])
    return {"user": auth_service.public_user(user)}


@router.post("/login", response_model=AuthUserOut, summary="登录并下发会话 Cookie")
async def login(
    body: AuthCredentials,
    conn: DbDep,
    request: Request,
    response: Response,
):
    ip = get_client_ip(request)
    ratelimit.check_allowed(LOGIN_BUCKET, ip, body.username)
    user = await auth_service.authenticate(conn, body.username, body.password)
    if user is None:
        ratelimit.record_failure(LOGIN_BUCKET, ip, body.username)
        _audit(request, "auth_login_failed", None)
        # 统一文案：不区分“用户不存在”与“密码错误”
        raise UnauthorizedError(auth_service.LOGIN_FAILED_MESSAGE)
    ratelimit.reset(LOGIN_BUCKET, ip, body.username)
    set_session_cookie(response, create_session_value(user["id"]))
    _audit(request, "auth_login", user["id"])
    return {"user": auth_service.public_user(user)}


@router.post("/logout", status_code=204, summary="登出（删除会话 Cookie）")
async def logout(request: Request, response: Response):
    # 幂等：无 Cookie 也返回 204（不泄露是否处于登录态）
    clear_session_cookie(response)
    _audit(request, "auth_logout", None)
    return None


@router.get("/me", response_model=MeOut, summary="当前登录用户")
async def me(request: Request, conn: DbDep, user_id: UserIdDep):
    user = await auth_service.get_user_by_id(conn, user_id)
    if user is None:
        # Cookie 签名有效但用户已被删除 → 视为未登录
        raise UnauthorizedError()
    _audit(request, "auth_me", user_id)
    return {"user": auth_service.public_user(user)}
