"""对话核心逻辑：消息收发、上下文裁剪、流式编排、并发互斥。

禁止事项（约束）：
- 禁止在 service 层直接读取 request.headers；user_id 必须经参数传入。
- 禁止同步阻塞调用；所有 DB 与 LLM 调用必须 await。
- 禁止把完整对话历史一次性发给模型；必须走 build_context 裁剪。
- 禁止在流式过程中 await 耗时操作后再 yield，会卡住前端首 token。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any, AsyncIterator

import aiosqlite

from app.config import settings
from app.db import get_db, persist_partial_sync, utcnow_iso
from app.errors import (
    AppError,
    ConversationBusyError,
    ConversationNotFoundError,
    ErrorCode,
    ValidationError,
)
from app.llm import client as llm_client
from app.llm.tokenizer import count_tokens, estimate_tokens
from app.schema import ChatRequest

logger = logging.getLogger(__name__)

# 上下文裁剪相关常量
MAX_CONTEXT_MESSAGES = 40  # 安全上限：即便 token 预算未满也最多取这么多条
_LOCK_TTL = 300.0  # 会话锁 TTL（秒），过期清理防止内存泄漏

# 模块级并发锁池：key=conversation_id，value=(asyncio.Lock, acquisition_time)
_conv_locks: dict[str, tuple[asyncio.Lock, float]] = {}

# 后台标题任务强引用集合，防止被 GC 提前回收
_background_tasks: set[asyncio.Task] = set()


# ───────────────────────── SSE 工具 ─────────────────────────


def _sse(event: str, payload: dict) -> str:
    """构造 SSE 帧：event/data 单行 JSON + 两换行。"""
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n"


# ───────────────────────── 并发互斥 ─────────────────────────


def _get_lock(conv_id: str) -> asyncio.Lock:
    """取会话锁；顺带清理已过期的锁条目，防止 dict 无限增长。"""
    now = time.monotonic()
    # 清理过期项
    expired = [k for k, (_, ts) in _conv_locks.items() if now - ts > _LOCK_TTL]
    for k in expired:
        _conv_locks.pop(k, None)
    entry = _conv_locks.get(conv_id)
    if entry is None:
        lock = asyncio.Lock()
        _conv_locks[conv_id] = (lock, now)
        return lock
    lock, _ = entry
    return lock


def _touch_lock(conv_id: str) -> None:
    """刷新锁的获取时间，避免仍在使用中的锁被 TTL 误清理。"""
    entry = _conv_locks.get(conv_id)
    if entry is not None:
        _conv_locks[conv_id] = (entry[0], time.monotonic())


def _release_lock(conv_id: str) -> None:
    """释放锁并从池中移除；release 必须与 acquire 在同一任务。"""
    entry = _conv_locks.pop(conv_id, None)
    if entry is None:
        return
    lock, _ = entry
    if lock.locked():
        try:
            lock.release()
        except RuntimeError:
            pass  # 已被释放则忽略


# ───────────────────────── 会话读写 ─────────────────────────


async def _get_conversation(
    conn: aiosqlite.Connection, conv_id: str, user_id: str
) -> dict:
    """取会话；不存在或不属于该 user_id 一律抛 ConversationNotFoundError（防 ID 遍历）。

    统一返回 404 而非 403，避免泄露“该 id 存在但非本人”的信息。
    """
    cur = await conn.execute(
        "SELECT * FROM conversation WHERE id = ? AND user_id = ?", (conv_id, user_id)
    )
    row = await cur.fetchone()
    if row is None:
        raise ConversationNotFoundError(conv_id)
    return dict(row)


async def _create_conversation(conn: aiosqlite.Connection, user_id: str) -> dict:
    """新建会话；title 留占位，真正标题由 e.4 异步 fire-and-forget 写入。"""
    now = utcnow_iso()
    conv_id = uuid.uuid4().hex
    await conn.execute(
        "INSERT INTO conversation (id, title, user_id, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (conv_id, "新对话", user_id, now, now),
    )
    await conn.commit()
    return {"id": conv_id, "title": "新对话", "created_at": now, "updated_at": now}


def _derive_title(text: str) -> str:
    """按首个句末标点或换行截断，最多 20 字符；截不到则取前 20 字符。"""
    stripped = text.strip()
    match = re.search(r"[。！？\n]", stripped)
    cut = stripped[: match.start() + 1] if match else stripped[:20]
    return cut[:20] or "新对话"


async def _add_message(
    conn: aiosqlite.Connection,
    conv_id: str,
    role: str,
    content: str,
    tokens: int,
) -> dict:
    """插入消息并刷新会话 updated_at；返回新行字典。"""
    now = utcnow_iso()
    cur = await conn.execute(
        "INSERT INTO message (conversation_id, role, content, tokens, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (conv_id, role, content, tokens, now),
    )
    await conn.execute(
        "UPDATE conversation SET updated_at = ? WHERE id = ?", (now, conv_id)
    )
    await conn.commit()
    return {
        "id": cur.lastrowid,
        "conversation_id": conv_id,
        "role": role,
        "content": content,
        "tokens": tokens,
        "created_at": now,
    }


async def _delete_message(conn: aiosqlite.Connection, message_id: int) -> None:
    """回滚用：删除已写入的 assistant 行，保证“用户看到错误”与“库一致”。"""
    await conn.execute("DELETE FROM message WHERE id = ?", (message_id,))
    await conn.commit()


# ───────────────────────── 上下文裁剪 ─────────────────────────


async def build_context(
    conversation_id: str,
    db: aiosqlite.Connection,
    max_context_tokens: int,
) -> list[dict]:
    """组装发给模型的上下文：固定 system 前缀 + 从最新向前取的消息。

    裁剪策略：
    - 固定前缀：1 条 system 消息（SYSTEM_PROMPT）。
    - 从最新消息向前累加 token，超过
      max_context_tokens - SYSTEM_TOKENS - MAX_RESPONSE_TOKENS 时停止。
    - 至少保留最新 1 条用户消息。
    - 顺序：system, ..., user（最新）。
    - 角色交替校验：连续相同 role 视为不合法，丢弃较旧的一条并告警。
    - 禁止插入摘要（MVP 不做摘要，避免幻觉与复杂度）。
    """
    cur = await db.execute(
        "SELECT role, content FROM message WHERE conversation_id = ?"
        " ORDER BY id DESC LIMIT ?",
        (conversation_id, MAX_CONTEXT_MESSAGES),
    )
    rows = [dict(r) for r in await cur.fetchall()]  # 由新到旧

    system_tokens = estimate_tokens([{"role": "system", "content": llm_client.SYSTEM_PROMPT}])
    budget = max(
        max_context_tokens - system_tokens - settings.max_response_tokens,
        # 兜底：预算再紧也要留出至少能放下最新一条用户消息的空间
        estimate_tokens([rows[0]]) if rows else 0,
    )

    kept: list[dict] = []
    used = 0
    for row in rows:  # 由新到旧
        msg = {"role": row["role"], "content": row["content"]}
        cost = estimate_tokens([msg])
        if used + cost > budget and kept:
            break
        # 角色交替校验：与已保留的“最新一条”比较
        if kept and kept[-1]["role"] == msg["role"]:
            logger.warning(
                "conversation %s 出现连续相同 role=%s，丢弃较旧的一条",
                conversation_id, msg["role"],
            )
            continue  # 丢弃较旧者（当前 row 比 kept[-1] 更旧）
        used += cost
        kept.append(msg)

    kept.reverse()  # 恢复为时间正序
    # 去重保护：如果最新消息被裁掉但仍是 user，仍要保证至少一条
    if not kept and rows:
        kept = [{"role": rows[0]["role"], "content": rows[0]["content"]}]

    return [{"role": "system", "content": llm_client.SYSTEM_PROMPT}, *kept]


# ───────────────────────── 流式编排 ─────────────────────────


async def send_message(
    req: ChatRequest,
    db: aiosqlite.Connection,
    user_id: str,
) -> AsyncIterator[str]:
    """发送消息并返回 SSE 流式响应。

    流程（严格按序）：
    a. 规范化输入：strip()，长度校验。
    b. 会话解析：conversation_id 为空则新建，否则校验存在。
    c. 持久化用户消息（role='user'）。
    d. 组装上下文 build_context。
    e. 流式 yield token；结束后落 assistant、刷 updated_at、（新会话）异步生成标题、
       yield done；流式中异常 yield error 并回滚已写入的 assistant 行。

    并发：同一 conversation_id 同一时刻只允许一个进行中的流式请求。
    """
    # a. 规范化输入
    message = (req.message or "").strip()
    if not message:
        raise ValidationError("message 不能为空")
    if len(message) > 4000:
        raise ValidationError("message 长度不能超过 4000 字符")

    # b. 会话解析（前置写库用传入的 db 连接）
    if req.conversation_id:
        conversation = await _get_conversation(db, req.conversation_id, user_id)
        conv_id = conversation["id"]
        is_new = False
    else:
        conversation = await _create_conversation(db, user_id)
        conv_id = conversation["id"]
        is_new = True

    # c. 持久化用户消息
    user_tokens = estimate_tokens([{"role": "user", "content": message}])
    await _add_message(db, conv_id, "user", message, user_tokens)

    # d. 组装上下文
    context = await build_context(conv_id, db, settings.max_context_tokens)
    prompt_tokens = estimate_tokens(context)

    # e. 并发互斥：同一会话同时只允许一个流式请求
    lock = _get_lock(conv_id)
    if lock.locked():
        # 已有进行中的流式请求 → 409 CONVERSATION_BUSY
        raise ConversationBusyError(conv_id)
    await lock.acquire()
    _touch_lock(conv_id)

    # SSE 头由路由层设置；这里返回生成器对象，由路由包成 StreamingResponse
    return _stream_response(
        conv_id=conv_id,
        context=context,
        model=req.model,
        prompt_tokens=prompt_tokens,
        is_new=is_new,
        first_message=message,
        lock=lock,
    )


async def _stream_response(
    *,
    conv_id: str,
    context: list[dict],
    model: str | None,
    prompt_tokens: int,
    is_new: bool,
    first_message: str,
    lock: asyncio.Lock,
) -> AsyncIterator[str]:
    """实际产出 SSE 事件流；持有会话锁直到流结束。"""
    parts: list[str] = []
    completion_tokens = 0
    assistant_row: dict | None = None
    # 生成器自管 DB 连接：FastAPI 的 yield 依赖在响应头发出后即关闭，无法覆盖整个流
    async with get_db() as conn:
        try:
            async for delta in llm_client.chat_stream(context, model=model):
                parts.append(delta)
                completion_tokens += count_tokens(delta)
                # 逐 token yield，格式严格按规格
                yield _sse("token", {"delta": delta})

            # 流式结束：1.拼接 2.INSERT assistant 3.UPDATE updated_at 4.（新会话）异步标题 5.yield done
            assistant_tokens = estimate_tokens(
                [{"role": "assistant", "content": "".join(parts)}]
            )
            assistant_row = await _add_message(
                conn, conv_id, "assistant", "".join(parts), assistant_tokens
            )
            # 4.（新会话）异步 fire-and-forget 生成标题：取首条消息按
            #    [。！？\n] 或前 20 字截断；禁止为此再调用 LLM（MVP 节省成本）
            if is_new:
                _spawn_title_task(conv_id, first_message)
            # 5. yield done 事件
            yield _sse(
                "done",
                {
                    "conversation_id": conv_id,
                    "message_id": assistant_row["id"],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                    },
                },
            )
        except AppError as exc:
            # 流式中异常：回滚已写入的 assistant 行，保证“用户看到错误”与“库一致”
            if assistant_row is not None:
                await _delete_message(conn, assistant_row["id"])
            yield _sse("error", exc.to_sse_data())
        except asyncio.CancelledError:
            # 取消作用域内任何 await 都会立刻重抛，只能同步落库部分内容
            if parts and assistant_row is None:
                persist_partial_sync(conv_id, "".join(parts), completion_tokens)
            elif assistant_row is not None:
                # 已写入：补全 tokens 后同步更新（取消路径下不能 await）
                persist_partial_sync(
                    conv_id, "".join(parts), completion_tokens, update_id=assistant_row["id"]
                )
            raise
        except Exception:
            logger.exception("unexpected error in chat stream for %s", conv_id)
            if assistant_row is not None:
                await _delete_message(conn, assistant_row["id"])
            yield _sse(
                "error",
                {"code": ErrorCode.INTERNAL_ERROR.value, "message": "服务内部错误"},
            )
        finally:
            _release_lock(conv_id)


# ───────────────────────── 列表与查询 ─────────────────────────


async def list_conversations(
    db: aiosqlite.Connection,
    user_id: str,
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    """会话列表（仅本人），按 updated_at DESC；limit<=100；用子查询取最后一条消息作预览。"""
    limit = min(max(limit, 1), 100)
    offset = max(offset, 0)
    cur = await db.execute(
        """
        SELECT c.id, c.title, c.created_at, c.updated_at,
               (SELECT m.content FROM message m
                WHERE m.conversation_id = c.id
                ORDER BY m.id DESC LIMIT 1) AS last_message
        FROM conversation c
        WHERE c.user_id = ?
        ORDER BY c.updated_at DESC, c.id DESC
        LIMIT ? OFFSET ?
        """,
        (user_id, limit, offset),
    )
    rows = [dict(r) for r in await cur.fetchall()]
    # last_message 截断到 80 字预览
    for r in rows:
        if r.get("last_message"):
            r["last_message"] = r["last_message"][:80]
    return rows


async def get_messages(
    conversation_id: str,
    db: aiosqlite.Connection,
    user_id: str,
    limit: int = 100,
) -> list[dict]:
    """校验会话存在且归属本人后，按 created_at ASC 返回消息（时间正序，前端直接渲染）。"""
    await _get_conversation(db, conversation_id, user_id)  # 不存在/非本人抛 404
    limit = min(max(limit, 1), 1000)
    cur = await db.execute(
        "SELECT id, conversation_id, role, content, tokens, created_at"
        " FROM message WHERE conversation_id = ?"
        " ORDER BY id ASC LIMIT ?",
        (conversation_id, limit),
    )
    return [dict(r) for r in await cur.fetchall()]


async def delete_conversation(
    conversation_id: str,
    db: aiosqlite.Connection,
    user_id: str,
) -> bool:
    """事务内先删消息再删会话；返回是否删除成功。非本人/不存在抛 404。"""
    await _get_conversation(db, conversation_id, user_id)  # 不存在/非本人抛 404
    await db.execute("BEGIN")
    try:
        await db.execute("DELETE FROM message WHERE conversation_id = ?", (conversation_id,))
        await db.execute("DELETE FROM conversation WHERE id = ?", (conversation_id,))
        await db.commit()
    except Exception:
        await db.execute("ROLLBACK")
        raise
    return True


# ───────────────────────── 标题生成 ─────────────────────────


def _spawn_title_task(conv_id: str, first_message: str) -> None:
    """fire-and-forget 生成标题：MVP 仅取首条消息截断，不调用 LLM。"""
    title = _derive_title(first_message)
    task = asyncio.create_task(_persist_title(conv_id, title))
    # 必须持强引用，asyncio 只存弱引用，否则任务可能被 GC 静默回收
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _persist_title(conv_id: str, title: str) -> None:
    """把截断标题写回会话；仅在当前标题为占位“新对话”时覆盖，避免重置用户改过的标题。"""
    try:
        async with get_db() as conn:
            await conn.execute(
                "UPDATE conversation SET title = ? WHERE id = ? AND title = '新对话'",
                (title, conv_id),
            )
            await conn.commit()
    except Exception as exc:
        logger.warning("title persist failed for %s: %s", conv_id, exc)


async def shutdown_tasks() -> None:
    """停机时取消游离任务，避免引用已关闭事件循环的挂起任务。"""
    pending = [t for t in _background_tasks if not t.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    _background_tasks.clear()
