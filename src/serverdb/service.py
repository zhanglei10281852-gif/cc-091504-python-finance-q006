from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from .auth import Auth, hash_password, require_role, verify_password
from .errors import BadRequest, Conflict, Forbidden, NotFound, Unauthorized
from .formulas import (
    ensure_acyclic,
    parse_expression,
    safe_eval,
    topological_order,
)
from .storage import Store

KEY_SEP = "\x1f"
SCENARIO_ALIASES = {"base", "bull", "bear"}


def vkey(metric_id: str, period: str, scenario: str) -> str:
    return KEY_SEP.join((metric_id, period, scenario))


def split_vkey(key: str) -> tuple[str, str, str]:
    m, p, s = key.split(KEY_SEP)
    return m, p, s


def canonical_digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Service:
    def __init__(self, store: Store, auth: Auth | None = None) -> None:
        self.store = store
        self.auth = auth or Auth()
        self._ast_cache: dict[str, Any] = {}

    def _ast(self, expression: str) -> Any:
        tree = self._ast_cache.get(expression)
        if tree is None:
            tree, _ = parse_expression(expression)
            self._ast_cache[expression] = tree
        return tree

    # ---- 身份与权限 --------------------------------------------------------

    def authenticate(self, token: str | None) -> dict | None:
        if not token:
            return None
        username = self.auth.username_of(token)
        with self.store.read() as data:
            user = data["users"].get(username)
        return copy.deepcopy(user) if user else None

    def login(self, username: str, password: str) -> str:
        with self.store.read() as data:
            user = data["users"].get(username)
            if not user or not verify_password(password, user["password"]):
                raise Unauthorized("用户名或口令错误")
        return self.auth.issue(username)

    def create_user(self, actor: dict | None, body: dict) -> dict:
        username = _require_str(body, "username")
        password = _require_str(body, "password")
        role = body.get("role", "analyst")
        if role not in ("analyst", "reviewer", "admin"):
            raise BadRequest("role 必须是 analyst/reviewer/admin")
        with self.store.transaction() as data:
            bootstrap = not data["users"]
            if not bootstrap:
                require_role(actor or {}, "admin")
            if username in data["users"]:
                raise Conflict("用户名已存在")
            user = {
                "username": username,
                "role": "admin" if bootstrap else role,
                "watchlist_access": bool(body.get("watchlist_access", False)),
                "password": hash_password(password),
                "created_at": self.store.tick(data)[0],
            }
            data["users"][username] = user
            self.store.audit(data, "system" if bootstrap else actor["username"], "user.create",
                             {"username": username, "role": user["role"]})
            return _public_user(user)

    def list_users(self, actor: dict) -> list[dict]:
        require_role(actor, "admin")
        with self.store.read() as data:
            return [_public_user(u) for u in sorted(data["users"].values(), key=lambda x: x["created_at"])]

    def update_user(self, actor: dict, username: str, body: dict) -> dict:
        require_role(actor, "admin")
        with self.store.transaction() as data:
            user = data["users"].get(username)
            if not user:
                raise NotFound("用户不存在")
            if "role" in body:
                if body["role"] not in ("analyst", "reviewer", "admin"):
                    raise BadRequest("role 非法")
                user["role"] = body["role"]
            if "watchlist_access" in body:
                user["watchlist_access"] = bool(body["watchlist_access"])
            if body.get("password"):
                user["password"] = hash_password(str(body["password"]))
            self.store.audit(data, actor["username"], "user.update", {"username": username})
            return _public_user(user)

    # ---- 公司 / 项目 -------------------------------------------------------

    def create_company(self, actor: dict, body: dict) -> dict:
        require_role(actor, "analyst", "reviewer", "admin")
        name = _require_str(body, "name")
        with self.store.transaction() as data:
            cid = self.store.new_id("co")
            rec = {
                "id": cid, "name": name,
                "ticker": body.get("ticker"),
                "identifiers": body.get("identifiers", {}),
                "created_by": actor["username"],
                "created_at": self.store.tick(data)[0],
            }
            data["companies"][cid] = rec
            self.store.audit(data, actor["username"], "company.create", {"id": cid})
            return copy.deepcopy(rec)

    def list_companies(self, actor: dict) -> list[dict]:
        with self.store.read() as data:
            return [copy.deepcopy(c) for c in data["companies"].values()]

    def create_project(self, actor: dict, body: dict) -> dict:
        require_role(actor, "admin")
        name = _require_str(body, "name")
        company_id = _require_str(body, "company_id")
        scenarios = body.get("scenarios") or ["base"]
        periods = body.get("periods") or []
        if not isinstance(scenarios, list) or not all(isinstance(s, str) and s for s in scenarios):
            raise BadRequest("scenarios 必须是非空字符串数组")
        if not isinstance(periods, list) or not all(isinstance(p, str) and p for p in periods):
            raise BadRequest("periods 必须是字符串数组")
        if len(set(periods)) != len(periods):
            raise BadRequest("periods 不能重复")
        watchlist = bool(body.get("watchlist", False))
        with self.store.transaction() as data:
            if company_id not in data["companies"]:
                raise NotFound("公司不存在")
            pid = self.store.new_id("prj")
            project = {
                "id": pid, "name": name, "company_id": company_id,
                "scenarios": list(scenarios), "periods": list(periods),
                "watchlist": watchlist,
                "members": [actor["username"]],
                "created_at": self.store.tick(data)[0],
            }
            data["projects"][pid] = project
            self.store.audit(data, actor["username"], "project.create",
                             {"id": pid, "watchlist": watchlist})
            return copy.deepcopy(project)

    def list_projects(self, actor: dict) -> list[dict]:
        with self.store.read() as data:
            out = []
            for p in data["projects"].values():
                if self._is_member(actor, p):
                    out.append(copy.deepcopy(p))
            return out

    def get_project(self, actor: dict, pid: str) -> dict:
        with self.store.read() as data:
            project = self._project(data, pid)
            self._require_member(actor, project)
            return copy.deepcopy(project)

    def add_periods(self, actor: dict, pid: str, body: dict) -> dict:
        periods = body.get("periods")
        if not isinstance(periods, list) or not all(isinstance(p, str) and p for p in periods):
            raise BadRequest("periods 必须是非空字符串数组")
        with self.store.transaction() as data:
            project = self._project(data, pid)
            self._require_member(actor, project)
            existing = set(project["periods"])
            for p in periods:
                if p not in existing:
                    project["periods"].append(p)
                    existing.add(p)
            self.store.audit(data, actor["username"], "project.periods", {"project": pid})
            return copy.deepcopy(project)

    def add_member(self, actor: dict, pid: str, body: dict) -> dict:
        require_role(actor, "admin")
        username = _require_str(body, "username")
        with self.store.transaction() as data:
            project = self._project(data, pid)
            if username not in data["users"]:
                raise NotFound("用户不存在")
            if username not in project["members"]:
                project["members"].append(username)
            self.store.audit(data, actor["username"], "project.member.add",
                             {"project": pid, "username": username})
            return {"members": list(project["members"])}

    def list_members(self, actor: dict, pid: str) -> dict:
        with self.store.read() as data:
            project = self._project(data, pid)
            self._require_member(actor, project)
            return {"members": list(project["members"])}

    # ---- 指标 / 数据源 / 引用 ----------------------------------------------

    def create_metric(self, actor: dict, pid: str, body: dict) -> dict:
        key = _require_str(body, "key")
        name = _require_str(body, "name")
        with self.store.transaction() as data:
            project = self._project(data, pid)
            self._require_member_role(actor, project, "analyst", "admin")
            if any(m["key"] == key for m in self._metrics(data, project)):
                raise Conflict(f"指标 key 已存在: {key}")
            mid = self.store.new_id("m")
            rec = {
                "id": mid, "project": pid, "key": key, "name": name,
                "unit": body.get("unit"),
                "decimals": int(body.get("decimals", 4)),
            }
            data["metrics"][mid] = rec
            self.store.audit(data, actor["username"], "metric.create", {"id": mid, "key": key})
            return copy.deepcopy(rec)

    def list_metrics(self, actor: dict, pid: str) -> list[dict]:
        with self.store.read() as data:
            project = self._project(data, pid)
            self._require_member(actor, project)
            return [copy.deepcopy(m) for m in self._metrics(data, project)]

    def create_data_source(self, actor: dict, body: dict) -> dict:
        require_role(actor, "analyst", "reviewer", "admin")
        name = _require_str(body, "name")
        with self.store.transaction() as data:
            sid = self.store.new_id("src")
            rec = {
                "id": sid, "name": name,
                "kind": body.get("kind", "external"),
                "provider": body.get("provider"),
                "current_revision": int(body.get("revision", 1)),
                "revisions": [{
                    "revision": int(body.get("revision", 1)),
                    "label": body.get("label", "initial"),
                    "notes": body.get("notes"),
                    "at": self.store.tick(data)[0],
                    "by": actor["username"],
                }],
            }
            data["data_sources"][sid] = rec
            self.store.audit(data, actor["username"], "source.create", {"id": sid})
            return copy.deepcopy(rec)

    def list_data_sources(self, actor: dict) -> list[dict]:
        with self.store.read() as data:
            return [copy.deepcopy(s) for s in data["data_sources"].values()]

    def revise_data_source(self, actor: dict, sid: str, body: dict) -> dict:
        """登记外部数据修订：只推进数据源版本并把相关假设标为过期，不改写任何观点。"""
        require_role(actor, "admin")
        label = _require_str(body, "label")
        with self.store.transaction() as data:
            source = data["data_sources"].get(sid)
            if not source:
                raise NotFound("数据源不存在")
            old_rev = source["current_revision"]
            new_rev = int(body.get("revision", old_rev + 1))
            if new_rev <= old_rev:
                raise Conflict(f"新修订号必须大于 {old_rev}")
            ts = self.store.tick(data)[0]
            source["current_revision"] = new_rev
            source["revisions"].append({
                "revision": new_rev, "label": label,
                "notes": body.get("notes"), "at": ts, "by": actor["username"],
            })
            stale = []
            for assump in data["assumptions"].values():
                if assump.get("data_source_id") == sid and \
                        int(assump.get("data_source_revision", old_rev)) < new_rev:
                    assump["stale"] = True
                    assump["stale_reason"] = {
                        "source_id": sid, "from_revision": old_rev,
                        "to_revision": new_rev, "at": ts,
                    }
                    stale.append(vkey(assump["metric"], assump["period"], assump["scenario"]))
            self.store.audit(data, actor["username"], "source.revise",
                             {"id": sid, "old": old_rev, "new": new_rev,
                              "stale_assumptions": len(stale)})
            return {"source": copy.deepcopy(source), "stale_assumptions": stale}

    def create_reference(self, actor: dict, pid: str, body: dict) -> dict:
        title = _require_str(body, "title")
        with self.store.transaction() as data:
            project = self._project(data, pid)
            self._require_member(actor, project)
            rid = self.store.new_id("ref")
            rec = {
                "id": rid, "project": pid, "title": title,
                "kind": body.get("kind", "document"),
                "uri": body.get("uri"),
                "citation": body.get("citation"),
                "published_at": body.get("published_at"),
                "created_by": actor["username"],
                "created_at": self.store.tick(data)[0],
            }
            data["references"][rid] = rec
            self.store.audit(data, actor["username"], "reference.create", {"id": rid})
            return copy.deepcopy(rec)

    def list_references(self, actor: dict, pid: str) -> list[dict]:
        with self.store.read() as data:
            project = self._project(data, pid)
            self._require_member(actor, project)
            return [copy.deepcopy(r) for r in data["references"].values() if r["project"] == pid]

    # ---- 公式与依赖图 ------------------------------------------------------

    def create_formula_version(self, actor: dict, pid: str, body: dict) -> dict:
        metric_key = _require_str(body, "metric_key")
        expression = _require_str(body, "expression")
        with self.store.transaction() as data:
            project = self._project(data, pid)
            self._require_member_role(actor, project, "analyst", "admin")
            metric = self._metric_by_key(data, project, metric_key)
            _, refs = parse_expression(expression)
            by_key = {m["key"]: m for m in self._metrics(data, project)}
            for referenced in refs.metrics | refs.prev | set(refs.at):
                if referenced not in by_key:
                    raise BadRequest(f"公式引用了未知指标: {referenced}", code="formula_unknown_metric")
            for s, ms in refs.scenarios.items():
                if s not in project["scenarios"]:
                    raise BadRequest(f"公式引用了项目未定义的情景: {s}")
                for referenced in ms:
                    if referenced not in by_key:
                        raise BadRequest(f"公式引用了未知指标: {referenced}", code="formula_unknown_metric")
            dep_ids = {by_key[k]["id"] for k in refs.metrics}
            graph = self._graph(data, project)
            ensure_acyclic(graph, metric["id"], dep_ids)

            vid = self.store.new_id("fv")
            ts = self.store.tick(data)[0]
            version_rec = {
                "id": vid, "project": pid, "metric": metric["id"],
                "metric_key": metric_key, "expression": expression,
                "refs": refs.as_dict(), "note": body.get("note"),
                "created_by": actor["username"], "created_at": ts,
            }
            data["formulas"][vid] = version_rec
            current = data["formula_graph"].setdefault(metric["id"], {})
            current["current_version"] = vid
            current["deps"] = sorted(dep_ids)
            # 记录不可变版本序列
            current.setdefault("versions", []).append(vid)
            self.store.audit(data, actor["username"], "formula.version",
                             {"metric": metric_key, "version": vid})
            return copy.deepcopy(version_rec)

    def list_formulas(self, actor: dict, pid: str) -> list[dict]:
        with self.store.read() as data:
            project = self._project(data, pid)
            self._require_member(actor, project)
            return [copy.deepcopy(f) for f in data["formulas"].values() if f["project"] == pid]

    # ---- 基线导入 ----------------------------------------------------------

    def import_baseline(self, actor: dict, pid: str, body: dict) -> dict:
        require_role(actor, "admin")
        label = _require_str(body, "label")
        with self.store.transaction() as data:
            project = self._project(data, pid)
            branch = self._ensure_baseline_branch(data, project, actor["username"])
            parent = data["branch_heads"].get(branch["id"])
            values, constants = self._values_from_body(
                data, project, body, base_values={}, actor=actor["username"])
            commit = self._make_commit(
                data, project, branch, parent, actor["username"],
                f"baseline: {label}", values, constants, origin="baseline_import",
            )
            self._sync_assumptions(data, project, commit, body.get("values", []), actor["username"])
            self.store.audit(data, actor["username"], "baseline.import",
                             {"project": pid, "commit": commit["id"]})
            return self._commit_view(data, project, commit)

    def baseline(self, actor: dict, pid: str) -> dict:
        with self.store.read() as data:
            project = self._project(data, pid)
            self._require_member(actor, project)
            branch = data["branches"].get(project.get("baseline_branch", ""))
            if not branch:
                return {"branch": None, "head": None}
            head = data["commits"].get(data["branch_heads"].get(branch["id"], ""))
            return {"branch": copy.deepcopy(branch),
                    "head": self._commit_view(data, project, head) if head else None}

    # ---- 分支与提交 --------------------------------------------------------

    def create_branch(self, actor: dict, pid: str, body: dict) -> dict:
        name = _require_str(body, "name")
        with self.store.transaction() as data:
            project = self._project(data, pid)
            self._require_member_role(actor, project, "analyst", "admin")
            base = data["projects"][pid].get("baseline_branch")
            default_head = data["branch_heads"].get(base, "") if base else ""
            from_commit = body.get("from_commit") or default_head
            if not from_commit:
                raise BadRequest("项目还没有基线提交，无法创建分支")
            parent_commit = data["commits"].get(from_commit)
            if not parent_commit or parent_commit["project"] != pid:
                raise NotFound("起点提交不存在")
            if any(b["project"] == pid and b["name"] == name for b in data["branches"].values()):
                raise Conflict("同名分支已存在")
            bid = self.store.new_id("br")
            ts = self.store.tick(data)[0]
            branch = {
                "id": bid, "project": pid, "name": name,
                "parent_branch": parent_commit["branch"],
                "created_from_commit": from_commit,
                "created_by": actor["username"], "created_at": ts,
            }
            data["branches"][bid] = branch
            data["branch_heads"][bid] = from_commit
            self.store.audit(data, actor["username"], "branch.create",
                             {"id": bid, "from": from_commit})
            return copy.deepcopy(branch)

    def list_branches(self, actor: dict, pid: str) -> list[dict]:
        with self.store.read() as data:
            project = self._project(data, pid)
            self._require_member(actor, project)
            return [copy.deepcopy(b) for b in data["branches"].values() if b["project"] == pid]

    def get_branch(self, actor: dict, pid: str, bid: str) -> dict:
        with self.store.read() as data:
            project = self._project(data, pid)
            self._require_member(actor, project)
            branch = self._branch(data, pid, bid)
            head = data["commits"][data["branch_heads"][branch["id"]]]
            return {"branch": copy.deepcopy(branch),
                    "head": self._commit_view(data, project, head)}

    def create_commit(self, actor: dict, pid: str, bid: str, body: dict) -> dict:
        message = _require_str(body, "message")
        with self.store.transaction() as data:
            project = self._project(data, pid)
            self._require_member_role(actor, project, "analyst", "admin")
            branch = self._branch(data, pid, bid)
            head_id = data["branch_heads"][branch["id"]]
            expected = body.get("expected_parent")
            if expected is not None and expected != head_id:
                # 精确到 指标×期间×情景 的合并冲突报告
                raise self._conflict_error(data, project, body, expected, head_id)
            head = data["commits"][head_id]
            base_values = copy.deepcopy(head["values"])
            values, constants = self._values_from_body(
                data, project, body, base_values=base_values,
                base_constants=head["constants"], actor=actor["username"])
            commit = self._make_commit(
                data, project, branch, head_id, actor["username"],
                message, values, constants, origin="branch")
            self._sync_assumptions(data, project, commit,
                                   body.get("changes") or body.get("values"),
                                   actor["username"])
            data["branch_heads"][branch["id"]] = commit["id"]
            self.store.audit(data, actor["username"], "commit.create",
                             {"branch": bid, "commit": commit["id"], "parent": head_id})
            return self._commit_view(data, project, commit)

    def get_commit(self, actor: dict, pid: str, cid: str) -> dict:
        with self.store.read() as data:
            project = self._project(data, pid)
            self._require_member(actor, project)
            commit = self._commit(data, pid, cid)
            return self._commit_view(data, project, commit)

    def commit_status(self, actor: dict, pid: str, cid: str) -> dict:
        with self.store.read() as data:
            project = self._project(data, pid)
            self._require_member(actor, project)
            commit = self._commit(data, pid, cid)
            return self._status(data, project, commit)

    def branch_status(self, actor: dict, pid: str, bid: str) -> dict:
        with self.store.read() as data:
            project = self._project(data, pid)
            self._require_member(actor, project)
            branch = self._branch(data, pid, bid)
            commit = data["commits"][data["branch_heads"][branch["id"]]]
            status = self._status(data, project, commit)
            status["branch_id"] = branch["id"]
            status["commit_id"] = commit["id"]
            return status

    # ---- 对比 / 血缘 / 结果 ------------------------------------------------

    def compare(self, actor: dict, pid: str, a_id: str, b_id: str) -> dict:
        with self.store.read() as data:
            project = self._project(data, pid)
            self._require_member(actor, project)
            a = self._commit(data, pid, a_id)
            b = self._commit(data, pid, b_id)
            metrics_by_id = {m["id"]: m for m in self._metrics(data, project)}
            keys = sorted(set(a["values"]) | set(b["values"]))
            assumptions = []
            for key in keys:
                va, vb = a["values"].get(key), b["values"].get(key)
                if va is None or vb is None or va["value"] != vb["value"] or \
                        va["kind"] != vb["kind"]:
                    mid, period, scenario = split_vkey(key)
                    assumptions.append({
                        "metric_key": metrics_by_id.get(mid, {}).get("key", mid),
                        "period": period, "scenario": scenario,
                        "a": None if va is None else self._value_brief(va),
                        "b": None if vb is None else self._value_brief(vb),
                        "change": _value_change(va, vb),
                    })
            formula_changes = []
            for mid, mk in metrics_by_id.items():
                va = a["formula_versions"].get(mid)
                vb = b["formula_versions"].get(mid)
                if va != vb:
                    formula_changes.append({
                        "metric_key": mk["key"],
                        "a": self._formula_brief(data.get("formulas", {}).get(va)),
                        "b": self._formula_brief(data.get("formulas", {}).get(vb)),
                    })
            const_changes = []
            for name in sorted(set(a["constants"]) | set(b["constants"])):
                if a["constants"].get(name) != b["constants"].get(name):
                    const_changes.append({"name": name,
                                          "a": a["constants"].get(name),
                                          "b": b["constants"].get(name)})
            return {
                "a": {"commit_id": a["id"], "digest": a["digest"], "created_at": a["created_at"]},
                "b": {"commit_id": b["id"], "digest": b["digest"], "created_at": b["created_at"]},
                "assumption_diffs": assumptions,
                "formula_changes": formula_changes,
                "constant_changes": const_changes,
                "explanation": self._explain(assumptions, formula_changes, const_changes),
                "pending_a": a["pending"],
                "pending_b": b["pending"],
            }

    def trace(self, actor: dict, pid: str, cid: str, metric_key: str, period: str, scenario: str) -> dict:
        with self.store.read() as data:
            project = self._project(data, pid)
            self._require_member(actor, project)
            commit = self._commit(data, pid, cid)
            metric = self._metric_by_key(data, project, metric_key)
            entry = commit["values"].get(vkey(metric["id"], period, scenario))
            tree = self._trace_node(data, project, commit, metric["id"], period, scenario, set())
            status = self._status(data, project, commit)
            return {
                "commit_id": cid, "metric_key": metric_key,
                "period": period, "scenario": scenario,
                "value": None if entry is None else entry["value"],
                "node": tree,
                "status": {
                    "pending": any(p["metric"] == metric["id"] and p["period"] == period
                                   and p["scenario"] == scenario for p in status["pending"]),
                    "outdated_formula": metric["id"] in status["outdated_formulas"],
                    "stale_assumption": any(
                        s["metric"] == metric["id"] and s["period"] == period
                        and s["scenario"] == scenario for s in status["stale_assumptions"]),
                },
            }

    def results(self, actor: dict, pid: str, cid: str, metric_key: str) -> dict:
        with self.store.read() as data:
            project = self._project(data, pid)
            self._require_member(actor, project)
            commit = self._commit(data, pid, cid)
            metric = self._metric_by_key(data, project, metric_key)
            status = self._status(data, project, commit)
            stale_keys = {vkey(s["metric"], s["period"], s["scenario"]) for s in status["stale_assumptions"]}
            pending_keys = {vkey(p["metric"], p["period"], p["scenario"]) for p in status["pending"]}
            grid = {}
            for scenario in project["scenarios"]:
                grid[scenario] = {}
                for period in project["periods"]:
                    k = vkey(metric["id"], period, scenario)
                    entry = commit["values"].get(k)
                    grid[scenario][period] = {
                        "value": None if entry is None else entry["value"],
                        "kind": None if entry is None else entry["kind"],
                        "stale": k in stale_keys,
                        "pending": k in pending_keys,
                    }
            return {"commit_id": cid, "metric_key": metric_key, "results": grid,
                    "formula_version": commit["formula_versions"].get(metric["id"])}

    # ---- 评审（意见追加） --------------------------------------------------

    def create_review(self, actor: dict, pid: str, body: dict) -> dict:
        commit_id = _require_str(body, "commit_id")
        with self.store.transaction() as data:
            project = self._project(data, pid)
            self._require_member_role(actor, project, "analyst", "admin")
            commit = self._commit(data, pid, commit_id)
            for r in data["reviews"].values():
                if r["project"] == pid and r["commit"] == commit_id and \
                        r["state"] in ("submitted", "approved"):
                    raise Conflict("该提交已存在进行中或已通过的评审")
            rid = self.store.new_id("rev")
            ts = self.store.tick(data)[0]
            rec = {
                "id": rid, "project": pid, "commit": commit_id,
                "submitter": actor["username"], "state": "submitted",
                "created_at": ts, "decided_by": None, "decided_at": None,
                "transitions": [{"at": ts, "by": actor["username"], "to": "submitted", "reason": None}],
                "comments": [],
            }
            data["reviews"][rid] = rec
            self.store.audit(data, actor["username"], "review.create",
                             {"id": rid, "commit": commit_id})
            return copy.deepcopy(rec)

    def list_reviews(self, actor: dict, pid: str) -> list[dict]:
        with self.store.read() as data:
            project = self._project(data, pid)
            self._require_member(actor, project)
            return [copy.deepcopy(r) for r in data["reviews"].values() if r["project"] == pid]

    def get_review(self, actor: dict, pid: str, rid: str) -> dict:
        with self.store.read() as data:
            project = self._project(data, pid)
            self._require_member(actor, project)
            return copy.deepcopy(self._review(data, pid, rid))

    def add_review_comment(self, actor: dict, pid: str, rid: str, body: dict) -> dict:
        text = _require_str(body, "body")
        with self.store.transaction() as data:
            project = self._project(data, pid)
            self._require_member(actor, project)
            review = self._review(data, pid, rid)
            if review["state"] not in ("submitted", "changes_requested"):
                raise Conflict("评审已终结，不能继续评论")
            ts, seq = self.store.tick(data)
            # 只允许 append：整段历史无修改/删除接口
            review["comments"].append({
                "seq": seq, "at": ts, "author": actor["username"],
                "body": text,
            })
            self.store.audit(data, actor["username"], "review.comment",
                             {"id": rid, "seq": seq})
            return copy.deepcopy(review)

    def decide_review(self, actor: dict, pid: str, rid: str, body: dict) -> dict:
        decision = _require_str(body, "decision")
        if decision not in ("approve", "request_changes", "reopen"):
            raise BadRequest("decision 必须是 approve/request_changes/reopen")
        with self.store.transaction() as data:
            project = self._project(data, pid)
            self._require_member_role(actor, project, "reviewer", "admin")
            review = self._review(data, pid, rid)
            transitions = {
                ("submitted", "approve"): "approved",
                ("submitted", "request_changes"): "changes_requested",
                ("changes_requested", "reopen"): "submitted",
                ("approved", "reopen"): "submitted",
            }
            new_state = transitions.get((review["state"], decision))
            if new_state is None:
                raise Conflict(f"当前状态 {review['state']} 不允许 {decision}")
            ts, _ = self.store.tick(data)
            review["state"] = new_state
            review["decided_by"] = actor["username"] if decision != "reopen" else review["decided_by"]
            review["decided_at"] = ts
            review["transitions"].append({
                "at": ts, "by": actor["username"], "to": new_state,
                "reason": body.get("reason"),
            })
            self.store.audit(data, actor["username"], "review.decision",
                             {"id": rid, "state": new_state})
            return copy.deepcopy(review)

    # ---- 发布（并发唯一权威版本）与导出 ------------------------------------

    def publish(self, actor: dict, pid: str, body: dict) -> dict:
        commit_id = _require_str(body, "commit_id")
        review_id = _require_str(body, "review_id")
        with self.store.transaction() as data:
            project = self._project(data, pid)
            self._require_member_role(actor, project, "reviewer", "admin")
            commit = self._commit(data, pid, commit_id)
            review = self._review(data, pid, review_id)
            if review["commit"] != commit_id or review["state"] != "approved":
                raise Conflict("只能发布审批通过的评审所对应的提交")
            status = self._status(data, project, commit)
            if status["needs_recompute"] and not bool(body.get("acknowledge_pending")):
                raise Conflict(
                    "该版本仍有待重算/过期结论，不能作为权威版本发布；"
                    "如主管确认接受，请在请求中携带 acknowledge_pending=true",
                    code="release_not_ready",
                )
            existing = data["release_winners"].get(pid)
            if existing:
                winner = data["releases"][existing]
                raise Conflict(
                    f"项目已存在权威发布版本 {winner['label']}（{existing}），并发/重复发布被拒绝",
                    code="release_exists",
                )
            rel_id = self.store.new_id("rel")
            ts = self.store.tick(data)[0]
            data_summary = self._data_summary(data, project, commit)
            rec = {
                "id": rel_id, "project": pid, "commit": commit_id,
                "review": review_id, "label": body.get("label", f"release-{rel_id}"),
                "published_by": actor["username"], "published_at": ts,
                "digest": commit["digest"],
                "formula_versions": dict(commit["formula_versions"]),
                "approver": review["decided_by"],
                "data_summary": data_summary,
                "pending_at_release": list(commit["pending"]),
            }
            data["releases"][rel_id] = rec
            data["release_winners"][pid] = rel_id
            self.store.audit(data, actor["username"], "release.publish",
                             {"id": rel_id, "commit": commit_id})
            return copy.deepcopy(rec)

    def list_releases(self, actor: dict, pid: str) -> list[dict]:
        with self.store.read() as data:
            project = self._project(data, pid)
            self._require_member(actor, project)
            return [copy.deepcopy(r) for r in data["releases"].values() if r["project"] == pid]

    def get_release(self, actor: dict, pid: str, rel_id: str) -> dict:
        with self.store.read() as data:
            project = self._project(data, pid)
            self._require_member(actor, project)
            return copy.deepcopy(self._release(data, pid, rel_id))

    def export_release(self, actor: dict, pid: str, rel_id: str) -> dict:
        """导出不可变发布包：固定数据摘要、公式版本与审批人。"""
        with self.store.read() as data:
            project = self._project(data, pid)
            self._require_member(actor, project)
            release = self._release(data, pid, rel_id)
            commit = self._commit(data, pid, release["commit"])
            metrics_by_id = {m["id"]: m for m in self._metrics(data, project)}
            values = []
            for key in sorted(commit["values"]):
                mid, period, scenario = split_vkey(key)
                e = commit["values"][key]
                values.append({
                    "metric_key": metrics_by_id[mid]["key"],
                    "period": period, "scenario": scenario,
                    "value": e["value"], "kind": e["kind"],
                    "set_by": e.get("set_by"), "set_at": e.get("set_at"),
                    "business_date": e.get("business_date"),
                    "data_source_id": e.get("data_source_id"),
                    "data_source_revision": e.get("data_source_revision"),
                    "formula_id": e.get("formula_id"),
                    "reference_ids": e.get("reference_ids", []),
                })
            formulas = []
            for mid, fv_id in commit["formula_versions"].items():
                fv = data["formulas"].get(fv_id)
                if fv:
                    formulas.append({"metric_key": metrics_by_id[mid]["key"],
                                     "formula_id": fv_id, "expression": fv["expression"],
                                     "version_created_at": fv["created_at"]})
            return {
                "export_kind": "research_assumptions_release",
                "exported_at": _utcnow(),
                "project": {"id": project["id"], "name": project["name"],
                            "scenarios": project["scenarios"], "periods": project["periods"]},
                "company": copy.deepcopy(data["companies"].get(project["company_id"])),
                "release": {"id": release["id"], "label": release["label"],
                            "published_at": release["published_at"],
                            "published_by": release["published_by"],
                            "approver": release["approver"],
                            "review_id": release["review"]},
                "data_summary": release["data_summary"],
                "formula_versions": sorted(formulas, key=lambda x: x["metric_key"]),
                "constants": dict(sorted(commit["constants"].items())),
                "values": values,
                "pending_at_release": release["pending_at_release"],
                "digest": release["digest"],
            }

    # ---- 内部：实体查找 ----------------------------------------------------

    def _project(self, data: dict, pid: str) -> dict:
        p = data["projects"].get(pid)
        if not p:
            raise NotFound("项目不存在")
        return p

    def _branch(self, data: dict, pid: str, bid: str) -> dict:
        b = data["branches"].get(bid)
        if not b or b["project"] != pid:
            raise NotFound("分支不存在")
        return b

    def _commit(self, data: dict, pid: str, cid: str) -> dict:
        c = data["commits"].get(cid)
        if not c or c["project"] != pid:
            raise NotFound("提交不存在")
        return c

    def _review(self, data: dict, pid: str, rid: str) -> dict:
        r = data["reviews"].get(rid)
        if not r or r["project"] != pid:
            raise NotFound("评审不存在")
        return r

    def _release(self, data: dict, pid: str, rel_id: str) -> dict:
        r = data["releases"].get(rel_id)
        if not r or r["project"] != pid:
            raise NotFound("发布版本不存在")
        return r

    def _metrics(self, data: dict, project: dict) -> list[dict]:
        return [m for m in data["metrics"].values() if m["project"] == project["id"]]

    def _metric_by_key(self, data: dict, project: dict, key: str) -> dict:
        for m in self._metrics(data, project):
            if m["key"] == key:
                return m
        raise NotFound(f"指标不存在: {key}")

    def _graph(self, data: dict, project: dict) -> dict[str, set[str]]:
        ids = {m["id"] for m in self._metrics(data, project)}
        return {k: set(v.get("deps", [])) for k, v in data["formula_graph"].items() if k in ids}

    def _current_formula(self, data: dict, project: dict, metric_id: str) -> dict | None:
        node = data["formula_graph"].get(metric_id)
        if not node:
            return None
        return data["formulas"].get(node["current_version"])

    def _is_member(self, user: dict, project: dict) -> bool:
        if user["role"] == "admin":
            # 观察名单项目即便 admin 也需要显式名单权限
            return (not project.get("watchlist")) or bool(user.get("watchlist_access"))
        if user["username"] not in project["members"]:
            return False
        if project.get("watchlist") and not user.get("watchlist_access"):
            return False
        return True

    def _require_member(self, user: dict | None, project: dict) -> dict:
        if not user:
            raise Unauthorized("缺少有效身份令牌")
        if not self._is_member(user, project):
            if user["username"] in project.get("members", []) and project.get("watchlist") \
                    and not user.get("watchlist_access"):
                raise Forbidden("敏感观察名单项目需要观察名单权限")
            raise Forbidden("不是项目成员")
        return user

    def _require_member_role(self, user: dict | None, project: dict, *roles: str) -> dict:
        self._require_member(user, project)
        if user["role"] not in roles:
            raise Forbidden(f"项目内该操作需要角色: {', '.join(roles)}")
        return user

    # ---- 内部：基线分支 ----------------------------------------------------

    def _ensure_baseline_branch(self, data: dict, project: dict, actor: str) -> dict:
        bid = project.get("baseline_branch")
        if bid and bid in data["branches"]:
            return data["branches"][bid]
        bid = self.store.new_id("br")
        ts = self.store.tick(data)[0]
        branch = {
            "id": bid, "project": project["id"], "name": "baseline",
            "parent_branch": None, "created_from_commit": None,
            "created_by": actor, "created_at": ts, "system": True,
        }
        data["branches"][bid] = branch
        project["baseline_branch"] = bid
        return branch

    # ---- 内部：值装载与重算引擎 --------------------------------------------

    def _values_from_body(
        self, data: dict, project: dict, body: dict, *,
        base_values: dict, base_constants: dict | None = None, actor: str = "unknown",
    ) -> tuple[dict, dict]:
        """把导入/提交载荷里的手工取值合入基础快照，返回 (values, constants)。不触发重算。"""
        values = copy.deepcopy(base_values)
        constants = copy.deepcopy(base_constants or {})
        if isinstance(body.get("constants"), dict):
            for name, val in body["constants"].items():
                if not isinstance(name, str) or not name:
                    raise BadRequest("常量名必须是非空字符串")
                constants[name] = _number(val, f"constants.{name}")
        rows = body.get("values") if "values" in body else body.get("changes")
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise BadRequest("values/changes 必须是数组")
        by_key = {m["key"]: m for m in self._metrics(data, project)}
        ts = self.store.tick(data)[0]
        for row in rows:
            self._validate_row(data, project, row, by_key)
            m = by_key[row["metric_key"]]
            key = vkey(m["id"], row["period"], row["scenario"])
            if row.get("value") is None and ("value" in row):
                values.pop(key, None)  # 显式撤回假设
                continue
            source_id = row.get("data_source_id")
            entry = {
                "kind": "manual", "value": _number(row["value"], "value"),
                "set_by": actor,
                "set_at": ts,
                "business_date": row.get("business_date"),
                "data_source_id": source_id,
                "data_source_revision": (
                    data["data_sources"][source_id]["current_revision"] if source_id else None),
                "reference_ids": list(row.get("reference_ids", [])),
            }
            values[key] = entry
        return values, constants

    def _validate_row(self, data: dict, project: dict, row: Any, by_key: dict) -> None:
        if not isinstance(row, dict):
            raise BadRequest("每个取值必须是对象")
        if row.get("metric_key") not in by_key:
            raise BadRequest(f"未知指标: {row.get('metric_key')}")
        if row.get("period") not in project["periods"]:
            raise BadRequest(f"期间未在项目中声明: {row.get('period')}")
        if row.get("scenario") not in project["scenarios"]:
            raise BadRequest(f"情景未在项目中定义: {row.get('scenario')}")
        if "value" in row and row["value"] is not None:
            _number(row["value"], "value")
        sid = row.get("data_source_id")
        if sid and sid not in data["data_sources"]:
            raise BadRequest(f"数据源不存在: {sid}")
        for rid in row.get("reference_ids", []):
            ref = data["references"].get(rid)
            if not ref or ref["project"] != project["id"]:
                raise BadRequest(f"引用材料不存在或不属于项目: {rid}")

    def _make_commit(
        self, data: dict, project: dict, branch: dict, parent: str | None,
        actor: str, message: str, values: dict, constants: dict, *, origin: str,
    ) -> dict:
        cid = self.store.new_id("c")
        ts, seq = self.store.tick(data)
        commit = {
            "id": cid, "project": project["id"], "branch": branch["id"],
            "parent": parent, "message": message, "author": actor,
            "created_at": ts, "seq": seq, "origin": origin,
            "values": values, "constants": constants,
            "formula_versions": {}, "pending": [],
        }
        # 记录创建时项目中全部公式的当前版本，用于之后识别"公式已更新、结论待重算"
        for m in self._metrics(data, project):
            f = self._current_formula(data, project, m["id"])
            if f:
                commit["formula_versions"][m["id"]] = f["id"]
        self._recompute(data, project, commit)
        commit["digest"] = self._digest(data, project, commit)
        data["commits"][cid] = commit
        data["branch_heads"][branch["id"]] = cid
        return commit

    def _recompute(self, data: dict, project: dict, commit: dict, changed: set[str] | None = None) -> None:
        """按依赖拓扑重算公式指标；缺输入的组合进入 pending（待重算）。"""
        graph = self._graph(data, project)
        formula_metrics = {m["id"] for m in self._metrics(data, project)
                           if self._current_formula(data, project, m["id"])}
        if changed is not None:
            # 变更指标 + 其全部下游
            roots: set[str] = set()
            reverse: dict[str, set[str]] = {}
            for node, deps in graph.items():
                for d in deps:
                    reverse.setdefault(d, set()).add(node)
            stack = [c for c in changed if c in formula_metrics or c in reverse]
            while stack:
                cur = stack.pop()
                if cur in roots:
                    continue
                roots.add(cur)
                stack.extend(reverse.get(cur, ()))
            roots &= formula_metrics
        else:
            roots = formula_metrics
        order = topological_order(graph, roots)
        # 遍历项目声明的全部 期间×情景，并包含历史值里出现过的组合（兼容旧数据）。
        combos = {(p, s) for s in project["scenarios"] for p in project["periods"]}
        combos |= {(split_vkey(k)[1], split_vkey(k)[2]) for k in commit["values"]}
        period_order = project["periods"]

        for mid in order:
            formula = self._current_formula(data, project, mid)
            if not formula:
                continue
            tree = self._ast(formula["expression"])
            refs = formula["refs"]
            for period, scenario in sorted(combos):
                self._compute_one(data, project, commit, mid, period, scenario,
                                  tree, refs, period_order, formula)

    def _compute_one(
        self, data, project, commit, mid, period, scenario, tree, refs, period_order, formula
    ) -> None:
        values = commit["values"]
        key = vkey(mid, period, scenario)
        env: dict[str, float] = {}
        resolved: list[dict] = []
        missing: list[str] = []
        present_inputs = 0
        by_id = {m["id"]: m for m in self._metrics(data, project)}

        def lookup(m_id: str, p: str, s: str, ref_name: str, kind: str) -> float:
            nonlocal present_inputs
            e = values.get(vkey(m_id, p, s))
            if e is None:
                missing.append(ref_name)
                raise KeyError(ref_name)
            present_inputs += 1
            resolved.append({
                "kind": kind, "metric": by_id[m_id]["key"],
                "metric_id": m_id, "period": p, "scenario": s,
                "value": e["value"], "value_kind": e["kind"],
            })
            return float(e["value"])

        commit["pending"] = [p for p in commit["pending"]
                             if not (p["metric"] == mid and p["period"] == period
                                     and p["scenario"] == scenario)]
        try:
            id_by_key = {m["key"]: m["id"] for m in self._metrics(data, project)}
            for mk in refs["metrics"]:
                env[mk] = lookup(id_by_key[mk], period, scenario, mk, "metric")
            forms = {
                "prev": lambda mk: lookup(
                    id_by_key[mk], _prev_period(period_order, period), scenario,
                    f"prev({mk})", "metric_prev"),
                "at": lambda mk, p: lookup(id_by_key[mk], p, scenario, f"at({mk},{p})", "metric_at"),
                "scenario": lambda sc, mk: lookup(
                    id_by_key[mk], period, sc, f"scenario({sc},{mk})", "metric_scenario"),
                "const": lambda name: self._lookup_const(commit, name, resolved, missing),
            }
            value = safe_eval(tree, env, forms)
        except KeyError:
            values.pop(key, None)
            if present_inputs or self._partial_inputs_present(commit, refs, id_by_key,
                                                              period, scenario, period_order):
                commit["pending"].append({
                    "metric": mid, "period": period, "scenario": scenario,
                    "reason": f"缺少输入: {', '.join(sorted(set(missing)))}",
                })
            return
        except BadRequest:
            raise
        values[key] = {
            "kind": "formula", "value": value,
            "formula_id": formula["id"], "formula_expression": formula["expression"],
            "computed_at": commit["created_at"],
            "inputs": resolved,
        }

    def _lookup_const(self, commit: dict, name: str, resolved: list[dict], missing: list[str]) -> float:
        if name not in commit["constants"]:
            missing.append(f"const({name})")
            raise KeyError(name)
        val = float(commit["constants"][name])
        resolved.append({"kind": "const", "name": name, "value": val})
        return val

    def _partial_inputs_present(
        self, commit, refs, id_by_key, period, scenario, period_order
    ) -> bool:
        values = commit["values"]
        for mk in refs["metrics"]:
            if vkey(id_by_key[mk], period, scenario) in values:
                return True
        for mk in refs["prev"]:
            if vkey(id_by_key[mk], _prev_period(period_order, period), scenario) in values:
                return True
        for mk, periods in refs["at"].items():
            for p in periods:
                if vkey(id_by_key[mk], p, scenario) in values:
                    return True
        for sc, mks in refs["scenarios"].items():
            for mk in mks:
                if vkey(id_by_key[mk], period, sc) in values:
                    return True
        return False

    # ---- 内部：假设索引同步 / 状态 / 血缘 ----------------------------------

    def _sync_assumptions(self, data: dict, project: dict, commit: dict, rows: list | None, actor: str) -> None:
        by_key = {m["key"]: m for m in self._metrics(data, project)}
        ts = commit["created_at"]
        for row in rows or []:
            if not isinstance(row, dict) or row.get("metric_key") not in by_key:
                continue
            m = by_key[row["metric_key"]]
            key = vkey(m["id"], row["period"], row["scenario"])
            composite = KEY_SEP.join((project["id"], key))
            rec = data["assumptions"].get(composite)
            source_id = row.get("data_source_id")
            if rec is None:
                rec = {"project": project["id"], "metric": m["id"],
                       "period": row["period"], "scenario": row["scenario"],
                       "stale": False, "stale_reason": None}
                data["assumptions"][composite] = rec
            if row.get("value") is not None or "value" in row:
                # 分析师重新确认取值即清除过期标记（以新的业务数据为准）
                rec["stale"] = False
                rec["stale_reason"] = None
            rec["data_source_id"] = source_id
            rec["data_source_revision"] = (
                data["data_sources"][source_id]["current_revision"] if source_id
                else rec.get("data_source_revision"))
            rec["updated_by"] = actor
            rec["updated_at"] = ts
            entry = commit["values"].get(key)
            rec["current_value"] = None if entry is None else entry["value"]

    def _status(self, data: dict, project: dict, commit: dict) -> dict:
        pending = list(commit["pending"])
        # 直接过期：手工假设绑定的数据源被修订
        stale_keys: set[str] = set()
        stale_meta: dict[str, dict] = {}
        for key in commit["values"]:
            assump = data["assumptions"].get(KEY_SEP.join((project["id"], key)))
            if assump and assump.get("stale"):
                stale_keys.add(key)
                stale_meta[key] = assump.get("stale_reason")
        # 间接过期：公式结果的任一输入过期（沿实际求值血缘传播到下游结论）
        changed = True
        while changed:
            changed = False
            for key, entry in commit["values"].items():
                if key in stale_keys or entry.get("kind") != "formula":
                    continue
                for inp in entry.get("inputs", []):
                    if inp.get("kind", "").startswith("metric") or inp.get("kind") == "manual":
                        dep_key = vkey(inp["metric_id"], inp["period"], inp["scenario"])
                        if dep_key in stale_keys:
                            stale_keys.add(key)
                            changed = True
                            break
        stale = []
        for key in sorted(stale_keys):
            mid, period, scenario = split_vkey(key)
            stale.append({"metric": mid, "period": period, "scenario": scenario,
                          "reason": stale_meta.get(key, {"propagated": True})})
        outdated = {}
        for m in self._metrics(data, project):
            cur = self._current_formula(data, project, m["id"])
            used_id = commit["formula_versions"].get(m["id"])
            if cur and used_id != cur["id"] and any(
                    split_vkey(k)[0] == m["id"] for k in commit["values"]):
                outdated[m["id"]] = {"used": used_id, "current": cur["id"]}
        return {
            "commit_id": commit["id"],
            "stale_assumptions": stale,
            "pending": pending,
            "outdated_formulas": outdated,
            "needs_recompute": bool(stale or pending or outdated),
        }

    def _commit_view(self, data: dict, project: dict, commit: dict) -> dict:
        view = copy.deepcopy({k: v for k, v in commit.items()})
        metrics_by_id = {m["id"]: m for m in self._metrics(data, project)}
        rows = []
        for key in sorted(commit["values"]):
            mid, period, scenario = split_vkey(key)
            e = commit["values"][key]
            rows.append({"metric_key": metrics_by_id.get(mid, {}).get("key", mid),
                         "period": period, "scenario": scenario,
                         "value": e["value"], "kind": e["kind"],
                         "formula_id": e.get("formula_id"),
                         "data_source_id": e.get("data_source_id"),
                         "reference_ids": e.get("reference_ids", [])})
        view["assumptions"] = rows
        view["status"] = self._status(data, project, commit)
        return view

    def _trace_node(self, data, project, commit, mid, period, scenario, seen: set) -> dict:
        metrics_by_id = {m["id"]: m for m in self._metrics(data, project)}
        key = vkey(mid, period, scenario)
        entry = commit["values"].get(key)
        base = {"metric_key": metrics_by_id.get(mid, {}).get("key", mid),
                "period": period, "scenario": scenario}
        if entry is None:
            return {**base, "value": None, "kind": "missing",
                    "pending": any(p["metric"] == mid and p["period"] == period
                                   and p["scenario"] == scenario for p in commit["pending"])}
        if (key in seen) and entry["kind"] == "formula":
            return {**base, "value": entry["value"], "kind": "cycle_guard"}
        seen = seen | {key}
        if entry["kind"] == "manual":
            source = data["data_sources"].get(entry.get("data_source_id") or "")
            assump = data["assumptions"].get(KEY_SEP.join((project["id"], key)))
            return {
                **base, "value": entry["value"], "kind": "manual",
                "set_by": entry.get("set_by"), "set_at": entry.get("set_at"),
                "business_date": entry.get("business_date"),
                "data_source": None if not source else {
                    "id": source["id"], "name": source["name"],
                    "revision_used": entry.get("data_source_revision"),
                    "current_revision": source["current_revision"],
                    "stale": bool(assump and assump.get("stale")),
                },
                "references": [copy.deepcopy(data["references"].get(r))
                               for r in entry.get("reference_ids", [])],
            }
        children = []
        for inp in entry.get("inputs", []):
            if inp["kind"] == "const":
                children.append({"kind": "const", "name": inp["name"], "value": inp["value"]})
            else:
                child_mid = inp["metric_id"]
                children.append(self._trace_node(data, project, commit, child_mid,
                                                 inp["period"], inp["scenario"], seen))
        return {**base, "value": entry["value"], "kind": "formula",
                "formula_id": entry.get("formula_id"),
                "expression": entry.get("formula_expression"),
                "computed_at": entry.get("computed_at"),
                "inputs": children}

    # ---- 内部：冲突 / 解释 / 摘要 / 摘要哈希 -------------------------------

    def _conflict_error(self, data, project, body, base_id, head_id) -> Conflict:
        base = data["commits"][base_id]
        head = data["commits"][head_id]
        metrics_by_id = {m["id"]: m for m in self._metrics(data, project)}
        id_by_key = {m["key"]: m["id"] for m in self._metrics(data, project)}
        conflicts = []
        for row in body.get("changes", []):
            if not isinstance(row, dict) or row.get("metric_key") not in id_by_key:
                continue
            k = vkey(id_by_key[row["metric_key"]], row["period"], row["scenario"])
            eb, eh = base["values"].get(k), head["values"].get(k)
            submitted = row.get("value")
            head_changed = (eb is None or eh is None or eb.get("value") != eh.get("value"))
            if head_changed and eh is not None and submitted is not None \
                    and float(submitted) != float(eh["value"]):
                mid, period, scenario = split_vkey(k)
                conflicts.append({
                    "metric_key": metrics_by_id[mid]["key"],
                    "period": period, "scenario": scenario,
                    "base_value": None if eb is None else eb.get("value"),
                    "current_value": None if eh is None else eh.get("value"),
                    "submitted_value": submitted,
                })
        const_conflicts = []
        if isinstance(body.get("constants"), dict):
            for name, val in body["constants"].items():
                if base["constants"].get(name) != head["constants"].get(name) \
                        and head["constants"].get(name) != val:
                    const_conflicts.append({"name": name,
                                            "base": base["constants"].get(name),
                                            "current": head["constants"].get(name),
                                            "submitted": val})
        return _ConflictWithDetail("提交在 指标×期间×情景 粒度存在合并冲突", {
            "expected_parent": base_id, "current_head": head_id,
            "assumption_conflicts": conflicts,
            "constant_conflicts": const_conflicts,
        })

    def _explain(self, assumption_diffs, formula_changes, const_changes) -> list[str]:
        notes = []
        manual = [d for d in assumption_diffs
                  if (d["a"] is None or d["a"]["kind"] == "manual")
                  and (d["b"] is None or d["b"]["kind"] == "manual")]
        if manual:
            notes.append(f"{len(manual)} 个指标×期间×情景的手工输入取值不同（数据差异）")
        driven = [d for d in assumption_diffs if d["change"] == "formula_vs_manual"
                  or (d["a"] and d["b"] and d["a"]["kind"] != d["b"]["kind"])]
        if driven:
            notes.append(f"{len(driven)} 个结果的计算来源（手工/公式）发生切换")
        if formula_changes:
            keys = ", ".join(sorted({c["metric_key"] for c in formula_changes}))
            notes.append(f"公式版本不同的指标: {keys}（模型选择差异）")
        if const_changes:
            notes.append(f"{len(const_changes)} 个常量假设不同（例如汇率/税率）")
        if not notes:
            notes.append("两个版本内容一致")
        return notes

    def _value_brief(self, entry: dict) -> dict:
        return {"value": entry["value"], "kind": entry["kind"],
                "formula_id": entry.get("formula_id"),
                "data_source_id": entry.get("data_source_id"),
                "data_source_revision": entry.get("data_source_revision"),
                "set_at": entry.get("set_at"), "computed_at": entry.get("computed_at")}

    def _formula_brief(self, fv: dict | None) -> dict | None:
        if fv is None:
            return None
        return {"formula_id": fv["id"], "expression": fv["expression"],
                "created_by": fv["created_by"], "created_at": fv["created_at"]}

    def _digest(self, data: dict, project: dict, commit: dict) -> str:
        values_payload = {}
        for key in sorted(commit["values"]):
            e = commit["values"][key]
            values_payload[key] = {
                "v": e["value"], "kind": e["kind"],
                "formula_id": e.get("formula_id"),
                "source": e.get("data_source_id"),
                "source_rev": e.get("data_source_revision"),
                "business_date": e.get("business_date"),
            }
        formulas_payload = {}
        for mid, fv_id in commit["formula_versions"].items():
            fv = data["formulas"].get(fv_id)
            formulas_payload[mid] = {"id": fv_id,
                                     "expression": None if fv is None else fv["expression"]}
        return canonical_digest({
            "project": project["id"],
            "values": values_payload,
            "constants": commit["constants"],
            "formulas": formulas_payload,
        })

    def _data_summary(self, data: dict, project: dict, commit: dict) -> dict:
        source_usage: dict[str, dict] = {}
        ref_ids: set[str] = set()
        for e in commit["values"].values():
            sid = e.get("data_source_id")
            if sid:
                row = source_usage.setdefault(sid, {"revisions_used": set()})
                if e.get("data_source_revision") is not None:
                    row["revisions_used"].add(e["data_source_revision"])
            ref_ids.update(e.get("reference_ids", []))
        sources = []
        for sid, row in source_usage.items():
            s = data["data_sources"][sid]
            sources.append({
                "id": sid, "name": s["name"], "kind": s.get("kind"),
                "provider": s.get("provider"),
                "revisions_used": sorted(row["revisions_used"]),
                "current_revision": s["current_revision"],
            })
        references = []
        for rid in sorted(ref_ids):
            r = data["references"].get(rid)
            if r:
                references.append({"id": rid, "title": r["title"],
                                   "citation": r.get("citation"), "uri": r.get("uri")})
        digest = canonical_digest({"sources": sources, "references": references})
        return {"sources": sources, "references": references, "digest": digest}


# ---- 模块级辅助 ------------------------------------------------------------

class _ConflictWithDetail(Conflict):
    def __init__(self, message: str, detail: dict):
        super().__init__(message, code="merge_conflict")
        self.detail = detail

    def to_payload(self) -> dict:
        return {**super().to_payload(), **self.detail}


def _utcnow() -> str:
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _require_str(body: dict, name: str) -> str:
    val = body.get(name)
    if not isinstance(val, str) or not val.strip():
        raise BadRequest(f"缺少字段或字段非字符串: {name}")
    return val


def _number(val: Any, label: str) -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise BadRequest(f"{label} 必须是数字")
    return float(val)


def _public_user(user: dict) -> dict:
    return {k: user[k] for k in ("username", "role", "watchlist_access", "created_at")}


def _value_change(a: dict | None, b: dict | None) -> str:
    if a is None:
        return "added_in_b"
    if b is None:
        return "removed_in_b"
    if a["kind"] != b["kind"]:
        return "formula_vs_manual"
    if a["kind"] == "formula" and a.get("formula_id") != b.get("formula_id"):
        return "formula_version"
    if a.get("data_source_revision") != b.get("data_source_revision"):
        return "external_revision"
    return "value"


def _prev_period(ordered: list[str], current: str) -> str:
    if current in ordered:
        i = ordered.index(current)
        if i == 0:
            raise KeyError(f"prev({current})")
        return ordered[i - 1]
    ordered_sorted = sorted(set(ordered) | {current})
    i = ordered_sorted.index(current)
    if i == 0:
        raise KeyError(f"prev({current})")
    return ordered_sorted[i - 1]
