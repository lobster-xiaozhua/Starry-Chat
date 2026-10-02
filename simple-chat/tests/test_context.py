"""上下文裁剪（build_context）单元测试：P0 — 上下文裁剪不保底。

直接调用 service.build_context，不经 HTTP；强制近似 tokenizer 使 token 估算确定。
覆盖：
  T1 当前用户消息必出现在 messages 末尾
  T2 超预算单条消息仍发送（可截断）
  T3 异常数据（连续两条 user）下角色严格交替
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

from app.chat import service  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import utcnow_iso  # noqa: E402
from app.llm import client as llm_client  # noqa: E402
import app.llm.tokenizer as _tok  # noqa: E402


@pytest.fixture(autouse=True)
def _approx_tokens(monkeypatch):
    """强制近似 tokenizer，使 token 估算与字符数成比例、跨环境确定。"""
    monkeypatch.setattr(_tok, "_encoder", None)
    monkeypatch.setattr(_tok, "_tried", True)


@pytest.fixture(autouse=True)
def _small_prompt(monkeypatch):
    """缩短 system prompt 且清零 max_response_tokens，便于精确控制预算。"""
    monkeypatch.setattr(llm_client, "SYSTEM_PROMPT", "sys")
    monkeypatch.setattr(settings, "max_response_tokens", 0)


async def _add_conv(db, conv_id):
    now = utcnow_iso()
    await db.execute(
        "INSERT INTO conversation (id,title,created_at,updated_at) VALUES (?,?,?,?)",
        (conv_id, "t", now, now),
    )
    await db.commit()


async def _insert(db, conv_id, role, content):
    now = utcnow_iso()
    await db.execute(
        "INSERT INTO message (conversation_id,role,content,tokens,created_at)"
        " VALUES (?,?,?,0,?)",
        (conv_id, role, content, now),
    )
    await db.commit()


async def test_current_user_message_always_in_context(db, monkeypatch):
    """T1: 当前用户消息必出现在发给模型的 messages 末尾。"""
    conv_id = "ctx-t1"
    await _add_conv(db, conv_id)
    for i in range(19):
        role = "user" if i % 2 == 0 else "assistant"
        await _insert(db, conv_id, role, f"h{i}")
    current = "Q" * 9000  # 近似下 ≈ 3000 token
    await _insert(db, conv_id, "user", current)

    ctx = await service.build_context(conv_id, db, 3062, current)

    assert ctx[0]["role"] == "system"
    assert ctx[-1]["role"] == "user"
    assert current in ctx[-1]["content"]


async def test_first_message_exceeds_budget_still_sent(db, monkeypatch):
    """T2: 单条用户消息 8000 token 超预算，仍发送（可截断），且不含旧历史。"""
    conv_id = "ctx-t2"
    await _add_conv(db, conv_id)
    for i in range(3):
        role = "user" if i % 2 == 0 else "assistant"
        await _insert(db, conv_id, role, f"old{i}")
    current = "X" * 24000  # 近似下 ≈ 8000 token
    await _insert(db, conv_id, "user", current)

    ctx = await service.build_context(conv_id, db, 1000, current)

    users = [m for m in ctx if m["role"] == "user"]
    assert users, "超预算时仍应保留当前用户消息"
    assert ctx[-1]["role"] == "user"
    assert ctx[-1]["content"], "用户消息内容不应为空"
    # 旧历史不应出现
    assert all("old" not in m["content"] for m in ctx if m["role"] != "system")
    # 单条超限应触发截断标记
    assert "[内容已截断]" in ctx[-1]["content"]


async def test_role_alternation(db, monkeypatch):
    """T3: DB 中存在连续两条 user 消息（异常数据），messages 不存在相邻同 role。"""
    conv_id = "ctx-t3"
    await _add_conv(db, conv_id)
    await _insert(db, conv_id, "user", "oldA")
    await _insert(db, conv_id, "user", "newB")

    ctx = await service.build_context(conv_id, db, 256000, "newB")

    roles = [m["role"] for m in ctx if m["role"] != "system"]
    for a, b in zip(roles, roles[1:]):
        assert a != b, f"相邻同 role: {a}"
    assert roles[-1] == "user"
    assert any(m["content"] == "newB" for m in ctx if m["role"] == "user")
    assert all(m["content"] != "oldA" for m in ctx if m["role"] == "user")
