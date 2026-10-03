"""健康检查测试：/healthz（不查 DB）与 /readyz（DB + Key + 模型可达）。"""

import pytest

from app.llm import client as llm_client
from app.errors import ErrorCode


async def test_healthz_ok(client):
    res = await client.get("/healthz")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    from app import __version__
    assert body["version"] == __version__
    assert "ts" in body


async def test_readyz_ok(client):
    res = await client.get("/readyz")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["checks"]["db"] == "ok"
    assert body["checks"]["llm_key"] == "ok"
    assert body["checks"]["model"] == "ok"


async def test_readyz_missing_api_key(client, monkeypatch):
    monkeypatch.setattr(llm_client.settings, "llm_api_key", "")
    res = await client.get("/readyz")
    assert res.status_code == 503
    body = res.json()
    assert body["status"] == "unavailable"
    assert body["reason"] == "llm_key_missing"


async def test_readyz_model_unreachable(client, monkeypatch):
    async def _boom():
        raise llm_client.LLMUnavailable()

    monkeypatch.setattr(llm_client, "ping", _boom)
    res = await client.get("/readyz")
    assert res.status_code == 503
    body = res.json()
    assert body["reason"] == "model_unreachable"


async def test_readyz_returns_error_envelope_on_failure(client, monkeypatch):
    monkeypatch.setattr(llm_client.settings, "llm_api_key", "")
    res = await client.get("/readyz")
    # readyz 用结构化健康检查体，而非统一 error 信封；这里仅确认含 reason/error 字段
    body = res.json()
    assert "reason" in body
    assert "error" in body
    # 同时确认失败不会误报为 ok
    assert body["status"] != "ok"
