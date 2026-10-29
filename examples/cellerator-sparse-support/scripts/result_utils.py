"""Small helpers for truthful, structured Autobench example outputs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_metrics(directory: Path, schema: str, values: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "metrics.json"
    target.write_text(json.dumps({"schema": schema, **values}, sort_keys=True,
                                 indent=2, default=str) + "\n", encoding="utf-8")
    return target
