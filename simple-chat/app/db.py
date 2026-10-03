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
    role            TEXT NOT NULL CHECK (role IN ('user','assistant','system','tool')),
    content         TEXT NOT NULL,
    tokens          INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    model           TEXT,
    tool_call_id    TEXT,
    tool_calls_json TEXT,
    FOREIGN KEY (conversation_id) REFERENCES conversation(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_message_conv_created
    ON message(conversation_id, created_at);

-- v0.2 认证：本地用户表。密码只存 scrypt 慢哈希（含随机 salt），绝不存明文。
-- 表名 user 在 SQLite 中不是保留字（最短唯一前缀是 "us"），可直接使用。
CREATE TABLE IF NOT EXISTS user (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

-- v0.4 工作记忆：每 N 轮生成的对话摘要。摘要只压缩已滑出原文窗口的旧消息，
-- 是数据（不是权限来源）；build_context 在 system 之后、原文之前注入。
-- upto_message_id 单调递增：只在新消息 id 大于已摘要的 id 时才重新摘要。
CREATE TABLE IF NOT EXISTS conversation_memory (
    conversation_id TEXT PRIMARY KEY,
    summary         TEXT,
    upto_message_id INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversation(id) ON DELETE CASCADE
);

-- v0.6 RAG：私有文档全文检索（FTS5，MVI 阶段禁向量数据库）。
-- document/chunk 与聊天表完全解耦：可整体删除重建索引而不影响会话历史。
-- chunk 与 FTS5 虚表通过触发器同步（content='chunk' 外部内容表），删除 document
-- 时 ON DELETE CASCADE 级联删 chunk，触发器再清理 FTS。
CREATE TABLE IF NOT EXISTS document (
    id        TEXT PRIMARY KEY,
    path      TEXT,
    hash      TEXT NOT NULL,
    added_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunk (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL,
    text        TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES document(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chunk_document ON chunk(document_id);

-- FTS5 全文索引（MVI 阶段禁向量数据库）。
-- 独立表（非 external content）：CJK 预分词在 indexing 层完成（每个汉字两侧加
-- 空格），使 unicode61 把每个汉字当独立 token，实现中文按字符匹配基线。
-- chunk_id UNINDEXED：只用于回溯原文行，不参与全文索引。
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    document_id,
    chunk_id UNINDEXED,
    tokenize='unicode61'
);
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

        # v0.3 多模型路由：为 message 表补充可空 model 列，记录实际选中的模型 id。
        # 可空保证向后兼容（旧消息 model 为 NULL，读取时按默认模型处理）。
        cur = await conn.execute("PRAGMA table_info(message)")
        msg_cols = {row["name"] for row in await cur.fetchall()}
        if "model" not in msg_cols:
            await conn.execute("ALTER TABLE message ADD COLUMN model TEXT")
            await conn.commit()

        # v0.5 工具调用：为 message 表补充可空 tool_call_id / tool_calls_json 列，
        # 并将 role CHECK 扩展到 'tool'（允许工具结果消息）。旧消息留 NULL，向后兼容。
        if "tool_call_id" not in msg_cols:
            await conn.execute("ALTER TABLE message ADD COLUMN tool_call_id TEXT")
        if "tool_calls_json" not in msg_cols:
            await conn.execute("ALTER TABLE message ADD COLUMN tool_calls_json TEXT")
        await conn.commit()
        # 扩展 role CHECK 必须重建表（SQLite 不支持 ALTER 修改 CHECK 约束）。
        # 仅当当前 message 表的 CHECK 未包含 'tool' 时才重建，已是最新则跳过。
        cur = await conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='message' AND type='table'"
        )
        row = await cur.fetchone()
        msg_sql = (row["sql"] if row else "") or ""
        if "'tool'" not in msg_sql:
            await conn.execute("ALTER TABLE message RENAME TO _message_old")
            await conn.execute(
                """
                CREATE TABLE message (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user','assistant','system','tool')),
                    content TEXT NOT NULL,
                    tokens INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    model TEXT,
                    tool_call_id TEXT,
                    tool_calls_json TEXT,
                    FOREIGN KEY (conversation_id) REFERENCES conversation(id) ON DELETE CASCADE
                )
                """
            )
            await conn.execute(
                "INSERT INTO message "
                "(id, conversation_id, role, content, tokens, created_at, model, tool_call_id, tool_calls_json) "
                "SELECT id, conversation_id, role, content, tokens, created_at, model, NULL, NULL "
                "FROM _message_old"
            )
            await conn.execute("DROP TABLE _message_old")
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_message_conv_created "
                "ON message(conversation_id, created_at)"
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
