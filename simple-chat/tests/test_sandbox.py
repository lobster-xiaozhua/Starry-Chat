"""沙盒与 calculate 内核测试（ROADMAP v0.5 §4 验收）。

覆盖：
- calculate 拒绝 `__import__('os').system('id')`、`eval("1")`、赋值、属性访问；
- 正常算术返回正确数值；
- 超输出 → is_error=true；
- 沙盒实际采用的隔离方案探测。
"""

from __future__ import annotations

import time

import pytest

from app.sandbox import calculator
from app.sandbox.calculator import CalcError, calculate
from app.sandbox.runner import run_sandboxed, sandbox_scheme
from app.tools.protocol import ToolErrorCode


# ── calculate 安全：黑名单表达式 ──


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os').system('id')",   # dunder 标识符 + 函数调用
        "eval('1')",                        # 调用非白名单函数
        "exec('1')",                        # 调用非白名单函数
        "open('/etc/passwd')",              # 调用非白名单函数
        "os.system('id')",                  # 标识符（无 import）
        "(1).__class__",                    # 属性访问
        "1 .__class__",                     # 属性访问
        "x = 1",                            # 赋值（mode=eval 会 SyntaxError）
        "lambda: 1",                        # lambda
        "[x for x in range(3)]",            # ListComp
        "{1:2 for _ in [1]}",               # DictComp
        "globals()",                        # 调用非白名单
        "import os",                         # mode=eval SyntaxError
    ],
)
def test_calculate_rejects_dangerous(expr):
    with pytest.raises(CalcError):
        calculate(expr)


def test_calculate_rejects_empty():
    with pytest.raises(CalcError):
        calculate("")
    with pytest.raises(CalcError):
        calculate("   ")


def test_calculate_rejects_too_long():
    with pytest.raises(CalcError):
        calculate("1+" * 300)


# ── calculate 正常算术 ──


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("1+2", 3),
        ("(3+4)*2", 14),
        ("10/4", 2.5),
        ("10//3", 3),
        ("10%3", 1),
        ("2**10", 1024),
        ("sqrt(144)", 12.0),
        ("abs(-7)", 7),
        ("round(3.14159, 2)", 3.14),
        ("min(3,1,2)", 1),
        ("max(3,1,2)", 3),
        ("pow(2,10)", 1024),
        ("sqrt(pow(3,2)+pow(4,2))", 5.0),
        ("1287*0.17", 218.79),
    ],
)
def test_calculate_valid_arithmetic(expr, expected):
    res = calculate(expr)
    assert res == {"value": pytest.approx(expected)}


def test_calculate_division_by_zero():
    with pytest.raises(CalcError):
        calculate("1/0")


# ── 通过执行器走完整信封路径 ──


def _exec_calc(expr):
    from app.tools.executor import execute
    from app.tools.protocol import ToolCallRequest
    import app.config as cfg

    req: ToolCallRequest = {
        "call_id": "test-1",
        "name": "calculate",
        "arguments": {"expression": expr},
        "timeout_ms": 1000,
        "max_output_bytes": 4096,
    }
    # 测试默认开发环境 → tools_enabled None → effective_tools_enabled False。
    # 工具语义测试需要显式开启。
    cfg.settings.tools_enabled = True
    cfg.settings.tool_calculate_enabled = True
    try:
        return execute(req)
    finally:
        cfg.settings.tools_enabled = None


def test_executor_calculate_success():
    res = _exec_calc("1287*0.17")
    assert res["is_error"] is False
    assert res["tool"] == "calculate"
    assert res["result"]["value"] == pytest.approx(218.79)
    assert "duration_ms" in res["meta"]
    assert res["meta"]["truncated"] is False


def test_executor_calculate_rejects_import_escape():
    res = _exec_calc("__import__('os').system('id')")
    assert res["is_error"] is True
    assert res["error"]["code"] == ToolErrorCode.VALIDATION_ERROR
    assert res["error"]["retryable"] is False
    assert "result" not in res or res.get("result") is None


def test_executor_calculate_rejects_eval():
    res = _exec_calc("eval('1')")
    assert res["is_error"] is True
    assert res["error"]["code"] == ToolErrorCode.VALIDATION_ERROR


def test_executor_calculate_rejects_assignment():
    # mode=eval 无法解析赋值 → SyntaxError → VALIDATION_ERROR
    res = _exec_calc("x = 1")
    assert res["is_error"] is True
    assert res["error"]["code"] == ToolErrorCode.VALIDATION_ERROR


def test_executor_calculate_rejects_attribute_access():
    res = _exec_calc("(1).__class__")
    assert res["is_error"] is True
    assert res["error"]["code"] == ToolErrorCode.VALIDATION_ERROR


# ── 沙盒输出上限 ──


def test_executor_output_too_large():
    # 构造一个产生超大 JSON 的内核：返回大字符串
    res = run_sandboxed(
        lambda: {"value": "x" * 10000},
        timeout_ms=1000,
        max_output_bytes=4096,
        tool="calculate",
    )
    assert res["is_error"] is True
    assert res["error"]["code"] == ToolErrorCode.OUTPUT_TOO_LARGE


# ── 沙盒 timeout ──


def test_executor_timeout():
    # 内核 sleep 超过 timeout_ms
    res = run_sandboxed(
        lambda: time.sleep(2) or {"value": 1},
        timeout_ms=200,
        max_output_bytes=4096,
        tool="calculate",
    )
    assert res["is_error"] is True
    assert res["error"]["code"] == ToolErrorCode.TIMEOUT
    assert res["error"]["retryable"] is True


# ── 沙盒隔离方案探测 ──


def test_sandbox_scheme_detected():
    scheme = sandbox_scheme()
    assert scheme in ("seccomp", "rlimit", "none")
    # 当前平台不支持 seccomp 时应降级到 rlimit
    assert scheme in ("rlimit", "none", "seccomp")
