"""请求体大小限制中间件（改动 5 方案 A）：流式读取并累加，超限立即 413。

实现要点：
- Content-Length 快速路径：带 CL 且超限直接拒绝，不读 body（最省资源）。
- 分块请求（无 CL / Transfer-Encoding: chunked）：经 request._receive 逐块读取累加，
  累计超过 max_bytes 立即返回 413，不等完整 body（防内存放大）。
- 读完的 body 回填 request._body：Starlette BaseHTTPMiddleware 的 _CachedRequest
  会在下游读取时把缓存的 body 原样回放（wrapped_receive state 3），
  因此 FastAPI/Pydantic 仍能正常解析请求体，不会出现“读一次就没了”。
- 仅限制“请求体”。SSE 是服务端流出的“响应”，本中间件从不触碰响应流。
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.errors import ErrorCode

_BODY_METHODS = {"POST", "PUT", "PATCH"}


def _too_large(max_bytes: int) -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content={
            "error": {
                "code": ErrorCode.VALIDATION_ERROR.value,
                "message": f"请求体超过大小上限 {max_bytes} 字节",
            }
        },
    )


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """流式累加请求体并限制大小（默认 1MB），超限立即 413。"""

    def __init__(self, app, max_bytes: int | None = None) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes if max_bytes is not None else settings.max_body_bytes

    async def _read_limited(self, request: Request) -> tuple[bytes, bool]:
        """逐块读取请求体；返回 (body, 是否超限)。超限时立即停止读取。"""
        chunks: list[bytes] = []
        total = 0
        receive = request._receive  # noqa: SLF001 - Starlette 仅此通道可读请求流
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            chunk = message.get("body") or b""
            chunks.append(chunk)
            total += len(chunk)
            if total > self.max_bytes:
                return b"".join(chunks), True
            if not message.get("more_body", False):
                break
        return b"".join(chunks), False

    async def dispatch(self, request: Request, call_next):
        if request.method in _BODY_METHODS:
            cl = request.headers.get("content-length")
            if cl and cl.isdigit() and int(cl) > self.max_bytes:
                return _too_large(self.max_bytes)
            body, exceeded = await self._read_limited(request)
            if exceeded:
                return _too_large(self.max_bytes)
            request._body = body  # noqa: SLF001 - 供 wrapped_receive 回放给下游
        return await call_next(request)


__all__ = ["BodySizeLimitMiddleware"]
