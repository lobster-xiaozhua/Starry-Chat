"""v0.5 / v0.6 integration: tools loop and RAG injection wired into chat."""

import json

from app.llm import client as llm_client


class _FakeDelta:
    def __init__(self, text):
        self.content = text


class _FakeChoice:
    def __init__(self, text):
        self.delta = _FakeDelta(text)
        self.finish_reason = "stop"


class _Chunk:
    def __init__(self, text):
        self.choices = [_FakeChoice(text)]


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for t in self._chunks:
            yield _Chunk(t)


class _Msg:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, id, name, arguments):
        self.id = id
        self.function = _Fn(name, arguments)


class _Choice:
    def __init__(self, msg):
        self.message = msg
        self.finish_reason = "stop"


class _Usage:
    prompt_tokens = 1
    completion_tokens = 1
    total_tokens = 2


class _NonStream:
    def __init__(self, msg):
        self.choices = [_Choice(msg)]
        self.usage = _Usage()


class _Completions:
    def __init__(self, responder):
        self._responder = responder

    async def create(self, **kwargs):
        return self._responder(kwargs)


class _FakeClient:
    def __init__(self, responder):
        self._responder = responder
        self.chat = type("C", (), {"completions": _Completions(responder)})()


def _tool_loop_responder():
    state = {"n": 0}

    def responder(kwargs):
        if kwargs.get("stream"):
            return _Stream(["the answer is 4."])
        state["n"] += 1
        if state["n"] == 1:
            tc = _ToolCall("call_1", "calculate", json.dumps({"expression": "2+2"}))
            return _NonStream(_Msg(content="", tool_calls=[tc]))
        return _NonStream(_Msg(content="the answer is 4."))

    return responder


def _record_responder(captured):
    def responder(kwargs):
        captured.append(kwargs.get("messages"))
        return _Stream(["answered from reference."])
    return responder


async def test_tools_loop_runs_and_persists(client, db, monkeypatch):
    from app.config import settings as s
    monkeypatch.setattr(s, "tools_enabled", True)
    monkeypatch.setattr(llm_client, "_client", _FakeClient(_tool_loop_responder()))

    resp = await client.post("/api/chat", json={"message": "what is 2+2", "stream": False})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "4" in body["message"]["content"], body

    conv_id = body["conversation_id"]
    msgs = await client.get(f"/api/chat/conversations/{conv_id}/messages")
    roles = [m["role"] for m in msgs.json()["messages"]]
    assert "tool" in roles, roles
    tool_msg = next(m for m in msgs.json()["messages"] if m["role"] == "tool")
    payload = json.loads(tool_msg["content"])
    assert payload["tool"] == "calculate"
    assert payload["result"]["value"] == 4, payload


async def test_rag_context_injected(client, db, monkeypatch, tmp_path):
    from app.config import settings as s
    from app.rag import indexing

    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    (doc_dir / "notes.md").write_text(
        "# star map\nSTARRY_UNIQUE_MARKER_12345 this is a private note about star coordinates.\n",
        encoding="utf-8",
    )
    await indexing.reindex(db, [str(doc_dir)])
    monkeypatch.setattr(s, "rag_enabled", True)

    captured = []
    monkeypatch.setattr(llm_client, "_client", _FakeClient(_record_responder(captured)))

    resp = await client.post(
        "/api/chat", json={"message": "what is STARRY_UNIQUE_MARKER_12345", "stream": False}
    )
    assert resp.status_code == 200, resp.text

    assert captured, "model was never called"
    last_messages = captured[-1]
    rag_text = "\n".join(
        m.get("content", "") for m in last_messages if m.get("role") == "system"
    )
    assert "STARRY_UNIQUE_MARKER_12345" in rag_text, rag_text
    assert "[来源:" in rag_text, rag_text
