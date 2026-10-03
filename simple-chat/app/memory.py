"""v0.4 工作记忆：每 N 轮生成一次对话摘要，注入 build_context。

设计约束（对齐 ROADMAP v0.4）：
- 禁止向量数据库 / embedding / 语义检索；只做每 N 轮摘要。
- 摘要只压缩已经滑出原文窗口（memory_raw_window，默认 40 条）的旧消息。
- 摘要是**数据**不是权限：放在固定位置，标注“较早对话摘要，可能不完整”，
  绝不进 system 段，system 语序在摘要之前。
- 摘要任务在 _background_tasks 里异步执行；失败静默降级为不注入（不抛、不阻塞聊天）。
- upto_message_id 单调递增：只在新消息 id 大于已摘要的 id 时才重新摘要。
- 安全边界不靠提示词：摘要模板只描述“做什么”（压缩旧消息），不写“不许做什么”。
- 摘要走便宜模型：调用 llm_client.chat()（非流式），用低价/小 max_tokens；不为摘要开流式。
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from app import metrics
from app.config import settings
from app.db import get_db, run_write, utcnow_iso
from app.llm import client as llm_client
from app.llm.tokenizer import estimate_tokens

logger = logging.getLogger(__name__)

# 摘要固化模板：只描述“做什么”——压缩旧消息。不写“不许做什么”，
# 安全边界由服务端控制（摘要不进 system、长度受限、失败降级），不靠提示词。
_SUMMARY_TEMPLATE = (
    "把以下旧对话压缩成一段简短记事。保留对方提到的事实、需求与已确认的结论，"
    "按时间顺序组织，使用与原文相同的语言。"
)

_SUMMARY_LABEL = "（较早对话摘要，可能不完整）"


async def get_summary(conn: aiosqlite.Connection, conversation_id: str) -> dict | None:
    """读取已持久化的摘要；无则返回 None。

    返回 dict: {summary, upto_message_id, updated_at}。
    """
    with metrics.db_time("select_memory"):
        cur = await conn.execute(
            "SELECT summary, upto_message_id, updated_at FROM conversation_memory"
            " WHERE conversation_id = ?",
            (conversation_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    d = dict(row)
    if not d.get("summary"):
        return None
    return d


async def _fetch_old_messages(
    conn: aiosqlite.Connection,
    conversation_id: str,
    *,
    raw_window: int,
    upto_message_id: int | None,
) -> list[dict]:
    """取已滑出原文窗口、且 id 大于已摘要 upto_message_id 的旧消息（id ASC）。

    只摘要“滑出窗口”的旧消息：即排除最近 raw_window 条后剩下的较早消息。
    upto_message_id 之前的消息已被上一轮摘要覆盖，不重复进入摘要输入。
    """
    # 先取全部消息 id（由旧到新），用于界定“滑出窗口”的边界。
    cur = await conn.execute(
        "SELECT id, role, content FROM message WHERE conversation_id = ? ORDER BY id ASC",
        (conversation_id,),
    )
    rows = [dict(r) for r in await cur.fetchall()]
    if len(rows) <= raw_window:
        return []
    # 滑出窗口的旧消息 = 全部去掉最近 raw_window 条后的前缀
    old = rows[: len(rows) - raw_window]
    # 只取 id 大于已摘要 upto_message_id 的部分（单调递增，避免重复摘要）
    if upto_message_id is not None:
        old = [r for r in old if r["id"] > upto_message_id]
    return old


async def maybe_generate_summary(
    conn: aiosqlite.Connection,
    conversation_id: str,
) -> None:
    """检查是否需要重新摘要；满足条件则异步不阻塞——本函数同步返回。

    判断条件：消息总数跨越 N 的倍数，且滑出窗口的旧消息 id 大于已摘要 upto_message_id。
    本函数只做轻量读检查；真正的摘要调用由 schedule_summary 触发为后台任务。
    """
    if not settings.effective_memory_enabled:
        return
    n = settings.memory_summary_every_n
    cur = await conn.execute(
        "SELECT COUNT(*) AS c, MAX(id) AS max_id FROM message WHERE conversation_id = ?",
        (conversation_id,),
    )
    row = await cur.fetchone()
    count = row["c"] if row else 0
    max_id = row["max_id"] if row else 0
    if count < n + settings.memory_raw_window:
        # 消息不足：至少要 N 条滑出窗口之外，才值得摘要
        return
    mem = await get_summary(conn, conversation_id)
    upto = mem["upto_message_id"] if mem else 0
    if max_id <= upto:
        return
    # 有新滑出消息 → 触发后台摘要
    schedule_summary(conversation_id)


def schedule_summary(conversation_id: str) -> None:
    """把摘要生成作为后台 fire-and-forget 任务投递；失败静默降级。

    复用 service._background_tasks 强引用集合，确保不被 GC；shutdown_tasks 会取消。
    """
    import asyncio

    from app.chat import service as _service

    task = asyncio.create_task(_generate_summary_task(conversation_id))
    _background_tasks_local.add(task)
    task.add_done_callback(_background_tasks_local.discard)
    # 同时加入 service 集合，使 shutdown_tasks 能取消
    _service._background_tasks.add(task)
    task.add_done_callback(_service._background_tasks.discard)


# 本模块自有的强引用集合（与 service._background_tasks 并行；两者都 add 是安全的）
_background_tasks_local: set = set()


async def _generate_summary_task(conversation_id: str) -> None:
    """实际生成并持久化摘要；任何异常都静默吞掉（不阻塞聊天、不注入旧摘要）。"""
    try:
        await generate_summary(conversation_id)
    except Exception as exc:
        logger.warning("summary generation failed for %s: %s", conversation_id, exc)


async def generate_summary(conversation_id: str) -> str | None:
    """生成并持久化摘要；返回新摘要文本（失败返回 None）。

    流程：
    1. 取已滑出原文窗口、且 id 大于已摘要 upto_message_id 的旧消息。
    2. 调用 llm_client.chat()（非流式，便宜模型，小 max_tokens）压缩。
    3. UPSERT 到 conversation_memory，单调更新 upto_message_id。
    """
    if not settings.effective_memory_enabled:
        return None
    async with get_db() as conn:
        mem = await get_summary(conn, conversation_id)
        upto = mem["upto_message_id"] if mem else 0
        old = await _fetch_old_messages(
            conn,
            conversation_id,
            raw_window=settings.memory_raw_window,
            upto_message_id=upto,
        )
        if not old:
            return mem["summary"] if mem else None
        new_upto = max(r["id"] for r in old)
        # 构造摘要输入：system 模板 + 旧对话（role/content）
        prompt_messages = [
            {"role": "system", "content": _SUMMARY_TEMPLATE},
        ]
        for r in old:
            prompt_messages.append({"role": r["role"], "content": r["content"]})
        # 若已有旧摘要，作为前缀一并提供，保持摘要连续性
        prior = mem["summary"] if mem and mem.get("summary") else None
        if prior:
            prompt_messages.insert(
                1,
                {
                    "role": "user",
                    "content": f"已有的旧摘要：\n{prior}\n请在此基础上补充并重写为一段。",
                },
            )

        resp = await llm_client.chat(
            prompt_messages,
            model=settings.effective_memory_summary_model,
            temperature=0.2,
            max_tokens=settings.memory_summary_max_tokens,
        )
        summary = (resp.get("content") or "").strip()
        if not summary:
            return mem["summary"] if mem else None
        # 截断到字符预算（注入时再兜底截断一次，这里先控制存储大小）
        if len(summary) > settings.memory_summary_max_chars * 2:
            summary = summary[: settings.memory_summary_max_chars * 2]

        await _upsert_summary(conn, conversation_id, summary, new_upto)
        return summary


async def _upsert_summary(
    conn: aiosqlite.Connection,
    conversation_id: str,
    summary: str,
    upto_message_id: int,
) -> None:
    """UPSERT 摘要；upto_message_id 单调递增——仅当新值更大才覆盖。"""
    # 单调保护：读取当前 upto，仅在大于时才写入
    cur = await conn.execute(
        "SELECT upto_message_id FROM conversation_memory WHERE conversation_id = ?",
        (conversation_id,),
    )
    row = await cur.fetchone()
    if row is not None and row["upto_message_id"] >= upto_message_id:
        return
    now = utcnow_iso()
    if row is None:
        await run_write(
            conn,
            [(
                "INSERT INTO conversation_memory"
                " (conversation_id, summary, upto_message_id, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (conversation_id, summary, upto_message_id, now),
            )],
        )
    else:
        await run_write(
            conn,
            [(
                "UPDATE conversation_memory SET summary = ?, upto_message_id = ?,"
                " updated_at = ? WHERE conversation_id = ?",
                (summary, upto_message_id, now, conversation_id),
            )],
        )


def summary_to_message(summary: str) -> dict:
    """把摘要包装为固定位置的数据消息（role=system，标注“可能不完整”）。

    注意：虽 role=system，但这是**数据段**，不是权限指令——内容只描述历史，
    不含任何指令。放固定位置（system 之后、原文之前），由 build_context 控制语序。
    """
    capped = summary[: settings.memory_summary_max_chars]
    if len(summary) > settings.memory_summary_max_chars:
        capped += "…"
    return {
        "role": "system",
        "content": f"{_SUMMARY_LABEL}\n{capped}",
    }


def summary_token_cost(summary: str) -> int:
    """估算摘要消息占用 token（用于 build_context 预算扣除）。"""
    return estimate_tokens([summary_to_message(summary)])
