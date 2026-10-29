from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


class SourceTrace:
    """Optional direct project-source trace adapter; results are diagnostic only."""

    def __init__(self, project: Path, state: Path, *, max_bytes: int = 64 * 1024 * 1024,
                 max_files: int = 10_000):
        self.project = project.resolve()
        self.state = state
        self.max_bytes = max_bytes
        self.max_files = max_files
        self.index = None
        self._source_index = None
        try:
            from project_source import Repository, SourceIndex
        except ImportError:
            self._source_index = None
        else:
            self._source_index = (Repository, SourceIndex)

    def incoming_summary(self, snapshot: Path, paths: list[str], *, input_budget_bytes: int | None = None) -> dict[str, Any] | None:
        if self._source_index is None:
            return None
        scoped = sorted({path for path in paths if ".autobench" not in Path(path).parts})
        if not scoped:
            return {"edge_count": 0, "complete": False, "coverage": "declared input scope",
                    "omissions": ["diagnostic index skipped: no declared source files"]}
        budget = min(self.max_bytes, max(0, input_budget_bytes)) if input_budget_bytes is not None else self.max_bytes
        if budget < 1:
            return {"edge_count": 0, "complete": False, "coverage": "declared input scope",
                    "omissions": ["diagnostic index skipped: no retained-data headroom"]}
        total_bytes = 0
        try:
            for relative in scoped:
                candidate = (snapshot / relative).resolve()
                candidate.relative_to(snapshot.resolve())
                if not candidate.is_file() or candidate.is_symlink():
                    raise OSError(f"captured source is not a regular file: {relative}")
                total_bytes += candidate.stat().st_size
                if len(scoped) > self.max_files or total_bytes > budget:
                    return {"edge_count": 0, "complete": False, "coverage": "declared input scope",
                            "omissions": ["diagnostic index skipped: declared source exceeds configured indexing budget"]}
        except (OSError, ValueError) as exc:
            return {"edge_count": 0, "complete": False, "coverage": "declared input scope",
                    "omissions": [f"diagnostic index skipped: {exc}"]}
        Repository, SourceIndex = self._source_index
        repository_id = hashlib.sha256(str(self.project).encode()).hexdigest()[:24]
        worktree_id = "autobench-" + hashlib.sha256(str(self.project).encode()).hexdigest()[:16]
        # The index root is the immutable declared-source snapshot, never the full
        # workspace. Keep the real workspace identity separate from that root.
        repository = Repository(snapshot, repository_id, self.state / "project-source", worktree_id=worktree_id,
                                exclude=(".git", ".ctxpp", ".autobench", "__pycache__", ".venv", "venv", "node_modules"))
        self.index = SourceIndex(repository)
        result = self.index.trace(scoped, direction="in", max_hops=3, limit=100)
        # Never interpret empty or incomplete traversal as proof that work can be skipped.
        return {"edge_count": len(result.get("edges", [])), "complete": bool(result.get("complete")),
                "coverage": "declared input scope", "omissions": result.get("omissions", [])[:8]}

