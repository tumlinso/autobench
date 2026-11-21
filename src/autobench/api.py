"""Public Python API; CLI entry points delegate to these same operations."""
from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .catalog import Catalog
from .errors import RegistrationError, SchedulerUnavailable
from .instructions import update_agent_instructions, validate_agent_instructions
from .registry import load_registry, parse_registration, registration_from_mapping, write_registration
from .settings import host_limits
from .store import Store

if TYPE_CHECKING:
    from .engine import Engine
    from .models import Benchmark
    from .scheduler import Scheduler


class _LocalOnlyScheduler:
    async def call(self, operation: str, arguments: Any = None) -> dict[str, Any]:
        raise RuntimeError(f"Scheduler operation {operation} is unavailable in this local-only workspace query")


def _workspace_root(project: str | Path) -> Path:
    root = Path(project).expanduser().resolve()
    if not (root / ".autobench" / "config.json").is_file():
        raise RuntimeError("workspace is not initialized; run `autobench init` first")
    if not (root / ".autobench" / "state" / "autobench.sqlite3").is_file():
        raise RuntimeError("initialized Autobench state database is missing; no state was recreated")
    return root


def _workspace_engine(project: str | Path, scheduler: Any | None = None):
    from .engine import Engine

    root = _workspace_root(project)
    if scheduler is None:
        scheduler = _LocalOnlyScheduler()
    return Engine(root, scheduler)


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


def workspace_status(project: str | Path = ".") -> dict[str, Any]:
    """Read project-local Autobench status without contacting Device Scheduler."""
    root = _workspace_root(project)
    engine = _workspace_engine(root)
    try:
        result = engine.store.status()
        result["registrations"] = [benchmark.name for benchmark in load_registry(root)]
        result["retry_cooldowns"] = engine.retry_cooldowns()
        return result
    finally:
        engine.close()


def _compact_metrics(value: Any, limit: int = 64, max_depth: int = 6,
                     max_path_chars: int = 160) -> dict[str, float | int]:
    if not isinstance(value, dict):
        return {}
    compact: dict[str, float | int] = {}

    def visit(node: dict[str, Any], path: tuple[str, ...], depth: int) -> None:
        if depth >= max_depth or len(compact) >= limit:
            return
        for key in sorted(node):
            if not isinstance(key, str) or not key or len(key) > 64:
                continue
            full_path = (*path, key)
            dotted = ".".join(full_path)
            if len(dotted) > max_path_chars:
                continue
            metric = node[key]
            if (isinstance(metric, (int, float)) and not isinstance(metric, bool)
                    and math.isfinite(metric)):
                compact[dotted] = metric
                if len(compact) >= limit:
                    return
            elif isinstance(metric, dict):
                visit(metric, full_path, depth + 1)
                if len(compact) >= limit:
                    return

    visit(value, (), 0)
    return compact


def _tail(value: Any, max_bytes: int = 512) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    raw = value.encode("utf-8", errors="replace")[-max_bytes:]
    return raw.decode("utf-8", errors="replace")


def _compact_attempt(root: Path, name: str, row: dict[str, Any], packet_names: dict[int, str], current_fp: str | None) -> dict[str, Any]:
    packet_index = int(row.get("packet_index", -1))
    fingerprint = str(row.get("fingerprint", ""))
    attempt_id = row.get("id")
    job_id = row.get("job_id")
    evidence_dir = root / ".autobench" / "evidence" / name / str(packet_index)
    artifact_dir = evidence_dir / fingerprint if fingerprint else None

    pointer: dict[str, Any] = {}
    evidence_role = None
    for pointer_name in ("current.json", "baseline.json"):
        try:
            pointer_path = evidence_dir / pointer_name
            if pointer_path.stat().st_size > 256 * 1024:
                continue
            candidate = json.loads(pointer_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (isinstance(candidate, dict) and candidate.get("fingerprint") == fingerprint and
                candidate.get("attempt") == attempt_id):
            pointer = candidate
            evidence_role = "selected_current" if pointer_name == "current.json" else "baseline"
            break

    artifact_ref = None
    artifact_matches_attempt = False
    artifact_result_path = artifact_dir / "result.json" if artifact_dir is not None else None
    if pointer and artifact_dir is not None and artifact_dir.is_dir() and job_id:
        try:
            if artifact_result_path is not None and artifact_result_path.stat().st_size <= 64 * 1024:
                artifact_result = json.loads(artifact_result_path.read_text(encoding="utf-8"))
                artifact_matches_attempt = (isinstance(artifact_result, dict) and
                                            artifact_result.get("scheduler_job_id") == job_id)
        except (OSError, json.JSONDecodeError):
            pass
        if artifact_matches_attempt:
            artifact_ref = artifact_dir.relative_to(root).as_posix()

    try:
        result = json.loads(row.get("result_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        result = {}
    result = result if isinstance(result, dict) else {}
    worker = result.get("worker") if isinstance(result.get("worker"), dict) else {}
    identity = result.get("identity") if isinstance(result.get("identity"), dict) else {}
    device = identity.get("device_identity", pointer.get("device_identity", []))
    short_device = []
    if isinstance(device, list):
        for descriptor in device[:8]:
            if not isinstance(descriptor, dict):
                continue
            short_device.append({key: descriptor[key] for key in ("resource_id", "uuid", "host_id", "kind")
                                if isinstance(descriptor.get(key), (str, int, float))})
    metrics: Any = pointer.get("metrics", {}) if artifact_matches_attempt else {}
    metrics_path = artifact_dir / "metrics.json" if artifact_matches_attempt and artifact_dir is not None else None
    if metrics_path is not None:
        try:
            if metrics_path.stat().st_size <= 64 * 1024:
                metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    error = _tail(row.get("error"))
    compact = {
        "id": row.get("id"), "job_id": row.get("job_id"), "packet_index": packet_index,
        "packet_name": "build" if packet_index < 0 else packet_names.get(packet_index, str(packet_index)),
        "state": row.get("state"), "fingerprint": fingerprint,
        "created": row.get("created"), "updated": row.get("updated"),
        "duration_seconds": worker.get("duration_seconds"),
        "metrics": _compact_metrics(metrics),
        "identity": {
            "source_fingerprint": identity.get("source_fingerprint", pointer.get("source_fingerprint")),
            "build_fingerprint": identity.get("build_fingerprint", pointer.get("build_fingerprint")),
            "data_fingerprint": identity.get("data_fingerprint", pointer.get("data_fingerprint")),
            "protocol_fingerprint": identity.get("protocol_fingerprint", pointer.get("protocol_fingerprint")),
            "device_identity": short_device,
        },
        "evidence_role": evidence_role,
    }
    if artifact_ref is not None:
        compact["artifact_ref"] = artifact_ref
    if error is not None:
        compact["error_tail"] = error
    return compact


def workspace_results(project: str | Path = ".", name: str = "all", *,
                      detailed: bool = False, limit: int | None = None) -> dict[str, list[dict[str, Any]]]:
    """Return compact attempt evidence; detailed=True opts into raw ledger rows.

    Compact queries default to 16 recent attempts. Detailed queries default to
    the legacy 100 row bound. An explicit limit may be from 1 through 100.
    """
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100):
        raise ValueError("results limit must be an integer from 1 to 100")
    effective_limit = limit if limit is not None else (100 if detailed else 16)
    root = _workspace_root(project)
    registrations = load_registry(root)
    names = [benchmark.name for benchmark in registrations] if name == "all" else [name]
    packet_names = {benchmark.name: {index: packet.name for index, packet in enumerate(benchmark.packets)}
                    for benchmark in registrations}
    store = Store(root / ".autobench/state/autobench.sqlite3", create=False)
    try:
        output = {}
        for benchmark in names:
            rows = [dict(row) for row in store.db.execute(
                "SELECT * FROM attempts WHERE benchmark=? ORDER BY created DESC LIMIT ?",
                (benchmark, effective_limit),
            )]
            if detailed:
                output[benchmark] = rows
                continue
            compact = []
            for row in rows:
                compact.append(_compact_attempt(root, benchmark, row,
                                                packet_names.get(benchmark, {}),
                                                store.current(benchmark, int(row.get("packet_index", -1)))))
            output[benchmark] = compact
        return output
    finally:
        store.close()


def workspace_explain(project: str | Path, name: str, *, scheduler: Any | None = None) -> dict[str, Any]:
    """Explain a registration's current freshness and retry state."""
    engine = _workspace_engine(project, scheduler)
    try:
        return engine.explain(name)
    finally:
        engine.close()


def workspace_compare(project: str | Path, name: str) -> dict[str, Any]:
    """Compare current and baseline measurements only when identities agree."""
    root = _workspace_root(project)
    output = root / ".autobench" / "evidence" / name
    comparisons = []
    for packet in sorted((path for path in output.iterdir() if path.is_dir()), key=lambda path: path.name) if output.exists() else []:
        current_file = packet / "current.json"
        baseline_file = packet / "baseline.json"
        if not current_file.exists() or not baseline_file.exists():
            continue
        current = json.loads(current_file.read_text(encoding="utf-8"))
        baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
        current_metrics = current.get("metrics", {})
        baseline_metrics = baseline.get("metrics", {})
        differences = []
        def has_complete_protocol(record: dict[str, Any]) -> bool:
            protocol = record.get("protocol")
            return (isinstance(record.get("protocol_fingerprint"), str) and
                    isinstance(protocol, dict) and protocol.get("schema_version") == 1 and
                    isinstance(protocol.get("argv"), list) and
                    isinstance(protocol.get("recipe_fingerprint"), str))
        if not has_complete_protocol(current) or not has_complete_protocol(baseline):
            differences.append("measurement protocol identity is incomplete")
        elif current.get("protocol_fingerprint") != baseline.get("protocol_fingerprint"):
            differences.append("measurement protocol differs")
        if current.get("data_fingerprint") != baseline.get("data_fingerprint"):
            differences.append("registered data identity differs")
        current_device = current.get("device_identity")
        baseline_device = baseline.get("device_identity")
        if not current_device or not baseline_device:
            differences.append("device identity is incomplete")
        elif current_device != baseline_device:
            differences.append("device identity differs")
        deltas = {}
        if not differences:
            for key, value in current_metrics.items():
                prior = baseline_metrics.get(key)
                if isinstance(value, (int, float)) and isinstance(prior, (int, float)):
                    deltas[key] = {"baseline": prior, "current": value, "delta": value - prior,
                                   "relative": (value - prior) / prior if prior else None}
        comparisons.append({"packet": packet.name, "comparable": not differences, "reasons": differences,
                            "identity": {"current_protocol": current.get("protocol"), "baseline_protocol": baseline.get("protocol"),
                                         "current_build": current.get("build_fingerprint"), "baseline_build": baseline.get("build_fingerprint"),
                                         "current_source": current.get("source_fingerprint"), "baseline_source": baseline.get("source_fingerprint"),
                                         "current_data": current.get("data_fingerprint"), "baseline_data": baseline.get("data_fingerprint"),
                                         "current_device": current_device, "baseline_device": baseline_device},
                            "metrics": deltas})
    return {"benchmark": name, "comparisons": comparisons}


def pause_project(project: str | Path = ".") -> dict[str, Any]:
    """Pause workspace scheduling and deactivate catalog enrollment; no host service calls."""
    root = _workspace_root(project)
    store = Store(root / ".autobench/state/autobench.sqlite3", create=False)
    try:
        store.set("paused", True)
    finally:
        store.close()
    _set_project_active(root, False)
    return {"paused": True}


def resume_project(project: str | Path = ".") -> dict[str, Any]:
    """Resume workspace scheduling and activate catalog enrollment; no host service calls."""
    root = _workspace_root(project)
    store = Store(root / ".autobench/state/autobench.sqlite3", create=False)
    try:
        store.set("paused", False)
    finally:
        store.close()
    _set_project_active(root, True)
    return {"paused": False, "active": True}


def _set_project_active(root: Path, active: bool) -> None:
    config_path = root / ".autobench" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["active"] = active
    _atomic_json(config_path, config)
    enrollment = root / ".autobench" / "enrolled.json"
    if active:
        _atomic_json(enrollment, {"active": True, "project": str(root), "activated_at": time.time()})
    else:
        enrollment.unlink(missing_ok=True)
    catalog = Catalog(create=False)
    try:
        catalog.enroll(root, active=active)
    finally:
        catalog.close()


async def reconcile_project(project: str | Path = ".", *, scheduler: Any | None = None) -> dict[str, Any]:
    """Run one coordinator reconciliation pass, optionally using an injected Scheduler client."""
    root = _workspace_root(project)
    if scheduler is None:
        config = json.loads((root / ".autobench" / "config.json").read_text(encoding="utf-8"))
        if not config.get("scheduler_url") or not config.get("token_file"):
            raise RuntimeError("Device Scheduler is not configured for this workspace")
        from .scheduler import McpScheduler
        scheduler = McpScheduler(config["scheduler_url"], config["token_file"])
    engine = _workspace_engine(root, scheduler)
    try:
        return await engine.reconcile_once()
    finally:
        engine.close()


def _run_blocker(phase: str, reason_code: str, reason: str, next_action: str,
                 *, run_id: str | None = None) -> dict[str, Any]:
    result = {"state": "blocked", "phase": phase, "reason_code": reason_code,
              "reason": reason, "next_action": next_action}
    if run_id is not None:
        result["run_id"] = run_id
    return result


def _open_run_engine(root: Path, scheduler: Scheduler | None,
                     *, run_id: str | None = None) -> Engine | dict[str, Any]:
    if not root.is_dir():
        return _run_blocker("setup", "workspace_not_found", f"Workspace directory does not exist: {root}",
                            "Pass an existing workspace directory with --project.", run_id=run_id)
    config_path = root / ".autobench" / "config.json"
    if not config_path.is_file():
        return _run_blocker("setup", "workspace_uninitialized", "Autobench workspace is not initialized",
                            "Run autobench init with this workspace's Scheduler URL and token file.",
                            run_id=run_id)
    if not (root / ".autobench" / "state" / "autobench.sqlite3").is_file():
        return _run_blocker("setup", "workspace_state_missing", "The workspace state database is missing",
                            "Restore the existing workspace state before running a benchmark.", run_id=run_id)
    try:
        settings = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            raise ValueError("workspace configuration must be a JSON object")
        if scheduler is None:
            url = settings.get("scheduler_url")
            token = settings.get("token_file")
            if not isinstance(url, str) or not url or not isinstance(token, str) or not token:
                return _run_blocker("setup", "scheduler_unconfigured", "Device Scheduler is not configured",
                                    "Run autobench init with this workspace's Scheduler URL and token file.",
                                    run_id=run_id)
            token_path = Path(token).expanduser()
            if not token_path.is_file() or not os.access(token_path, os.R_OK):
                return _run_blocker("setup", "scheduler_token_unreadable", "The configured Scheduler token file cannot be read",
                                    "Restore access to the token file or update the workspace configuration.",
                                    run_id=run_id)
            from .scheduler import McpScheduler
            scheduler = McpScheduler(url, str(token_path))
        return _workspace_engine(root, scheduler)
    except SchedulerUnavailable as exc:
        return _run_blocker("setup", "scheduler_client_unavailable", str(exc),
                            "Install the Device Scheduler Python SDK in the Autobench environment.", run_id=run_id)
    except (OSError, ValueError) as exc:
        return _run_blocker("setup", "workspace_config_invalid", str(exc),
                            "Repair the workspace's Autobench configuration.", run_id=run_id)
    except sqlite3.DatabaseError as exc:
        return _run_blocker("setup", "workspace_state_unavailable", str(exc),
                            "Inspect the workspace's Autobench state database.", run_id=run_id)


def _run_benchmark(root: Path, *, name: str | None, definition: str | Path | None,
                   argv: Sequence[str] | None, include: Sequence[str], data: Sequence[str],
                   kind: str | None, gpu: int | None, gpu_memory_mib: int | None,
                   expected_seconds: int | None, kill_seconds: int | None) -> Benchmark:
    if sum(value is not None for value in (name, definition, argv)) != 1:
        raise RegistrationError("specify one of a registered name, --file, or argv after --")
    if argv is None:
        if include or data or any(value is not None for value in
                                  (kind, gpu, gpu_memory_mib, expected_seconds, kill_seconds)):
            raise RegistrationError("input, resource, kind, and runtime flags apply to raw argv runs")
        if definition is not None:
            return parse_registration(Path(definition).expanduser().resolve(), root)
        benchmark = next((item for item in load_registry(root) if item.name == name), None)
        if benchmark is None:
            raise RegistrationError(f"no registered benchmark named {name!r}")
        return benchmark

    if isinstance(argv, (str, bytes)) or isinstance(include, (str, bytes)) or isinstance(data, (str, bytes)):
        raise RegistrationError("argv, include, and data must be sequences of strings")
    if (gpu is None) != (gpu_memory_mib is None):
        raise RegistrationError("specify --gpu and --gpu-memory-mib together")
    resources: dict[str, Any] = {}
    if gpu is not None:
        if type(gpu) is not int or not 1 <= gpu <= 16:
            raise RegistrationError("--gpu must be an integer from 1 through 16")
        if type(gpu_memory_mib) is not int or gpu_memory_mib < 1:
            raise RegistrationError("--gpu-memory-mib must be a positive integer")
        resources = {"gpu_count": gpu, "gpu_memory_bytes": gpu_memory_mib * 1024**2}
    if kind is None:
        kind = "timing"
    if kind not in ("timing", "correctness"):
        raise RegistrationError("raw argv runs support timing or correctness")
    if any(value is not None and type(value) is not int for value in (expected_seconds, kill_seconds)):
        raise RegistrationError("expected and hard-runtime seconds must be integers")
    recipe = {"benchmark": {"name": "argv"}, "source": {"include": list(include)},
              "data": {"paths": list(data)}, "packet": [{
                  "kind": kind, "argv": list(argv), "resources": resources,
                  "expected_seconds": 5 if expected_seconds is None else expected_seconds,
                  "kill_seconds": 30 if kill_seconds is None else kill_seconds,
              }]}
    return registration_from_mapping(recipe, root, label="argv")


async def start_run(project: str | Path = ".", *, name: str | None = None,
                    definition: str | Path | None = None, argv: Sequence[str] | None = None,
                    include: Sequence[str] = (), data: Sequence[str] = (), kind: str | None = None,
                    gpu: int | None = None, gpu_memory_mib: int | None = None,
                    expected_seconds: int | None = None, kill_seconds: int | None = None,
                    admission_seconds: int = 60, foreground: bool = False,
                    scheduler: Scheduler | None = None) -> dict[str, Any]:
    """Freeze and accept one explicit run, returning its durable ID or a blocker."""
    from .run import RunBlocked, start_run as accept_run

    root = Path(project).expanduser().resolve()
    engine = _open_run_engine(root, scheduler)
    if isinstance(engine, dict):
        return engine
    try:
        try:
            benchmark = _run_benchmark(root, name=name, definition=definition, argv=argv,
                                       include=include, data=data, kind=kind, gpu=gpu,
                                       gpu_memory_mib=gpu_memory_mib, expected_seconds=expected_seconds,
                                       kill_seconds=kill_seconds)
        except (RegistrationError, ValueError) as exc:
            return _run_blocker("validation", "invalid_recipe", str(exc),
                                "Correct the benchmark name, recipe, or argv options.")
        try:
            run_id = await accept_run(engine, benchmark, foreground=foreground, admission_seconds=admission_seconds)
        except RunBlocked as exc:
            return exc.result
        return {"run_id": run_id, "state": "accepted", "phase": "accepted"}
    finally:
        engine.close()


async def wait_run(project: str | Path, run_id: str, *, wait_seconds: float | None = None,
                   scheduler: Scheduler | None = None) -> dict[str, Any]:
    """Wait within the run's bounds, returning evidence, a blocker, or reconnect state."""
    from .run import RunBlocked, wait_run as collect_run

    if wait_seconds is not None and (isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float))
                                     or not math.isfinite(wait_seconds) or wait_seconds < 0):
        return _run_blocker("validation", "invalid_wait_time", "Wait seconds must be a nonnegative finite number",
                            "Set --wait-seconds to a nonnegative number.", run_id=run_id)
    root = Path(project).expanduser().resolve()
    engine = _open_run_engine(root, scheduler, run_id=run_id)
    if isinstance(engine, dict):
        return engine
    try:
        return await collect_run(engine, run_id, wait_seconds=wait_seconds)
    except RunBlocked as exc:
        return {"run_id": run_id, **exc.result}
    finally:
        engine.close()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _atomic_text(path: Path, value: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)

