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

        if not {"runs", "run_attempts"}.issubset({row[0] for row in schema}):
            self.db.executescript("""
            BEGIN;
            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY, state TEXT NOT NULL, request_json TEXT NOT NULL,
              result_json TEXT, created REAL NOT NULL, updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_attempts (
              attempt_id TEXT PRIMARY KEY REFERENCES attempts(id) ON DELETE CASCADE,
              run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS run_attempts_run ON run_attempts(run_id);
            COMMIT;
            """)

    def begin_run(self, run_id: str, request: dict) -> None:
        now = time.time()
        with self.db:
            self.db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?)",
                            (run_id, "pending", json.dumps(request), None, now, now))

    @staticmethod
    def _run_record(row: sqlite3.Row) -> dict:
        record = dict(row)
        record["request"] = json.loads(record.pop("request_json"))
        result = record.pop("result_json")
        record["result"] = json.loads(result) if result is not None else None
        return record

    def get_run(self, run_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._run_record(row) if row is not None else None

    def active_runs(self) -> list[dict]:
        return [self._run_record(row) for row in self.db.execute(
            "SELECT * FROM runs WHERE state IN ('pending','running') ORDER BY created, rowid"
        )]

    def terminal_runs(self) -> list[dict]:
        return [self._run_record(row) for row in self.db.execute(
            "SELECT * FROM runs WHERE state NOT IN ('pending','running') ORDER BY updated, rowid"
        )]

    def set_run(self, run_id: str, state: str, result: dict | None = None) -> None:
        with self.db:
            cursor = self.db.execute("UPDATE runs SET state=?,result_json=?,updated=? WHERE id=?",
                                     (state, json.dumps(result) if result is not None else None, time.time(), run_id))
            if not cursor.rowcount:
                raise KeyError(run_id)

    def run_attempts(self, run_id: str) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT attempts.* FROM attempts JOIN run_attempts ON attempts.id=run_attempts.attempt_id "
            "WHERE run_attempts.run_id=? ORDER BY attempts.created, attempts.rowid", (run_id,)
        )]

    def attempt_run_id(self, attempt_id: str) -> str | None:
        row = self.db.execute("SELECT run_id FROM run_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        return row[0] if row is not None else None

    def compact_runs(self, keep: int = 100) -> list[str]:
        """Drop old terminal records whose attempts no longer require reconciliation."""
        if keep < 0:
            raise ValueError("keep must be nonnegative")
        with self.db:
            stale = [row[0] for row in self.db.execute("""
                SELECT runs.id FROM runs WHERE state NOT IN ('pending','running')
                AND NOT EXISTS (
                  SELECT 1 FROM run_attempts JOIN attempts ON attempts.id=run_attempts.attempt_id
                  WHERE run_attempts.run_id=runs.id
                    AND attempts.state IN ('prepared','submit_intent','submitted','running','cleanup_unknown')
                )
                ORDER BY runs.updated DESC, runs.rowid DESC LIMIT -1 OFFSET ?
            """, (keep,))]
            for run_id in stale:
                attempts = self.run_attempts(run_id)
                self.db.executemany("DELETE FROM settings WHERE key=?",
                                    ((prefix + row["id"],) for row in attempts for prefix in ("attempt:", "reservation:", "cancel_requested:")))
                self.db.execute("DELETE FROM attempts WHERE id IN (SELECT attempt_id FROM run_attempts WHERE run_id=?)", (run_id,))
                self.db.execute("DELETE FROM runs WHERE id=?", (run_id,))
        return stale

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

    def begin_attempt(self, benchmark: str, revision: int, packet_index: int, fingerprint: str, *, attempt_id: str | None = None, state: str = "submit_intent", run_id: str | None = None) -> str:
        import uuid
        attempt_id = attempt_id or str(uuid.uuid4())
        now = time.time()
        with self.db:
            self.db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)", (attempt_id, benchmark, revision, packet_index, fingerprint, None, state, now, now, None, None))
            if run_id is not None:
                self.db.execute("INSERT INTO run_attempts VALUES(?,?)", (attempt_id, run_id))
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
        with self.db:
            stale = [row[0] for row in self.db.execute("""
                SELECT attempts.id FROM attempts
                WHERE benchmark=? AND packet_index=?
                  AND state NOT IN ('prepared','submit_intent','submitted','running','cleanup_unknown')
                  AND NOT EXISTS (SELECT 1 FROM run_attempts WHERE attempt_id=attempts.id)
                ORDER BY created DESC, attempts.rowid DESC LIMIT -1 OFFSET ?
            """, (benchmark, packet_index, keep))]
            if not stale:
                return
            self.db.executemany("DELETE FROM attempts WHERE id=?", [(attempt_id,) for attempt_id in stale])
            self.db.executemany("DELETE FROM settings WHERE key=?", [(f"attempt:{attempt_id}",) for attempt_id in stale])
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.execute("VACUUM")
