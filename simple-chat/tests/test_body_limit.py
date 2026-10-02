"""T4（PR-2 改动 5 方案 A）：chunked 请求体超限 → 413。

发送 Transfer-Encoding: chunked 的 2MB body（> 1MB 上限），
中间件流式累加超限后必须立即返回 413，且不把完整 body 读进内存。
"""

import pytest


async def test_chunked_body_limit(client):
    chunk = b"x" * (256 * 1024)  # 256KB

    async def gen():
        for _ in range(8):  # 8 * 256KB = 2MB > 1MB
            yield chunk

    res = await client.post(
        "/api/chat",
        content=gen(),
        headers={"Transfer-Encoding": "chunked", "Content-Type": "application/json"},
    )
    assert res.status_code == 413
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_normal_body_still_parses(client):
    """反例：未超限的正常请求体必须仍可被下游正常解析（回填 _body 后回放）。"""
    res = await client.post("/api/chat", json={"message": "hi"})
    assert res.status_code == 200
