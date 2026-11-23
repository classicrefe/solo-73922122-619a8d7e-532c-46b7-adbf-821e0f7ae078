"""SQLite 持久化：现行日表、版号、封存区间。

所有写操作在单个事务内完成，失败时整体回滚，调用方继续供应原表。
连接以 WAL 模式打开；进程内用一把锁串行化写事务（SQLite 单写者）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


class StorageError(RuntimeError):
    """写入失败（已回滚，原表保持可用）。"""


SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS versions (
    version      INTEGER PRIMARY KEY,
    year         INTEGER NOT NULL,
    month        INTEGER NOT NULL,
    sim_rain     TEXT    NOT NULL,
    sim_total    INTEGER NOT NULL,
    created_at   TEXT    NOT NULL,
    note         TEXT
);
CREATE TABLE IF NOT EXISTS day_rows (
    version      INTEGER NOT NULL REFERENCES versions(version),
    day          INTEGER NOT NULL,
    amount       INTEGER NOT NULL CHECK (amount >= 0),
    source       TEXT    NOT NULL CHECK (source IN ('approved', 'estimated')),
    exact_num    INTEGER NOT NULL,
    exact_den    INTEGER NOT NULL,
    PRIMARY KEY (version, day)
);
CREATE TABLE IF NOT EXISTS seals (
    seal_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    version      INTEGER NOT NULL,
    through_day  INTEGER NOT NULL,
    sealed_at    TEXT    NOT NULL,
    UNIQUE (version, through_day)
);
CREATE TABLE IF NOT EXISTS current_version (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    version      INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class DayRow:
    day: int
    amount: int
    source: str
    exact_num: int
    exact_den: int


@dataclass(frozen=True)
class VersionMeta:
    version: int
    year: int
    month: int
    sim_rain: tuple[int, ...]
    sim_total: int
    created_at: str
    note: Optional[str]


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            db_path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- 读 ----------
    def current_version_no(self) -> Optional[int]:
        row = self._conn.execute(
            "SELECT version FROM current_version WHERE id = 1"
        ).fetchone()
        return None if row is None else int(row["version"])

    def get_version(self, version: int) -> Optional[VersionMeta]:
        row = self._conn.execute(
            "SELECT * FROM versions WHERE version = ?", (version,)
        ).fetchone()
        if row is None:
            return None
        return VersionMeta(
            version=row["version"],
            year=row["year"],
            month=row["month"],
            sim_rain=tuple(json.loads(row["sim_rain"])),
            sim_total=row["sim_total"],
            created_at=row["created_at"],
            note=row["note"],
        )

    def get_current(self) -> Optional[VersionMeta]:
        v = self.current_version_no()
        return None if v is None else self.get_version(v)

    def list_days(self, version: int) -> list[DayRow]:
        rows = self._conn.execute(
            "SELECT * FROM day_rows WHERE version = ? ORDER BY day", (version,)
        ).fetchall()
        return [
            DayRow(r["day"], r["amount"], r["source"],
                   r["exact_num"], r["exact_den"])
            for r in rows
        ]

    def sealed_through(self, version: int) -> int:
        """该版已封存到哪一天；未封存返回 0。"""
        row = self._conn.execute(
            "SELECT MAX(through_day) AS m FROM seals WHERE version = ?",
            (version,),
        ).fetchone()
        return 0 if row is None or row["m"] is None else int(row["m"])

    def list_seals(self, version: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT seal_id, version, through_day, sealed_at FROM seals "
            "WHERE version = ? ORDER BY through_day", (version,)
        ).fetchall()
        return [dict(r) for r in rows]

    def list_versions(self) -> list[VersionMeta]:
        rows = self._conn.execute(
            "SELECT * FROM versions ORDER BY version"
        ).fetchall()
        return [
            VersionMeta(r["version"], r["year"], r["month"],
                        tuple(json.loads(r["sim_rain"])), r["sim_total"],
                        r["created_at"], r["note"])
            for r in rows
        ]

    # ---------- 写 ----------
    def write_new_version(self, *, year: int, month: int,
                          sim_rain: tuple[int, ...], sim_total: int,
                          rows: list[DayRow], note: str) -> int:
        """写入一个全新版本并切换为现行版本（初始转换用）。"""
        now = _now()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                next_v = self._next_version_locked()
                self._insert_version_locked(
                    next_v, year, month, sim_rain, sim_total, now, note, rows
                )
                self._conn.execute(
                    "INSERT INTO current_version(id, version) VALUES (1, ?) "
                    "ON CONFLICT(id) DO UPDATE SET version = excluded.version",
                    (next_v,),
                )
                self._conn.execute("COMMIT")
                return next_v
            except Exception as exc:
                self._rollback()
                raise StorageError(f"写入新版本失败: {exc}") from exc

    def add_version_from(self, base_version: int, *, sim_rain: tuple[int, ...],
                         sim_total: int, rows: list[DayRow], note: str,
                         seal_through: int) -> int:
        """以基版为基础追加新版（补报/更正重算），继承封存区间并切为现行。"""
        base = self.get_version(base_version)
        if base is None:
            raise StorageError(f"基版 {base_version} 不存在")
        now = _now()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                next_v = self._next_version_locked()
                self._insert_version_locked(
                    next_v, base.year, base.month, sim_rain, sim_total,
                    now, note, rows
                )
                # 继承封存记录
                for s in self.list_seals(base_version):
                    self._conn.execute(
                        "INSERT INTO seals(version, through_day, sealed_at) "
                        "VALUES (?, ?, ?)",
                        (next_v, s["through_day"], s["sealed_at"]),
                    )
                self._conn.execute(
                    "UPDATE current_version SET version = ? WHERE id = 1",
                    (next_v,),
                )
                self._conn.execute("COMMIT")
                return next_v
            except Exception as exc:
                self._rollback()
                raise StorageError(f"追加版本失败: {exc}") from exc

    def seal_current(self, through_day: int) -> dict:
        """封存现行版本从月初到指定日的结果。"""
        v = self.current_version_no()
        if v is None:
            raise StorageError("尚无现行版本，无法封存")
        now = _now()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                cur = self.sealed_through(v)
                if through_day <= cur:
                    raise StorageError(
                        f"现行版本已封存至第 {cur} 日，无需重复封存"
                    )
                self._conn.execute(
                    "INSERT INTO seals(version, through_day, sealed_at) "
                    "VALUES (?, ?, ?)", (v, through_day, now)
                )
                self._conn.execute("COMMIT")
                return {"seal_id": self._conn.execute(
                        "SELECT last_insert_rowid() AS i").fetchone()["i"],
                        "version": v, "through_day": through_day,
                        "sealed_at": now}
            except Exception as exc:
                self._rollback()
                raise StorageError(f"封存失败: {exc}") from exc

    # ---------- 内部 ----------
    def _next_version_locked(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM versions"
        ).fetchone()
        return int(row["v"])

    def _insert_version_locked(self, version: int, year: int, month: int,
                               sim_rain: tuple[int, ...], sim_total: int,
                               created_at: str, note: str,
                               rows: list[DayRow]) -> None:
        self._conn.execute(
            "INSERT INTO versions(version, year, month, sim_rain, sim_total, "
            "created_at, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (version, year, month, json.dumps(list(sim_rain)),
             sim_total, created_at, note),
        )
        self._conn.executemany(
            "INSERT INTO day_rows(version, day, amount, source, exact_num, "
            "exact_den) VALUES (?, ?, ?, ?, ?, ?)",
            [(version, r.day, r.amount, r.source,
              r.exact_num, r.exact_den) for r in rows],
        )

    def _rollback(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
