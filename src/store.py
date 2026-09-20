"""JSON 文件持久化层。

所有写操作在进程内锁下串行执行，并通过临时文件 + os.replace 原子落盘，
保证发布等 compare-and-swap 操作在并发下安全。
读写均返回深拷贝，调用方拿到的副本与内部状态互不影响。
"""
from __future__ import annotations

import copy
import json
import os
import threading

COLLECTIONS = (
    "users",
    "projects",
    "companies",
    "metrics",
    "periods",
    "sources",
    "observations",
    "branches",
    "assumptions",
    "formulas",
    "citations",
    "versions",
    "comments",
    "exports",
    "events",
)


class Store:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.data: dict[str, dict[str, dict]] = {name: {} for name in COLLECTIONS}
        self.counters: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        for name in COLLECTIONS:
            self.data[name] = raw.get("collections", {}).get(name, {})
        self.counters = raw.get("counters", {})

    def save(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                {"collections": self.data, "counters": self.counters},
                fh,
                ensure_ascii=False,
                indent=1,
            )
        os.replace(tmp, self.path)

    def next_id(self, prefix: str) -> str:
        n = self.counters.get(prefix, 0) + 1
        self.counters[prefix] = n
        return f"{prefix}_{n}"

    def insert(self, collection: str, record: dict) -> dict:
        with self.lock:
            self.data[collection][record["id"]] = copy.deepcopy(record)
            self.save()
            return copy.deepcopy(record)

    def get(self, collection: str, record_id: str) -> dict | None:
        with self.lock:
            record = self.data[collection].get(record_id)
            return copy.deepcopy(record) if record is not None else None

    def all(self, collection: str) -> list[dict]:
        with self.lock:
            return copy.deepcopy(list(self.data[collection].values()))

    def update(self, collection: str, record: dict) -> dict:
        with self.lock:
            self.data[collection][record["id"]] = copy.deepcopy(record)
            self.save()
            return copy.deepcopy(record)
