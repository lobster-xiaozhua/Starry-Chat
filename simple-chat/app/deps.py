"""依赖注入：请求级 DB 连接、当前用户身份（认证 Cookie 或兼容 header）。

身份解析顺序（v0.2）：
1. 认证开启时：优先校验签名会话 Cookie；有效 → 返回 user_id；
   无效/缺失 → 抛 401 UNAUTHORIZED（不静默回退到 anonymous/header）。
2. 认证关闭时：保留 v0.1 兼容路径，从 X-User-Id 头读取，缺失回退 "anonymous"。
"""

from typing import Annotated, AsyncIterator

import aiosqlite
from fastapi import Depends, Header, Request

from app.auth.session import SESSION_COOKIE, verify_session_value
from app.config import settings
from app.db import get_db as _get_db
from app.errors import UnauthorizedError

ANONYMOUS_USER_ID = "anonymous"


async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    """请求级数据库连接；yield 后在请求结束时关闭。"""
    async with _get_db() as conn:
        yield conn


DbDep = Annotated[aiosqlite.Connection, Depends(get_db)]


def resolve_user_id(
    cookie_value: str | None,
    x_user_id: str | None,
    *,
    auth_enabled: bool | None = None,
) -> str:
    """纯函数版身份解析，便于单测；语义见模块 docstring。"""
    enabled = settings.effective_auth_enabled if auth_enabled is None else auth_enabled
    if enabled:
        uid = verify_session_value(cookie_value)
        if uid is None:
            raise UnauthorizedError()
        return uid
    legacy = (x_user_id or "").strip()
    return legacy or ANONYMOUS_USER_ID


def get_current_user_id(
    request: Request,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> str:
    """FastAPI 依赖：解析当前用户；认证开启且无有效 Cookie 时抛 401。"""
    return resolve_user_id(
        request.cookies.get(SESSION_COOKIE),
        x_user_id,
    )


UserIdDep = Annotated[str, Depends(get_current_user_id)]


__all__ = [
    "get_db",
    "DbDep",
    "get_current_user_id",
    "UserIdDep",
    "resolve_user_id",
    "ANONYMOUS_USER_ID",
]
