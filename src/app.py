from __future__ import annotations

import json
import os
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

from serverdb.auth import Auth
from serverdb.errors import ApiError
from serverdb.service import Service
from serverdb.storage import Store

SERVICE_NAME = '投研假设协作服务'

# (method, compiled_path_regex, handler_name)
# 路径段中的 {name} 捕获为关键字参数；查询串统一放入 body["_query"]。


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


def _q(query: dict[str, list[str]], name: str) -> str:
    val = query.get(name)
    if not val:
        raise ApiError(f"缺少查询参数: {name}", code="bad_request", status=400)
    return val[0]


def _build_service(db_path: str | None = None) -> Service:
    path = db_path or os.getenv("SERVERDB_DB", ".runtime/db.json")
    secret = os.getenv("SERVERDB_SECRET")
    return Service(Store(path), Auth(secret))


def create_server(host: str, port: int, db_path: str | None = None) -> ThreadingHTTPServer:
    service = _build_service(db_path)
    handler = _make_handler(service)
    return ThreadingHTTPServer((host, port), handler)


def _make_handler(service: Service) -> type[BaseHTTPRequestHandler]:
    routes: list[tuple[str, re.Pattern[str], str]] = []

    def route(method: str, pattern: str):
        regex = re.compile(r"^/api/v1" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + r"$")

        def deco(fn):
            routes.append((method, regex, fn.__name__))
            return fn

        return deco

    class Handler(BaseHTTPRequestHandler):
        server_version = "ResearchAssumptions/1.0"

        # ---- 认证/公司/用户 ----------------------------------------------

        @route("POST", "/auth/login")
        def auth_login(self, body, q):
            return {"token": service.login(str(body.get("username", "")),
                                           str(body.get("password", "")))}

        @route("POST", "/users")
        def users_create(self, body, q):
            return service.create_user(self._user(), body)

        @route("GET", "/users")
        def users_list(self, body, q):
            return service.list_users(self._require_auth())

        @route("PATCH", "/users/{username}")
        def users_update(self, body, q, username):
            return service.update_user(self._require_auth(), username, body)

        @route("POST", "/companies")
        def companies_create(self, body, q):
            return service.create_company(self._require_auth(), body)

        @route("GET", "/companies")
        def companies_list(self, body, q):
            return service.list_companies(self._require_auth())

        # ---- 项目 ---------------------------------------------------------

        @route("POST", "/projects")
        def projects_create(self, body, q):
            return service.create_project(self._require_auth(), body)

        @route("GET", "/projects")
        def projects_list(self, body, q):
            return service.list_projects(self._require_auth())

        @route("GET", "/projects/{pid}")
        def project_get(self, body, q, pid):
            return service.get_project(self._require_auth(), pid)

        @route("POST", "/projects/{pid}/periods")
        def project_periods(self, body, q, pid):
            return service.add_periods(self._require_auth(), pid, body)

        @route("GET", "/projects/{pid}/members")
        def members_list(self, body, q, pid):
            return service.list_members(self._require_auth(), pid)

        @route("POST", "/projects/{pid}/members")
        def members_add(self, body, q, pid):
            return service.add_member(self._require_auth(), pid, body)

        # ---- 指标 / 引用 / 公式 ------------------------------------------

        @route("POST", "/projects/{pid}/metrics")
        def metrics_create(self, body, q, pid):
            return service.create_metric(self._require_auth(), pid, body)

        @route("GET", "/projects/{pid}/metrics")
        def metrics_list(self, body, q, pid):
            return service.list_metrics(self._require_auth(), pid)

        @route("POST", "/projects/{pid}/references")
        def references_create(self, body, q, pid):
            return service.create_reference(self._require_auth(), pid, body)

        @route("GET", "/projects/{pid}/references")
        def references_list(self, body, q, pid):
            return service.list_references(self._require_auth(), pid)

        @route("POST", "/projects/{pid}/formulas")
        def formulas_create(self, body, q, pid):
            return service.create_formula_version(self._require_auth(), pid, body)

        @route("GET", "/projects/{pid}/formulas")
        def formulas_list(self, body, q, pid):
            return service.list_formulas(self._require_auth(), pid)

        # ---- 基线 / 分支 / 提交 ------------------------------------------

        @route("POST", "/projects/{pid}/baseline/import")
        def baseline_import(self, body, q, pid):
            return service.import_baseline(self._require_auth(), pid, body)

        @route("GET", "/projects/{pid}/baseline")
        def baseline_get(self, body, q, pid):
            return service.baseline(self._require_auth(), pid)

        @route("POST", "/projects/{pid}/branches")
        def branches_create(self, body, q, pid):
            return service.create_branch(self._require_auth(), pid, body)

        @route("GET", "/projects/{pid}/branches")
        def branches_list(self, body, q, pid):
            return service.list_branches(self._require_auth(), pid)

        @route("GET", "/projects/{pid}/branches/{bid}")
        def branch_get(self, body, q, pid, bid):
            return service.get_branch(self._require_auth(), pid, bid)

        @route("GET", "/projects/{pid}/branches/{bid}/status")
        def branch_status(self, body, q, pid, bid):
            return service.branch_status(self._require_auth(), pid, bid)

        @route("POST", "/projects/{pid}/branches/{bid}/commits")
        def commits_create(self, body, q, pid, bid):
            return service.create_commit(self._require_auth(), pid, bid, body)

        @route("GET", "/projects/{pid}/commits/{cid}")
        def commit_get(self, body, q, pid, cid):
            return service.get_commit(self._require_auth(), pid, cid)

        @route("GET", "/projects/{pid}/commits/{cid}/status")
        def commit_status(self, body, q, pid, cid):
            return service.commit_status(self._require_auth(), pid, cid)

        @route("GET", "/projects/{pid}/compare")
        def compare(self, body, q, pid):
            return service.compare(self._require_auth(), pid,
                                   _q(q, "a"), _q(q, "b"))

        @route("GET", "/projects/{pid}/commits/{cid}/trace")
        def trace(self, body, q, pid, cid):
            return service.trace(self._require_auth(), pid, cid,
                                 _q(q, "metric"), _q(q, "period"), _q(q, "scenario"))

        @route("GET", "/projects/{pid}/commits/{cid}/results/{metric_key}")
        def results(self, body, q, pid, cid, metric_key):
            return service.results(self._require_auth(), pid, cid, metric_key)

        # ---- 数据源 -------------------------------------------------------

        @route("POST", "/data-sources")
        def sources_create(self, body, q):
            return service.create_data_source(self._require_auth(), body)

        @route("GET", "/data-sources")
        def sources_list(self, body, q):
            return service.list_data_sources(self._require_auth())

        @route("POST", "/data-sources/{sid}/revisions")
        def source_revise(self, body, q, sid):
            return service.revise_data_source(self._require_auth(), sid, body)

        # ---- 评审 ---------------------------------------------------------

        @route("POST", "/projects/{pid}/reviews")
        def reviews_create(self, body, q, pid):
            return service.create_review(self._require_auth(), pid, body)

        @route("GET", "/projects/{pid}/reviews")
        def reviews_list(self, body, q, pid):
            return service.list_reviews(self._require_auth(), pid)

        @route("GET", "/projects/{pid}/reviews/{rid}")
        def review_get(self, body, q, pid, rid):
            return service.get_review(self._require_auth(), pid, rid)

        @route("POST", "/projects/{pid}/reviews/{rid}/comments")
        def review_comment(self, body, q, pid, rid):
            return service.add_review_comment(self._require_auth(), pid, rid, body)

        @route("POST", "/projects/{pid}/reviews/{rid}/decision")
        def review_decide(self, body, q, pid, rid):
            return service.decide_review(self._require_auth(), pid, rid, body)

        # ---- 发布 / 导出 --------------------------------------------------

        @route("POST", "/projects/{pid}/releases")
        def releases_publish(self, body, q, pid):
            return service.publish(self._require_auth(), pid, body)

        @route("GET", "/projects/{pid}/releases")
        def releases_list(self, body, q, pid):
            return service.list_releases(self._require_auth(), pid)

        @route("GET", "/projects/{pid}/releases/{rid}")
        def release_get(self, body, q, pid, rid):
            return service.get_release(self._require_auth(), pid, rid)

        @route("GET", "/projects/{pid}/releases/{rid}/export")
        def release_export(self, body, q, pid, rid):
            return service.export_release(self._require_auth(), pid, rid)

        # ---- HTTP 机制 ----------------------------------------------------

        def _user(self):
            return service.authenticate(self._token())

        def _require_auth(self):
            user = self._user()
            if user is None:
                raise ApiError("缺少有效身份令牌", code="unauthorized", status=401)
            return user

        def _token(self) -> str | None:
            header = self.headers.get("Authorization", "")
            if header.startswith("Bearer "):
                return header[7:].strip()
            return self.headers.get("X-Auth-Token")

        def _dispatch(self, method: str):
            parts = urlsplit(self.path)
            if parts.path == "/health":
                self._write_json(200, health_payload())
                return
            for m, regex, name in routes:
                if m != method:
                    continue
                match = regex.match(parts.path)
                if match:
                    try:
                        body = self._read_body() if method in ("POST", "PATCH", "PUT") else {}
                        result = getattr(self, name)(body, parse_qs(parts.query), **match.groupdict())
                    except ApiError as exc:
                        self._write_json(exc.status, exc.to_payload())
                        return
                    except Exception:  # noqa: BLE001 - 兜底，保证输出 JSON
                        traceback.print_exc()
                        self._write_json(500, {"error": "internal_error",
                                               "message": "服务内部错误"})
                        return
                    self._write_json(200, result)
                    return
            self._write_json(404, {"error": "not_found", "message": f"无此路由: {method} {parts.path}"})

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > 4_000_000:
                raise ApiError("请求体过大", code="payload_too_large", status=413)
            raw = self.rfile.read(length)
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ApiError("请求体不是合法 JSON", code="bad_json") from exc
            if not isinstance(parsed, dict):
                raise ApiError("请求体必须是 JSON 对象")
            return parsed

        def _write_json(self, status: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler
