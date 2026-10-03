"""工具执行器：name → 内核分发，强制 timeout_ms / max_output_bytes（ROADMAP §4.2）。

执行器是工具调用的唯一入口：
- 校验请求（name 在白名单、参数齐全、工具开关启用）；
- 取 ROADMAP §4.2 表中的固定策略（不采信模型/调用方传入的 timeout/output）；
- 预校验透出受控精确 code（VALIDATION_ERROR / PATH_NOT_ALLOWED /
  UNSUPPORTED / NOT_CONFIGURED）；
- 通过预校验后在沙盒内运行内核，由沙盒兜底 timeout/输出上限并归一化
  TIMEOUT / OUTPUT_TOO_LARGE / INTERNAL_ERROR。
"""

from __future__ import annotations

import ast as _ast
import os as _os
import time as _time
from typing import Any

from app.config import Settings, settings as _settings
from app.sandbox.calculator import CalcError, calculate
from app.sandbox.calculator import _validate as _calc_validate
from app.sandbox.reader import ReadFileError, read_file
from app.sandbox.reader import _is_denied, _resolve_path
from app.sandbox.runner import run_sandboxed
from app.tools.definitions import TOOL_POLICIES
from app.tools.protocol import (
    ToolCallRequest,
    ToolErrorCode,
    ToolResult,
    error_result,
    ok_result,
    to_payload,
)


def _settings_obj(explicit: Settings | None) -> Settings:
    return explicit if explicit is not None else _settings


def _check_tool_enabled(name: str, cfg: Settings) -> str | None:
    """工具开关未启用时返回错误码，否则 None。"""
    if not cfg.effective_tools_enabled:
        return ToolErrorCode.UNAUTHORIZED
    mapping = {
        "calculate": cfg.tool_calculate_enabled,
        "read_file": cfg.tool_read_file_enabled,
        "web_search": cfg.tool_web_search_enabled,
    }
    if not mapping.get(name, False):
        return ToolErrorCode.UNAUTHORIZED
    return None


def _validate_arguments(name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """参数 schema 校验（最小手写校验，不引入 jsonschema 依赖）。"""
    if not isinstance(arguments, dict):
        return {}, ToolErrorCode.VALIDATION_ERROR
    if name == "calculate":
        expr = arguments.get("expression")
        if not isinstance(expr, str) or not expr.strip():
            return {}, ToolErrorCode.VALIDATION_ERROR
        return {"expression": expr}, None
    if name == "read_file":
        path = arguments.get("path")
        if not isinstance(path, str) or not path.strip():
            return {}, ToolErrorCode.VALIDATION_ERROR
        mb = arguments.get("max_bytes", 65536)
        if not isinstance(mb, int) or isinstance(mb, bool) or mb < 1:
            return {}, ToolErrorCode.VALIDATION_ERROR
        return {"path": path, "max_bytes": min(mb, 262144)}, None
    if name == "web_search":
        q = arguments.get("query")
        if not isinstance(q, str) or not q.strip():
            return {}, ToolErrorCode.VALIDATION_ERROR
        mr = arguments.get("max_results", 3)
        if not isinstance(mr, int) or isinstance(mr, bool) or mr < 1:
            return {}, ToolErrorCode.VALIDATION_ERROR
        return {"query": q, "max_results": min(mr, 5)}, None
    return {}, ToolErrorCode.VALIDATION_ERROR


def _web_search_kernel(query: str, max_results: int, cfg: Settings) -> dict[str, Any]:
    """web_search MVI：受控 egress broker 占位实现。

    - endpoint 未配置 → NOT_CONFIGURED（在预校验阶段返回）；
    - endpoint 配置时通过 httpx 调用 broker（broker 只接受 query +
      max_results，不接受任意 URL）。本 MVI 阶段不实际联网，返回占位结构。
    真实 broker 接入由集成阶段在 service 层完成（避免与并行 agent 冲突）。
    """
    return {
        "query": query,
        "max_results": max_results,
        "results": [],
        "note": "egress broker configured; MVI placeholder not networked",
    }


def _precheck(name: str, args: dict[str, Any], cfg: Settings) -> str | None:
    """纯校验：不执行可能慢/大的操作，只做结构与权限校验。

    返回错误码字符串或 None。失败时 executor 直接构造精确 code 信封，
    不进入沙盒。
    """
    if name == "calculate":
        expr = args["expression"]
        try:
            tree = _ast.parse(expr.strip(), mode="eval")
            _calc_validate(tree)
        except CalcError as exc:
            return exc.code
        except SyntaxError:
            return ToolErrorCode.VALIDATION_ERROR
        return None
    if name == "read_file":
        try:
            resolved = _resolve_path(args["path"])
        except ReadFileError as exc:
            return exc.code
        base = _os.path.basename(resolved)
        if _is_denied(base) or _is_denied(resolved):
            return ToolErrorCode.UNSUPPORTED
        return None
    if name == "web_search":
        if not (cfg.tool_web_search_endpoint or "").strip():
            return ToolErrorCode.NOT_CONFIGURED
        return None
    return ToolErrorCode.VALIDATION_ERROR


_PRECHECK_MESSAGES: dict[str, str] = {
    ToolErrorCode.VALIDATION_ERROR: "表达式或参数非法",
    ToolErrorCode.PATH_NOT_ALLOWED: "路径不在允许目录内",
    ToolErrorCode.UNSUPPORTED: "该文件类型或操作不支持",
    ToolErrorCode.NOT_CONFIGURED: "工具出口代理未配置",
}


def execute(request: ToolCallRequest, *, settings: Settings | None = None) -> ToolResult:
    """执行一次工具调用，返回结果信封。

    对齐 ROADMAP §4.2/§4.3：
    - timeout_ms / max_output_bytes 取 TOOL_POLICIES 固定值，不采信 request；
    - 受控失败在预校验阶段返回精确 code；
    - TIMEOUT / OUTPUT_TOO_LARGE / INTERNAL_ERROR 由沙盒归一化。
    """
    cfg = _settings_obj(settings)
    name = request.get("name")
    if name not in TOOL_POLICIES:
        return error_result(
            "web_search",  # 占位 tool 名（不会出现在成功路径）
            ToolErrorCode.VALIDATION_ERROR,
            f"未知工具 {name!r}",
            duration_ms=0,
        )
    policy = TOOL_POLICIES[name]

    disabled = _check_tool_enabled(name, cfg)
    if disabled is not None:
        return error_result(
            name,  # type: ignore[arg-type]
            disabled,
            f"工具 {name} 未启用",
            duration_ms=0,
            retryable=False,
        )

    args, err = _validate_arguments(name, request.get("arguments", {}))
    if err is not None:
        return error_result(
            name,  # type: ignore[arg-type]
            err,
            "参数校验失败",
            duration_ms=0,
            retryable=False,
        )

    # 预校验：受控精确 code
    pre_err = _precheck(name, args, cfg)
    if pre_err is not None:
        return error_result(
            name,  # type: ignore[arg-type]
            pre_err,
            _PRECHECK_MESSAGES.get(pre_err, "工具校验失败"),
            duration_ms=0,
            retryable=False,
        )

    # 通过预校验，进入沙盒执行
    if name == "calculate":
        kernel = lambda: calculate(args["expression"])
    elif name == "read_file":
        kernel = lambda: read_file(args["path"], args["max_bytes"])
    else:  # web_search
        kernel = lambda: _web_search_kernel(args["query"], args["max_results"], cfg)

    started = _time.perf_counter()
    duration = lambda: int((_time.perf_counter() - started) * 1000)

    import json as _json
    timeout_s = max(0.001, policy["timeout_ms"] / 1000.0)

    result_box: dict[str, Any] = {}

    def worker() -> None:
        try:
            result_box["value"] = kernel()
        except BaseException as exc:  # noqa: BLE001
            result_box["error"] = exc

    import threading as _threading
    t = _threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=timeout_s)

    if t.is_alive():
        return error_result(
            name,  # type: ignore[arg-type]
            ToolErrorCode.TIMEOUT,
            f"工具执行超过 {policy['timeout_ms']}ms 上限",
            duration_ms=duration(),
            retryable=True,
        )
    if "error" in result_box:
        exc = result_box["error"]
        if isinstance(exc, CalcError):
            return error_result(
                name,  # type: ignore[arg-type]
                exc.code,
                str(exc),
                duration_ms=duration(),
                retryable=False,
            )
        if isinstance(exc, ReadFileError):
            return error_result(
                name,  # type: ignore[arg-type]
                exc.code,
                str(exc),
                duration_ms=duration(),
                retryable=False,
            )
        return error_result(
            name,  # type: ignore[arg-type]
            ToolErrorCode.INTERNAL_ERROR,
            f"工具内部故障: {type(exc).__name__}",
            duration_ms=duration(),
            retryable=False,
        )
    if "value" not in result_box:
        return error_result(
            name,  # type: ignore[arg-type]
            ToolErrorCode.INTERNAL_ERROR,
            "沙盒未返回结果",
            duration_ms=duration(),
            retryable=False,
        )

    raw = result_box["value"]
    try:
        payload = _json.dumps(raw, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        payload = str(raw)
    if len(payload.encode("utf-8")) > policy["max_output_bytes"]:
        return error_result(
            name,  # type: ignore[arg-type]
            ToolErrorCode.OUTPUT_TOO_LARGE,
            f"工具输出超过 {policy['max_output_bytes']} 字节上限",
            duration_ms=duration(),
            retryable=False,
        )

    truncated = False
    if isinstance(raw, dict) and isinstance(raw.get("truncated"), bool):
        truncated = raw["truncated"]
    return ok_result(name, raw, duration_ms=duration(), truncated=truncated)  # type: ignore[arg-type]


__all__ = ["execute", "to_payload"]
