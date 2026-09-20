from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from service import ResearchService, ServiceError
from store import Store


def make_service() -> ResearchService:
    tmp = tempfile.mkdtemp()
    return ResearchService(Store(str(Path(tmp) / "state.json")), str(ROOT / "reference" / "domain.json"))


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        self.lead, _ = self.svc.create_user({"name": "主管"})
        self.lead = self.lead["id"]
        self.ana_a, _ = self.svc.create_user({"name": "分析师甲"})
        self.ana_a = self.ana_a["id"]
        self.ana_b, _ = self.svc.create_user({"name": "分析师乙"})
        self.ana_b = self.ana_b["id"]
        self.reviewer, _ = self.svc.create_user({"name": "复核人"})
        self.reviewer = self.reviewer["id"]
        self.outsider, _ = self.svc.create_user({"name": "路人"})
        self.outsider = self.outsider["id"]

        project, _ = self.svc.create_project(
            self.lead,
            {
                "name": "季度观点",
                "kind": "normal",
                "members": {
                    self.ana_a: "analyst",
                    self.ana_b: "analyst",
                    self.reviewer: "reviewer",
                },
            },
        )
        self.project = project["id"]
        self.main = project["main_branch_id"]

        company, _ = self.svc.create_company(self.lead, {"ticker": "AAA", "name": "示例公司"})
        self.company = company["id"]
        self.revenue, _ = self.svc.create_metric(
            self.lead, {"company_id": self.company, "code": "revenue"}
        )
        self.revenue = self.revenue["id"]
        self.fx, _ = self.svc.create_metric(
            self.lead, {"company_id": self.company, "code": "fx_rate"}
        )
        self.fx = self.fx["id"]
        self.gross, _ = self.svc.create_metric(
            self.lead, {"company_id": self.company, "code": "gross_profit"}
        )
        self.gross = self.gross["id"]
        self.period, _ = self.svc.create_period(self.lead, {"label": "FY2026"})
        self.period = self.period["id"]
        self.period2, _ = self.svc.create_period(self.lead, {"label": "FY2027"})
        self.period2 = self.period2["id"]
        self.source, _ = self.svc.create_source(self.lead, {"name": "央行中间价"})
        self.source = self.source["id"]

    def put(self, branch: str, metric: str, period: str, value: float, user: str | None = None, **kw):
        body = {
            "metric_id": metric,
            "period_id": period,
            "scenario": "base",
            "value": value,
            **kw,
        }
        return self.svc.put_assumption(user or self.ana_a, branch, body)

    def publish(self, expected, user=None):
        return self.svc.publish(
            user or self.reviewer, self.project, {"expected_version_id": expected}
        )


class StoryTest(Base):
    """还原题述场景：甲更新收入、乙用旧汇率、外部修订只标记过期。"""

    def test_full_story(self):
        # 基线：收入 100、汇率 7.0，毛利 = 收入 * 汇率
        self.put(self.main, self.revenue, self.period, 100.0)
        self.put(self.main, self.fx, self.period, 7.0, source_id=self.source)
        self.svc.put_formula(
            self.ana_a, self.main, {"metric_id": self.gross, "expression": "revenue * fx_rate"}
        )
        v1, status = self.publish(None)
        self.assertEqual(201, status)
        self.assertEqual(1, v1["number"])

        # 甲从已发布基线拉分支，更新收入预测
        branch, _ = self.svc.create_branch(self.ana_a, self.project, {"name": "甲-收入更新"})
        branch = branch["id"]
        self.put(branch, self.revenue, self.period, 120.0)

        # 乙在 main 上沿用旧汇率补了下一期间假设（与甲的修改不冲突）
        self.put(self.main, self.revenue, self.period2, 130.0, user=self.ana_b)
        self.put(self.main, self.fx, self.period2, 7.0, user=self.ana_b, source_id=self.source)

        merged, status = self.svc.merge_branch(self.ana_a, branch)
        self.assertEqual(200, status)
        self.assertTrue(merged["merged"])

        # 估值可追溯：700 -> revenue(120) * fx_rate(7.0)
        trace, _ = self.svc.compute(self.lead, self.main, self.gross, self.period, "base")
        self.assertEqual(840.0, trace["value"])
        self.assertEqual("formula", trace["via"])
        self.assertEqual(1, trace["formula_version"])
        leaves = {i["metric_code"]: i for i in trace["inputs"]}
        self.assertEqual(120.0, leaves["revenue"]["value"])
        self.assertEqual(self.source, leaves["fx_rate"]["source_id"])

        v2, _ = self.publish(v1["id"])

        # 版本差异：结论变化归因到数据（收入假设），而非模型
        diff, _ = self.svc.diff_versions(self.lead, v1["id"], v2["id"])
        changed = {(c["metric_id"], c["period_id"]) for c in diff["input_changes"]}
        self.assertIn((self.revenue, self.period), changed)
        self.assertEqual([], diff["formula_changes"])
        impact = {o["metric_id"]: o["causes"] for o in diff["impacted_outputs"]}
        self.assertEqual(["data"], impact[self.gross])

        # 外部数据修订：只标记相关假设过期，不改已发布版本
        result, _ = self.svc.create_observation(
            self.lead,
            {
                "source_id": self.source,
                "metric_id": self.fx,
                "period_id": self.period,
                "value": 7.0,
            },
        )
        self.assertEqual([], result["stale_assumptions"])  # 首次登记不是修订
        result, _ = self.svc.create_observation(
            self.lead,
            {
                "source_id": self.source,
                "metric_id": self.fx,
                "period_id": self.period,
                "value": 7.2,
            },
        )
        self.assertEqual(2, result["observation"]["revision"])
        self.assertTrue(result["stale_assumptions"])

        # 已发布 v2 快照中的值不受影响
        v2_after, _ = self.svc.get_version(self.lead, v2["id"])
        fx_v2 = [
            a
            for a in v2_after["snapshot"]["assumptions"]
            if a["metric_id"] == self.fx and a["period_id"] == self.period
        ][0]
        self.assertEqual(7.0, fx_v2["value"])

        # 待重算清单：毛利结论因汇率输入过期而待重算
        pending, _ = self.svc.pending_recompute(self.lead, self.main)
        self.assertIn(self.gross, {p["metric_id"] for p in pending["items"]})

        # 计算路径上能看到过期标记与来源修订号
        trace, _ = self.svc.compute(self.lead, self.main, self.gross, self.period, "base")
        self.assertTrue(trace["stale"])
        fx_node = [i for i in trace["inputs"] if i["metric_code"] == "fx_rate"][0]
        self.assertTrue(fx_node["stale"])
        self.assertEqual(2, fx_node["source_revision"])


class FormulaCycleTest(Base):
    def test_cycle_rejected(self):
        self.svc.put_formula(
            self.ana_a, self.main, {"metric_id": self.gross, "expression": "revenue * 2"}
        )
        with self.assertRaises(ServiceError) as ctx:
            self.svc.put_formula(
                self.ana_a, self.main, {"metric_id": self.revenue, "expression": "gross_profit / 2"}
            )
        self.assertEqual(409, ctx.exception.status)
        self.assertEqual("cycle", ctx.exception.code)

    def test_self_cycle_rejected(self):
        with self.assertRaises(ServiceError):
            self.svc.put_formula(
                self.ana_a, self.main, {"metric_id": self.revenue, "expression": "revenue + 1"}
            )

    def test_formula_update_bumps_version(self):
        f1, _ = self.svc.put_formula(
            self.ana_a, self.main, {"metric_id": self.gross, "expression": "revenue * fx_rate"}
        )
        f2, _ = self.svc.put_formula(
            self.ana_a, self.main, {"metric_id": self.gross, "expression": "revenue * fx_rate * 0.5"}
        )
        self.assertEqual(f1["version"] + 1, f2["version"])


class MergeConflictTest(Base):
    def test_conflict_precise_to_metric_period_scenario(self):
        self.put(self.main, self.revenue, self.period, 100.0)
        self.put(self.main, self.fx, self.period, 7.0)
        v1, _ = self.publish(None)

        branch, _ = self.svc.create_branch(self.ana_a, self.project, {"name": "b1"})
        branch = branch["id"]
        # 双方在 同一指标x期间x情景 上改出不同值 -> 冲突
        self.put(branch, self.revenue, self.period, 120.0)
        self.put(self.main, self.revenue, self.period, 110.0)
        # 只有分支改汇率 -> 应干净合入
        self.put(branch, self.fx, self.period, 7.1)

        with self.assertRaises(Exception) as ctx:
            self.svc.merge_branch(self.ana_a, branch)
        exc = ctx.exception
        self.assertEqual(1, len(exc.cell_conflicts))
        conflict = exc.cell_conflicts[0]
        self.assertEqual(self.revenue, conflict["metric_id"])
        self.assertEqual(self.period, conflict["period_id"])
        self.assertEqual("base", conflict["scenario"])
        self.assertEqual(100.0, conflict["base"])
        self.assertEqual(110.0, conflict["main"])
        self.assertEqual(120.0, conflict["branch"])

        # 解决冲突：main 采纳分支值后重试
        self.put(self.main, self.revenue, self.period, 120.0)
        _, status = self.svc.merge_branch(self.ana_a, branch)
        self.assertEqual(200, status)
        fx_cells = [
            a
            for a in self.svc.list_assumptions(self.lead, self.main)[0]["items"]
            if a["metric_id"] == self.fx
        ]
        self.assertEqual(7.1, fx_cells[0]["value"])

    def test_merged_branch_readonly(self):
        self.put(self.main, self.revenue, self.period, 100.0)
        self.publish(None)
        branch, _ = self.svc.create_branch(self.ana_a, self.project, {"name": "b1"})
        self.svc.merge_branch(self.ana_a, branch["id"])
        with self.assertRaises(ServiceError) as ctx:
            self.put(branch["id"], self.revenue, self.period, 1.0)
        self.assertEqual(409, ctx.exception.status)


class PublishConcurrencyTest(Base):
    def test_concurrent_publish_single_authoritative(self):
        self.put(self.main, self.revenue, self.period, 100.0)
        v1, _ = self.publish(None)

        results: list[tuple[int, str | None]] = []

        def attempt():
            try:
                _, status = self.svc.publish(
                    self.reviewer, self.project, {"expected_version_id": v1["id"]}
                )
                results.append((status, None))
            except ServiceError as exc:
                results.append((exc.status, exc.code))

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if r[0] == 201]
        conflicts = [r for r in results if r[0] == 409 and r[1] == "stale_base"]
        self.assertEqual(1, len(successes))
        self.assertEqual(7, len(conflicts))

        versions, _ = self.svc.list_versions(self.lead, self.project)
        authoritative = [v for v in versions["items"] if v["authoritative"]]
        self.assertEqual(1, len(authoritative))
        self.assertEqual(2, authoritative[0]["number"])

    def test_publish_requires_approver(self):
        with self.assertRaises(ServiceError) as ctx:
            self.svc.publish(self.ana_a, self.project, {"expected_version_id": None})
        self.assertEqual(403, ctx.exception.status)


class DiffAttributionTest(Base):
    def test_model_change_attribution(self):
        self.put(self.main, self.revenue, self.period, 100.0)
        self.put(self.main, self.fx, self.period, 7.0)
        self.svc.put_formula(
            self.ana_a, self.main, {"metric_id": self.gross, "expression": "revenue * fx_rate"}
        )
        v1, _ = self.publish(None)
        self.svc.put_formula(
            self.ana_a, self.main, {"metric_id": self.gross, "expression": "revenue * fx_rate * 0.5"}
        )
        v2, _ = self.publish(v1["id"])
        diff, _ = self.svc.diff_versions(self.lead, v1["id"], v2["id"])
        self.assertEqual([], diff["input_changes"])
        self.assertEqual([self.gross], [c["metric_id"] for c in diff["formula_changes"]])
        impact = {o["metric_id"]: o["causes"] for o in diff["impacted_outputs"]}
        self.assertEqual(["model"], impact[self.gross])


class WatchlistIsolationTest(Base):
    def test_watchlist_hidden_from_non_members(self):
        project, _ = self.svc.create_project(
            self.lead, {"name": "敏感观察名单", "kind": "watchlist"}
        )
        pid = project["id"]
        listed, _ = self.svc.list_projects(self.outsider)
        self.assertNotIn(pid, {p["id"] for p in listed["items"]})
        listed, _ = self.svc.list_projects(self.lead)
        self.assertIn(pid, {p["id"] for p in listed["items"]})
        with self.assertRaises(ServiceError) as ctx:
            self.svc.get_project(self.outsider, pid)
        self.assertEqual(404, ctx.exception.status)
        with self.assertRaises(ServiceError) as ctx:
            self.svc.create_branch(self.outsider, pid, {"name": "x"})
        self.assertEqual(404, ctx.exception.status)


class CommentExportTest(Base):
    def test_comments_append_only(self):
        self.put(self.main, self.revenue, self.period, 100.0)
        v1, _ = self.publish(None)
        self.svc.add_comment(self.reviewer, v1["id"], {"body": "第一条"})
        self.svc.add_comment(self.ana_a, v1["id"], {"body": "第二条"})
        comments, _ = self.svc.list_comments(self.lead, v1["id"])
        self.assertEqual(["第一条", "第二条"], [c["body"] for c in comments["items"]])
        # 服务层不提供任何修改/删除意见的入口
        for name in dir(self.svc):
            self.assertNotIn("comment", name.lower().replace("add_comment", "").replace("list_comments", ""))

    def test_export_pins_digest_formula_version_approver(self):
        self.put(self.main, self.revenue, self.period, 100.0)
        self.put(self.main, self.fx, self.period, 7.0)
        self.svc.put_formula(
            self.ana_a, self.main, {"metric_id": self.gross, "expression": "revenue * fx_rate"}
        )
        v1, _ = self.publish(None)
        export, status = self.svc.create_export(self.lead, v1["id"])
        self.assertEqual(201, status)
        self.assertEqual(v1["data_digest"], export["data_digest"])
        self.assertEqual({self.gross: 1}, export["formula_versions"])
        self.assertEqual(self.reviewer, export["approver_id"])
        fetched, _ = self.svc.get_export(self.lead, export["id"])
        self.assertEqual(export, fetched)


class AuthTest(Base):
    def test_unknown_user_rejected(self):
        with self.assertRaises(ServiceError) as ctx:
            self.svc.list_projects("usr_999")
        self.assertEqual(401, ctx.exception.status)

    def test_analyst_cannot_write_without_membership(self):
        project, _ = self.svc.create_project(self.lead, {"name": "私有", "kind": "normal"})
        with self.assertRaises(ServiceError) as ctx:
            self.svc.create_branch(self.ana_a, project["id"], {"name": "x"})
        self.assertEqual(403, ctx.exception.status)


if __name__ == "__main__":
    unittest.main()
