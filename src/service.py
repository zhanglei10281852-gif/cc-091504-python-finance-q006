"""投研假设协作业务服务层。

承载全部业务规则：
- 项目（普通 / 敏感观察名单）与成员权限隔离
- 数据来源修订 -> 仅标记相关假设过期，不改写已发布版本
- 公式依赖防环、估值计算与完整追溯链
- 分支（从已发布基线派生）-> 三方合并（冲突精确到 指标x期间x情景）
- 发布 compare-and-swap，保证并发下最多一个权威版本
- 复核意见只追加；导出固定数据摘要、公式版本与审批人
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import domain
from store import Store

WRITE_ROLES = {"lead", "analyst"}
APPROVE_ROLES = {"lead", "reviewer"}
PROJECT_KINDS = {"normal", "watchlist"}
DEFAULT_SCENARIOS = ["base", "bull", "bear"]


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def cell_key(metric_id: str, period_id: str, scenario: str) -> str:
    return f"{metric_id}|{period_id}|{scenario}"


def split_cell(key: str) -> tuple[str, str, str]:
    return tuple(key.split("|"))  # type: ignore[return-value]


class ResearchService:
    def __init__(self, store: Store, reference_path: str | None = None):
        self.store = store
        self.scenarios = list(DEFAULT_SCENARIOS)
        if reference_path and os.path.exists(reference_path):
            with open(reference_path, "r", encoding="utf-8") as fh:
                ref = json.load(fh)
            if ref.get("scenarios"):
                self.scenarios = list(ref["scenarios"])

    # ------------------------------------------------------------ 基础校验

    def _user(self, user_id: str | None) -> dict:
        if not user_id:
            raise ServiceError(401, "unauthenticated", "缺少 X-User-Id 请求头")
        user = self.store.get("users", user_id)
        if not user:
            raise ServiceError(401, "unauthenticated", "未知用户")
        return user

    def _project(self, project_id: str) -> dict:
        project = self.store.get("projects", project_id)
        if not project:
            raise ServiceError(404, "not_found", "项目不存在")
        return project

    def _branch(self, branch_id: str) -> dict:
        branch = self.store.get("branches", branch_id)
        if not branch:
            raise ServiceError(404, "not_found", "分支不存在")
        return branch

    def _require_read(self, project: dict, user_id: str) -> None:
        # 敏感观察名单对非成员不可见：按不存在处理，避免暴露其存在
        if project["kind"] == "watchlist" and user_id not in project["members"]:
            raise ServiceError(404, "not_found", "项目不存在")

    def _require_write(self, project: dict, user_id: str) -> None:
        self._require_read(project, user_id)
        if project["members"].get(user_id) not in WRITE_ROLES:
            raise ServiceError(403, "forbidden", "需要项目分析权限")

    def _require_approve(self, project: dict, user_id: str) -> None:
        self._require_read(project, user_id)
        if project["members"].get(user_id) not in APPROVE_ROLES:
            raise ServiceError(403, "forbidden", "需要审批权限")

    def _event(self, kind: str, actor: str, detail: dict) -> None:
        self.store.insert(
            "events",
            {
                "id": self.store.next_id("evt"),
                "kind": kind,
                "actor": actor,
                "detail": detail,
                "at": now_iso(),
            },
        )

    def _metric(self, metric_id: str) -> dict:
        metric = self.store.get("metrics", metric_id)
        if not metric:
            raise ServiceError(404, "not_found", "指标不存在")
        return metric

    def _period(self, period_id: str) -> dict:
        period = self.store.get("periods", period_id)
        if not period:
            raise ServiceError(404, "not_found", "预测期间不存在")
        return period

    # ------------------------------------------------------------ 用户

    def create_user(self, body: dict) -> dict:
        name = (body or {}).get("name")
        if not name:
            raise ServiceError(400, "invalid", "name 必填")
        user = {
            "id": self.store.next_id("usr"),
            "name": name,
            "created_at": now_iso(),
        }
        return self.store.insert("users", user), 201

    # ------------------------------------------------------------ 项目

    def create_project(self, user_id: str, body: dict) -> dict:
        self._user(user_id)
        name = (body or {}).get("name")
        kind = (body or {}).get("kind", "normal")
        if not name:
            raise ServiceError(400, "invalid", "name 必填")
        if kind not in PROJECT_KINDS:
            raise ServiceError(400, "invalid", f"kind 必须是 {sorted(PROJECT_KINDS)}")
        members = {user_id: "lead"}
        for uid, role in (body.get("members") or {}).items():
            self._user(uid)
            if role not in WRITE_ROLES | APPROVE_ROLES:
                raise ServiceError(400, "invalid", f"非法角色: {role}")
            members.setdefault(uid, role)
        project = {
            "id": self.store.next_id("prj"),
            "name": name,
            "kind": kind,
            "members": members,
            "created_at": now_iso(),
        }
        self.store.insert("projects", project)
        main = {
            "id": self.store.next_id("brn"),
            "project_id": project["id"],
            "name": "main",
            "base_version_id": None,
            "status": "open",
            "created_by": user_id,
            "created_at": now_iso(),
        }
        self.store.insert("branches", main)
        project["main_branch_id"] = main["id"]
        self.store.update("projects", project)
        self._event("project_created", user_id, {"project_id": project["id"]})
        return project, 201

    def list_projects(self, user_id: str) -> dict:
        self._user(user_id)
        visible = [
            p
            for p in self.store.all("projects")
            if p["kind"] == "normal" or user_id in p["members"]
        ]
        return {"items": visible}, 200

    def get_project(self, user_id: str, project_id: str) -> dict:
        self._user(user_id)
        project = self._project(project_id)
        self._require_read(project, user_id)
        return project, 200

    # ------------------------------------------------------------ 参考数据

    def create_company(self, user_id: str, body: dict) -> dict:
        self._user(user_id)
        if not body.get("ticker") or not body.get("name"):
            raise ServiceError(400, "invalid", "ticker 与 name 必填")
        company = {
            "id": self.store.next_id("cmp"),
            "ticker": body["ticker"],
            "name": body["name"],
            "created_at": now_iso(),
        }
        return self.store.insert("companies", company), 201

    def list_companies(self, user_id: str) -> dict:
        self._user(user_id)
        return {"items": self.store.all("companies")}, 200

    def create_metric(self, user_id: str, body: dict) -> dict:
        self._user(user_id)
        company = self.store.get("companies", body.get("company_id", ""))
        if not company:
            raise ServiceError(404, "not_found", "公司不存在")
        code = body.get("code")
        if not code:
            raise ServiceError(400, "invalid", "code 必填")
        for m in self.store.all("metrics"):
            if m["company_id"] == company["id"] and m["code"] == code:
                raise ServiceError(409, "conflict", "同一公司下指标代码必须唯一")
        metric = {
            "id": self.store.next_id("mtr"),
            "company_id": company["id"],
            "code": code,
            "unit": body.get("unit", ""),
            "precision": int(body.get("precision", 2)),
            "created_at": now_iso(),
        }
        return self.store.insert("metrics", metric), 201

    def list_metrics(self, user_id: str, company_id: str | None = None) -> dict:
        self._user(user_id)
        items = self.store.all("metrics")
        if company_id:
            items = [m for m in items if m["company_id"] == company_id]
        return {"items": items}, 200

    def create_period(self, user_id: str, body: dict) -> dict:
        self._user(user_id)
        if not body.get("label"):
            raise ServiceError(400, "invalid", "label 必填")
        period = {
            "id": self.store.next_id("per"),
            "label": body["label"],
            "start": body.get("start"),
            "end": body.get("end"),
            "created_at": now_iso(),
        }
        return self.store.insert("periods", period), 201

    def list_periods(self, user_id: str) -> dict:
        self._user(user_id)
        return {"items": self.store.all("periods")}, 200

    def create_source(self, user_id: str, body: dict) -> dict:
        self._user(user_id)
        if not body.get("name"):
            raise ServiceError(400, "invalid", "name 必填")
        source = {
            "id": self.store.next_id("src"),
            "name": body["name"],
            "kind": body.get("kind", "external"),
            "created_at": now_iso(),
        }
        return self.store.insert("sources", source), 201

    def list_sources(self, user_id: str) -> dict:
        self._user(user_id)
        return {"items": self.store.all("sources")}, 200

    # ------------------------------------------------------------ 外部数据与修订

    def create_observation(self, user_id: str, body: dict) -> dict:
        """登记一条外部数据。同 (来源, 指标, 期间) 的新记录视为修订：

        旧记录被取代；引用该来源（或未指定来源）的相关假设只被标记为
        stale，已发布版本快照不受影响。
        """
        user = self._user(user_id)
        source = self.store.get("sources", body.get("source_id", ""))
        if not source:
            raise ServiceError(404, "not_found", "数据来源不存在")
        metric = self._metric(body.get("metric_id", ""))
        period = self._period(body.get("period_id", ""))
        if not isinstance(body.get("value"), (int, float)):
            raise ServiceError(400, "invalid", "value 必须是数值")

        current = None
        for obs in self.store.all("observations"):
            if (
                obs["source_id"] == source["id"]
                and obs["metric_id"] == metric["id"]
                and obs["period_id"] == period["id"]
                and obs.get("superseded_by") is None
            ):
                current = obs
                break
        revision = 1
        if current:
            revision = current["revision"] + 1
        observation = {
            "id": self.store.next_id("obs"),
            "source_id": source["id"],
            "metric_id": metric["id"],
            "period_id": period["id"],
            "value": body["value"],
            "business_time": body.get("business_time"),
            "received_time": now_iso(),
            "revision": revision,
            "supersedes_id": current["id"] if current else None,
            "superseded_by": None,
            "created_by": user["id"],
        }
        stale_marked: list[str] = []
        with self.store.lock:
            self.store.insert("observations", observation)
            if current:
                current["superseded_by"] = observation["id"]
                self.store.update("observations", current)
            if current:  # 仅修订才触发过期标记
                stale_marked = self._mark_stale(
                    metric["id"], period["id"], source["id"]
                )
        self._event(
            "observation_recorded",
            user["id"],
            {
                "observation_id": observation["id"],
                "revision": revision,
                "stale_assumptions": stale_marked,
            },
        )
        return {"observation": observation, "stale_assumptions": stale_marked}, 201

    def _mark_stale(self, metric_id: str, period_id: str, source_id: str) -> list[str]:
        marked = []
        for a in self.store.all("assumptions"):
            if (
                a["metric_id"] == metric_id
                and a["period_id"] == period_id
                and a["status"] == "current"
                and a.get("source_id") in (None, source_id)
            ):
                a["status"] = "stale"
                self.store.update("assumptions", a)
                marked.append(a["id"])
        return marked

    # ------------------------------------------------------------ 分支

    def _latest_version(self, project_id: str) -> dict | None:
        versions = [
            v for v in self.store.all("versions") if v["project_id"] == project_id
        ]
        if not versions:
            return None
        return max(versions, key=lambda v: v["number"])

    def create_branch(self, user_id: str, project_id: str, body: dict) -> dict:
        """从当前已发布基线派生分支，物化基线快照到分支工作区。"""
        user = self._user(user_id)
        project = self._project(project_id)
        self._require_write(project, user["id"])
        name = (body or {}).get("name")
        if not name:
            raise ServiceError(400, "invalid", "name 必填")
        base = self._latest_version(project["id"])
        branch = {
            "id": self.store.next_id("brn"),
            "project_id": project["id"],
            "name": name,
            "base_version_id": base["id"] if base else None,
            "status": "open",
            "created_by": user["id"],
            "created_at": now_iso(),
        }
        with self.store.lock:
            self.store.insert("branches", branch)
            if base:
                for a in base["snapshot"]["assumptions"]:
                    self.store.insert(
                        "assumptions",
                        {
                            **a,
                            "id": self.store.next_id("asm"),
                            "branch_id": branch["id"],
                        },
                    )
                for f in base["snapshot"]["formulas"]:
                    self.store.insert(
                        "formulas",
                        {
                            **f,
                            "id": self.store.next_id("fml"),
                            "branch_id": branch["id"],
                        },
                    )
        self._event(
            "branch_created",
            user["id"],
            {"branch_id": branch["id"], "base_version_id": branch["base_version_id"]},
        )
        return branch, 201

    def list_branches(self, user_id: str, project_id: str) -> dict:
        self._user(user_id)
        project = self._project(project_id)
        self._require_read(project, user_id)
        items = [
            b for b in self.store.all("branches") if b["project_id"] == project_id
        ]
        return {"items": items}, 200

    # ------------------------------------------------------------ 假设

    def _branch_checked(self, user_id: str, branch_id: str, write: bool) -> tuple[dict, dict]:
        branch = self._branch(branch_id)
        project = self._project(branch["project_id"])
        if write:
            self._require_write(project, user_id)
            if branch["status"] != "open":
                raise ServiceError(409, "conflict", "分支已合并，不能再修改")
        else:
            self._require_read(project, user_id)
        return branch, project

    def put_assumption(self, user_id: str, branch_id: str, body: dict) -> dict:
        branch, _ = self._branch_checked(user_id, branch_id, write=True)
        metric = self._metric(body.get("metric_id", ""))
        period = self._period(body.get("period_id", ""))
        scenario = body.get("scenario")
        if scenario not in self.scenarios:
            raise ServiceError(400, "invalid", f"scenario 必须是 {self.scenarios}")
        if not isinstance(body.get("value"), (int, float)):
            raise ServiceError(400, "invalid", "value 必须是数值")
        source_id = body.get("source_id")
        if source_id and not self.store.get("sources", source_id):
            raise ServiceError(404, "not_found", "数据来源不存在")
        citation_ids = list(body.get("citation_ids") or [])
        for cid in citation_ids:
            citation = self.store.get("citations", cid)
            if not citation or citation["project_id"] != branch["project_id"]:
                raise ServiceError(404, "not_found", f"引用材料不存在: {cid}")

        existing = None
        for a in self.store.all("assumptions"):
            if (
                a["branch_id"] == branch["id"]
                and a["metric_id"] == metric["id"]
                and a["period_id"] == period["id"]
                and a["scenario"] == scenario
            ):
                existing = a
                break
        if existing:
            existing.update(
                {
                    "value": body["value"],
                    "source_id": source_id,
                    "citation_ids": citation_ids,
                    "status": "current",
                    "revision": existing["revision"] + 1,
                    "updated_by": user_id,
                    "updated_at": now_iso(),
                }
            )
            self.store.update("assumptions", existing)
            return existing, 200
        assumption = {
            "id": self.store.next_id("asm"),
            "branch_id": branch["id"],
            "metric_id": metric["id"],
            "period_id": period["id"],
            "scenario": scenario,
            "value": body["value"],
            "source_id": source_id,
            "citation_ids": citation_ids,
            "status": "current",
            "revision": 1,
            "updated_by": user_id,
            "updated_at": now_iso(),
        }
        return self.store.insert("assumptions", assumption), 201

    def list_assumptions(self, user_id: str, branch_id: str) -> dict:
        branch, _ = self._branch_checked(user_id, branch_id, write=False)
        items = [
            a for a in self.store.all("assumptions") if a["branch_id"] == branch["id"]
        ]
        return {"items": items}, 200

    # ------------------------------------------------------------ 公式

    def _branch_formulas(self, branch_id: str) -> list[dict]:
        return [f for f in self.store.all("formulas") if f["branch_id"] == branch_id]

    def _formula_graph(self, branch_id: str) -> dict[str, set[str]]:
        return {f["metric_id"]: set(f["deps"]) for f in self._branch_formulas(branch_id)}

    def put_formula(self, user_id: str, branch_id: str, body: dict) -> dict:
        branch, _ = self._branch_checked(user_id, branch_id, write=True)
        metric = self._metric(body.get("metric_id", ""))
        expression = body.get("expression")
        if not expression:
            raise ServiceError(400, "invalid", "expression 必填")
        try:
            dep_codes = domain.validate_expression(expression)
        except domain.DomainError as exc:
            raise ServiceError(400, exc.code, exc.message) from exc
        # 依赖按同公司指标代码解析
        by_code = {
            m["code"]: m
            for m in self.store.all("metrics")
            if m["company_id"] == metric["company_id"]
        }
        dep_ids = set()
        for code in dep_codes:
            dep = by_code.get(code)
            if not dep:
                raise ServiceError(400, "invalid", f"同公司下不存在指标代码: {code}")
            dep_ids.add(dep["id"])
        graph = self._formula_graph(branch["id"])
        graph.pop(metric["id"], None)  # 以新公式替换旧边后再判环
        if domain.would_cycle(graph, metric["id"], dep_ids):
            raise ServiceError(409, "cycle", "公式依赖会形成环，已拒绝")

        existing = None
        for f in self._branch_formulas(branch["id"]):
            if f["metric_id"] == metric["id"]:
                existing = f
                break
        if existing:
            existing.update(
                {
                    "expression": expression,
                    "deps": sorted(dep_ids),
                    "version": existing["version"] + 1,
                    "updated_by": user_id,
                    "updated_at": now_iso(),
                }
            )
            self.store.update("formulas", existing)
            return existing, 200
        formula = {
            "id": self.store.next_id("fml"),
            "branch_id": branch["id"],
            "metric_id": metric["id"],
            "expression": expression,
            "deps": sorted(dep_ids),
            "version": 1,
            "updated_by": user_id,
            "updated_at": now_iso(),
        }
        return self.store.insert("formulas", formula), 201

    def list_formulas(self, user_id: str, branch_id: str) -> dict:
        branch, _ = self._branch_checked(user_id, branch_id, write=False)
        return {"items": self._branch_formulas(branch["id"])}, 200

    # ------------------------------------------------------------ 估值计算与追溯

    def _assumption_cell(
        self, branch_id: str, metric_id: str, period_id: str, scenario: str
    ) -> dict | None:
        for a in self.store.all("assumptions"):
            if (
                a["branch_id"] == branch_id
                and a["metric_id"] == metric_id
                and a["period_id"] == period_id
                and a["scenario"] == scenario
            ):
                return a
        return None

    def _latest_observation(self, source_id: str, metric_id: str, period_id: str) -> dict | None:
        for obs in self.store.all("observations"):
            if (
                obs["source_id"] == source_id
                and obs["metric_id"] == metric_id
                and obs["period_id"] == period_id
                and obs.get("superseded_by") is None
            ):
                return obs
        return None

    def compute(
        self, user_id: str, branch_id: str, metric_id: str, period_id: str, scenario: str
    ) -> dict:
        branch, _ = self._branch_checked(user_id, branch_id, write=False)
        metric = self._metric(metric_id)
        self._period(period_id)
        if scenario not in self.scenarios:
            raise ServiceError(400, "invalid", f"scenario 必须是 {self.scenarios}")
        try:
            return self._eval(branch, metric, period_id, scenario, ()), 200
        except domain.DomainError as exc:
            raise ServiceError(400, exc.code, exc.message) from exc

    def _eval(
        self,
        branch: dict,
        metric: dict,
        period_id: str,
        scenario: str,
        stack: tuple[str, ...],
    ) -> dict:
        key = cell_key(metric["id"], period_id, scenario)
        if key in stack:
            raise domain.DomainError("cycle", "计算路径出现环（数据异常）")
        stack = stack + (key,)
        formula = next(
            (
                f
                for f in self._branch_formulas(branch["id"])
                if f["metric_id"] == metric["id"]
            ),
            None,
        )
        node = {
            "metric_id": metric["id"],
            "metric_code": metric["code"],
            "period_id": period_id,
            "scenario": scenario,
        }
        if formula:
            env: dict[str, float] = {}
            inputs = []
            for dep_id in formula["deps"]:
                dep_metric = self._metric(dep_id)
                child = self._eval(branch, dep_metric, period_id, scenario, stack)
                env[dep_metric["code"]] = child["value"]
                inputs.append(child)
            node.update(
                {
                    "via": "formula",
                    "expression": formula["expression"],
                    "formula_version": formula["version"],
                    "value": domain.evaluate(formula["expression"], env),
                    "inputs": inputs,
                    "stale": any(i["stale"] for i in inputs),
                }
            )
            return node
        assumption = self._assumption_cell(branch["id"], metric["id"], period_id, scenario)
        if not assumption:
            raise domain.DomainError(
                "missing_input",
                f"指标 {metric['code']} 在 {period_id}/{scenario} 缺少假设且无公式",
            )
        observation = None
        if assumption.get("source_id"):
            observation = self._latest_observation(
                assumption["source_id"], metric["id"], period_id
            )
        node.update(
            {
                "via": "assumption",
                "assumption_id": assumption["id"],
                "value": assumption["value"],
                "stale": assumption["status"] == "stale",
                "source_id": assumption.get("source_id"),
                "source_revision": observation["revision"] if observation else None,
                "citation_ids": assumption.get("citation_ids", []),
                "inputs": [],
            }
        )
        return node

    def pending_recompute(self, user_id: str, branch_id: str) -> dict:
        """列出因输入过期而待重算的结论（公式输出单元格）。"""
        branch, _ = self._branch_checked(user_id, branch_id, write=False)
        graph = self._formula_graph(branch["id"])
        stale_cells = [
            a
            for a in self.store.all("assumptions")
            if a["branch_id"] == branch["id"] and a["status"] == "stale"
        ]
        stale_by_metric: dict[str, list[dict]] = {}
        for a in stale_cells:
            stale_by_metric.setdefault(a["metric_id"], []).append(a)
        pending = []
        for metric_id in graph:
            affected = stale_by_metric.keys() & domain.transitive_deps(graph, metric_id)
            if not affected:
                continue
            cells = [
                {
                    "period_id": a["period_id"],
                    "scenario": a["scenario"],
                    "stale_assumption_id": a["id"],
                    "stale_metric_id": a["metric_id"],
                }
                for m in affected
                for a in stale_by_metric[m]
            ]
            pending.append({"metric_id": metric_id, "cells": cells})
        return {"items": pending, "stale_assumptions": [a["id"] for a in stale_cells]}, 200

    # ------------------------------------------------------------ 合并

    def _cells(self, branch_id: str) -> dict[str, dict]:
        cells: dict[str, dict] = {}
        for a in self.store.all("assumptions"):
            if a["branch_id"] == branch_id:
                cells[cell_key(a["metric_id"], a["period_id"], a["scenario"])] = a
        return cells

    @staticmethod
    def _canon_assumption(rec: dict) -> dict:
        """假设的可比较字段：id/branch_id/时间戳/revision/status 均为易变或派生字段。"""
        return {
            "metric_id": rec["metric_id"],
            "period_id": rec["period_id"],
            "scenario": rec["scenario"],
            "value": rec["value"],
            "source_id": rec.get("source_id"),
            "citation_ids": sorted(rec.get("citation_ids") or []),
        }

    @staticmethod
    def _canon_formula(rec: dict) -> dict:
        return {
            "metric_id": rec["metric_id"],
            "expression": rec["expression"],
            "deps": sorted(rec["deps"]),
        }

    def merge_branch(self, user_id: str, branch_id: str) -> dict:
        """把分支三方合并进 main。冲突精确到 指标x期间x情景。"""
        branch, project = self._branch_checked(user_id, branch_id, write=True)
        main = self.store.get("branches", project["main_branch_id"])
        if branch["id"] == main["id"]:
            raise ServiceError(400, "invalid", "main 分支无需合并")

        base_cells: dict[str, dict] = {}
        base_formulas: dict[str, dict] = {}
        if branch["base_version_id"]:
            base_version = self.store.get("versions", branch["base_version_id"])
            if base_version:
                for a in base_version["snapshot"]["assumptions"]:
                    base_cells[cell_key(a["metric_id"], a["period_id"], a["scenario"])] = a
                for f in base_version["snapshot"]["formulas"]:
                    base_formulas[f["metric_id"]] = f

        ours_cells = self._cells(main["id"])
        theirs_cells = self._cells(branch["id"])
        ours_formulas = {f["metric_id"]: f for f in self._branch_formulas(main["id"])}
        theirs_formulas = {f["metric_id"]: f for f in self._branch_formulas(branch["id"])}

        canon_base = {k: self._canon_assumption(v) for k, v in base_cells.items()}
        canon_ours = {k: self._canon_assumption(v) for k, v in ours_cells.items()}
        canon_theirs = {k: self._canon_assumption(v) for k, v in theirs_cells.items()}
        merged_canon, cell_conflicts = domain.three_way_merge(
            canon_base, canon_ours, canon_theirs
        )
        merged_fcanon, formula_conflicts = domain.three_way_merge(
            {k: self._canon_formula(v) for k, v in base_formulas.items()},
            {k: self._canon_formula(v) for k, v in ours_formulas.items()},
            {k: self._canon_formula(v) for k, v in theirs_formulas.items()},
        )
        if cell_conflicts or formula_conflicts:
            raise MergeConflict(
                [
                    {
                        "metric_id": m,
                        "period_id": p,
                        "scenario": s,
                        "base": c["base"] and c["base"]["value"],
                        "main": c["ours"] and c["ours"]["value"],
                        "branch": c["theirs"] and c["theirs"]["value"],
                    }
                    for c in cell_conflicts
                    for m, p, s in [split_cell(c["key"])]
                ],
                [
                    {
                        "metric_id": c["key"],
                        "base": c["base"] and c["base"]["expression"],
                        "main": c["ours"] and c["ours"]["expression"],
                        "branch": c["theirs"] and c["theirs"]["expression"],
                    }
                    for c in formula_conflicts
                ],
            )

        # 过期标记只增不减：任一侧 stale 且该单元格未被显式改值时保持 stale
        merged_cells: dict[str, dict] = {}
        for key, canon in merged_canon.items():
            if canon_base.get(key) == canon:
                stale = (
                    (ours_cells.get(key) or {}).get("status") == "stale"
                    or (theirs_cells.get(key) or {}).get("status") == "stale"
                )
                status = "stale" if stale else (base_cells.get(key) or {}).get(
                    "status", "current"
                )
            elif canon_theirs.get(key) == canon:
                status = theirs_cells[key]["status"]
            else:
                status = ours_cells.get(key, {}).get("status", "current")
            merged_cells[key] = {**canon, "status": status}

        # 公式版本取两侧较大者，保持 main 上版本号单调
        merged_formulas: dict[str, dict] = {}
        for key, canon in merged_fcanon.items():
            version = max(
                (ours_formulas.get(key) or {}).get("version", 0),
                (theirs_formulas.get(key) or {}).get("version", 0),
                (base_formulas.get(key) or {}).get("version", 0),
                1,
            )
            merged_formulas[key] = {**canon, "version": version}

        # 合并结果公式图必须仍然无环
        graph = {m: set(f["deps"]) for m, f in merged_formulas.items()}
        for m in graph:
            if domain.would_cycle(graph, m, set()):
                raise ServiceError(409, "cycle", "合并后的公式依赖会形成环")

        with self.store.lock:
            # 重建 main 的假设与公式
            for a in list(self.store.all("assumptions")):
                if a["branch_id"] == main["id"]:
                    del self.store.data["assumptions"][a["id"]]
            for f in list(self.store.all("formulas")):
                if f["branch_id"] == main["id"]:
                    del self.store.data["formulas"][f["id"]]
            self.store.save()
            for record in merged_cells.values():
                self.store.insert(
                    "assumptions",
                    {
                        **record,
                        "id": self.store.next_id("asm"),
                        "branch_id": main["id"],
                        "revision": 1,
                        "updated_by": user_id,
                        "updated_at": now_iso(),
                    },
                )
            for record in merged_formulas.values():
                self.store.insert(
                    "formulas",
                    {
                        **record,
                        "id": self.store.next_id("fml"),
                        "branch_id": main["id"],
                        "updated_by": user_id,
                        "updated_at": now_iso(),
                    },
                )
            branch["status"] = "merged"
            branch["merged_at"] = now_iso()
            branch["merged_by"] = user_id
            self.store.update("branches", branch)
        self._event(
            "branch_merged", user_id, {"branch_id": branch["id"], "into": main["id"]}
        )
        return {"merged": True, "branch_id": branch["id"], "into": main["id"]}, 200

    # ------------------------------------------------------------ 发布

    def publish(self, user_id: str, project_id: str, body: dict) -> dict:
        """发布 main 快照为新版本。

        expected_version_id 必须等于当前权威版本（首次发布传 null），
        在锁内检查并落盘，保证并发发布最多产生一个权威版本。
        """
        user = self._user(user_id)
        project = self._project(project_id)
        self._require_approve(project, user["id"])
        expected = (body or {}).get("expected_version_id")
        note = (body or {}).get("note", "")

        with self.store.lock:
            current = self._latest_version(project["id"])
            current_id = current["id"] if current else None
            if expected != current_id:
                raise ServiceError(
                    409,
                    "stale_base",
                    f"发布基线过期：当前权威版本为 {current_id}，请基于最新版本重新发布",
                )
            main = self.store.get("branches", project["main_branch_id"])
            assumptions = [
                dict(a)
                for a in self.store.all("assumptions")
                if a["branch_id"] == main["id"]
            ]
            formulas = [
                dict(f)
                for f in self.store.all("formulas")
                if f["branch_id"] == main["id"]
            ]
            snapshot = {"assumptions": assumptions, "formulas": formulas}
            version = {
                "id": self.store.next_id("ver"),
                "project_id": project["id"],
                "number": (current["number"] if current else 0) + 1,
                "snapshot": snapshot,
                "data_digest": domain.digest(assumptions),
                "formula_versions": {f["metric_id"]: f["version"] for f in formulas},
                "approver_id": user["id"],
                "note": note,
                "authoritative": True,
                "created_at": now_iso(),
            }
            if current:
                current["authoritative"] = False
                self.store.update("versions", current)
            self.store.insert("versions", version)
        self._event(
            "version_published",
            user["id"],
            {"version_id": version["id"], "number": version["number"]},
        )
        return version, 201

    def list_versions(self, user_id: str, project_id: str) -> dict:
        self._user(user_id)
        project = self._project(project_id)
        self._require_read(project, user_id)
        items = [
            v for v in self.store.all("versions") if v["project_id"] == project_id
        ]
        items.sort(key=lambda v: v["number"])
        return {"items": items}, 200

    def get_version(self, user_id: str, version_id: str) -> dict:
        self._user(user_id)
        version = self.store.get("versions", version_id)
        if not version:
            raise ServiceError(404, "not_found", "版本不存在")
        project = self._project(version["project_id"])
        self._require_read(project, user_id)
        return version, 200

    # ------------------------------------------------------------ 版本差异

    def diff_versions(self, user_id: str, version_a_id: str, version_b_id: str) -> dict:
        """比较两个版本：输入差异、公式差异，并把结论差异归因到 数据/模型。"""
        self._user(user_id)
        va = self.store.get("versions", version_a_id)
        vb = self.store.get("versions", version_b_id)
        if not va or not vb or va["project_id"] != vb["project_id"]:
            raise ServiceError(404, "not_found", "版本不存在或不属于同一项目")
        project = self._project(va["project_id"])
        self._require_read(project, user_id)

        def cells_of(v):
            return {
                cell_key(a["metric_id"], a["period_id"], a["scenario"]): a
                for a in v["snapshot"]["assumptions"]
            }

        def formulas_of(v):
            return {f["metric_id"]: f for f in v["snapshot"]["formulas"]}

        ca, cb = cells_of(va), cells_of(vb)
        fa, fb = formulas_of(va), formulas_of(vb)

        input_changes = []
        for key in sorted(set(ca) | set(cb)):
            xa, xb = ca.get(key), cb.get(key)
            if (xa and xa["value"]) != (xb and xb["value"]):
                m, p, s = split_cell(key)
                input_changes.append(
                    {
                        "metric_id": m,
                        "period_id": p,
                        "scenario": s,
                        "a": xa and xa["value"],
                        "b": xb and xb["value"],
                    }
                )
        formula_changes = []
        for mid in sorted(set(fa) | set(fb)):
            xa, xb = fa.get(mid), fb.get(mid)
            if (xa and (xa["expression"], xa["version"])) != (
                xb and (xb["expression"], xb["version"])
            ):
                formula_changes.append(
                    {
                        "metric_id": mid,
                        "a": xa and {"expression": xa["expression"], "version": xa["version"]},
                        "b": xb and {"expression": xb["expression"], "version": xb["version"]},
                    }
                )

        # 归因：公式变了 -> 模型；传递依赖的输入变了 -> 数据
        changed_input_metrics = {c["metric_id"] for c in input_changes}
        changed_input_cells = {
            (c["metric_id"], c["period_id"], c["scenario"]) for c in input_changes
        }
        changed_formula_metrics = {c["metric_id"] for c in formula_changes}
        graph_b = {m: set(f["deps"]) for m, f in fb.items()}
        all_metrics = set(fa) | set(fb)
        periods_scenarios = {(p, s) for _, p, s in changed_input_cells}
        outputs = []
        for mid in sorted(all_metrics):
            causes = set()
            if mid in changed_formula_metrics:
                causes.add("model")
            impacted = changed_input_metrics & domain.transitive_deps(graph_b, mid)
            if impacted:
                causes.add("data")
            if not causes:
                continue
            cells = [
                {"period_id": p, "scenario": s}
                for m, p, s in changed_input_cells
                if m in impacted
            ] or [
                {"period_id": p, "scenario": s} for p, s in sorted(periods_scenarios)
            ]
            outputs.append({"metric_id": mid, "causes": sorted(causes), "cells": cells})
        return {
            "a": version_a_id,
            "b": version_b_id,
            "input_changes": input_changes,
            "formula_changes": formula_changes,
            "impacted_outputs": outputs,
        }, 200

    # ------------------------------------------------------------ 复核意见（只追加）

    def add_comment(self, user_id: str, version_id: str, body: dict) -> dict:
        user = self._user(user_id)
        version = self.store.get("versions", version_id)
        if not version:
            raise ServiceError(404, "not_found", "版本不存在")
        project = self._project(version["project_id"])
        self._require_read(project, user["id"])
        text = (body or {}).get("body")
        if not text:
            raise ServiceError(400, "invalid", "body 必填")
        comment = {
            "id": self.store.next_id("cmt"),
            "version_id": version["id"],
            "author_id": user["id"],
            "body": text,
            "created_at": now_iso(),
        }
        return self.store.insert("comments", comment), 201

    def list_comments(self, user_id: str, version_id: str) -> dict:
        self._user(user_id)
        version = self.store.get("versions", version_id)
        if not version:
            raise ServiceError(404, "not_found", "版本不存在")
        project = self._project(version["project_id"])
        self._require_read(project, user_id)
        items = [
            c for c in self.store.all("comments") if c["version_id"] == version["id"]
        ]
        items.sort(key=lambda c: c["created_at"])
        return {"items": items}, 200

    # ------------------------------------------------------------ 引用材料

    def create_citation(self, user_id: str, project_id: str, body: dict) -> dict:
        user = self._user(user_id)
        project = self._project(project_id)
        self._require_write(project, user["id"])
        if not body.get("title"):
            raise ServiceError(400, "invalid", "title 必填")
        citation = {
            "id": self.store.next_id("cit"),
            "project_id": project["id"],
            "title": body["title"],
            "uri": body.get("uri"),
            "note": body.get("note"),
            "created_by": user["id"],
            "created_at": now_iso(),
        }
        return self.store.insert("citations", citation), 201

    def list_citations(self, user_id: str, project_id: str) -> dict:
        self._user(user_id)
        project = self._project(project_id)
        self._require_read(project, user_id)
        items = [
            c for c in self.store.all("citations") if c["project_id"] == project_id
        ]
        return {"items": items}, 200

    # ------------------------------------------------------------ 导出

    def create_export(self, user_id: str, version_id: str) -> dict:
        """导出版本：固定数据摘要、公式版本与审批人，内容不可变。"""
        user = self._user(user_id)
        version = self.store.get("versions", version_id)
        if not version:
            raise ServiceError(404, "not_found", "版本不存在")
        project = self._project(version["project_id"])
        self._require_read(project, user["id"])
        export = {
            "id": self.store.next_id("exp"),
            "version_id": version["id"],
            "project_id": project["id"],
            "version_number": version["number"],
            "data_digest": version["data_digest"],
            "formula_versions": dict(version["formula_versions"]),
            "approver_id": version["approver_id"],
            "generated_by": user["id"],
            "created_at": now_iso(),
            "content": {
                "assumptions": [
                    {
                        "metric_id": a["metric_id"],
                        "period_id": a["period_id"],
                        "scenario": a["scenario"],
                        "value": a["value"],
                        "source_id": a.get("source_id"),
                        "citation_ids": a.get("citation_ids", []),
                    }
                    for a in version["snapshot"]["assumptions"]
                ],
                "formulas": [
                    {
                        "metric_id": f["metric_id"],
                        "expression": f["expression"],
                        "version": f["version"],
                    }
                    for f in version["snapshot"]["formulas"]
                ],
            },
        }
        return self.store.insert("exports", export), 201

    def get_export(self, user_id: str, export_id: str) -> dict:
        self._user(user_id)
        export = self.store.get("exports", export_id)
        if not export:
            raise ServiceError(404, "not_found", "导出不存在")
        project = self._project(export["project_id"])
        self._require_read(project, user_id)
        return export, 200


class MergeConflict(Exception):
    """携带结构化冲突明细，供 HTTP 层放进 409 响应。"""

    def __init__(self, cell_conflicts: list[dict], formula_conflicts: list[dict]):
        super().__init__("merge conflict")
        self.cell_conflicts = cell_conflicts
        self.formula_conflicts = formula_conflicts
