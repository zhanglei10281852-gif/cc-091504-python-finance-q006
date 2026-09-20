"""HTTP 层：路由、鉴权、JSON 编解码、错误映射。

业务规则全部在 service.py；本层只做协议转换。
所有接口（除 /health）要求 X-User-Id 请求头。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from service import MergeConflict, ResearchService, ServiceError

SERVICE_NAME = "投研假设协作服务"


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


# 路由表: (方法, 正则, 服务方法名)；路径参数以 (?P<name>...) 捕获
ROUTES: list[tuple[str, str, str]] = [
    ("POST", r"/users", "create_user"),
    ("GET", r"/projects", "list_projects"),
    ("POST", r"/projects", "create_project"),
    ("GET", r"/projects/(?P<project_id>[^/]+)", "get_project"),
    ("GET", r"/projects/(?P<project_id>[^/]+)/branches", "list_branches"),
    ("POST", r"/projects/(?P<project_id>[^/]+)/branches", "create_branch"),
    ("POST", r"/projects/(?P<project_id>[^/]+)/publish", "publish"),
    ("GET", r"/projects/(?P<project_id>[^/]+)/versions", "list_versions"),
    ("GET", r"/projects/(?P<project_id>[^/]+)/citations", "list_citations"),
    ("POST", r"/projects/(?P<project_id>[^/]+)/citations", "create_citation"),
    ("GET", r"/companies", "list_companies"),
    ("POST", r"/companies", "create_company"),
    ("GET", r"/metrics", "list_metrics"),
    ("POST", r"/metrics", "create_metric"),
    ("GET", r"/periods", "list_periods"),
    ("POST", r"/periods", "create_period"),
    ("GET", r"/sources", "list_sources"),
    ("POST", r"/sources", "create_source"),
    ("POST", r"/observations", "create_observation"),
    ("GET", r"/branches/(?P<branch_id>[^/]+)/assumptions", "list_assumptions"),
    ("PUT", r"/branches/(?P<branch_id>[^/]+)/assumptions", "put_assumption"),
    ("GET", r"/branches/(?P<branch_id>[^/]+)/formulas", "list_formulas"),
    ("PUT", r"/branches/(?P<branch_id>[^/]+)/formulas", "put_formula"),
    ("GET", r"/branches/(?P<branch_id>[^/]+)/pending", "pending_recompute"),
    ("POST", r"/branches/(?P<branch_id>[^/]+)/compute", "compute_endpoint"),
    ("POST", r"/branches/(?P<branch_id>[^/]+)/merge", "merge_branch"),
    ("GET", r"/versions/(?P<version_id>[^/]+)", "get_version"),
    ("GET", r"/versions/(?P<version_id>[^/]+)/diff", "diff_endpoint"),
    ("GET", r"/versions/(?P<version_id>[^/]+)/comments", "list_comments"),
    ("POST", r"/versions/(?P<version_id>[^/]+)/comments", "add_comment"),
    ("POST", r"/versions/(?P<version_id>[^/]+)/exports", "create_export"),
    ("GET", r"/exports/(?P<export_id>[^/]+)", "get_export"),
]

_COMPILED = [(method, re.compile(f"^{pattern}$"), name) for method, pattern, name in ROUTES]


class RequestHandler(BaseHTTPRequestHandler):
    service: ResearchService  # 由 create_server 注入

    # ------------------------------------------------------------ 基础工具

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, code: str, message: str, extra: dict | None = None) -> None:
        payload = {"error": {"code": code, "message": message}}
        if extra:
            payload["error"].update(extra)
        self._send_json(status, payload)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ServiceError(400, "invalid_json", "请求体不是合法 JSON")
        if not isinstance(parsed, dict):
            raise ServiceError(400, "invalid_json", "请求体必须是 JSON 对象")
        return parsed

    # ------------------------------------------------------------ 分发

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(200, health_payload())
            return
        self._dispatch("GET", parsed)

    def do_POST(self) -> None:
        self._dispatch("POST", urlparse(self.path))

    def do_PUT(self) -> None:
        self._dispatch("PUT", urlparse(self.path))

    def _dispatch(self, method: str, parsed) -> None:
        try:
            for route_method, pattern, name in _COMPILED:
                if route_method != method:
                    continue
                match = pattern.match(parsed.path)
                if not match:
                    continue
                body = self._read_body() if method in ("POST", "PUT") else {}
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                result = self._invoke(name, match.groupdict(), body, query)
                payload, status = result if isinstance(result, tuple) else (result, 200)
                self._send_json(status, payload)
                return
            self._send_error(404, "not_found", "接口不存在")
        except ServiceError as exc:
            self._send_error(exc.status, exc.code, exc.message)
        except MergeConflict as exc:
            self._send_error(
                409,
                "merge_conflict",
                "合并存在冲突，请逐项解决后重试",
                {"conflicts": {"cells": exc.cell_conflicts, "formulas": exc.formula_conflicts}},
            )
        except BrokenPipeError:
            return
        except Exception as exc:  # noqa: BLE001 - 兜底，避免泄露堆栈给客户端
            self._send_error(500, "internal", f"服务内部错误: {type(exc).__name__}")

    def _invoke(self, name: str, path: dict, body: dict, query: dict):
        svc = self.service
        user_id = self.headers.get("X-User-Id")

        if name == "create_user":
            return svc.create_user(body)
        if name == "compute_endpoint":
            return svc.compute(
                user_id,
                path["branch_id"],
                body.get("metric_id", ""),
                body.get("period_id", ""),
                body.get("scenario", ""),
            )
        if name == "diff_endpoint":
            other = query.get("other")
            if not other:
                raise ServiceError(400, "invalid", "缺少 other 查询参数")
            return svc.diff_versions(user_id, path["version_id"], other)
        if name == "list_metrics":
            return svc.list_metrics(user_id, query.get("company_id"))

        handler = getattr(svc, name)
        args = [user_id, *path.values()]
        if name in {
            "create_project",
            "create_company",
            "create_metric",
            "create_period",
            "create_source",
            "create_observation",
            "create_branch",
            "create_citation",
            "put_assumption",
            "put_formula",
            "publish",
            "add_comment",
        }:
            args.append(body)
        return handler(*args)

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(host: str, port: int, service: ResearchService) -> ThreadingHTTPServer:
    handler = type("BoundRequestHandler", (RequestHandler,), {"service": service})
    return ThreadingHTTPServer((host, port), handler)
