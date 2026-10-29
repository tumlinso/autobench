from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .settings import state_home


class Catalog:
    """User-level workspace catalog; no scheduler credentials are stored here."""

    def __init__(self, path: Path | None = None, *, create: bool = True):
        self.path = path or (state_home() / "autobench" / "catalog.sqlite3")
        existed = self.path.is_file()
        if not create and not self.path.is_file():
            raise FileNotFoundError(f"Autobench workspace catalog is missing: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(f"file:{self.path}?mode=rw" if existed or not create else self.path, uri=existed or not create, timeout=15)
        if not existed and create:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("CREATE TABLE IF NOT EXISTS workspaces (path TEXT PRIMARY KEY, active INTEGER NOT NULL, registered REAL NOT NULL)")
        else:
            tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "workspaces" not in tables:
                self.db.close()
                raise sqlite3.DatabaseError(f"Autobench workspace catalog is incomplete: {self.path}")
            check = self.db.execute("PRAGMA quick_check").fetchone()
            if not check or check[0] != "ok":
                self.db.close()
                raise sqlite3.DatabaseError(f"Autobench workspace catalog integrity check failed: {self.path}")

    def enroll(self, path: Path, active: bool = True) -> None:
        self.db.execute("INSERT INTO workspaces VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET active=excluded.active", (str(path.resolve()), int(active), time.time()))
        self.db.commit()

    def set_active(self, path: Path, active: bool) -> None:
        self.db.execute("UPDATE workspaces SET active=? WHERE path=?", (int(active), str(path.resolve())))
        self.db.commit()

    def workspaces(self, active_only: bool = True) -> list[Path]:
        query = "SELECT path FROM workspaces WHERE active=1 ORDER BY path" if active_only else "SELECT path FROM workspaces ORDER BY path"
        return [Path(row[0]) for row in self.db.execute(query)]

    def close(self) -> None:
        self.db.close()

