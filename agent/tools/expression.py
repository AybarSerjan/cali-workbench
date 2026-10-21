from __future__ import annotations

import ast
from typing import Any

import numpy as np


class UnsafeExpression(ValueError):
    pass


_BIN = {ast.Add: np.add, ast.Sub: np.subtract, ast.Mult: np.multiply, ast.Div: np.divide,
        ast.Mod: np.mod, ast.Pow: np.power}
_CMP = {ast.Lt: np.less, ast.LtE: np.less_equal, ast.Gt: np.greater,
        ast.GtE: np.greater_equal, ast.Eq: np.equal, ast.NotEq: np.not_equal}


def _call(name: str, args: list[Any]) -> Any:
    if name == "abs" and len(args) == 1:
        return np.abs(args[0])
    if name in {"min", "max"} and args:
        fn = np.minimum if name == "min" else np.maximum
        result = args[0]
        for value in args[1:]:
            result = fn(result, value)
        return result
    if name == "deriv" and len(args) in {1, 2}:
        return np.gradient(args[0], args[1]) if len(args) == 2 else np.gradient(args[0])
    raise UnsafeExpression(f"unsupported function call: {name}")


def _evaluate(node: ast.AST, names: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, names)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, bool)):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in names:
            raise UnsafeExpression(f"unknown signal: {node.id}")
        return names[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        return _BIN[type(node.op)](_evaluate(node.left, names), _evaluate(node.right, names))
    if isinstance(node, ast.UnaryOp):
        value = _evaluate(node.operand, names)
        if isinstance(node.op, ast.USub): return np.negative(value)
        if isinstance(node.op, ast.UAdd): return value
        if isinstance(node.op, (ast.Not, ast.Invert)): return np.logical_not(value)
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        values = [_evaluate(item, names) for item in node.values]
        fn = np.logical_and if isinstance(node.op, ast.And) else np.logical_or
        result = values[0]
        for value in values[1:]: result = fn(result, value)
        return result
    if isinstance(node, ast.Compare):
        left = _evaluate(node.left, names)
        result: Any = True
        for op, comparator in zip(node.ops, node.comparators):
            if type(op) not in _CMP:
                raise UnsafeExpression(f"unsupported comparison: {type(op).__name__}")
            right = _evaluate(comparator, names)
            result = np.logical_and(result, _CMP[type(op)](left, right))
            left = right
        return result
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.keywords:
            raise UnsafeExpression("keyword arguments are not allowed")
        return _call(node.func.id, [_evaluate(arg, names) for arg in node.args])
    raise UnsafeExpression(f"expression node is not allowed: {type(node).__name__}")


def evaluate_expression(expression: str, names: dict[str, Any]) -> np.ndarray:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise UnsafeExpression(f"invalid expression: {exc.msg}") from exc
    value = _evaluate(tree, {key: np.asarray(item) for key, item in names.items()})
    return np.asarray(value)


def align_series(series: dict[str, tuple[Any, Any]]) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    if not series:
        raise ValueError("at least one signal series is required")
    prepared = {name: (np.asarray(t, dtype=float), np.asarray(v, dtype=float)) for name, (t, v) in series.items()}
    if any(len(t) != len(v) or len(t) == 0 for t, v in prepared.values()):
        raise ValueError("signal timestamps and values must be non-empty and have equal lengths")
    start = max(t[0] for t, _ in prepared.values())
    end = min(t[-1] for t, _ in prepared.values())
    base_t = next(iter(prepared.values()))[0]
    axis = base_t[(base_t >= start) & (base_t <= end)]
    if len(axis) == 0:
        raise ValueError("signals have no overlapping time range")
    aligned = {name: np.interp(axis, t, v, left=np.nan, right=np.nan) for name, (t, v) in prepared.items()}
    finite = np.logical_and.reduce([np.isfinite(value) for value in aligned.values()])
    return axis[finite], {name: value[finite] for name, value in aligned.items()}
