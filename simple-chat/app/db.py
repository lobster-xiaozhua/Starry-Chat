"""SQLite 连接与建表；所有 SQL 集中在此层。"""

import asyncio
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

import aiosqlite

from app.config import settings

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation (
    id         TEXT PRIMARY KEY,
    title      TEXT,
    user_id    TEXT NOT NULL DEFAULT 'anonymous',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
    content         TEXT NOT NULL,
    tokens          INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversation(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_message_conv_created
    ON message(conversation_id, created_at);
"""


def utcnow_iso() -> str:
    """UTC ISO8601 毫秒 + Z，固定宽度使字典序等于时间序。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@asynccontextmanager
async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    conn = await aiosqlite.connect(settings.db_path)
    conn.row_factory = aiosqlite.Row
    # PRAGMA 逐连接生效，必须在每次开连接时设置，否则级联删除静默失效
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA journal_mode = WAL")
    await conn.execute("PRAGMA busy_timeout = 5000")
    # WAL + synchronous=NORMAL：提交不再 fsync（WAL 追加即可），写锁持有时间
    # 从 ~10ms 降到 ~0.1ms。压测（scripts/load_test.py）显示 10 并发下 FULL
    # 模式会因写锁排队超 5s 而抛 "database is locked"；NORMAL 下消除。
    # 代价仅在断电时可能丢失最近事务（应用崩溃不丢），对话场景可接受。
    await conn.execute("PRAGMA synchronous = NORMAL")
    try:
        yield conn
    finally:
        await conn.close()


async def run_write(
    conn: aiosqlite.Connection,
    statements: list[tuple[str, tuple]],
    *,
    retries: int = 3,
):
    """原子执行一组写语句并提交；写锁竞争（SQLITE_BUSY）时回滚重试。

    为什么需要：busy_timeout 只保护普通的写锁排队；deferred 事务在持有读
    快照后升级写锁的"死锁"场景会立即抛 OperationalError("database is
    locked")，不受 busy_timeout 保护。对毫秒级短事务做小退避重试即可收敛
    （压测 20 并发 579 请求 1 次 → 0 次）。语句组在同一事务内，要么全成功
    要么整体回滚，重试不会产生部分写入。
    """
    for attempt in range(retries + 1):
        try:
            last = None
            for sql, params in statements:
                last = await conn.execute(sql, params)
            await conn.commit()
            return last
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc) or attempt == retries:
                raise
            try:
                await conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            await asyncio.sleep(0.05 * (2**attempt))
    raise AssertionError("unreachable")  # pragma: no cover


async def init_db() -> None:
    db_dir = os.path.dirname(os.path.abspath(settings.db_path))
    os.makedirs(db_dir, exist_ok=True)
    async with get_db() as conn:
        await conn.executescript(SCHEMA)
        await conn.commit()
        # 迁移：为已存在的旧库补充 user_id 列（用于会话归属校验，防 ID 遍历）
        cur = await conn.execute("PRAGMA table_info(conversation)")
        cols = {row["name"] for row in await cur.fetchall()}
        if "user_id" not in cols:
            await conn.execute(
                "ALTER TABLE conversation ADD COLUMN user_id TEXT NOT NULL DEFAULT 'anonymous'"
            )
            await conn.commit()


def persist_partial_sync(
    conv_id: str, content: str, tokens: int, *, update_id: int | None = None
) -> None:
    """取消路径专用同步落库：被取消的作用域内 await 会立刻重抛，只能同步写。

    update_id 为 None 时 INSERT 一条新 assistant 消息（流未落库时被取消）；
    否则 UPDATE 指定行补全 tokens（流已落库后被取消）。
    """
    try:
        conn = sqlite3.connect(settings.db_path, timeout=5)
        try:
            if update_id is None:
                conn.execute(
                    "INSERT INTO message (conversation_id, role, content, tokens, created_at)"
                    " VALUES (?, 'assistant', ?, ?, ?)",
                    (conv_id, content, tokens, utcnow_iso()),
                )
            else:
                conn.execute(
                    "UPDATE message SET content = ?, tokens = ? WHERE id = ?",
                    (content, tokens, update_id),
                )
            conn.execute(
                "UPDATE conversation SET updated_at = ? WHERE id = ?",
                (utcnow_iso(), conv_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.exception("sync partial persist failed")
