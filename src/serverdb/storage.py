from __future__ import annotations

import contextlib
import fcntl
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

# 进程内锁，保证同一进程多线程串行化写操作；
# 文件锁（LOCK_EX）保证多进程并发时同样安全（例如 uvicorn 多 worker / 测试并行）。
_GLOBAL_LOCK = threading.RLock()


class Store:
    """以单个 JSON 文件为快照的持久化层。

    - 每次写入都是全量落盘 + fsync，文件首先写入临时文件再原子替换，避免半截文件。
    - ``audit.log`` 仅追加，记录所有变更动作，满足"审批/复核过程可追溯"。
    - 单调时钟 ``seq`` 与 ``now`` 同时返回，业务时间与系统接收时间分开保存。
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.audit_path = self.path.with_suffix(".audit.log")
        self._lock_fh: Any = None
        self._data: dict[str, Any] | None = None
        self._mtime: float | None = None

    # ---- 基础原语 ----------------------------------------------------------

    def _initial_data(self) -> dict[str, Any]:
        return {
            "users": {},
            "projects": {},
            "companies": {},
            "data_sources": {},
            "metrics": {},
            "assumptions": {},
            "formulas": {},
            "formula_graph": {},
            "references": {},
            "branches": {},
            "branch_heads": {},
            "commits": {},
            "reviews": {},
            "review_comments": {},
            "releases": {},
            "release_winners": {},
            "audit": [],
            "counters": {},
        }

    def load(self) -> dict[str, Any]:
        with _GLOBAL_LOCK:
            return self._load_locked()

    def _load_locked(self) -> dict[str, Any]:
        # 持锁状态下检查文件 mtime：其他进程落盘后本进程缓存必须失效重载。
        if self.path.exists():
            mtime = self.path.stat().st_mtime_ns
            if self._data is not None and self._mtime is not None and mtime != self._mtime:
                self._data = None
        if self._data is not None:
            return self._data
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            for key, default in self._initial_data().items():
                data.setdefault(key, default if not isinstance(default, dict) else {})
            self._data = data
            self._mtime = self.path.stat().st_mtime_ns
        else:
            self._data = self._initial_data()
            self._flush_locked()
        return self._data

    def _flush_locked(self) -> None:
        tmp = self.path.with_name(self.path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self._data, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime_ns

    @contextlib.contextmanager
    def transaction(self) -> Iterator[dict[str, Any]]:
        """持有进程锁 + 文件排他锁的读写事务。"""
        with _GLOBAL_LOCK:
            self._lock_fh = open(self.lock_path, "a+")
            fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                data = self._load_locked()
                yield data
                self._flush_locked()
            except BaseException:
                # 事务体抛错后丢弃内存缓存，避免半截修改残留并在之后被落盘。
                self._data = None
                raise
            finally:
                fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_UN)
                self._lock_fh.close()
                self._lock_fh = None

    @contextlib.contextmanager
    def read(self) -> Iterator[dict[str, Any]]:
        with _GLOBAL_LOCK:
            fh = open(self.lock_path, "a+")
            fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
            try:
                yield self._load_locked()
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                fh.close()

    # ---- 工具方法 ----------------------------------------------------------

    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    def tick(self, data: dict[str, Any]) -> tuple[str, int]:
        """返回 (ISO 系统时间, 单调序号)，序号在全库范围内严格递增。"""
        counters = data["counters"]
        counters["seq"] = counters.get("seq", 0) + 1
        # 毫秒精度 + 序号后缀，保证跨机器排序仍然单调可读。
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{counters['seq']:06d}Z", counters["seq"]

    def audit(self, data: dict[str, Any], actor: str, action: str, detail: dict[str, Any]) -> None:
        ts, seq = self.tick(data)
        entry = {"seq": seq, "at": ts, "actor": actor, "action": action, "detail": detail}
        data["audit"].append(entry)
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
