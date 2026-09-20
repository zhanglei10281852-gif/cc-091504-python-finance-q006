from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app import create_server
from service import ResearchService
from store import Store


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        tmp = tempfile.mkdtemp()
        service = ResearchService(
            Store(str(Path(tmp) / "state.json")), str(ROOT / "reference" / "domain.json")
        )
        cls.server = create_server("127.0.0.1", 0, service)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, method: str, path: str, body: dict | None = None, user: str | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if user:
            req.add_header("X-User-Id", user)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health(self):
        status, payload = self.request("GET", "/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_auth_required(self):
        status, payload = self.request("GET", "/projects")
        self.assertEqual(401, status)
        self.assertEqual("unauthenticated", payload["error"]["code"])

    def test_unknown_route(self):
        status, _ = self.request("GET", "/nope")
        self.assertEqual(404, status)

    def test_end_to_end_over_http(self):
        _, lead = self.request("POST", "/users", {"name": "主管"})
        lead = lead["id"]
        _, reviewer = self.request("POST", "/users", {"name": "复核"})
        reviewer = reviewer["id"]

        status, project = self.request(
            "POST",
            "/projects",
            {"name": "HTTP 项目", "members": {reviewer: "reviewer"}},
            user=lead,
        )
        self.assertEqual(201, status)
        pid, main = project["id"], project["main_branch_id"]

        _, company = self.request(
            "POST", "/companies", {"ticker": "BBB", "name": "乙公司"}, user=lead
        )
        _, metric = self.request(
            "POST",
            "/metrics",
            {"company_id": company["id"], "code": "revenue"},
            user=lead,
        )
        _, period = self.request("POST", "/periods", {"label": "FY2026"}, user=lead)

        status, _ = self.request(
            "PUT",
            f"/branches/{main}/assumptions",
            {
                "metric_id": metric["id"],
                "period_id": period["id"],
                "scenario": "base",
                "value": 42.0,
            },
            user=lead,
        )
        self.assertEqual(201, status)

        # 非审批人不能发布
        status, version = self.request(
            "POST", f"/projects/{pid}/publish", {"expected_version_id": None}, user=lead
        )
        self.assertEqual(201, status)  # lead 同时具备审批角色
        version_id = version["id"]

        # 重复以旧基线发布 -> 409
        status, payload = self.request(
            "POST",
            f"/projects/{pid}/publish",
            {"expected_version_id": None},
            user=reviewer,
        )
        self.assertEqual(409, status)
        self.assertEqual("stale_base", payload["error"]["code"])

        # 复核意见只追加
        self.request(
            "POST", f"/versions/{version_id}/comments", {"body": "同意"}, user=reviewer
        )
        status, comments = self.request("GET", f"/versions/{version_id}/comments", user=lead)
        self.assertEqual(200, status)
        self.assertEqual(1, len(comments["items"]))

        # 计算可追溯
        status, trace = self.request(
            "POST",
            f"/branches/{main}/compute",
            {"metric_id": metric["id"], "period_id": period["id"], "scenario": "base"},
            user=lead,
        )
        self.assertEqual(200, status)
        self.assertEqual(42.0, trace["value"])
        self.assertEqual("assumption", trace["via"])

        # 导出固定摘要与审批人
        status, export = self.request(
            "POST", f"/versions/{version_id}/exports", {}, user=lead
        )
        self.assertEqual(201, status)
        self.assertEqual(version["data_digest"], export["data_digest"])
        self.assertEqual(lead, export["approver_id"])

    def test_invalid_json(self):
        url = f"http://127.0.0.1:{self.port}/users"
        req = urllib.request.Request(url, data=b"{not json", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(400, ctx.exception.code)


if __name__ == "__main__":
    unittest.main()
