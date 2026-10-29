from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    expected_seconds: int
    kill_seconds: int
    resources: dict[str, Any]
    kind: str = "correctness"
    name: str = ""
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class Benchmark:
    name: str
    revision: int
    root: Path
    include: tuple[str, ...]
    data: tuple[str, ...]
    build: Command | None
    packets: tuple[Command, ...]
    enabled: bool = True
    recipe_revision: str = "1"
    protocol: dict[str, Any] | None = None


@dataclass(frozen=True)
class Snapshot:
    fingerprint: str
    manifest: dict[str, str]
    directory: Path

