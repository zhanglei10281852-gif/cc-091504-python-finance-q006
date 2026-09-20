"""领域纯函数：公式表达式、依赖防环、三方合并、摘要计算。

不依赖存储层，便于单测。所有函数抛出 DomainError 表示业务校验失败。
"""
from __future__ import annotations

import ast
import hashlib
import json


class DomainError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- 表达式

_ALLOWED_FUNCS = {"min": min, "max": max, "abs": abs, "round": round}
_BIN_OPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
}
_UNARY_OPS = {ast.UAdd: lambda a: a, ast.USub: lambda a: -a}


def validate_expression(expression: str) -> set[str]:
    """解析公式表达式，返回引用的指标代码集合；非法结构抛 DomainError。"""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise DomainError("invalid_expression", f"公式无法解析: {exc}") from exc
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
                raise DomainError("invalid_expression", "公式仅允许 min/max/abs/round 函数")
        elif isinstance(
            node,
            (
                ast.Expression,
                ast.BinOp,
                ast.UnaryOp,
                ast.Constant,
                ast.Load,
                ast.Add,
                ast.Sub,
                ast.Mult,
                ast.Div,
                ast.Mod,
                ast.Pow,
                ast.UAdd,
                ast.USub,
            ),
        ):
            continue
        else:
            raise DomainError(
                "invalid_expression",
                f"公式包含不允许的结构: {type(node).__name__}",
            )
    return names


def evaluate(expression: str, env: dict[str, float]) -> float:
    """在给定指标代码 -> 数值的环境下求值。"""
    tree = ast.parse(expression, mode="eval")
    return _eval_node(tree.body, env)


def _eval_node(node: ast.AST, env: dict[str, float]) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise DomainError("invalid_expression", "公式常量必须是数值")
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        raise DomainError("missing_input", f"指标 {node.id} 缺少取值")
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left = _eval_node(node.left, env)
        right = _eval_node(node.right, env)
        try:
            return _BIN_OPS[type(node.op)](left, right)
        except ZeroDivisionError as exc:
            raise DomainError("invalid_expression", "公式出现除零") from exc
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand, env))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        func = _ALLOWED_FUNCS.get(node.func.id)
        if func is None:
            raise DomainError("invalid_expression", "公式仅允许 min/max/abs/round 函数")
        return func(*[_eval_node(arg, env) for arg in node.args])
    raise DomainError("invalid_expression", f"公式包含不允许的结构: {type(node).__name__}")


# ---------------------------------------------------------------- 依赖防环


def would_cycle(graph: dict[str, set[str]], metric_id: str, deps: set[str]) -> bool:
    """若给 metric_id 增加依赖 deps 后会形成环则返回 True。

    graph: 指标 id -> 其直接依赖的指标 id 集合（不含本次新增边）。
    """
    adjacency = {m: set(ds) for m, ds in graph.items()}
    adjacency.setdefault(metric_id, set()).update(deps)
    seen: set[str] = set()
    stack = [metric_id]
    while stack:
        node = stack.pop()
        for dep in adjacency.get(node, ()):
            if dep == metric_id:
                return True
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return False


def transitive_deps(graph: dict[str, set[str]], metric_id: str) -> set[str]:
    """返回 metric_id 的全部间接依赖（不含自身）。"""
    seen: set[str] = set()
    stack = list(graph.get(metric_id, ()))
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph.get(node, ()))
    seen.discard(metric_id)
    return seen


# ---------------------------------------------------------------- 三方合并


def three_way_merge(
    base: dict, ours: dict, theirs: dict
) -> tuple[dict, list[dict]]:
    """按键做三方合并，冲突精确到键（调用方用 指标x期间x情景 作为键）。

    返回 (merged, conflicts)；conflicts 元素含 key/base/ours/theirs。
    """
    merged = dict(ours)
    conflicts: list[dict] = []
    for key, tval in theirs.items():
        b = base.get(key)
        o = ours.get(key)
        if o == tval or b == tval:
            continue  # 双方一致，或对方未改
        if b == o:
            merged[key] = tval  # 仅对方修改
        else:
            conflicts.append({"key": key, "base": b, "ours": o, "theirs": tval})
    for key in base:
        if key not in theirs:
            if key not in ours:
                continue
            if ours[key] == base[key]:
                del merged[key]  # 对方删除、己方未改
            else:
                conflicts.append(
                    {"key": key, "base": base[key], "ours": ours[key], "theirs": None}
                )
    return merged, conflicts


# ---------------------------------------------------------------- 摘要


def digest(obj) -> str:
    """对任意 JSON 可序列化对象计算稳定摘要，用于导出固定数据快照。"""
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
