from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app import create_server  # noqa: E402


class HttpCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "db.json")
        self.server = create_server("127.0.0.1", 0, self.db)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def call(self, method: str, path: str, body=None, token: str | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    # ---- 夹具 --------------------------------------------------------------

    def bootstrap(self) -> dict[str, str]:
        self.call("POST", "/api/v1/users",
                  {"username": "boss", "password": "pw", "role": "admin",
                   "watchlist_access": True})
        admin = self.call("POST", "/api/v1/auth/login",
                          {"username": "boss", "password": "pw"})[1]["token"]
        self.call("POST", "/api/v1/users",
                  {"username": "ana", "password": "pw", "role": "analyst"}, admin)
        self.call("POST", "/api/v1/users",
                  {"username": "rev", "password": "pw", "role": "reviewer"}, admin)
        ana = self.call("POST", "/api/v1/auth/login",
                        {"username": "ana", "password": "pw"})[1]["token"]
        rev = self.call("POST", "/api/v1/auth/login",
                        {"username": "rev", "password": "pw"})[1]["token"]
        _, company = self.call("POST", "/api/v1/companies", {"name": "甲"}, admin)
        _, project = self.call("POST", "/api/v1/projects", {
            "name": "Q3", "company_id": company["id"],
            "scenarios": ["base"], "periods": ["2026Q1", "2026Q2"]}, admin)
        pid = project["id"]
        self.call("POST", f"/api/v1/projects/{pid}/members",
                  {"username": "ana"}, admin)
        self.call("POST", f"/api/v1/projects/{pid}/members",
                  {"username": "rev"}, admin)
        return {"admin": admin, "ana": ana, "rev": rev, "pid": pid}

    def seed_workflow(self, ctx):
        pid, ana, rev, admin = ctx["pid"], ctx["ana"], ctx["rev"], ctx["admin"]
        for key in ("revenue", "cost", "gp"):
            self.call("POST", f"/api/v1/projects/{pid}/metrics",
                      {"key": key, "name": key}, ana)
        _, src = self.call("POST", "/api/v1/data-sources", {"name": "wind"}, ana)
        _, base = self.call("POST", f"/api/v1/projects/{pid}/baseline/import", {
            "label": "b", "values": [
                {"metric_key": "revenue", "period": "2026Q1", "scenario": "base",
                 "value": 100, "data_source_id": src["id"]},
                {"metric_key": "revenue", "period": "2026Q2", "scenario": "base",
                 "value": 110, "data_source_id": src["id"]},
                {"metric_key": "cost", "period": "2026Q1", "scenario": "base", "value": 60},
                {"metric_key": "cost", "period": "2026Q2", "scenario": "base", "value": 66},
            ]}, admin)
        self.call("POST", f"/api/v1/projects/{pid}/formulas",
                  {"metric_key": "gp", "expression": "revenue - cost"}, ana)
        _, br = self.call("POST", f"/api/v1/projects/{pid}/branches",
                          {"name": "work", "from_commit": base["id"]}, ana)
        _, commit = self.call("POST", f"/api/v1/projects/{pid}/branches/{br['id']}/commits",
                              {"message": "formula", "changes": [],
                               "expected_parent": base["id"]}, ana)
        return {"src": src, "base": base, "branch": br, "commit": commit}


class HealthTest(unittest.TestCase):
    def test_health(self) -> None:
        # 保留原有无依赖健康检查行为
        from app import health_payload
        self.assertEqual("ok", health_payload()["status"])


class HttpWorkflowTest(HttpCase):
    def test_health_endpoint(self) -> None:
        status, payload = self.call("GET", "/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_auth_required(self) -> None:
        status, payload = self.call("GET", "/api/v1/projects")
        self.assertEqual(401, status)
        self.assertEqual("unauthorized", payload["error"])

    def test_bad_json_is_400(self) -> None:
        url = f"http://127.0.0.1:{self.port}/api/v1/auth/login"
        req = urllib.request.Request(url, data=b"{not json", method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req)
            self.fail()
        except urllib.error.HTTPError as exc:
            self.assertEqual(400, exc.code)

    def test_full_workflow_over_http(self) -> None:
        ctx = self.bootstrap()
        pid, ana, rev = ctx["pid"], ctx["ana"], ctx["rev"]
        wf = self.seed_workflow(ctx)
        commit = wf["commit"]

        # 结果可查
        status, res = self.call(
            "GET", f"/api/v1/projects/{pid}/commits/{commit['id']}/results/gp", token=ana)
        self.assertEqual(200, status)
        self.assertEqual(40.0, res["results"]["base"]["2026Q1"]["value"])
        self.assertEqual(44.0, res["results"]["base"]["2026Q2"]["value"])

        # 血缘
        status, trace = self.call(
            "GET", f"/api/v1/projects/{pid}/commits/{commit['id']}/trace"
                  f"?metric=gp&period=2026Q1&scenario=base", token=ana)
        self.assertEqual(200, status)
        revenue_node = next(c for c in trace["node"]["inputs"]
                            if c.get("metric_key") == "revenue")
        self.assertEqual("wind", revenue_node["data_source"]["name"])

        # 公式防环
        status, payload = self.call(
            "POST", f"/api/v1/projects/{pid}/formulas",
            {"metric_key": "revenue", "expression": "gp + 1"}, ana)
        self.assertEqual(400, status)
        self.assertEqual("formula_cycle", payload["error"])

        # 提交 + 乐观并发冲突
        status, winner = self.call(
            "POST", f"/api/v1/projects/{pid}/branches/{wf['branch']['id']}/commits",
            {"message": "win", "expected_parent": commit["id"],
             "changes": [{"metric_key": "revenue", "period": "2026Q2",
                          "scenario": "base", "value": 115}]}, ana)
        self.assertEqual(200, status)
        status, payload = self.call(
            "POST", f"/api/v1/projects/{pid}/branches/{wf['branch']['id']}/commits",
            {"message": "stale", "expected_parent": commit["id"],
             "changes": [{"metric_key": "revenue", "period": "2026Q2",
                          "scenario": "base", "value": 130}]}, ana)
        self.assertEqual(409, status)
        self.assertEqual("merge_conflict", payload["error"])
        self.assertEqual(1, len(payload["assumption_conflicts"]))

        # 外部修订 -> stale 标记
        status, revised = self.call(
            "POST", f"/api/v1/data-sources/{wf['src']['id']}/revisions",
            {"label": "rev2"}, ctx["admin"])
        self.assertEqual(200, status)
        # Q1 收入仍绑定 wind -> 过期；Q2 被赢家提交改为无数据源的分析师预测 -> 不跟随
        self.assertEqual(1, len(revised["stale_assumptions"]))
        status, st = self.call(
            "GET", f"/api/v1/projects/{pid}/commits/{winner['id']}/status", token=ana)
        self.assertTrue(st["needs_recompute"])

        # 评审 -> 审批 -> 发布 -> 导出
        _, review = self.call("POST", f"/api/v1/projects/{pid}/reviews",
                              {"commit_id": winner["id"]}, ana)
        self.call("POST", f"/api/v1/projects/{pid}/reviews/{review['id']}/comments",
                  {"body": "收入确认依据？"}, ana)
        self.call("POST", f"/api/v1/projects/{pid}/reviews/{review['id']}/decision",
                  {"decision": "approve", "reason": "已核对"}, rev)
        status, payload = self.call(
            "POST", f"/api/v1/projects/{pid}/releases",
            {"commit_id": winner["id"], "review_id": review["id"],
             "acknowledge_pending": True, "label": "Q3权威"}, rev)
        self.assertEqual(200, status)
        release = payload
        status, again = self.call(
            "POST", f"/api/v1/projects/{pid}/releases",
            {"commit_id": winner["id"], "review_id": review["id"],
             "acknowledge_pending": True}, rev)
        self.assertEqual(409, status)
        self.assertEqual("release_exists", again["error"])

        status, export = self.call(
            "GET", f"/api/v1/projects/{pid}/releases/{release['id']}/export", token=ana)
        self.assertEqual(200, status)
        self.assertEqual(winner["digest"], export["digest"])
        self.assertEqual("rev", export["release"]["approver"])
        self.assertEqual("wind", export["data_summary"]["sources"][0]["name"])

        # 版本对比：为什么不同
        status, cmp_res = self.call(
            "GET", f"/api/v1/projects/{pid}/compare?a={commit['id']}&b={winner['id']}",
            token=ana)
        self.assertEqual(200, status)
        self.assertTrue(cmp_res["assumption_diffs"])
        self.assertTrue(cmp_res["explanation"])

    def test_watchlist_isolation_over_http(self) -> None:
        ctx = self.bootstrap()
        pid, ana, admin = ctx["pid"], ctx["ana"], ctx["admin"]
        _, company = self.call("GET", f"/api/v1/companies", token=admin)
        # 直接再造一个 watchlist 项目
        _, wl = self.call("POST", "/api/v1/projects", {
            "name": "敏感", "company_id": self.call("POST", "/api/v1/companies",
                                                    {"name": "乙"}, admin)[1]["id"],
            "scenarios": ["base"], "periods": ["2026Q1"], "watchlist": True}, admin)
        self.call("POST", f"/api/v1/projects/{wl['id']}/members",
                  {"username": "ana"}, admin)
        status, payload = self.call("GET", f"/api/v1/projects/{wl['id']}", token=ana)
        self.assertEqual(403, status)
        # 授予后可访问
        self.call("PATCH", "/api/v1/users/ana",
                  {"watchlist_access": True}, admin)
        ana2 = self.call("POST", "/api/v1/auth/login",
                         {"username": "ana", "password": "pw"})[1]["token"]
        self.assertEqual(200, self.call("GET", f"/api/v1/projects/{wl['id']}",
                                        token=ana2)[0])


if __name__ == "__main__":
    unittest.main()
