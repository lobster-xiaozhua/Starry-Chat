"""认证业务逻辑：注册、口令校验、用户查询；不碰 HTTP，不读 request。

约束：
- 密码只进 scrypt 慢哈希（app.auth.passwords），任何路径都不落明文、不写日志。
- 用户不存在与密码错误对外表现一致；未知用户也做一次等价 scrypt 计算，
  避免通过响应时间枚举账号。
- 所有 SQL 参数化；用户名统一小写规范化。
"""

from __future__ import annotations

import logging
import re
import sqlite3
import uuid
from functools import lru_cache

import aiosqlite

from app.auth.passwords import hash_password, verify_password
from app.config import settings
from app.db import run_write, utcnow_iso
from app.errors import ConflictError, ValidationError

logger = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")
PASSWORD_MIN_LEN = 8
PASSWORD_MAX_LEN = 128

# 登录失败对外统一文案（不区分“用户不存在”与“密码错误”）
LOGIN_FAILED_MESSAGE = "用户名或密码错误"


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def validate_credentials(username: str, password: str) -> tuple[str, str]:
    """校验并规范化注册凭据；不合法抛 422。"""
    name = normalize_username(username)
    if not USERNAME_RE.match(name):
        raise ValidationError(
            "用户名需为 3-32 位小写字母、数字、下划线、点或短横线，且以字母或数字开头"
        )
    if len(password or "") < PASSWORD_MIN_LEN:
        raise ValidationError(f"密码长度至少 {PASSWORD_MIN_LEN} 位")
    if len(password) > PASSWORD_MAX_LEN:
        # 上限同时是 DoS 护栏：scrypt 成本随输入长度增长
        raise ValidationError(f"密码长度不能超过 {PASSWORD_MAX_LEN} 位")
    return name, password


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """未知用户时用于等价计算的占位哈希（惰性生成一次）。"""
    return hash_password("dummy-password-for-timing-equalization")


async def create_user(
    conn: aiosqlite.Connection, username: str, password: str
) -> dict:
    """注册新用户；用户名重复抛 409。返回 {"id","username","created_at"}。"""
    name, password = validate_credentials(username, password)
    user_id = uuid.uuid4().hex
    now = utcnow_iso()
    try:
        await run_write(
            conn,
            [(
                "INSERT INTO user (id, username, password_hash, created_at)"
                " VALUES (?, ?, ?, ?)",
                (user_id, name, hash_password(password), now),
            )],
        )
    except sqlite3.IntegrityError:
        # UNIQUE(username) 冲突（含并发注册竞态）
        raise ConflictError("用户名已被占用") from None
    logger.info(
        "auth_register",
        extra={"event": "auth_register", "user_id": user_id},
    )
    return {"id": user_id, "username": name, "created_at": now}


async def authenticate(
    conn: aiosqlite.Connection, username: str, password: str
) -> dict | None:
    """校验用户名与密码；成功返回用户行（含 password_hash），失败返回 None。

    无论用户是否存在都执行一次 scrypt，失败路径耗时基本一致。
    """
    name = normalize_username(username)
    cur = await conn.execute(
        "SELECT id, username, password_hash, created_at FROM user WHERE username = ?",
        (name,),
    )
    row = await cur.fetchone()
    if row is None:
        verify_password(password or "", _dummy_hash())  # 时间均衡，忽略结果
        logger.info(
            "auth_login_failed",
            extra={"event": "auth_login_failed", "user_id": None},
        )
        return None
    user = dict(row)
    if not verify_password(password or "", user["password_hash"]):
        logger.info(
            "auth_login_failed",
            extra={"event": "auth_login_failed", "user_id": user["id"]},
        )
        return None
    return user


async def get_user_by_id(conn: aiosqlite.Connection, user_id: str) -> dict | None:
    """按 id 查用户；会话 Cookie 指向已删除用户时返回 None。"""
    cur = await conn.execute(
        "SELECT id, username, created_at FROM user WHERE id = ?", (user_id,)
    )
    row = await cur.fetchone()
    return dict(row) if row else None


def public_user(user: dict) -> dict:
    """对外用户视图：绝不包含 password_hash。"""
    return {
        "id": user["id"],
        "username": user["username"],
        "created_at": user.get("created_at"),
    }


__all__ = [
    "create_user",
    "authenticate",
    "get_user_by_id",
    "public_user",
    "normalize_username",
    "validate_credentials",
    "LOGIN_FAILED_MESSAGE",
]
