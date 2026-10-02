"""会话归属（ownership）测试：防止通过 ID 遍历访问他人会话。

每个会话绑定创建者的 user_id（取自 X-User-Id 头）；非本人访问统一返回 404，
不泄露“存在但非本人”的信息。
"""

import json
import pytest

from app.chat import service
from app.errors import ConversationNotFoundError
from app.schema import ChatRequest


async def _create_conv(client, user):
    async with client.stream(
        "POST",
        "/api/chat",
        json={"message": "hi"},
        headers={"X-User-Id": user},
    ) as res:
        raw = ""
        async for chunk in res.aiter_text():
            raw += chunk
    for frame in raw.split("\n\n"):
        if frame.startswith("event: done"):
            for line in frame.split("\n"):
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())["conversation_id"]
    raise AssertionError("未返回 done 事件")


async def test_other_user_cannot_read_conversation(client):
    conv_id = await _create_conv(client, "alice")
    res = await client.get(
        f"/api/chat/conversations/{conv_id}/messages",
        headers={"X-User-Id": "bob"},
    )
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "NOT_FOUND"


async def test_other_user_cannot_delete_conversation(client):
    conv_id = await _create_conv(client, "alice")
    res = await client.request(
        "DELETE",
        f"/api/chat/conversations/{conv_id}",
        headers={"X-User-Id": "bob"},
    )
    assert res.status_code == 404


async def test_owner_can_read_and_list(client):
    await _create_conv(client, "alice")
    res = await client.get("/api/chat/conversations", headers={"X-User-Id": "alice"})
    assert res.status_code == 200
    assert res.json()["conversations"]
    res_bob = await client.get("/api/chat/conversations", headers={"X-User-Id": "bob"})
    assert res_bob.status_code == 200
    assert res_bob.json()["conversations"] == []


async def test_service_rejects_other_user(db, monkeypatch):
    gen = await service.send_message(ChatRequest(message="hi"), db, "u1")
    _ = [c async for c in gen]
    cur = await db.execute("SELECT id FROM conversation")
    conv_id = (await cur.fetchone())["id"]

    with pytest.raises(ConversationNotFoundError):
        await service.get_messages(conv_id, db, "u2")
    with pytest.raises(ConversationNotFoundError):
        await service.delete_conversation(conv_id, db, "u2")

    msgs = await service.get_messages(conv_id, db, "u1")
    assert len(msgs) >= 2
