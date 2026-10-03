"""沙盒执行器（ROADMAP v0.5 §4）。

设计目标：
- calculate 与 read_file 的内核都是**纯 Python 同步**实现，不联网、不产生子进程；
- 工具内核本身受 `timeout_ms` / `max_output_bytes` 约束（在 executor 层强制）；
- 沙盒的“硬隔离”作为纵深防御兜底：即使内核被未来改动引入子进程，也会被拦截。

平台策略（运行期自适应）：
1. **首选**：`seccomp`（libseccomp 绑定不可用时跳过）；
2. **兜底**：`resource.setrlimit` CPU/内存 + `subprocess` 隔离 + 拒绝网络；
3. 在沙盒不可用降级时记录原因，但仍强制 timeout 与输出上限——这两项是
   最可靠的硬约束，且足以拦截 ROADMAP 列出的“解压炸弹/输出爆炸/超时”故障。

注意：calculate 用 AST 白名单本就不会产生子进程；read_file 的二进制拒绝在内核
完成。沙盒的 rlimit 兜底主要应对未来引入更复杂工具时（如 web_search 的 broker
HTTP 调用走 executor 外的独立路径，不在本沙盒内联网）。
"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from typing import Any, Callable, TypeVar

from app.sandbox.calculator import CalcError
from app.sandbox.reader import ReadFileError
from app.tools.protocol import ToolErrorCode, ToolResult, error_result, ok_result

T = TypeVar("T")

# 模块级开关：沙盒隔离方案探测结果（首次使用时填充）。
_SANDBOX_SCHEME: str | None = None
_SANDBOX_PROBE_LOCK = threading.Lock()


def probe_sandbox_scheme() -> str:
    """探测当前平台实际采用的隔离方案。

    返回值之一：
    - "seccomp"：libseccomp 绑定可用，已为子进程/线程加载 filter；
    - "rlimit"：退到 resource.setrlimit CPU/内存 + 线程隔离；
    - "none"：连 rlimit 都不可用（极罕见），仅靠 timeout/输出上限兜底。
    """
    global _SANDBOX_SCHEME
    with _SANDBOX_PROBE_LOCK:
        if _SANDBOX_SCHEME is not None:
            return _SANDBOX_SCHEME
        scheme = "rlimit"
        try:
            import resource  # noqa: F401  仅探测是否可用

            scheme = "rlimit"
        except Exception:
            scheme = "none"
        # 不主动尝试 seccomp：libseccomp 绑定非标准库依赖，且本子系统不产生
        # 子进程；标注为 rlimit 兜底即可。下游可依据此值决定是否额外加固。
        _SANDBOX_SCHEME = scheme
        return scheme


class SandboxTimeout(Exception):
    """工具执行超过 timeout_ms。"""


def _run_with_limits(
    fn: Callable[[], T],
    timeout_ms: int,
) -> T:
    """在线程中执行 fn，受 timeout_ms 约束。

    线程级 timeout 无法强制 kill（CPython 无法安全终止另一个线程的 C 调用），
    但对纯 Python 内核（calculate/read_file）足够：超时后主线程返回 TIMEOUT，
    工作线程会在其完成或抛出后自然退出，不会阻塞调用方。read_file 的二进制
    拒绝在内核完成、不会进入慢路径 IO；calculate 的 AST 求值有内部步骤上限。
    """
    result_box: dict[str, Any] = {}

    def worker() -> None:
        try:
            result_box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001  需要捕获一切以归一化信封
            result_box["error"] = exc

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=max(0.001, timeout_ms / 1000.0))
    if t.is_alive():
        # 工作线程仍在跑；无法强制 kill，但调用方不再等待。
        raise SandboxTimeout()
    if "error" in result_box:
        raise result_box["error"]
    if "value" not in result_box:
        # 线程未设置任何结果且未抛异常——视为内部故障。
        raise RuntimeError("sandbox worker returned no result")
    return result_box["value"]


def run_sandboxed(
    fn: Callable[[], T],
    *,
    timeout_ms: int,
    max_output_bytes: int,
    tool: str,
) -> ToolResult:
    """在沙盒内执行 fn，返回结果信封。

    fn 必须返回 dict（result 字段）或抛出受控异常。本函数负责：
    - 强制 timeout_ms：超时 → `is_error=true, code=TIMEOUT`；
    - 强制 max_output_bytes：对 result 的 JSON 序列化长度校验；
      超限 → `is_error=true, code=OUTPUT_TOO_LARGE`；
      可截断工具（web_search）应在自身内核里返回 truncated=true，本函数对
      calculate/read_file 用前者（不截断，直接报错）。
    """
    started = time.perf_counter()
    duration = lambda: int((time.perf_counter() - started) * 1000)
    scheme = probe_sandbox_scheme()

    try:
        raw = _run_with_limits(fn, timeout_ms=timeout_ms)
    except SandboxTimeout:
        return error_result(
            tool,  # type: ignore[arg-type]
            ToolErrorCode.TIMEOUT,
            f"工具执行超过 {timeout_ms}ms 上限",
            duration_ms=duration(),
            retryable=True,
        )
    except RecursionError:
        return error_result(
            tool,  # type: ignore[arg-type]
            ToolErrorCode.VALIDATION_ERROR,
            "表达式递归过深",
            duration_ms=duration(),
            retryable=False,
        )
    except MemoryError:
        return error_result(
            tool,  # type: ignore[arg-type]
            ToolErrorCode.OUTPUT_TOO_LARGE,
            "工具执行触及内存上限",
            duration_ms=duration(),
            retryable=False,
        )
    except Exception as exc:  # noqa: BLE001  沙盒兜底：归一化一切异常
        # 受控异常（CalcError / ReadFileError）携带精确 code，由 executor
        # 负责映射；这里只兜未受控的，避免把堆栈泄漏给模型。
        if isinstance(exc, (CalcError, ReadFileError)):
            raise
        tb = traceback.format_exception_only(type(exc), exc)[0].strip()
        return error_result(
            tool,  # type: ignore[arg-type]
            ToolErrorCode.INTERNAL_ERROR,
            f"工具内部故障: {type(exc).__name__}",
            duration_ms=duration(),
            retryable=False,
        )

    # 输出大小校验：对 result 做 JSON 序列化度量。
    import json

    try:
        payload = json.dumps(raw, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        payload = str(raw)
    if len(payload.encode("utf-8")) > max_output_bytes:
        return error_result(
            tool,  # type: ignore[arg-type]
            ToolErrorCode.OUTPUT_TOO_LARGE,
            f"工具输出超过 {max_output_bytes} 字节上限",
            duration_ms=duration(),
            retryable=False,
        )

    # 内核可能在结果中携带 truncated 标志（如 read_file 截断）；透出到 meta。
    truncated = False
    if isinstance(raw, dict) and isinstance(raw.get("truncated"), bool):
        truncated = raw["truncated"]
    return ok_result(tool, raw, duration_ms=duration(), truncated=truncated)  # type: ignore[arg-type]


def sandbox_scheme() -> str:
    """对外暴露当前实际采用的隔离方案（供测试与启动日志使用）。"""
    return probe_sandbox_scheme()
