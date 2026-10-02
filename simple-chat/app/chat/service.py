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
import sqlite3
import time
import uuid
from typing import Any, AsyncIterator

import aiosqlite

from app import metrics
from app.config import settings
from app.db import get_db, persist_partial_sync, run_write, utcnow_iso
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

# 模块级并发锁池：key=conversation_id，value=_LockEntry。
# 同一会话同时只允许一个进行中的流式请求（互斥），其余返回 409。
_conv_locks: dict[str, "_LockEntry"] = {}

# 保护 _conv_locks 字典自身的锁，保证“检查 + 写入 owner_id”原子。
# 注意：asyncio.Lock 会绑定到首次 acquire 所在的事件循环；pytest-asyncio 为
# 每个测试新建事件循环，若用模块级单例会被绑定到第一个（随后关闭的）循环，
# 导致后续测试在已关闭的循环上操作而抛 “Event loop is closed”。因此按运行循环
# 惰性重建：每个事件循环持有自己独立的 meta 锁，跨循环互不干扰。
_lock_meta: asyncio.Lock | None = None
_lock_meta_loop: "asyncio.AbstractEventLoop | None" = None


def _meta_lock() -> asyncio.Lock:
    """返回当前运行事件循环专属的 meta 锁（按 loop 惰性创建）。"""
    loop = asyncio.get_running_loop()
    global _lock_meta, _lock_meta_loop
    if _lock_meta is None or _lock_meta_loop is not loop:
        _lock_meta = asyncio.Lock()
        _lock_meta_loop = loop
    return _lock_meta

# 锁条目空闲（已释放）超过该秒数后，由 _reap_locks 回收，防止 dict 无限增长。
_LOCK_IDLE_TTL = 300.0

# 后台标题任务强引用集合，防止被 GC 提前回收
_background_tasks: set[asyncio.Task] = set()


# ───────────────────────── SSE 工具 ─────────────────────────


def _sse(event: str, payload: dict) -> str:
    """构造 SSE 帧：event/data 单行 JSON + 两换行。"""
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n"


# ───────────────────────── 并发互斥 ─────────────────────────


class _LockEntry:
    """会话锁条目：持有 asyncio.Lock、owner_id 与最近活跃时间。"""

    __slots__ = ("lock", "owner_id", "last_used")

    def __init__(self, lock: asyncio.Lock, owner_id: str, last_used: float) -> None:
        self.lock = lock
        self.owner_id = owner_id
        self.last_used = last_used


class _ConversationGate:
    """同一会话的并发互斥闸门（异步上下文管理器）。

    - 进入时原子地：在 _lock_meta 保护下检查锁是否已占用，已占用则抛
      ConversationBusyError（409）；否则创建/复用锁对象并 acquire。
    - 退出时校验：仅当池中条目仍是“本任务持有的那把锁”才 release，
      避免释放别人的锁。
    """

    def __init__(self, conv_id: str, owner_id: str) -> None:
        self.conv_id = conv_id
        self.owner_id = owner_id
        self._lock: asyncio.Lock | None = None

    async def __aenter__(self) -> asyncio.Lock:
        async with _meta_lock():
            entry = _conv_locks.get(self.conv_id)
            if entry is not None and entry.lock.locked():
                # 已有进行中的流式请求 → 409
                metrics.locks_contended_total.inc()
                raise ConversationBusyError(self.conv_id)
            if entry is None:
                lock = asyncio.Lock()
                entry = _LockEntry(lock, self.owner_id, time.monotonic())
                _conv_locks[self.conv_id] = entry
            else:
                # 复用已存在的锁对象，刷新 owner 与活跃时间
                entry.owner_id = self.owner_id
                entry.last_used = time.monotonic()
                lock = entry.lock
            # 无竞争：此时锁必为未占用，acquire 不会挂起（未占用时立即返回）
            await lock.acquire()
            entry.last_used = time.monotonic()
            self._lock = lock
            return lock

    async def __aexit__(self, exc_type, exc, tb) -> None:
        async with _meta_lock():
            entry = _conv_locks.get(self.conv_id)
            if entry is None:
                return
            # 仅当池中条目仍是“当前任务持有的那把锁”时才释放，避免释放他人锁
            if entry.lock is self._lock and entry.lock.locked():
                try:
                    entry.lock.release()
                except RuntimeError:
                    pass
            # 释放后立即移除条目，dict 不会无限增长；
            # 安全网由 _reap_locks 兜底清理崩溃泄漏（locked 但未释放）的条目。
            _conv_locks.pop(self.conv_id, None)


async def _reap_locks() -> None:
    """后台回收：每 30s 清理“已释放且空闲超过 _LOCK_IDLE_TTL”的条目。

    正常路径下条目在 __aexit__ 即被移除；本任务只作为安全网，
    清理因任务异常崩溃而未正常释放的残留条目。
    """
    while True:
        await asyncio.sleep(30)
        async with _meta_lock():
            now = time.monotonic()
            stale = [
                k
                for k, e in _conv_locks.items()
                if not e.lock.locked() and now - e.last_used > _LOCK_IDLE_TTL
            ]
            for k in stale:
                _conv_locks.pop(k, None)


# ───────────────────────── 会话读写 ─────────────────────────


async def _get_conversation(
    conn: aiosqlite.Connection, conv_id: str, user_id: str
) -> dict:
    """取会话；不存在或不属于该 user_id 一律抛 ConversationNotFoundError（防 ID 遍历）。

    统一返回 404 而非 403，避免泄露“该 id 存在但非本人”的信息。
    """
    with metrics.db_time("select_conversation"):
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
    await run_write(
        conn,
        [(
            "INSERT INTO conversation (id, title, user_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (conv_id, "新对话", user_id, now, now),
        )],
    )
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
    with metrics.db_time("insert_message"):
        cur = await run_write(
            conn,
            [
                (
                    "INSERT INTO message (conversation_id, role, content, tokens, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (conv_id, role, content, tokens, now),
                ),
                (
                    "UPDATE conversation SET updated_at = ? WHERE id = ?",
                    (now, conv_id),
                ),
            ],
        )
    metrics.conversation_messages_total.inc(role=role)
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


def delete_message_sync(message_id: int) -> None:
    """取消路径专用同步删除：被取消的作用域内 await 会立刻重抛，只能同步写。"""
    try:
        conn = sqlite3.connect(settings.db_path, timeout=5)
        try:
            conn.execute("DELETE FROM message WHERE id = ?", (message_id,))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.exception("sync delete assistant failed")


# ───────────────────────── 上下文裁剪 ─────────────────────────


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """将 text 粗略截断到不超过 max_tokens（estimate_tokens 近似，O(n)）。

    单趟估算后按比例缩减字符数，避免逐字符二分带来的额外开销。
    """
    if max_tokens <= 0:
        return ""
    est = estimate_tokens([{"role": "user", "content": text}])
    if est <= max_tokens:
        return text
    ratio = max_tokens / est
    cut = int(len(text) * ratio * 0.9)  # 再留 10% 余量
    return text[: max(cut, 0)]


async def build_context(
    conversation_id: str,
    db: aiosqlite.Connection,
    max_context_tokens: int,
    current_user_message: str | None = None,
) -> list[dict]:
    """组装发给模型的上下文。

    保证：
    - 当前用户消息（DB 中最新一条 role=user）必然出现在 messages 末尾。
    - 角色严格交替：由新到旧遍历时跳过与“上一条已保留”同 role 的条目。
    - budget 扣除 system 与 max_response_tokens；当前用户消息即使单条超过 budget
      也必发送（可截断 + “[内容已截断]”），绝不允许“用户问了但模型没收到”。
    - token 估算保持 O(n) 近似（沿用 estimate_tokens，不引入 tiktoken）。
    """
    with metrics.db_time("select_messages"):
        cur = await db.execute(
            "SELECT role, content FROM message WHERE conversation_id = ?"
            " ORDER BY id DESC LIMIT ?",
            (conversation_id, MAX_CONTEXT_MESSAGES),
        )
        rows = [dict(r) for r in await cur.fetchall()]  # 由新到旧

    system_msg = {"role": "system", "content": llm_client.SYSTEM_PROMPT}
    system_tokens = estimate_tokens([system_msg])
    # 预算（含 system，不含 max_response_tokens）
    cap = max(max_context_tokens - system_tokens - settings.max_response_tokens, 0)

    # 当前用户消息：DB 中最新一条 role=user（避免参数穿透）；
    # 若 DB 尚未落库（极端边界），用传入参数兜底。
    current_idx = next(
        (i for i, r in enumerate(rows) if r["role"] == "user"), None
    )
    if current_idx is None:
        current = {"role": "user", "content": current_user_message or ""}
        history = rows
    else:
        current = {"role": "user", "content": rows[current_idx]["content"]}
        history = [r for i, r in enumerate(rows) if i != current_idx]  # 仍由新到旧

    # 由新到旧贪心加入历史：超预算则跳过该条；同时保证角色严格交替
    # （与已保留的最新一条同 role 则丢弃较旧者）。current 始终在末尾。
    chain: list[dict] = [current]
    for row in history:  # 由新到旧
        msg = {"role": row["role"], "content": row["content"]}
        if chain and chain[0]["role"] == msg["role"]:
            logger.warning(
                "conversation %s 连续相同 role=%s，丢弃较旧的一条",
                conversation_id, msg["role"],
            )
            continue
        trial = [msg, *chain]
        if estimate_tokens([system_msg, *trial]) > cap and len(chain) >= 1:
            continue  # 本条超预算，跳过（不加入）
        chain = trial

    # 边界：current 单条就超 cap → 截断内容，仍发送
    if estimate_tokens([system_msg, *chain]) > cap and len(chain) == 1:
        metrics.context_truncated_total.inc()
        allowed = max(cap - system_tokens - 8, 0)  # 留 8 token 余量给 “[内容已截断]”
        current["content"] = _truncate_to_tokens(current["content"], allowed) + "[内容已截断]"

    return [system_msg, *chain]


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
    # 首 token 延迟计时起点 = service 收到请求（PR-3 改动 1：最关键指标）
    t_start = time.monotonic()
    mdl = req.model or settings.llm_model

    # a–e 为预检段：此段抛出的业务异常（422/404/409）在此统一计入
    # chat_requests_total 后原样上抛；流式段的指标由 _do_stream 负责。
    try:
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
        context = await build_context(conv_id, db, settings.max_context_tokens, message)
        prompt_tokens = estimate_tokens(context)

        # e. 并发互斥：同一会话同时只允许一个流式请求（占用中则 409）
        gate = _ConversationGate(conv_id, user_id)
        await gate.__aenter__()  # 占用中抛 ConversationBusyError
    except AppError as exc:
        metrics.chat_requests_total.inc(
            model=mdl, error_code=exc.code.value, status=str(exc.status_code)
        )
        raise

    # SSE 头由路由层设置；这里返回生成器对象，由路由包成 StreamingResponse
    return _do_stream(
        conv_id=conv_id,
        context=context,
        model=req.model,
        prompt_tokens=prompt_tokens,
        is_new=is_new,
        first_message=message,
        gate=gate,
        t_start=t_start,
        mdl=mdl,
    )


async def _do_stream(
    *,
    conv_id: str,
    context: list[dict],
    model: str | None,
    prompt_tokens: int,
    is_new: bool,
    first_message: str,
    gate: _ConversationGate,
    t_start: float,
    mdl: str,
) -> AsyncIterator[str]:
    """实际产出 SSE 事件流；持有会话锁直到流结束（gate 在 finally 释放）。

    - assistant 消息仅在流成功结束后、且内容非空时才落库（committed）。
    - 异常：删除已落库但未提交的 assistant 行，yield error 事件后 return（不再 yield 任何帧）。
    - 取消（CancelledError）：若已落库但未提交，同步删除（禁止 await）；必须 re-raise，
      否则 StreamingResponse 会认为流正常结束、客户端误以为成功。
    - 绝不写出“幽灵”assistant 消息：取消/异常路径下不调用 persist_partial_sync 写残留。

    指标（PR-3 改动 1）：active_streams 覆盖全生命周期；首 token 延迟以 t_start
    （send_message 入口）为起点、第一个 delta 产出为终点；tokens/duration/requests
    按成功 / 业务错误 / 取消 / 内部错误四条路径分别计入。
    """
    parts: list[str] = []
    completion_tokens = 0
    assistant_rowid: int | None = None
    committed = False
    first_token_seen = False
    metrics.chat_active_streams.inc()
    metrics.chat_tokens_total.inc(prompt_tokens, model=mdl, role="prompt")
    # 生成器自管 DB 连接：FastAPI 的 yield 依赖在响应头发出后即关闭，无法覆盖整个流
    async with get_db() as conn:
        upstream = llm_client.chat_stream(context, model=model)
        try:
            async for delta in upstream:
                if delta:
                    if not first_token_seen:
                        first_token_seen = True
                        metrics.chat_first_token_seconds.observe(
                            time.monotonic() - t_start, model=mdl
                        )
                    parts.append(delta)
                    completion_tokens += count_tokens(delta)
                    # 逐 token yield，格式严格按规格
                    yield _sse("token", {"delta": delta})

            # 流式结束：1.拼接 2.INSERT assistant 3.（新会话）异步标题 4.yield done
            full = "".join(parts)
            if full.strip():
                assistant_row = await _add_message(
                    conn, conv_id, "assistant", full, completion_tokens
                )
                assistant_rowid = assistant_row["id"]
                committed = True  # 落库与 committed 原子（无 await 间隔）
                if is_new:
                    _spawn_title_task(conv_id, first_message)
                metrics.chat_duration_seconds.observe(time.monotonic() - t_start, model=mdl)
                metrics.chat_requests_total.inc(model=mdl, error_code="", status="200")
                yield _sse(
                    "done",
                    {
                        "conversation_id": conv_id,
                        "message_id": assistant_rowid,
                        "usage": {
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": completion_tokens,
                            "total_tokens": prompt_tokens + completion_tokens,
                        },
                    },
                )
            else:
                # 模型未产出任何内容：不写 assistant，仅通知流结束
                metrics.chat_duration_seconds.observe(time.monotonic() - t_start, model=mdl)
                metrics.chat_requests_total.inc(model=mdl, error_code="", status="200")
                yield _sse(
                    "done",
                    {
                        "conversation_id": conv_id,
                        "message_id": None,
                        "usage": {
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": 0,
                            "total_tokens": prompt_tokens,
                        },
                    },
                )
        except AppError as exc:
            # 业务异常：若已落库（rowid 已设）则删除残留；否则无需回滚
            if assistant_rowid is not None and not committed:
                await _delete_message(conn, assistant_rowid)
            metrics.chat_duration_seconds.observe(time.monotonic() - t_start, model=mdl)
            metrics.chat_requests_total.inc(
                model=mdl, error_code=exc.code.value, status=str(exc.status_code)
            )
            yield _sse("error", exc.to_sse_data())
            return
        except GeneratorExit:
            # 生成器被直接 aclose（非流式路径复用 / 关闭链）：同步计数后立刻上抛，
            # GeneratorExit 处理器中禁止 await 之后的再次挂起。
            metrics.chat_requests_total.inc(
                model=mdl, error_code="CLIENT_DISCONNECTED", status="499"
            )
            raise
        except asyncio.CancelledError:
            # 客户端断开 / 请求被取消：绝不允许“幽灵”assistant。
            # 本设计下 assistant 仅在成功结束时落库，故此处 rowid 必为 None；
            # 若因极端时序已落库（rowid 已设）但未 committed，同步删除（禁止 await）。
            if assistant_rowid is not None and not committed:
                delete_message_sync(assistant_rowid)
            metrics.chat_duration_seconds.observe(time.monotonic() - t_start, model=mdl)
            metrics.chat_requests_total.inc(
                model=mdl, error_code="CLIENT_DISCONNECTED", status="499"
            )
            raise
        except Exception:
            logger.exception("unexpected error in chat stream for %s", conv_id)
            if assistant_rowid is not None and not committed:
                await _delete_message(conn, assistant_rowid)
            metrics.chat_duration_seconds.observe(time.monotonic() - t_start, model=mdl)
            metrics.chat_requests_total.inc(
                model=mdl, error_code=ErrorCode.INTERNAL_ERROR.value, status="500"
            )
            yield _sse(
                "error",
                {"code": ErrorCode.INTERNAL_ERROR.value, "message": "服务内部错误"},
            )
            return
        finally:
            # completion token 计数覆盖全部退出路径（已产出即已计费）
            if completion_tokens:
                metrics.chat_tokens_total.inc(
                    completion_tokens, model=mdl, role="completion"
                )
            # 关键：确定性关闭上游生成器。客户端断开时本生成器被 aclose() 抛
            # GeneratorExit，内层 chat_stream 不会自动关闭（只能等 GC），导致
            # 上游 HTTP 连接与计费悬挂——必须在此显式 aclose。
            # 防御：测试桩可能是无 aclose 的协程/对象，跳过即可，绝不能让
            # 关闭失败中断 finally（否则会话锁与 gauge 永久泄漏）。
            closer = getattr(upstream, "aclose", None)
            if closer is not None:
                await closer()
            metrics.chat_active_streams.dec()
            await gate.__aexit__(None, None, None)


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
    with metrics.db_time("list_conversations"):
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
    with metrics.db_time("select_messages"):
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
    with metrics.db_time("delete_conversation"):
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
            await run_write(
                conn,
                [(
                    "UPDATE conversation SET title = ? WHERE id = ? AND title = '新对话'",
                    (title, conv_id),
                )],
            )
    except Exception as exc:
        logger.warning("title persist failed for %s: %s", conv_id, exc)


async def shutdown_tasks() -> None:
    """停机时取消游离任务，避免引用已关闭事件循环的挂起任务。

    _background_tasks 为模块级集合；在 pytest-asyncio 下每个用例拥有独立事件循环，
    fire-and-forget 的标题任务可能绑定在“上一个已关闭的循环”上。若直接对其
    asyncio.gather 会抛 “attached to a different loop”。故只 gather 当前运行循环
    上的任务，其余（孤儿/已失效循环）直接丢弃——生产环境只有一个循环，行为不变。
    """
    loop = asyncio.get_running_loop()
    live = [t for t in _background_tasks if not t.done() and t.get_loop() is loop]
    orphan = [t for t in _background_tasks if not t.done() and t.get_loop() is not loop]
    if orphan:
        logger.warning("discarding %d background task(s) bound to a closed loop", len(orphan))
    for task in live:
        task.cancel()
    if live:
        await asyncio.gather(*live, return_exceptions=True)
    _background_tasks.clear()
