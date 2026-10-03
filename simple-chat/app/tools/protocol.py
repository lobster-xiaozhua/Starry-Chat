"""工具调用协议类型（ROADMAP v0.5 §4.3）。

结果信封强制约束：
- `is_error` 永远是 bool 且必出现；
- 成功必有 `result`、无 `error`；
- 失败只有 `error`、无 `result`；
- `meta` 必含 `duration_ms` 与 `truncated`。

这些模型不依赖 HTTP/运行时，可作为协议契约在测试中校验。
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict


# ── 错误码常量（与 app/errors.py 的 ErrorCode 同源理念：单一字典）──

class ToolErrorCode:
    """工具结果信封中的错误码字符串常量。

    工具错误码是独立的命名空间，不复用 HTTP ErrorCode（后者面向客户端，
    前者面向工具调用方/模型），但语义上保持可映射。
    """

    VALIDATION_ERROR = "VALIDATION_ERROR"          # 表达式非法 / 参数解析失败
    PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"           # 路径不在白名单根目录内
    UNSUPPORTED = "UNSUPPORTED"                     # 二进制文件等不支持的输入
    NOT_CONFIGURED = "NOT_CONFIGURED"              # web_search endpoint 未配置
    TIMEOUT = "TIMEOUT"                             # 超过 timeout_ms
    OUTPUT_TOO_LARGE = "OUTPUT_TOO_LARGE"          # 输出超过 max_output_bytes
    UNAUTHORIZED = "UNAUTHORIZED"                  # 工具被开关禁用
    INTERNAL_ERROR = "INTERNAL_ERROR"              # 沙盒/执行器内部故障


ToolName = Literal["web_search", "calculate", "read_file"]


class ToolCallRequest(TypedDict):
    """执行器内部请求（ROADMAP §4.2）。

    timeout_ms / max_output_bytes 由服务端强制，不采信模型传入的值。
    """

    call_id: str
    name: ToolName
    arguments: dict[str, Any]
    timeout_ms: int
    max_output_bytes: int


class ToolErrorBody(TypedDict):
    code: str
    message: str
    retryable: bool


class ToolMeta(TypedDict):
    duration_ms: int
    truncated: bool


class ToolResult(TypedDict):
    """工具结果信封（ROADMAP §4.3）。"""

    is_error: bool
    tool: ToolName
    result: Any | None           # 成功时必有；失败时缺省
    error: ToolErrorBody | None  # 失败时必有；成功时缺省
    meta: ToolMeta


# ── 构造器：保证信封约束的单一入口，避免散落手拼 ──


def ok_result(
    tool: ToolName,
    result: Any,
    duration_ms: int,
    *,
    truncated: bool = False,
) -> ToolResult:
    """构造成功信封：必有 result、无 error。"""
    return {
        "is_error": False,
        "tool": tool,
        "result": result,
        "error": None,
        "meta": {"duration_ms": max(0, int(duration_ms)), "truncated": bool(truncated)},
    }


def error_result(
    tool: ToolName,
    code: str,
    message: str,
    duration_ms: int,
    *,
    retryable: bool = False,
    truncated: bool = False,
) -> ToolResult:
    """构造失败信封：必有 error、无 result。"""
    return {
        "is_error": True,
        "tool": tool,
        "result": None,
        "error": {"code": str(code), "message": str(message), "retryable": bool(retryable)},
        "meta": {"duration_ms": max(0, int(duration_ms)), "truncated": bool(truncated)},
    }


def to_payload(result: ToolResult) -> dict[str, Any]:
    """转成写入消息历史的紧凑载荷：剔除 None 字段，便于上下文裁剪。

    成功：{is_error:false, tool, result, meta}
    失败：{is_error:true, tool, error, meta}
    meta 始终保留 duration_ms 与 truncated。
    """
    payload: dict[str, Any] = {
        "is_error": result["is_error"],
        "tool": result["tool"],
    }
    if result["is_error"]:
        payload["error"] = result["error"]
    else:
        payload["result"] = result["result"]
    payload["meta"] = result["meta"]
    return payload
