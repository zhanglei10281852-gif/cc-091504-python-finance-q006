from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import domain


class ExpressionTest(unittest.TestCase):
    def test_validate_and_evaluate(self):
        names = domain.validate_expression("revenue * gross_margin / 100")
        self.assertEqual({"revenue", "gross_margin"}, names)
        value = domain.evaluate("revenue * gross_margin / 100", {"revenue": 200.0, "gross_margin": 25.0})
        self.assertEqual(50.0, value)

    def test_functions_allowed(self):
        self.assertEqual(3.0, domain.evaluate("max(1, 3)", {}))
        self.assertEqual(2.0, domain.evaluate("abs(-2)", {}))

    def test_rejects_unsafe_constructs(self):
        for expr in ("__import__('os')", "x.y", "f(1)", "[1][0]", "a if b else c"):
            with self.assertRaises(domain.DomainError, msg=expr):
                domain.validate_expression(expr)

    def test_missing_input(self):
        with self.assertRaises(domain.DomainError):
            domain.evaluate("revenue + 1", {})


class CycleTest(unittest.TestCase):
    def test_direct_self_cycle(self):
        self.assertTrue(domain.would_cycle({}, "a", {"a"}))

    def test_indirect_cycle(self):
        graph = {"b": {"c"}, "c": {"d"}}
        self.assertTrue(domain.would_cycle(graph, "d", {"b"}))

    def test_no_cycle(self):
        graph = {"b": {"c"}}
        self.assertFalse(domain.would_cycle(graph, "a", {"b", "c"}))

    def test_transitive_deps(self):
        graph = {"a": {"b"}, "b": {"c"}, "c": set()}
        self.assertEqual({"b", "c"}, domain.transitive_deps(graph, "a"))


class MergeTest(unittest.TestCase):
    def test_clean_merge(self):
        base = {"m|p|base": {"value": 1}}
        ours = {"m|p|base": {"value": 1}, "m2|p|base": {"value": 2}}
        theirs = {"m|p|base": {"value": 3}, "m2|p|base": {"value": 2}}
        merged, conflicts = domain.three_way_merge(base, ours, theirs)
        self.assertEqual([], conflicts)
        self.assertEqual(3, merged["m|p|base"]["value"])
        self.assertEqual(2, merged["m2|p|base"]["value"])

    def test_conflict_precise_to_cell(self):
        base = {"m|p1|base": {"value": 1}, "m|p2|base": {"value": 1}}
        ours = {"m|p1|base": {"value": 2}, "m|p2|base": {"value": 1}}
        theirs = {"m|p1|base": {"value": 3}, "m|p2|base": {"value": 9}}
        merged, conflicts = domain.three_way_merge(base, ours, theirs)
        self.assertEqual(["m|p1|base"], [c["key"] for c in conflicts])
        self.assertEqual(9, merged["m|p2|base"]["value"])

    def test_delete_vs_edit_conflicts(self):
        base = {"m|p|base": {"value": 1}}
        ours = {"m|p|base": {"value": 2}}
        theirs: dict = {}
        _, conflicts = domain.three_way_merge(base, ours, theirs)
        self.assertEqual(1, len(conflicts))
        self.assertIsNone(conflicts[0]["theirs"])


class DigestTest(unittest.TestCase):
    def test_stable(self):
        a = {"x": 1, "y": [2, 3]}
        b = {"y": [2, 3], "x": 1}
        self.assertEqual(domain.digest(a), domain.digest(b))
        self.assertNotEqual(domain.digest(a), domain.digest({"x": 1}))


if __name__ == "__main__":
    unittest.main()
