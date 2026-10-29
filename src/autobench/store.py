from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class Store:
    """Small project-local ledger; SQLite is authoritative for execution state."""

    def __init__(self, path: Path, *, create: bool = True):
        if not create and not path.is_file():
            raise FileNotFoundError(f"Autobench state database is missing: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        if create:
            self.db = sqlite3.connect(path, timeout=15)
        else:
            self.db = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=15)
        self.db.row_factory = sqlite3.Row
        if create:
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        schema = self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if create:
            self.db.executescript("""
        CREATE TABLE IF NOT EXISTS benchmarks (
          name TEXT PRIMARY KEY, revision INTEGER NOT NULL, fingerprint TEXT,
          updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attempts (
          id TEXT PRIMARY KEY, benchmark TEXT NOT NULL, revision INTEGER NOT NULL,
          packet_index INTEGER NOT NULL, fingerprint TEXT NOT NULL, job_id TEXT,
          state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
          result_json TEXT, error TEXT
        );
        CREATE INDEX IF NOT EXISTS attempts_benchmark ON attempts(benchmark, created DESC);
        CREATE TABLE IF NOT EXISTS coverage (
          benchmark TEXT NOT NULL, packet_index INTEGER NOT NULL, fingerprint TEXT NOT NULL,
          current_attempt TEXT NOT NULL, updated REAL NOT NULL,
          PRIMARY KEY(benchmark, packet_index)
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        else:
            tables = {row[0] for row in schema}
            required = {"benchmarks", "attempts", "coverage", "settings"}
            if not required.issubset(tables):
                self.db.close()
                raise sqlite3.DatabaseError(f"Autobench database schema is incomplete: {path}")
            check = self.db.execute("PRAGMA quick_check").fetchone()
            if not check or check[0] != "ok":
                self.db.close()
                raise sqlite3.DatabaseError(f"Autobench database integrity check failed: {path}")

    def close(self) -> None:
        self.db.close()

    def set(self, key: str, value: Any) -> None:
        self.db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
        self.db.commit()

    def delete_setting(self, key: str) -> None:
        self.db.execute("DELETE FROM settings WHERE key=?", (key,))
        self.db.commit()

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def status(self) -> dict[str, Any]:
        return {
            "paused": bool(self.get("paused", False)),
            "attempts": [dict(row) for row in self.db.execute(
                "SELECT id,benchmark,packet_index,fingerprint,job_id,state,created,updated,error FROM attempts ORDER BY created DESC LIMIT 100"
            )],
            "coverage": [dict(row) for row in self.db.execute("SELECT * FROM coverage ORDER BY benchmark,packet_index")],
        }

    def begin_attempt(self, benchmark: str, revision: int, packet_index: int, fingerprint: str, *, attempt_id: str | None = None, state: str = "submit_intent") -> str:
        import uuid
        attempt_id = attempt_id or str(uuid.uuid4())
        now = time.time()
        self.db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)", (attempt_id, benchmark, revision, packet_index, fingerprint, None, state, now, now, None, None))
        self.db.commit()
        return attempt_id

    def set_job(self, attempt_id: str, job_id: str, state: str = "submitted") -> None:
        self.db.execute("UPDATE attempts SET job_id=?,state=?,updated=? WHERE id=?", (job_id, state, time.time(), attempt_id))
        self.db.commit()

    def set_attempt(self, attempt_id: str, state: str, result: Any = None, error: str | None = None) -> None:
        self.db.execute("UPDATE attempts SET state=?,result_json=?,error=?,updated=? WHERE id=?", (state, json.dumps(result) if result is not None else None, error, time.time(), attempt_id))
        self.db.commit()

    def unresolved(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM attempts WHERE state IN ('submit_intent','submitted','running','cleanup_unknown') ORDER BY created"))

    def current(self, benchmark: str, packet_index: int) -> str | None:
        row = self.db.execute("SELECT fingerprint FROM coverage WHERE benchmark=? AND packet_index=?", (benchmark, packet_index)).fetchone()
        return str(row[0]) if row else None

    def mark_current(self, benchmark: str, packet_index: int, fingerprint: str, attempt_id: str) -> None:
        self.db.execute("INSERT INTO coverage VALUES(?,?,?,?,?) ON CONFLICT(benchmark,packet_index) DO UPDATE SET fingerprint=excluded.fingerprint,current_attempt=excluded.current_attempt,updated=excluded.updated", (benchmark, packet_index, fingerprint, attempt_id, time.time()))
        self.db.commit()

    def attempts_for(self, benchmark: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM attempts WHERE benchmark=? ORDER BY created DESC LIMIT 100", (benchmark,))]

    def compact_history(self, benchmark: str, packet_index: int, keep: int = 16) -> None:
        count = self.db.execute("SELECT COUNT(*) FROM attempts WHERE benchmark=? AND packet_index=? AND state NOT IN ('prepared','submit_intent','submitted','running','cleanup_unknown')", (benchmark, packet_index)).fetchone()[0]
        if count <= keep:
            return
        stale = [row[0] for row in self.db.execute("SELECT id FROM attempts WHERE benchmark=? AND packet_index=? AND state NOT IN ('prepared','submit_intent','submitted','running','cleanup_unknown') AND id NOT IN (SELECT id FROM attempts WHERE benchmark=? AND packet_index=? ORDER BY created DESC LIMIT ?)", (benchmark, packet_index, benchmark, packet_index, keep))]
        self.db.execute("DELETE FROM attempts WHERE benchmark=? AND packet_index=? AND state NOT IN ('prepared','submit_intent','submitted','running','cleanup_unknown') AND id NOT IN (SELECT id FROM attempts WHERE benchmark=? AND packet_index=? ORDER BY created DESC LIMIT ?)", (benchmark, packet_index, benchmark, packet_index, keep))
        self.db.executemany("DELETE FROM settings WHERE key=?", [(f"attempt:{attempt_id}",) for attempt_id in stale])
        self.db.commit()
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.execute("VACUUM")

