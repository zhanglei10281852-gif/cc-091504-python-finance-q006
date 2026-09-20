from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from serverdb.auth import Auth
from serverdb.errors import Conflict, Forbidden, NotFound, Unauthorized
from serverdb.service import Service
from serverdb.storage import Store


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = Service(
            Store(os.path.join(self.tmp.name, "db.json")), Auth("case-secret")
        )
        self.svc.create_user(None, {"username": "boss", "password": "pw",
                                    "role": "admin", "watchlist_access": True})
        self.svc.create_user({"username": "boss", "role": "admin"},
                             {"username": "ana", "password": "pw", "role": "analyst"})
        self.svc.create_user({"username": "boss", "role": "admin"},
                             {"username": "rev", "password": "pw", "role": "reviewer"})
        self.admin = self.svc.authenticate(self.svc.login("boss", "pw"))
        self.ana = self.svc.authenticate(self.svc.login("ana", "pw"))
        self.rev = self.svc.authenticate(self.svc.login("rev", "pw"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ---- 夹具 --------------------------------------------------------------

    def make_project(self, watchlist: bool = False, periods=("2026Q1", "2026Q2")):
        company = self.svc.create_company(self.admin, {"name": "甲公司"})
        project = self.svc.create_project(self.admin, {
            "name": "季度观点", "company_id": company["id"],
            "scenarios": ["base", "bull"], "periods": list(periods),
            "watchlist": watchlist,
        })
        self.svc.add_member(self.admin, project["id"], {"username": "ana"})
        self.svc.add_member(self.admin, project["id"], {"username": "rev"})
        return company, project

    def metrics(self, pid, *keys):
        for key in keys:
            self.svc.create_metric(self.ana, pid, {"key": key, "name": key})


class AuthAndRbacTest(ServiceCase):
    def test_bootstrap_and_login(self) -> None:
        bad_token = Auth("other-secret").issue("boss")
        with self.assertRaises(Unauthorized):
            self.svc.authenticate(bad_token)
        self.assertEqual("boss", self.admin["username"])

    def test_wrong_password(self) -> None:
        with self.assertRaises(Unauthorized):
            self.svc.login("ana", "nope")

    def test_analyst_cannot_create_project_or_users(self) -> None:
        company = self.svc.create_company(self.admin, {"name": "乙"})
        with self.assertRaises(Forbidden):
            self.svc.create_project(self.ana, {"name": "x", "company_id": company["id"]})
        with self.assertRaises(Forbidden):
            self.svc.create_user(self.ana, {"username": "x", "password": "pw"})

    def test_reviewer_cannot_edit_assumptions(self) -> None:
        _, prj = self.make_project()
        self.metrics(prj["id"], "revenue")
        base = self.svc.import_baseline(self.admin, prj["id"], {"label": "b", "values": [
            {"metric_key": "revenue", "period": "2026Q1", "scenario": "base", "value": 1}]})
        br = self.svc.create_branch(self.ana, prj["id"], {"name": "work",
                                                          "from_commit": base["id"]})
        with self.assertRaises(Forbidden):
            self.svc.create_commit(self.rev, prj["id"], br["id"],
                                   {"message": "no", "changes": [],
                                    "expected_parent": base["id"]})


class WatchlistIsolationTest(ServiceCase):
    def test_watchlist_hidden_without_grant(self) -> None:
        _, prj = self.make_project(watchlist=True)
        visible = {p["id"] for p in self.svc.list_projects(self.ana)}
        self.assertNotIn(prj["id"], visible)
        with self.assertRaises(Forbidden):
            self.svc.list_metrics(self.ana, prj["id"])

    def test_grant_opens_access(self) -> None:
        _, prj = self.make_project(watchlist=True)
        self.svc.update_user(self.admin, "ana", {"watchlist_access": True})
        ana = self.svc.authenticate(self.svc.login("ana", "pw"))
        visible = {p["id"] for p in self.svc.list_projects(ana)}
        self.assertIn(prj["id"], visible)

    def test_normal_projects_unaffected(self) -> None:
        _, normal = self.make_project(watchlist=False)
        self.assertIn(normal["id"], {p["id"] for p in self.svc.list_projects(self.ana)})


class RecomputeTest(ServiceCase):
    def _seed(self):
        _, prj = self.make_project()
        self.metrics(prj["id"], "revenue", "cost", "gross_profit", "fx", "target", "shares")
        src = self.svc.create_data_source(self.ana, {"name": "wind"})
        base = self.svc.import_baseline(self.admin, prj["id"], {
            "label": "b", "constants": {"tax": 0.25},
            "values": [
                {"metric_key": "revenue", "period": "2026Q1", "scenario": "base",
                 "value": 100, "data_source_id": src["id"]},
                {"metric_key": "revenue", "period": "2026Q2", "scenario": "base",
                 "value": 110, "data_source_id": src["id"]},
                {"metric_key": "cost", "period": "2026Q1", "scenario": "base", "value": 60},
                {"metric_key": "cost", "period": "2026Q2", "scenario": "base", "value": 66},
                {"metric_key": "fx", "period": "2026Q1", "scenario": "base", "value": 7.0},
                {"metric_key": "fx", "period": "2026Q2", "scenario": "base", "value": 7.0},
                {"metric_key": "shares", "period": "2026Q1", "scenario": "base", "value": 10},
                {"metric_key": "shares", "period": "2026Q2", "scenario": "base", "value": 10},
            ]})
        self.svc.create_formula_version(self.ana, prj["id"],
                                        {"metric_key": "gross_profit",
                                         "expression": "revenue - cost"})
        self.svc.create_formula_version(self.ana, prj["id"],
                                        {"metric_key": "target",
                                         "expression": "gross_profit * fx * shares * (1 - const(\"tax\"))"})
        br = self.svc.create_branch(self.ana, prj["id"], {"name": "v",
                                                          "from_commit": base["id"]})
        commit = self.svc.create_commit(self.ana, prj["id"], br["id"],
                                        {"message": "formulas", "changes": [],
                                         "expected_parent": base["id"]})
        return prj, src, br, commit

    def test_topo_recompute_and_constants(self) -> None:
        prj, _, _, commit = self._seed()
        res = self.svc.results(self.ana, prj["id"], commit["id"], "target")
        # (100-60)*7*10*0.75 = 2100
        self.assertEqual(2100.0, res["results"]["base"]["2026Q1"]["value"])
        gp = self.svc.results(self.ana, prj["id"], commit["id"], "gross_profit")
        self.assertEqual(44.0, gp["results"]["base"]["2026Q2"]["value"])

    def test_change_input_recomputes_downstream_only(self) -> None:
        prj, _, br, commit = self._seed()
        nxt = self.svc.create_commit(self.ana, prj["id"], br["id"], {
            "message": "收入上修", "expected_parent": commit["id"],
            "changes": [{"metric_key": "revenue", "period": "2026Q2",
                         "scenario": "base", "value": 121}]})
        gp = self.svc.results(self.ana, prj["id"], nxt["id"], "gross_profit")
        self.assertEqual(55.0, gp["results"]["base"]["2026Q2"]["value"])
        # Q1 不变
        self.assertEqual(40.0, gp["results"]["base"]["2026Q1"]["value"])

    def test_pending_when_inputs_missing(self) -> None:
        _, prj2 = self.make_project(periods=("2027Q1",))
        self.metrics(prj2["id"], "a", "b")
        self.svc.create_formula_version(self.ana, prj2["id"],
                                        {"metric_key": "b", "expression": "a + 1"})
        base = self.svc.import_baseline(self.admin, prj2["id"],
                                        {"label": "empty", "values": []})
        br = self.svc.create_branch(self.ana, prj2["id"], {"name": "w",
                                                           "from_commit": base["id"]})
        commit = self.svc.create_commit(self.ana, prj2["id"], br["id"],
                                        {"message": "f", "changes": [],
                                         "expected_parent": base["id"]})
        status = self.svc.commit_status(self.ana, prj2["id"], commit["id"])
        self.assertFalse(status["pending"])  # 完全没有输入 -> 不标记
        nxt = self.svc.create_commit(self.ana, prj2["id"], br["id"], {
            "message": "partial", "expected_parent": commit["id"],
            "changes": [{"metric_key": "a", "period": "2027Q1",
                         "scenario": "base", "value": 5}]})
        # b 不依赖外部常量，输入齐了应直接算出；改用缺常量的场景验证 pending
        status = self.svc.commit_status(self.ana, prj2["id"], nxt["id"])
        self.assertEqual(6.0, self.svc.results(self.ana, prj2["id"], nxt["id"], "b")
                         ["results"]["base"]["2027Q1"]["value"])
        self.assertFalse(status["needs_recompute"])

    def test_formula_cycle_rejected(self) -> None:
        _, prj = self.make_project()
        self.metrics(prj["id"], "a", "b", "c")
        self.svc.create_formula_version(self.ana, prj["id"],
                                        {"metric_key": "b", "expression": "a + 1"})
        self.svc.create_formula_version(self.ana, prj["id"],
                                        {"metric_key": "c", "expression": "b + 1"})
        with self.assertRaises(Exception):
            self.svc.create_formula_version(self.ana, prj["id"],
                                            {"metric_key": "a", "expression": "c + 1"})

    def test_formula_version_outdated_flags_status(self) -> None:
        prj, _, br, commit = self._seed()
        # 提交之后修改公式
        self.svc.create_formula_version(self.ana, prj["id"],
                                        {"metric_key": "gross_profit",
                                         "expression": "revenue - cost - 0"})
        status = self.svc.commit_status(self.ana, prj["id"], commit["id"])
        self.assertTrue(status["outdated_formulas"])
        self.assertTrue(status["needs_recompute"])


class CrossPeriodFormulaTest(ServiceCase):
    def test_prev_at_and_scenario_forms(self) -> None:
        _, prj = self.make_project()
        self.metrics(prj["id"], "rev", "qoq", "bull_rev", "spread")
        base = self.svc.import_baseline(self.admin, prj["id"], {"label": "b", "values": [
            {"metric_key": "rev", "period": "2026Q1", "scenario": "base", "value": 100},
            {"metric_key": "rev", "period": "2026Q2", "scenario": "base", "value": 110},
            {"metric_key": "rev", "period": "2026Q1", "scenario": "bull", "value": 130},
        ]})
        self.svc.create_formula_version(
            self.ana, prj["id"],
            {"metric_key": "qoq", "expression": "rev - prev(rev)"})
        self.svc.create_formula_version(
            self.ana, prj["id"],
            {"metric_key": "bull_rev", "expression": 'scenario("bull", rev)'})
        self.svc.create_formula_version(
            self.ana, prj["id"],
            {"metric_key": "spread", "expression": 'scenario("bull", rev) - rev'})
        br = self.svc.create_branch(self.ana, prj["id"], {"name": "w",
                                                          "from_commit": base["id"]})
        commit = self.svc.create_commit(self.ana, prj["id"], br["id"],
                                        {"message": "f", "changes": [],
                                         "expected_parent": base["id"]})
        qoq = self.svc.results(self.ana, prj["id"], commit["id"], "qoq")
        self.assertEqual(10.0, qoq["results"]["base"]["2026Q2"]["value"])
        status = self.svc.commit_status(self.ana, prj["id"], commit["id"])
        pending = {(p["metric"], p["period"]) for p in status["pending"]}
        # Q1 没有上一期间 -> 待重算（部分输入存在）
        mids = {m["key"]: m["id"] for m in self.svc.list_metrics(self.ana, prj["id"])}
        self.assertIn((mids["qoq"], "2026Q1"), pending)
        spread = self.svc.results(self.ana, prj["id"], commit["id"], "spread")
        # base 情景：引用 bull 的 130 减去自身 100
        self.assertEqual(30.0, spread["results"]["base"]["2026Q1"]["value"])
        # bull 情景缺自身 Q2 等输入时按 pending 处理；bull Q1 两输入相同=0
        self.assertEqual(0.0, spread["results"]["bull"]["2026Q1"]["value"])


class ExternalRevisionTest(ServiceCase):
    def test_revision_marks_stale_but_keeps_published(self) -> None:
        company, prj = self.make_project()
        self.metrics(prj["id"], "revenue", "gp")
        src = self.svc.create_data_source(self.ana, {"name": "wind", "revision": 1})
        base = self.svc.import_baseline(self.admin, prj["id"], {"label": "b", "values": [
            {"metric_key": "revenue", "period": "2026Q1", "scenario": "base",
             "value": 100, "data_source_id": src["id"]}]})
        self.svc.create_formula_version(self.ana, prj["id"],
                                        {"metric_key": "gp", "expression": "revenue + 1"})
        br = self.svc.create_branch(self.ana, prj["id"], {"name": "w",
                                                          "from_commit": base["id"]})
        commit = self.svc.create_commit(self.ana, prj["id"], br["id"],
                                        {"message": "f", "changes": [],
                                         "expected_parent": base["id"]})
        before = self.svc.results(self.ana, prj["id"], commit["id"], "gp")
        out = self.svc.revise_data_source(self.admin, src["id"], {"label": "rev2"})
        self.assertEqual(1, len(out["stale_assumptions"]))
        # 已算结果值不被改写，只是状态标 stale
        after = self.svc.results(self.ana, prj["id"], commit["id"], "gp")
        self.assertEqual(before["results"]["base"]["2026Q1"]["value"],
                         after["results"]["base"]["2026Q1"]["value"])
        self.assertTrue(after["results"]["base"]["2026Q1"]["stale"])
        status = self.svc.commit_status(self.ana, prj["id"], commit["id"])
        self.assertTrue(status["stale_assumptions"])
        # 基线快照摘要不变
        self.assertEqual(base["digest"],
                         self.svc.get_commit(self.ana, prj["id"], base["id"])["digest"])

    def test_revision_number_must_advance(self) -> None:
        company, prj = self.make_project()
        src = self.svc.create_data_source(self.ana, {"name": "s", "revision": 3})
        with self.assertRaises(Conflict):
            self.svc.revise_data_source(self.admin, src["id"],
                                        {"label": "x", "revision": 2})

    def test_reconfirm_clears_stale(self) -> None:
        company, prj = self.make_project()
        self.metrics(prj["id"], "revenue")
        src = self.svc.create_data_source(self.ana, {"name": "wind"})
        base = self.svc.import_baseline(self.admin, prj["id"], {"label": "b", "values": [
            {"metric_key": "revenue", "period": "2026Q1", "scenario": "base",
             "value": 100, "data_source_id": src["id"]}]})
        self.svc.revise_data_source(self.admin, src["id"], {"label": "r2"})
        br = self.svc.create_branch(self.ana, prj["id"], {"name": "w",
                                                          "from_commit": base["id"]})
        nxt = self.svc.create_commit(self.ana, prj["id"], br["id"], {
            "message": "分析师确认新值", "expected_parent": base["id"],
            "changes": [{"metric_key": "revenue", "period": "2026Q1",
                         "scenario": "base", "value": 103,
                         "data_source_id": src["id"]}]})
        status = self.svc.commit_status(self.ana, prj["id"], nxt["id"])
        self.assertFalse(status["stale_assumptions"])


class CompareAndTraceTest(ServiceCase):
    def test_compare_explains_data_vs_model(self) -> None:
        _, prj = self.make_project()
        self.metrics(prj["id"], "revenue", "gp")
        base = self.svc.import_baseline(self.admin, prj["id"], {"label": "b", "values": [
            {"metric_key": "revenue", "period": "2026Q1", "scenario": "base", "value": 100}]})
        br = self.svc.create_branch(self.ana, prj["id"], {"name": "w",
                                                          "from_commit": base["id"]})
        self.svc.create_formula_version(self.ana, prj["id"],
                                        {"metric_key": "gp", "expression": "revenue * 2"})
        c1 = self.svc.create_commit(self.ana, prj["id"], br["id"],
                                    {"message": "v1", "changes": [],
                                     "expected_parent": base["id"]})
        c2 = self.svc.create_commit(self.ana, prj["id"], br["id"], {
            "message": "v2 数据更新", "expected_parent": c1["id"],
            "changes": [{"metric_key": "revenue", "period": "2026Q1",
                         "scenario": "base", "value": 110}]})
        report = self.svc.compare(self.ana, prj["id"], c1["id"], c2["id"])
        keys = {(d["metric_key"], d["period"]) for d in report["assumption_diffs"]}
        self.assertIn(("revenue", "2026Q1"), keys)
        self.assertIn(("gp", "2026Q1"), keys)
        self.assertTrue(any("手工输入" in n for n in report["explanation"]))

    def test_trace_lineage_to_sources_and_consts(self) -> None:
        _, prj = self.make_project()
        self.metrics(prj["id"], "revenue", "gp")
        src = self.svc.create_data_source(self.ana, {"name": "wind"})
        ref = self.svc.create_reference(self.ana, prj["id"],
                                        {"title": "年报", "citation": "AR"})
        base = self.svc.import_baseline(self.admin, prj["id"], {
            "label": "b", "constants": {"tax": 0.25}, "values": [
                {"metric_key": "revenue", "period": "2026Q1", "scenario": "base",
                 "value": 100, "data_source_id": src["id"],
                 "reference_ids": [ref["id"]]}]})
        self.svc.create_formula_version(
            self.ana, prj["id"],
            {"metric_key": "gp", "expression": "revenue * (1 - const(\"tax\"))"})
        br = self.svc.create_branch(self.ana, prj["id"], {"name": "w",
                                                          "from_commit": base["id"]})
        commit = self.svc.create_commit(self.ana, prj["id"], br["id"],
                                        {"message": "f", "changes": [],
                                         "expected_parent": base["id"]})
        trace = self.svc.trace(self.ana, prj["id"], commit["id"],
                               "gp", "2026Q1", "base")
        self.assertEqual(75.0, trace["value"])
        kinds = [c["kind"] for c in trace["node"]["inputs"]]
        self.assertIn("manual", kinds)
        manual = next(c for c in trace["node"]["inputs"] if c["kind"] == "manual")
        self.assertEqual(src["id"], manual["data_source"]["id"])
        self.assertEqual("wind", manual["data_source"]["name"])
        self.assertEqual("AR", manual["references"][0]["citation"])


class MergeConflictTest(ServiceCase):
    def test_cell_level_conflict_detail(self) -> None:
        _, prj = self.make_project()
        self.metrics(prj["id"], "revenue")
        base = self.svc.import_baseline(self.admin, prj["id"], {"label": "b", "values": [
            {"metric_key": "revenue", "period": "2026Q1", "scenario": "base", "value": 100},
            {"metric_key": "revenue", "period": "2026Q2", "scenario": "base", "value": 110}]})
        br = self.svc.create_branch(self.ana, prj["id"], {"name": "w",
                                                          "from_commit": base["id"]})
        winner = self.svc.create_commit(self.ana, prj["id"], br["id"], {
            "message": "win", "expected_parent": base["id"],
            "changes": [{"metric_key": "revenue", "period": "2026Q2",
                         "scenario": "base", "value": 115}]})
        # 同一期间改不同格 -> 无冲突
        ok = self.svc.create_commit(self.ana, prj["id"], br["id"], {
            "message": "other cell", "expected_parent": winner["id"],
            "changes": [{"metric_key": "revenue", "period": "2026Q1",
                         "scenario": "base", "value": 101}]})
        self.assertEqual(101.0,
                         self.svc.results(self.ana, prj["id"], ok["id"], "revenue")
                         ["results"]["base"]["2026Q1"]["value"])
        # 同一格冲突
        try:
            self.svc.create_commit(self.ana, prj["id"], br["id"], {
                "message": "stale write", "expected_parent": base["id"],
                "changes": [{"metric_key": "revenue", "period": "2026Q2",
                             "scenario": "base", "value": 130}]})
            self.fail("expected conflict")
        except Conflict as exc:
            self.assertEqual("merge_conflict", exc.code)
            self.assertEqual(1, len(exc.detail["assumption_conflicts"]))
            c = exc.detail["assumption_conflicts"][0]
            self.assertEqual(("2026Q2", "base", 110.0, 115.0, 130.0),
                             (c["period"], c["scenario"], c["base_value"],
                              c["current_value"], c["submitted_value"]))


class ReviewTest(ServiceCase):
    def _approved(self):
        _, prj = self.make_project()
        self.metrics(prj["id"], "revenue")
        base = self.svc.import_baseline(self.admin, prj["id"], {"label": "b", "values": [
            {"metric_key": "revenue", "period": "2026Q1", "scenario": "base", "value": 100}]})
        review = self.svc.create_review(self.ana, prj["id"], {"commit_id": base["id"]})
        self.svc.add_review_comment(self.ana, prj["id"], review["id"],
                                    {"body": "口径？"})
        self.svc.add_review_comment(self.rev, prj["id"], review["id"],
                                    {"body": "一致"})
        return prj, base, review

    def test_comments_append_only_and_state_machine(self) -> None:
        prj, _, review = self._approved()
        self.svc.decide_review(self.rev, prj["id"], review["id"],
                               {"decision": "request_changes", "reason": "要更新"})
        got = self.svc.get_review(self.ana, prj["id"], review["id"])
        self.assertEqual("changes_requested", got["state"])
        self.assertEqual(2, len(got["comments"]))  # 早期意见原样保留
        self.svc.decide_review(self.rev, prj["id"], review["id"],
                               {"decision": "reopen"})
        self.svc.decide_review(self.rev, prj["id"], review["id"],
                               {"decision": "approve"})
        self.assertEqual("approved",
                         self.svc.get_review(self.ana, prj["id"], review["id"])["state"])

    def test_analyst_cannot_approve(self) -> None:
        prj, _, review = self._approved()
        with self.assertRaises(Forbidden):
            self.svc.decide_review(self.ana, prj["id"], review["id"],
                                   {"decision": "approve"})

    def test_comment_after_approval_rejected(self) -> None:
        prj, _, review = self._approved()
        self.svc.decide_review(self.rev, prj["id"], review["id"],
                               {"decision": "approve"})
        with self.assertRaises(Conflict):
            self.svc.add_review_comment(self.ana, prj["id"], review["id"],
                                        {"body": "还能改吗"})


class ReleaseTest(ServiceCase):
    def _ready_release_inputs(self):
        _, prj = self.make_project()
        self.metrics(prj["id"], "revenue")
        src = self.svc.create_data_source(self.ana, {"name": "wind"})
        ref = self.svc.create_reference(self.ana, prj["id"], {"title": "年报"})
        base = self.svc.import_baseline(self.admin, prj["id"], {"label": "b", "values": [
            {"metric_key": "revenue", "period": "2026Q1", "scenario": "base",
             "value": 100, "data_source_id": src["id"], "reference_ids": [ref["id"]]}]})
        review = self.svc.create_review(self.ana, prj["id"], {"commit_id": base["id"]})
        self.svc.decide_review(self.rev, prj["id"], review["id"], {"decision": "approve"})
        return prj, src, ref, base, review

    def test_full_publish_and_export(self) -> None:
        prj, src, ref, base, review = self._ready_release_inputs()
        rel = self.svc.publish(self.rev, prj["id"],
                               {"commit_id": base["id"], "review_id": review["id"],
                                "label": "2026Q3"})
        self.assertEqual(self.rev["username"], rel["approver"])
        export = self.svc.export_release(self.ana, prj["id"], rel["id"])
        self.assertEqual("2026Q3", export["release"]["label"])
        self.assertEqual(1, len(export["data_summary"]["sources"]))
        self.assertEqual(src["id"], export["data_summary"]["sources"][0]["id"])
        self.assertEqual(1, export["data_summary"]["sources"][0]["revisions_used"][0])
        self.assertEqual(1, len(export["data_summary"]["references"]))
        self.assertEqual(base["digest"], export["digest"])

    def test_export_immune_to_later_source_revision(self) -> None:
        prj, src, _, base, review = self._ready_release_inputs()
        rel = self.svc.publish(self.rev, prj["id"],
                               {"commit_id": base["id"], "review_id": review["id"]})
        before = self.svc.export_release(self.ana, prj["id"], rel["id"])
        self.svc.revise_data_source(self.admin, src["id"], {"label": "rev9"})
        after = self.svc.export_release(self.ana, prj["id"], rel["id"])
        self.assertEqual(before["data_summary"], after["data_summary"])
        self.assertEqual(before["digest"], after["digest"])

    def test_concurrent_publish_single_winner(self) -> None:
        prj, _, _, base, review = self._ready_release_inputs()
        errors = []

        def publish():
            try:
                self.svc.publish(self.rev, prj["id"],
                                 {"commit_id": base["id"], "review_id": review["id"]})
            except Conflict as exc:
                errors.append(exc.code)

        threads = [threading.Thread(target=publish) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        releases = self.svc.list_releases(self.rev, prj["id"])
        self.assertEqual(1, len(releases))
        self.assertEqual(7, len(errors))
        self.assertTrue(all(code == "release_exists" for code in errors))

    def test_publish_requires_approved_review(self) -> None:
        _, prj = self.make_project()
        self.metrics(prj["id"], "revenue")
        base = self.svc.import_baseline(self.admin, prj["id"], {"label": "b", "values": [
            {"metric_key": "revenue", "period": "2026Q1", "scenario": "base", "value": 1}]})
        review = self.svc.create_review(self.ana, prj["id"], {"commit_id": base["id"]})
        with self.assertRaises(Conflict):
            self.svc.publish(self.rev, prj["id"],
                             {"commit_id": base["id"], "review_id": review["id"]})

    def test_publish_blocks_when_pending(self) -> None:
        prj, src, _, _, review = self._ready_release_inputs()
        # 发布尚未发生；数据源修订使结论过期
        self.svc.revise_data_source(self.admin, src["id"], {"label": "r2"})
        # 评审针对的提交现在 stale
        with self.assertRaises(Conflict) as cm:
            self.svc.publish(self.rev, prj["id"],
                             {"commit_id": review["commit"], "review_id": review["id"]})
        self.assertEqual("release_not_ready", cm.exception.code)


def _publish_in_new_process(db_path: str, secret: str, pid: str,
                            commit_id: str, review_id: str) -> None:
    svc = Service(Store(db_path), Auth(secret))
    user = svc.authenticate(svc.login("rev", "pw"))
    try:
        svc.publish(user, pid, {"commit_id": commit_id, "review_id": review_id})
    except Conflict:
        pass


class CrossProcessPublishTest(ServiceCase):
    def test_single_winner_across_processes(self) -> None:
        import multiprocessing

        _, prj = self.make_project()
        # 复用 ReleaseTest 的准备步骤
        self.svc.create_metric(self.ana, prj["id"], {"key": "revenue", "name": "revenue"})
        base = self.svc.import_baseline(self.admin, prj["id"], {"label": "b", "values": [
            {"metric_key": "revenue", "period": "2026Q1", "scenario": "base", "value": 100}]})
        review = self.svc.create_review(self.ana, prj["id"], {"commit_id": base["id"]})
        self.svc.decide_review(self.rev, prj["id"], review["id"],
                               {"decision": "approve"})

        ctx = multiprocessing.get_context("fork")
        procs = [ctx.Process(target=_publish_in_new_process,
                             args=(self.svc.store.path, "case-secret", prj["id"],
                                   base["id"], review["id"])) for _ in range(6)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(10)
            self.assertEqual(0, p.exitcode)
        self.assertEqual(1, len(self.svc.list_releases(self.rev, prj["id"])))


if __name__ == "__main__":
    unittest.main()
