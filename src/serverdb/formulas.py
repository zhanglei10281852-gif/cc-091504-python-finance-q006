from __future__ import annotations

import ast
import operator
from typing import Any, Callable

from .errors import BadRequest

# 公式使用纯表达式书写，变量为指标 key（同期间、同情景），另支持四种特殊形式：
#   prev(volume)                  上一期间的同指标
#   at(volume, "2026Q1")          指定期间的同指标
#   scenario("bull", volume)      同期间另一情景的指标
#   const("tax_rate")             项目常量
# 以及 min/max/abs/round 数值函数。
_ALLOWED_BINOPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
}
_ALLOWED_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_ALLOWED_CMP = {
    ast.Eq: operator.eq, ast.Lt: operator.lt, ast.LtE: operator.le,
    ast.Gt: operator.gt, ast.GtE: operator.ge,
}
_BUILTIN_FUNCS = {"min": min, "max": max, "abs": abs, "round": round}
_SPECIAL_FORMS = {"prev", "at", "scenario", "const"}

MAX_EXPRESSION_NODES = 200
MAX_POW_EXPONENT = 8


class Refs:
    """公式中出现的结构化引用。"""

    def __init__(self) -> None:
        self.metrics: set[str] = set()        # 同期间、同情景指标
        self.prev: set[str] = set()
        self.at: dict[str, set[str]] = {}     # metric -> {period}
        self.scenario: dict[str, dict[str, str]] = {}  # 占位，见下方
        self.scenarios: dict[str, set[str]] = {}       # scenario -> {metric}
        self.consts: set[str] = set()

    def as_dict(self) -> dict[str, Any]:
        return {
            "metrics": sorted(self.metrics),
            "prev": sorted(self.prev),
            "at": {m: sorted(ps) for m, ps in self.at.items()},
            "scenarios": {s: sorted(ms) for s, ms in self.scenarios.items()},
            "consts": sorted(self.consts),
        }


def _literal_str(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    raise BadRequest("该位置需要字符串字面量参数", code="formula_bad_syntax")


def _metric_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    raise BadRequest("特殊形式的指标参数必须是指标名", code="formula_bad_syntax")


class _RefExtractor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.refs = Refs()

    def visit_Name(self, node: ast.Name) -> None:
        self.refs.metrics.add(node.id)

    def visit_Call(self, node: ast.Call) -> None:
        name = node.func.id if isinstance(node.func, ast.Name) else None
        if name == "prev" and len(node.args) == 1:
            self.refs.prev.add(_metric_name(node.args[0]))
        elif name == "at" and len(node.args) == 2:
            m = _metric_name(node.args[0])
            self.refs.at.setdefault(m, set()).add(_literal_str(node.args[1]))
        elif name == "scenario" and len(node.args) == 2:
            s = _literal_str(node.args[0])
            m = _metric_name(node.args[1])
            self.refs.scenarios.setdefault(s, set()).add(m)
        elif name == "const" and len(node.args) == 1:
            self.refs.consts.add(_literal_str(node.args[0]))
        elif name in _BUILTIN_FUNCS:
            for arg in node.args:
                self.visit(arg)
        else:
            raise BadRequest("公式包含不允许的函数调用", code="formula_bad_syntax")


def _validate_node(node: ast.AST, depth: int = 0) -> None:
    if depth > 20:
        raise BadRequest("公式嵌套过深", code="formula_too_deep")
    if isinstance(node, ast.Expression):
        _validate_node(node.body, depth + 1)
    elif isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float, bool)):
            raise BadRequest("公式只允许数字与字符串字面量", code="formula_bad_constant")
    elif isinstance(node, ast.Name):
        pass
    elif isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        _validate_node(node.left, depth + 1)
        _validate_node(node.right, depth + 1)
        if isinstance(node.op, ast.Pow) and isinstance(node.right, ast.Constant):
            if abs(float(node.right.value)) > MAX_POW_EXPONENT:
                raise BadRequest("幂运算指数过大", code="formula_bad_syntax")
    elif isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
        _validate_node(node.operand, depth + 1)
    elif isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _ALLOWED_CMP:
        _validate_node(node.left, depth + 1)
        _validate_node(node.comparators[0], depth + 1)
    elif isinstance(node, ast.BoolOp):
        for v in node.values:
            _validate_node(v, depth + 1)
    elif isinstance(node, ast.IfExp):
        _validate_node(node.test, depth + 1)
        _validate_node(node.body, depth + 1)
        _validate_node(node.orelse, depth + 1)
    elif isinstance(node, ast.Call):
        name = node.func.id if isinstance(node.func, ast.Name) else None
        if name in _SPECIAL_FORMS:
            # 语法形状由 RefExtractor 再次校验，这里校验参数是字面量/Name 即可
            for arg in node.args:
                if not isinstance(arg, (ast.Name, ast.Constant)):
                    raise BadRequest("特殊形式参数非法", code="formula_bad_syntax")
        elif name in _BUILTIN_FUNCS and not node.keywords:
            for arg in node.args:
                _validate_node(arg, depth + 1)
        else:
            raise BadRequest("公式包含不允许的语法", code="formula_bad_syntax")
    else:
        raise BadRequest("公式包含不允许的语法", code="formula_bad_syntax")


def parse_expression(expr: str) -> tuple[ast.Expression, Refs]:
    """解析并校验公式，返回 AST 与结构化引用。"""
    if not isinstance(expr, str) or not expr.strip():
        raise BadRequest("公式不能为空", code="formula_empty")
    if len(expr) > 2000:
        raise BadRequest("公式过长", code="formula_too_long")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise BadRequest(f"公式语法错误: {exc.msg}", code="formula_bad_syntax") from exc
    if sum(1 for _ in ast.walk(tree)) > MAX_EXPRESSION_NODES:
        raise BadRequest("公式过于复杂", code="formula_too_complex")
    _validate_node(tree)
    extractor = _RefExtractor()
    extractor.visit(tree)
    return tree, extractor.refs


def _eval_node(
    node: ast.AST,
    env: dict[str, float],
    forms: dict[str, Callable[..., float]],
) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, env, forms)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in env:
            raise KeyError(node.id)
        return env[node.id]
    if isinstance(node, ast.BinOp):
        return _ALLOWED_BINOPS[type(node.op)](
            _eval_node(node.left, env, forms), _eval_node(node.right, env, forms)
        )
    if isinstance(node, ast.UnaryOp):
        return _ALLOWED_UNARY[type(node.op)](_eval_node(node.operand, env, forms))
    if isinstance(node, ast.Compare):
        return _ALLOWED_CMP[type(node.ops[0])](
            _eval_node(node.left, env, forms),
            _eval_node(node.comparators[0], env, forms),
        )
    if isinstance(node, ast.BoolOp):
        vals = [_eval_node(v, env, forms) for v in node.values]
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    if isinstance(node, ast.IfExp):
        return (
            _eval_node(node.body, env, forms)
            if _eval_node(node.test, env, forms)
            else _eval_node(node.orelse, env, forms)
        )
    if isinstance(node, ast.Call):
        name = node.func.id if isinstance(node.func, ast.Name) else None
        if name == "prev":
            return forms["prev"](_metric_name(node.args[0]))
        if name == "at":
            return forms["at"](_metric_name(node.args[0]), _literal_str(node.args[1]))
        if name == "scenario":
            return forms["scenario"](_literal_str(node.args[0]), _metric_name(node.args[1]))
        if name == "const":
            return forms["const"](_literal_str(node.args[0]))
        if name in _BUILTIN_FUNCS:
            return _BUILTIN_FUNCS[name](*(_eval_node(a, env, forms) for a in node.args))
    raise BadRequest("公式包含不允许的语法", code="formula_bad_syntax")


def safe_eval(
    expr: str | ast.Expression,
    env: dict[str, float],
    forms: dict[str, Callable[..., float]] | None = None,
) -> float:
    """在给定变量环境下求值。变量缺失抛 KeyError，由上层标记为待重算。"""
    tree = parse_expression(expr)[0] if isinstance(expr, str) else expr
    forms = forms or {}
    try:
        result = _eval_node(tree, env, forms)
    except ZeroDivisionError as exc:
        raise BadRequest("公式求值时除以零", code="formula_divzero") from exc
    if isinstance(result, bool):
        raise BadRequest("公式必须返回数值，不能是布尔值", code="formula_not_numeric")
    return float(result)


# ---- 依赖图：防环 ----------------------------------------------------------

def ensure_acyclic(graph: dict[str, set[str]], target: str, deps: set[str]) -> None:
    """加入 ``target -> deps``（同上下文依赖边）后若成环则抛错。

    跨期间 / 跨情景引用不参与同一次求值的拓扑序，因此不进入该图，
    例如 ``prev(revenue)`` 不会被误判为 revenue 的自环。
    """
    if target in deps:
        raise BadRequest(f"指标 {target} 不能依赖自身", code="formula_cycle")

    def reaches(start: str, goal: str) -> bool:
        stack = [start]
        seen: set[str] = set()
        while stack:
            cur = stack.pop()
            for nxt in graph.get(cur, ()):  # node -> 它依赖的节点
                if nxt == goal:
                    return True
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return False

    for dep in deps:
        if reaches(dep, target):
            raise BadRequest(
                f"添加依赖 {target} -> {dep} 会形成循环依赖", code="formula_cycle"
            )


def topological_order(graph: dict[str, set[str]], roots: set[str]) -> list[str]:
    """返回计算 roots 所需的拓扑顺序（被依赖者在前）。"""
    order: list[str] = []
    state: dict[str, int] = {}  # 0=visiting, 1=done

    def visit(n: str) -> None:
        mark = state.get(n)
        if mark == 1:
            return
        if mark == 0:
            raise BadRequest("依赖图存在循环", code="formula_cycle")
        state[n] = 0
        for d in sorted(graph.get(n, ())):
            visit(d)
        state[n] = 1
        order.append(n)

    for r in sorted(roots):
        visit(r)
    return order
