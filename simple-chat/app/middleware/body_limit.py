"""请求体大小限制中间件：超出 max_body_bytes 直接返回 413。

说明：
- 基于 Content-Length 头做快速拒绝（最常见、最省资源）。
- 对未携带 Content-Length 的分块请求（Transfer-Encoding: chunked），本中间件不逐字节计数；
  生产环境务必在反向代理层（Nginx client_max_body_size / 网关 body limits）再做一道硬限制。
- 默认值 1MB，可通过 settings.max_body_bytes 调整。
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.errors import ErrorCode

_BODY_METHODS = {"POST", "PUT", "PATCH"}


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """拒绝超大请求体（默认 1MB）。详见模块 docstring。"""

    def __init__(self, app, max_bytes: int | None = None) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes if max_bytes is not None else settings.max_body_bytes

    async def dispatch(self, request: Request, call_next):
        if request.method in _BODY_METHODS:
            cl = request.headers.get("content-length")
            if cl and cl.isdigit() and int(cl) > self.max_bytes:
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": {
                            "code": ErrorCode.VALIDATION_ERROR.value,
                            "message": f"请求体超过大小上限 {self.max_bytes} 字节",
                        }
                    },
                )
        return await call_next(request)


__all__ = ["BodySizeLimitMiddleware"]
