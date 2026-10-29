"""Public Python API; CLI entry points delegate to these same operations."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .catalog import Catalog
from .instructions import update_agent_instructions, validate_agent_instructions
from .registry import load_registry, parse_registration, write_registration
from .settings import host_limits
from .store import Store


def init_project(project: str | Path = ".", *, scheduler_url: str | None = None,
                 token_file: str | Path | None = None, retained_cap_bytes: int = 1024**3,
                 agent_instructions: str = "write", activate: bool = True) -> dict[str, Any]:
    root = Path(project).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"project directory does not exist: {root}")
    if agent_instructions not in {"write", "skip"}:
        raise ValueError("agent_instructions must be 'write' or 'skip'")
    limits = host_limits()
    if not 1 <= retained_cap_bytes <= limits["retained_cap_bytes"]:
        raise ValueError(f"retained cap must be between 1 byte and the host limit {limits['retained_cap_bytes']}")
    if agent_instructions == "write":
        validate_agent_instructions(root / "AGENTS.md")
    autobench = root / ".autobench"
    state_path = autobench / "state" / "autobench.sqlite3"
    config_path = autobench / "config.json"
    if config_path.exists() and not state_path.is_file():
        raise RuntimeError(f"initialized Autobench state is missing at {state_path}; no state was replaced")
    if (autobench / "enrolled.json").exists() and not state_path.is_file():
        raise RuntimeError(f"enrolled Autobench workspace has no state database at {state_path}; inspect and recover it before init")
    state_dir = autobench / "state"
    if state_dir.exists() and not state_path.is_file() and any(state_dir.iterdir()):
        raise RuntimeError(f"Autobench generated state exists without its database at {state_path}; inspect and recover it before init")
    autobench.mkdir(exist_ok=True)
    autobench_gitignore = autobench / ".gitignore"
    if not autobench_gitignore.exists():
        _atomic_text(autobench_gitignore, "*\n!.gitignore\n!registry/\n!registry/**\n")
    for name in ("registry", "state", "builds", "evidence", "scratch", "snapshots"):
        (autobench / name).mkdir(exist_ok=True)
    config_path = autobench / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {"schema_version": 1}
    if scheduler_url is not None:
        config["scheduler_url"] = scheduler_url
    if token_file is not None:
        config["token_file"] = str(Path(token_file).expanduser())
    config.update({"active": bool(activate), "retained_cap_bytes": retained_cap_bytes})
    _atomic_json(config_path, config)
    store = Store(state_path, create=not state_path.exists())
    store.set("retained_cap_bytes", retained_cap_bytes)
    store.close()
    if agent_instructions == "write":
        update_agent_instructions(root / "AGENTS.md")
    catalog = Catalog()
    catalog.enroll(root, active=activate)
    catalog.close()
    if activate:
        _atomic_json(autobench / "enrolled.json", {"active": True, "project": str(root), "activated_at": time.time()})
    token = Path(config["token_file"]).expanduser() if config.get("token_file") else None
    token_readable = bool(token and token.is_file() and os.access(token, os.R_OK))
    return {"project": str(root), "active": bool(activate), "retained_cap_bytes": retained_cap_bytes,
            "scheduler_configured": bool(config.get("scheduler_url") and token_readable),
            "scheduler_token_file_readable": token_readable}


def register_benchmark(project: str | Path, name: str, *, include: list[str], argv: list[str],
                       expected_seconds: int = 5, kill_seconds: int = 30, kind: str = "correctness") -> Path:
    root = Path(project).expanduser().resolve()
    result = write_registration(root, name, include, argv, expected_seconds, kill_seconds, kind)
    load_registry(root)
    return result


def register_definition(project: str | Path, definition: str | Path, *, replace_revision: bool = False) -> Path:
    """Validate and atomically publish a complete TOML benchmark definition."""
    root = Path(project).expanduser().resolve()
    if not (root / ".autobench").is_dir():
        raise RuntimeError("run `autobench init` before registering benchmarks")
    source = Path(definition).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    benchmark = parse_registration(source, root)
    directory = root / ".autobench" / "registry"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{benchmark.name}.toml"
    for registered_path in sorted(directory.glob("*.toml")):
        if registered_path == target:
            continue
        registered = parse_registration(registered_path, root)
        if registered.name == benchmark.name:
            raise RuntimeError(f"a separate registration already owns benchmark name {benchmark.name!r}")
    if target.exists():
        if not replace_revision:
            raise RuntimeError(f"registration already exists: {target}; use explicit revision replacement")
        previous = parse_registration(target, root)
        if benchmark.revision <= previous.revision:
            raise RuntimeError(f"replacement revision must exceed existing revision {previous.revision}")
    elif replace_revision:
        raise RuntimeError("revision replacement was requested but no registration exists")
    payload = source.read_bytes()
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(payload)
        parse_registration(temporary, root)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _atomic_text(path: Path, value: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)

