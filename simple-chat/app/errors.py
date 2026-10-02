"""统一错误类型与错误码枚举：对外错误体恒为 {"error": {"code", "message", "request_id"?}}。

ErrorCode 是全项目唯一的错误码字典：value 即对外 JSON 中的 code 字符串，
http_status / default_message 集中在此，避免散落。
"""

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """对外错误码；value 即 JSON 中 code 字段的字符串常量。

    必备成员：VALIDATION_ERROR / AUTH_ERROR / MODEL_UNAVAILABLE /
    CONTEXT_OVERFLOW / RATE_LIMITED / INTERNAL_ERROR / NOT_FOUND。
    METHOD_NOT_ALLOWED 为 HTTP 405 处理器额外成员，不在用户枚举清单内但共用此类。
    """

    VALIDATION_ERROR = "VALIDATION_ERROR"
    AUTH_ERROR = "AUTH_ERROR"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    CONTEXT_OVERFLOW = "CONTEXT_OVERFLOW"
    RATE_LIMITED = "RATE_LIMITED"
    NOT_FOUND = "NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    CONVERSATION_BUSY = "CONVERSATION_BUSY"

    @property
    def http_status(self) -> int:
        """每个错误码对应的 HTTP 状态码。"""
        return _HTTP_STATUS[self]

    @property
    def default_message(self) -> str:
        """每个错误码的默认面向用户文案。"""
        return _DEFAULT_MESSAGES[self]


_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.AUTH_ERROR: 500,
    ErrorCode.MODEL_UNAVAILABLE: 503,
    ErrorCode.CONTEXT_OVERFLOW: 413,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.METHOD_NOT_ALLOWED: 405,
    ErrorCode.CONVERSATION_BUSY: 409,
}

_DEFAULT_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.VALIDATION_ERROR: "请求参数不合法",
    ErrorCode.AUTH_ERROR: "上游模型鉴权失败，请检查服务端配置",
    ErrorCode.MODEL_UNAVAILABLE: "上游模型暂时不可用",
    ErrorCode.CONTEXT_OVERFLOW: "上下文长度超限",
    ErrorCode.RATE_LIMITED: "上游限流，请稍后重试",
    ErrorCode.NOT_FOUND: "资源不存在",
    ErrorCode.INTERNAL_ERROR: "服务内部错误",
    ErrorCode.METHOD_NOT_ALLOWED: "请求方法不被允许",
    ErrorCode.CONVERSATION_BUSY: "该会话正在生成回复，请稍候",
}


class AppError(Exception):
    """业务异常基类；路由抛出后由全局处理器转成统一错误体。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message or code.default_message)
        self.code = code
        self.status_code = code.http_status
        self.message = message or code.default_message
        self.request_id = request_id

    def to_body(self) -> dict[str, Any]:
        """HTTP 错误响应体：{"error": {"code", "message", "request_id"?}}。"""
        err: dict[str, Any] = {"code": self.code.value, "message": self.message}
        if self.request_id:
            err["request_id"] = self.request_id
        return {"error": err}

    def to_sse_data(self) -> dict[str, Any]:
        """SSE error 事件载荷：扁平 {code, message}（按 SSE 契约，不包 error 外壳）。"""
        return {"code": self.code.value, "message": self.message}


class ValidationError(AppError):
    """请求参数校验失败（422）。"""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(ErrorCode.VALIDATION_ERROR, message)


class NotFoundError(AppError):
    """资源不存在（404）。"""

    def __init__(self, resource: str = "资源", request_id: str | None = None) -> None:
        super().__init__(ErrorCode.NOT_FOUND, f"{resource}不存在", request_id)


class ConversationNotFoundError(NotFoundError):
    """会话不存在；保留旧名便于 service 层引用。"""

    def __init__(self, conversation_id: str) -> None:
        super().__init__(f"会话 {conversation_id}")


class ConversationBusyError(AppError):
    """会话已有进行中的流式请求（409）。"""

    def __init__(self, conversation_id: str) -> None:
        super().__init__(
            ErrorCode.CONVERSATION_BUSY,
            f"会话 {conversation_id} 正在生成回复，请稍候",
        )
