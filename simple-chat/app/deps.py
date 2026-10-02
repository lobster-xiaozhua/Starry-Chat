"""依赖注入：请求级 DB 连接、临时用户标识。

设计要点：
- get_db 为异步生成器依赖：每请求一条连接，FastAPI 在请求结束后自动关闭。
- get_current_user_id 是临时方案，仅从请求头 X-User-Id 读取；后续替换为 JWT。
- LLM 调用已收敛为 app.llm.client 的模块级函数（chat_stream / chat），
  不再需要 LLM 客户端依赖注入。
"""

from typing import Annotated, AsyncIterator

import aiosqlite
from fastapi import Depends, Header, Request

from app.db import get_db as _get_db


async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    """请求级数据库连接；yield 后在请求结束时关闭。"""
    async with _get_db() as conn:
        yield conn


DbDep = Annotated[aiosqlite.Connection, Depends(get_db)]


def get_current_user_id(
    request: Request,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> str:
    """临时方案：从 X-User-Id 头读取用户标识，缺失返回 anonymous；后续替换 JWT。"""
    uid = (x_user_id or "").strip()
    return uid or "anonymous"


UserIdDep = Annotated[str, Depends(get_current_user_id)]


__all__ = [
    "get_db",
    "DbDep",
    "get_current_user_id",
    "UserIdDep",
]
