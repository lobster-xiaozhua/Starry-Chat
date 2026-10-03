"""AST 白名单求值器：calculate 工具内核（ROADMAP v0.5 §4）。

安全模型：
- 用 `ast.parse(expr, mode="eval")` 解析为 AST；
- 递归校验节点类型，仅允许：Expression / BinOp / UnaryOp / Num/Constant /
  BoolOp（受限） / Compare（受限） / Call（仅白名单函数） / 关键字参数不可；
- 任何 Import / ImportFrom / Attribute / Assign / AnnAssign / AugAssign /
  Lambda / GeneratorExp / ListComp/SetComp/DictComp / 名称为 dunder 的标识符
  → 立即拒绝；
- 不调用 eval/exec/compile 执行 AST；只在受控遍历中直接计算。

函数白名单：sqrt（math）/abs/round/min/max/pow。不暴露任何 import 能力。
"""

from __future__ import annotations

import ast
import math
import operator
from typing import Any

# 允许的二元运算符
_BIN_OPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

# 允许的一元运算符
_UNARY_OPS: dict[type, Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# 允许的函数名 → 实现。严格白名单，不接受任何别名或属性访问。
_FUNC_WHITELIST: dict[str, Any] = {
    "sqrt": math.sqrt,
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "pow": pow,
}

# AST 节点黑名单：出现即拒绝（防御性，即使遍历逻辑漏判也兜底）。
_BLACKLIST_NODES = (
    ast.Import,
    ast.ImportFrom,
    ast.Attribute,
    ast.Assign,
    ast.AnnAssign,
    ast.AugAssign,
    ast.NamedExpr,
    ast.Lambda,
    ast.GeneratorExp,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.ClassDef,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Delete,
    ast.Global,
    ast.Nonlocal,
    ast.Raise,
    ast.Try,
    ast.With,
    ast.AsyncWith,
    ast.Assert,
)


class CalcError(ValueError):
    """表达式校验/求值失败。code 字段供 executor 映射到信封。"""

    def __init__(self, message: str, code: str = "VALIDATION_ERROR") -> None:
        super().__init__(message)
        self.code = code


def _reject(node: ast.AST, reason: str) -> None:
    raise CalcError(f"不允许的表达式结构: {reason}")


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _eval_node(node: ast.AST) -> Any:
    """递归求值；任何不在白名单的节点直接拒绝。"""
    # 数字字面量（Python 3.8+ 用 Constant）
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        _reject(node, "常量类型不允许")
    elif hasattr(ast, "Num") and isinstance(node, ast.Num):  # pragma: no cover  旧版本兼容
        return node.n

    # 二元运算
    elif isinstance(node, ast.BinOp):
        op_fn = _BIN_OPS.get(type(node.op))
        if op_fn is None:
            _reject(node, f"运算符 {type(node.op).__name__} 不允许")
        try:
            return op_fn(_eval_node(node.left), _eval_node(node.right))
        except ZeroDivisionError as exc:
            raise CalcError(f"计算错误: 除零: {exc}", code="VALIDATION_ERROR") from exc
        except (ValueError, TypeError, OverflowError) as exc:
            raise CalcError(f"计算错误: {exc}", code="VALIDATION_ERROR") from exc

    # 一元运算
    elif isinstance(node, ast.UnaryOp):
        op_fn = _UNARY_OPS.get(type(node.op))
        if op_fn is None:
            _reject(node, f"一元运算符 {type(node.op).__name__} 不允许")
        return op_fn(_eval_node(node.operand))

    # 函数调用（仅白名单函数、仅位置参数、不允许关键字参数）
    elif isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            _reject(node, "调用目标必须是函数名")
        fname = node.func.id
        if fname not in _FUNC_WHITELIST:
            _reject(node, f"函数 {fname!r} 不在白名单")
        if node.keywords:
            _reject(node, "不允许使用关键字参数")
        # 拒绝 *args / **kwargs
        if any(isinstance(a, ast.Starred) for a in node.args):
            _reject(node, "不允许使用 *args")
        args = [_eval_node(a) for a in node.args]
        try:
            return _FUNC_WHITELIST[fname](*args)
        except ZeroDivisionError as exc:
            raise CalcError(f"计算错误: {exc}", code="VALIDATION_ERROR") from exc
        except (ValueError, TypeError, OverflowError) as exc:
            raise CalcError(f"计算错误: {exc}", code="VALIDATION_ERROR") from exc

    # 标识符（仅允许白名单函数名作为调用目标；裸标识符无值）
    elif isinstance(node, ast.Name):
        _reject(node, f"不允许的标识符 {node.id!r}")

    # 元组（仅用于 min/max 多参数场景，ast.Tuple 在 Call.args 中即可）
    elif isinstance(node, ast.Tuple):
        return tuple(_eval_node(e) for e in node.elts)

    else:
        _reject(node, type(node).__name__)

    # 我的覆盖分支都显式 return 或 raise；这里 unreachable
    raise CalcError("无法求值的表达式结构")  # pragma: no cover


def _validate(node: ast.AST) -> None:
    """先做一次纯结构校验，黑名单节点立即拒绝。"""
    for n in ast.walk(node):
        if isinstance(n, _BLACKLIST_NODES):
            raise CalcError(f"表达式含禁止节点 {type(n).__name__}")
        # dunder 标识符拒绝（防 __import__ 等）
        if isinstance(n, ast.Name) and _is_dunder(n.id):
            raise CalcError(f"表达式含禁止标识符 {n.id!r}")
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and _is_dunder(n.func.id):
            raise CalcError(f"表达式含禁止函数 {n.func.id!r}")


def calculate(expression: str) -> dict[str, Any]:
    """对表达式做 AST 白名单求值，返回 {"value": <number>}。

    失败抛 CalcError（executor 转为信封 is_error=true）。
    不使用 eval/exec/compile 执行，仅 ast.parse 解析结构。
    """
    if not isinstance(expression, str) or not expression.strip():
        raise CalcError("表达式不能为空")
    if len(expression) > 512:
        raise CalcError("表达式过长（>512 字符）")

    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise CalcError(f"表达式语法错误: {exc.msg}") from exc

    _validate(tree)
    value = _eval_node(tree.body)

    # 结果必须是数值类型
    if not isinstance(value, (int, float)):
        raise CalcError("表达式结果必须为数值")
    return {"value": value}
