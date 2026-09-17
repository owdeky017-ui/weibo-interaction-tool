"""断点续传存储（SQLite）。

早期版本把整份断点放在一个 JSON 文件里，每次保存都要
「读全量 → 按 _key 在内存里合并 → 写全量」，复杂度 O(记录数)；
实测单个断点文件已到 665 KB，再涨下去每次保存都会明显变慢。

改成 SQLite 后：
  · 去重交给主键（INSERT OR IGNORE），不再需要在内存里合并；
  · 保存只写「还没落库」的新增部分，复杂度 O(新增)；
  · 不再需要 os.replace 做原子替换，断电也不会把整份断点写坏；
  · 首次使用会自动把同名的旧 .json 断点导入进来，历史记录不丢。

对外只暴露 load() / save()，调用方（analyzer）不感知底层存储。
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import uuid
from collections.abc import Iterable
from typing import Any

from models import CheckpointData

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
CREATE TABLE IF NOT EXISTS records (
    k    TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scanned_mids (
    mid TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS failed_weibos (
    k    TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
"""

_MIGRATED_FLAG = "migrated_from_json"


def _record_key(rec: dict[str, Any]) -> str:
    """记录的稳定主键。_key 为 None 时给一个随机键（等价于「不去重」，与早期版本一致）。"""
    k = rec.get("_key")
    if k:
        return json.dumps(k, ensure_ascii=False)
    return "@nokey:" + uuid.uuid4().hex


class CheckpointStore:
    """一个「用户对」对应一个断点库。"""

    def __init__(self, path: str) -> None:
        # 兼容旧调用：传 .json 时自动改用同名 .db，并把旧文件作为迁移来源
        if path.lower().endswith(".json"):
            self.legacy_json: str = path
            self.path: str = path[:-5] + ".db"
        else:
            self.path = path
            self.legacy_json = (path[:-3] + ".json") if path.lower().endswith(".db") else path + ".json"
        self._schema_ready: bool = False
        self._migrated: bool = False
        self.migrated_count: int = 0
        # 已落库的数量/集合，用于「只写新增」
        self._saved_records: int = 0
        self._saved_mids: set[str] = set()
        self._saved_fail_keys: set[str] = set()

    # ---------- 底层 ----------

    def _connect(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        con = sqlite3.connect(self.path, timeout=15)
        if not self._schema_ready:
            con.executescript(_SCHEMA)
            con.commit()
            self._schema_ready = True
        return con

    @staticmethod
    def _write_meta(
        con: sqlite3.Connection,
        last_run_ts: float | None,
        stats: dict[str, Any] | None,
        like_api_ok: bool | None,
    ) -> None:
        rows = [
            ("last_run_ts", repr(float(last_run_ts or 0.0))),
            ("stats", json.dumps(stats or {}, ensure_ascii=False)),
            ("like_api_ok", "1" if like_api_ok else "0"),
        ]
        con.executemany("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", rows)

    # ---------- 旧 JSON 迁移 ----------

    def _migrate_legacy_json(self, con: sqlite3.Connection) -> None:
        """把同名旧 .json 断点导入一次。迁移标记写在 meta 里，之后不再重复导入。"""
        if self._migrated:
            return
        self._migrated = True
        if not self.legacy_json or not os.path.exists(self.legacy_json):
            return
        row = con.execute("SELECT v FROM meta WHERE k = ?", (_MIGRATED_FLAG,)).fetchone()
        if row:
            return
        try:
            with open(self.legacy_json, encoding="utf-8") as f:
                cp = json.load(f)
        except Exception:
            return
        try:
            records = cp.get("records") or []
            mids = cp.get("scanned_mids") or []
            failed = cp.get("failed_weibos") or []
            con.executemany(
                "INSERT OR IGNORE INTO records(k, data) VALUES(?, ?)",
                [(_record_key(r), json.dumps(r, ensure_ascii=False)) for r in records],
            )
            con.executemany(
                "INSERT OR IGNORE INTO scanned_mids(mid) VALUES(?)",
                [(str(m),) for m in mids],
            )
            con.executemany(
                "INSERT OR IGNORE INTO failed_weibos(k, data) VALUES(?, ?)",
                [(str(f.get("mid") or uuid.uuid4().hex), json.dumps(f, ensure_ascii=False)) for f in failed],
            )
            self._write_meta(con, cp.get("last_run_ts"), cp.get("stats"), cp.get("like_api_ok"))
            con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, '1')", (_MIGRATED_FLAG,))
            con.commit()
            self.migrated_count = len(records)
        except Exception:
            # 迁移失败不影响新断点继续用
            self.migrated_count = 0

    # ---------- 对外 ----------

    def load(self) -> CheckpointData:
        """读出全部断点内容；文件不存在或损坏时返回空结构（不抛异常）。"""
        out: CheckpointData = {
            "records": [],
            "scanned_mids": [],
            "last_run_ts": 0.0,
            "stats": {},
            "like_api_ok": None,
            "failed_weibos": [],
            "migrated_count": 0,
        }
        try:
            con = self._connect()
        except Exception:
            return out
        try:
            self._migrate_legacy_json(con)
            for (data,) in con.execute("SELECT data FROM records"):
                try:
                    out["records"].append(json.loads(data))
                except Exception:
                    continue
            out["scanned_mids"] = [r[0] for r in con.execute("SELECT mid FROM scanned_mids")]
            for k, v in con.execute("SELECT k, v FROM meta"):
                if k == "last_run_ts":
                    out["last_run_ts"] = float(v or 0.0)
                elif k == "stats":
                    out["stats"] = json.loads(v or "{}")
                elif k == "like_api_ok":
                    out["like_api_ok"] = v == "1"
            for (data,) in con.execute("SELECT data FROM failed_weibos"):
                try:
                    out["failed_weibos"].append(json.loads(data))
                except Exception:
                    continue
        except Exception:
            return out
        finally:
            with contextlib.suppress(Exception):
                con.close()

        # 记住已落库的部分，save() 只写增量
        self._saved_records = len(out["records"])
        self._saved_mids = set(out["scanned_mids"])
        self._saved_fail_keys = {str(f.get("mid") or "") for f in out["failed_weibos"]}
        out["migrated_count"] = self.migrated_count
        return out

    def save(
        self,
        records: list[dict[str, Any]],
        scanned_mids: Iterable[str],
        last_run_ts: float,
        stats: dict[str, Any],
        like_api_ok: bool,
        failed_weibos: list[dict[str, Any]],
    ) -> None:
        """增量落库：只写新增记录 / 新增 mid / 新增失败项。失败时抛异常，由调用方决定是否提示。"""
        con = self._connect()
        try:
            # 记录：records 是只追加的，从上次落库位置往后写
            new_records = records[self._saved_records :]
            if new_records:
                con.executemany(
                    "INSERT OR IGNORE INTO records(k, data) VALUES(?, ?)",
                    [(_record_key(r), json.dumps(r, ensure_ascii=False)) for r in new_records],
                )
                self._saved_records = len(records)

            # 已扫 mid：只写新增（集合差集，不产生 I/O）
            new_mids = list(set(scanned_mids) - self._saved_mids)
            if new_mids:
                con.executemany(
                    "INSERT OR IGNORE INTO scanned_mids(mid) VALUES(?)",
                    [(str(m),) for m in new_mids],
                )
                self._saved_mids.update(new_mids)

            # 失败微博：按 mid 去重，保留最近 500 条（与早期版本一致）
            new_fail = [
                f for f in failed_weibos[-500:] if str(f.get("mid") or "") not in self._saved_fail_keys
            ]
            if new_fail:
                con.executemany(
                    "INSERT OR IGNORE INTO failed_weibos(k, data) VALUES(?, ?)",
                    [
                        (str(f.get("mid") or uuid.uuid4().hex), json.dumps(f, ensure_ascii=False))
                        for f in new_fail
                    ],
                )
                self._saved_fail_keys.update(str(f.get("mid") or "") for f in new_fail)

            self._write_meta(con, last_run_ts, stats, like_api_ok)
            con.commit()
        finally:
            with contextlib.suppress(Exception):
                con.close()
