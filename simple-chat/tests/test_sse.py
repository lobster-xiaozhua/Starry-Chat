"""T1（PR-2 改动 1）：SSE 响应头断言。

StreamingResponse 必须携带反代友好的头：
- X-Accel-Buffering: no                    → 禁用 Nginx 缓冲，保证逐字推送
- Content-Type: text/event-stream; charset → 客户端按 UTF-8 解码
- Cache-Control: no-cache, no-transform    → 禁止中间代理缓存/转码
- Connection: keep-alive                   → 长连接，降低首 token 延迟
"""

import pytest


async def test_sse_headers(client):
    res = await client.post("/api/chat", json={"message": "hi", "stream": True})
    assert res.status_code == 200
    assert res.headers.get("x-accel-buffering") == "no"
    ct = res.headers.get("content-type", "")
    assert ct.startswith("text/event-stream")
    assert "charset=utf-8" in ct.lower()
    assert res.headers.get("cache-control") == "no-cache, no-transform"
    assert res.headers.get("connection") == "keep-alive"
