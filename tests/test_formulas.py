from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from serverdb.errors import BadRequest
from serverdb.formulas import (
    ensure_acyclic,
    parse_expression,
    safe_eval,
    topological_order,
)


class FormulaParserTest(unittest.TestCase):
    def test_arithmetic_and_refs(self) -> None:
        tree, refs = parse_expression("revenue * (1 - cogs_ratio) + prev(revenue)")
        self.assertIn("revenue", refs.metrics)
        self.assertIn("cogs_ratio", refs.metrics)
        self.assertIn("revenue", refs.prev)
        self.assertAlmostEqual(safe_eval(tree, {"revenue": 100.0, "cogs_ratio": 0.6},
                                         {"prev": lambda m: 90.0}), 130.0)

    def test_special_forms(self) -> None:
        _, refs = parse_expression(
            'at(x, "2026Q1") + scenario("bull", x) + const("tax")')
        self.assertEqual(refs.at, {"x": {"2026Q1"}})
        self.assertEqual(refs.scenarios, {"bull": {"x"}})
        self.assertEqual(refs.consts, {"tax"})

    def test_boolean_result_rejected(self) -> None:
        tree, _ = parse_expression("1 if x > 0 else 2")
        self.assertEqual(safe_eval(tree, {"x": 1.0}), 1.0)
        with self.assertRaises(BadRequest):
            safe_eval(parse_expression("x > 0")[0], {"x": 1.0})

    def test_forbidden_syntax(self) -> None:
        for evil in ("__import__('os')", "x.real", "lambda: 1",
                     "[i for i in range(3)]", "x := 1", "f'{x}'", "open('x')"):
            with self.assertRaises(BadRequest):
                parse_expression(evil)

    def test_division_by_zero(self) -> None:
        with self.assertRaises(BadRequest):
            safe_eval(parse_expression("a / b")[0], {"a": 1.0, "b": 0.0})

    def test_missing_variable_is_keyerror(self) -> None:
        with self.assertRaises(KeyError):
            safe_eval(parse_expression("a + b")[0], {"a": 1.0})


class DependencyGraphTest(unittest.TestCase):
    def test_self_dependency(self) -> None:
        with self.assertRaises(BadRequest):
            ensure_acyclic({"a": set()}, "a", {"a"})

    def test_indirect_cycle(self) -> None:
        graph = {"a": {"b"}, "b": {"c"}, "c": set()}
        with self.assertRaises(BadRequest):
            ensure_acyclic(graph, "c", {"a"})  # c -> a -> b -> c 闭环
        # 指向新节点（新节点没有任何回到 c 的路径）不构成环
        ensure_acyclic(graph, "c", {"d"})

    def test_topological_order(self) -> None:
        graph = {"d": {"b", "c"}, "c": {"a"}, "b": {"a"}, "a": set()}
        order = topological_order(graph, {"d"})
        self.assertLess(order.index("a"), order.index("c"))
        self.assertLess(order.index("c"), order.index("d"))


if __name__ == "__main__":
    unittest.main()
