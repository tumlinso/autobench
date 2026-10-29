from __future__ import annotations

import asyncio
from contextlib import contextmanager
import fnmatch
import hashlib
import json
import math
import os
import shutil
import sys
import sysconfig
import uuid
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .errors import AutobenchError, SchedulerUnavailable
from .models import Benchmark, Command
from .registry import load_registry
from .scheduler import Scheduler, cleanup_verified, job_object, terminal
from .snapshot import capture, observe
from .source_trace import SourceTrace
from .store import Store
from .settings import host_limits

_DEFAULT_LIMITS = host_limits()
POLL_SECONDS = _DEFAULT_LIMITS["poll_seconds"]
SETTLE_SECONDS = _DEFAULT_LIMITS["settle_seconds"]
SCRATCH_LIMIT = _DEFAULT_LIMITS["scratch_limit_bytes"]
RETRY_BACKOFF_INITIAL_SECONDS = 30
RETRY_BACKOFF_MAX_SECONDS = 15 * 60
FAILURE_LOG_TAIL_BYTES = 8 * 1024


def _state(project: Path) -> Path:
    return project / ".autobench" / "state"


def _measurement_protocol(benchmark: Benchmark, command: Command, recipe_fingerprint: str) -> dict[str, Any]:
    resources = {key: value for key, value in command.resources.items() if key != "_environment"}
    environment = command.resources.get("_environment", {})
    environment_hashes = {}
    if isinstance(environment, dict):
        for key, value in sorted(environment.items()):
            serialized = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
            environment_hashes[str(key)] = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return {
        "schema_version": 1,
        "registered": benchmark.protocol or {},
        "argv": list(command.argv),
        "kind": command.kind,
        "resources": resources,
        "environment_sha256": environment_hashes,
        "recipe_fingerprint": recipe_fingerprint,
    }


def _protocol_fingerprint(protocol: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(protocol, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _fingerprint(benchmark: Benchmark, source_fingerprint: str, command: Command, data: dict[str, str], recipe_hash: str, build_fingerprint: str | None = None) -> str:
    payload = {"benchmark": benchmark.name, "revision": benchmark.revision,
               "fingerprint_schema": 2,
               "source": source_fingerprint, "argv": command.argv,
               "expected": command.expected_seconds, "kill": command.kill_seconds,
               "kind": command.kind, "resources": command.resources, "data": data, "recipe": recipe_hash, "build": build_fingerprint,
               "protocol_fingerprint": _protocol_fingerprint(_measurement_protocol(benchmark, command, recipe_hash))}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _autobench_root(config: dict[str, Any] | None = None) -> Path:
    if config and config.get("recipe_root"):
        return Path(config["recipe_root"]).expanduser().resolve()
    installed = Path(sysconfig.get_path("data")) / "share" / "autobench"
    if (installed / "examples").exists():
        return installed
    return Path(__file__).resolve().parents[2]


def _recipe_hash(command: Command, autobench_root: Path) -> str:
    recipe_dirs: set[Path] = set()
    for arg in command.argv:
        marker = "{autobench}/"
        if marker not in arg:
            continue
        raw = arg.split(marker, 1)[1].split("{", 1)[0]
        path = (autobench_root / raw).resolve()
        try:
            relative = path.relative_to(autobench_root.resolve())
        except ValueError:
            continue
        parts = relative.parts
        if "examples" in parts:
            index = parts.index("examples")
            if len(parts) >= index + 2:
                recipe_dirs.add(autobench_root / Path(*parts[:index + 2]))
    files: list[Path] = []
    for directory in recipe_dirs:
        for base, dirs, names in os.walk(directory, followlinks=False):
            dirs[:] = [d for d in dirs if d not in {"__pycache__", ".git"}]
            for name in names:
                candidate = Path(base) / name
                if candidate.is_file() and not candidate.is_symlink():
                    files.append(candidate)
    digest = hashlib.sha256()
    for path in sorted(set(files)):
        rel = path.relative_to(autobench_root).as_posix()
        digest.update(rel.encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def _substitute(command: Command, snapshot: Path, build: Path, output: Path, project: Path, artifacts: dict[str, Path], autobench_root: Path) -> tuple[list[str], dict[str, str]]:
    values = {"{snapshot}": str(snapshot), "{data}": str(snapshot), "{build}": str(build), "{output}": str(output), "{project}": str(project), "{autobench}": str(autobench_root)}
    values.update({f"{{artifact.{name}}}": str(path) for name, path in artifacts.items()})
    def sub(value: str) -> str:
        for key, val in values.items():
            value = value.replace(key, val)
        return value
    environment = command.resources.get("_environment", {})
    return [sub(v) for v in command.argv], {k: sub(v) for k, v in environment.items()}


def _disk_usage(path: Path) -> int:
    total = 0
    if path.exists():
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    pass
    return total


def _read_file_tail(path: Path, limit: int = FAILURE_LOG_TAIL_BYTES) -> tuple[str, bool]:
    try:
        if path.is_symlink() or not path.is_file():
            return "", False
        resolved = path.resolve(strict=True)
        size = resolved.stat().st_size
        with resolved.open("rb") as stream:
            stream.seek(max(0, size - limit))
            content = stream.read(limit)
        return content.decode("utf-8", errors="replace"), size > limit
    except OSError:
        return "", False


def _bounded_tail(value: Any, limit: int = FAILURE_LOG_TAIL_BYTES) -> tuple[str, bool]:
    if not isinstance(value, str):
        return "", False
    encoded = value.encode("utf-8", errors="replace")
    truncated = len(encoded) > limit
    if truncated:
        encoded = encoded[-limit:]
    return encoded.decode("utf-8", errors="replace"), truncated


def _compact_scheduler_result(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    compact = dict(value)
    for stream in ("stdout", "stderr"):
        text = compact.pop(stream, None)
        if isinstance(text, str):
            compact[f"{stream}_bytes"] = len(text.encode("utf-8", errors="replace"))
    logs = compact.pop("logs", None)
    if isinstance(logs, dict):
        compact["logs_summary"] = {key: len(item.encode("utf-8", errors="replace"))
                                   for key, item in logs.items() if isinstance(item, str)}
    return compact


def _compact_scheduler_job(job: dict[str, Any]) -> dict[str, Any]:
    compact = dict(job)
    compact["result"] = _compact_scheduler_result(job.get("result"))
    attempt = job.get("attempt")
    if isinstance(attempt, dict):
        attempt_copy = dict(attempt)
        attempt_copy["result"] = _compact_scheduler_result(attempt.get("result"))
        compact["attempt"] = attempt_copy
    return compact


def _build_output_identity(build: Path) -> str | None:
    digest = hashlib.sha256()
    files = []
    if not build.exists():
        return None
    for base, dirs, names in os.walk(build, followlinks=False):
        dirs[:] = [name for name in dirs if name not in {".git", "__pycache__"}]
        for name in names:
            path = Path(base) / name
            if name != "SUCCESS.json" and path.is_file() and not path.is_symlink():
                files.append(path)
    if not files:
        return None
    for path in sorted(files):
        digest.update(path.relative_to(build).as_posix().encode() + b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _device_identity(job: dict[str, Any]) -> list[dict[str, Any]]:
    attempt = job.get("attempt")
    allocations: dict[str, dict[str, Any]] = {}
    descriptors: dict[str, dict[str, Any]] = {}
    sources: list[tuple[str, Any]] = [("allocations", job.get("allocations")), ("resources", job.get("resources"))]
    if isinstance(attempt, dict):
        sources.extend([("allocations", attempt.get("allocations")), ("resources", attempt.get("resources"))])
        execution = attempt.get("execution_context")
        if isinstance(execution, dict):
            sources.extend([("allocations", execution.get("allocations")), ("resources", execution.get("resources"))])
        result = attempt.get("result")
        if isinstance(result, dict):
            sources.extend([("allocations", result.get("allocations")), ("resources", result.get("resources"))])
    for source_name, candidate in sources:
        if not isinstance(candidate, list):
            continue
        for item in candidate:
            if not isinstance(item, dict):
                continue
            resource_id = item.get("resource_id") or item.get("id")
            if isinstance(resource_id, str) and resource_id:
                target = allocations if source_name == "allocations" else descriptors
                target[resource_id] = item
    # Scheduler jobs.get binds the real grant under attempt.execution_context.
    # Prefer those allocation IDs and enrich them with their descriptor so we
    # persist the stable GPU UUID and host identity, not merely the request.
    execution = attempt.get("execution_context") if isinstance(attempt, dict) else None
    if isinstance(execution, dict):
        allocations = {str(item.get("resource_id")): item for item in execution.get("allocations", [])
                       if isinstance(item, dict) and isinstance(item.get("resource_id"), str)}
        descriptors = {str(item.get("resource_id")): item for item in execution.get("resources", [])
                       if isinstance(item, dict) and isinstance(item.get("resource_id"), str)}
    found: list[dict[str, Any]] = []
    for resource_id in sorted(set(allocations) | set(descriptors)):
        allocation = allocations.get(resource_id, {})
        resource = descriptors.get(resource_id, {})
        attributes = resource.get("attributes", {}) if isinstance(resource.get("attributes"), dict) else {}
        found.append({
            "resource_id": resource_id,
            "kind": resource.get("kind"),
            "host_id": resource.get("host_id"),
            "quantities": allocation.get("quantities", {}),
            "uuid": attributes.get("uuid"),
            "name": attributes.get("name"),
            "pci_bus_id": attributes.get("pci_bus_id"),
            "topology": resource.get("topology", {}),
        })
    return found


def _validate_staged_outputs(directory: Path) -> None:
    root = directory.resolve()
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise AutobenchError(f"staged output contains a symlink: {path.relative_to(directory)}")
        if path.is_file():
            try:
                path.resolve().relative_to(root)
            except ValueError as exc:
                raise AutobenchError(f"staged output escapes its result directory: {path}") from exc


def _timestamp(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return float("inf")


def _scheduler_resources(raw: dict[str, Any], selected_resource_ids: list[str] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Translate the compact registration envelope to Scheduler requirements."""
    explicit = raw.get("scheduler")
    tags = [str(tag) for tag in raw.get("tags", [])] if isinstance(raw.get("tags", []), list) else []
    if explicit is not None:
        if not isinstance(explicit, list):
            raise AutobenchError("resources.scheduler must be an array of Scheduler resource requirements")
        requirements = [dict(item) for item in explicit]
        gpu_indices = [i for i, item in enumerate(requirements) if isinstance(item, dict) and str(item.get("kind", "")).casefold() == "gpu"]
        if selected_resource_ids is not None and gpu_indices:
            if len(selected_resource_ids) != len(gpu_indices):
                raise AutobenchError("Scheduler forecast GPU IDs do not match the registered GPU requirement count")
            for index, resource_id in zip(gpu_indices, selected_resource_ids):
                requirements[index]["selectors"] = {**requirements[index].get("selectors", {}), "resource_id": resource_id}
        if gpu_indices and "gpu.exclusive" not in tags:
            tags.append("gpu.exclusive")
        return requirements, tags
    requirements: list[dict[str, Any]] = []
    if raw.get("cpu_cores"):
        requirements.append({"kind": "cpu", "quantities": {"cores": raw["cpu_cores"]}, "mode": "shared"})
    if raw.get("host_memory_bytes"):
        requirements.append({"kind": "memory", "quantities": {"bytes": raw["host_memory_bytes"]}, "mode": "shared"})
    if raw.get("gpu_memory_bytes"):
        count = raw.get("gpu_count", 1)
        if isinstance(count, bool) or not isinstance(count, int) or count < 1 or count > 16:
            raise AutobenchError("resources.gpu_count must be an integer from 1 to 16")
        if selected_resource_ids is not None and len(selected_resource_ids) != count:
            raise AutobenchError("Scheduler forecast GPU IDs do not match resources.gpu_count")
        topology = raw.get("topology_constraints", {})
        if not isinstance(topology, dict):
            raise AutobenchError("resources.topology_constraints must be a Scheduler topology constraint table")
        for index in range(count):
            requirement = {"kind": "gpu", "quantities": {"memory_bytes": raw["gpu_memory_bytes"]},
                           "mode": raw.get("mode", "exclusive"), "same_host": True,
                           "topology_constraints": topology}
            if selected_resource_ids is not None:
                requirement["selectors"] = {"resource_id": selected_resource_ids[index]}
            requirements.append(requirement)
        if "gpu.exclusive" not in tags:
            tags.append("gpu.exclusive")
    return requirements, tags


class Engine:
    def __init__(self, project: Path, scheduler: Scheduler):
        self.project = project.resolve()
        self.autobench = self.project / ".autobench"
        self.state = _state(self.project)
        self.scheduler = scheduler
        state_path = self.state / "autobench.sqlite3"
        if not state_path.is_file():
            raise AutobenchError(f"Autobench state database is missing at {state_path}; inspect and recover the existing workspace state before reinitializing")
        self.store = Store(state_path, create=False)
        self.source_trace = SourceTrace(self.project, self.state)
        self.config_path = self.autobench / "config.json"
        self.config = json.loads(self.config_path.read_text(encoding="utf-8")) if self.config_path.exists() else {}
        self.limits = host_limits()

    def close(self) -> None:
        self.store.close()

    @contextmanager
    def _workspace_writer_lock(self):
        """A fresh open-file-description lock shared by all coordinators/processes."""
        lock_path = self.state / "coordinator.lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        acquired = False
        try:
            try:
                import fcntl
            except ImportError as exc:
                raise AutobenchError("workspace coordination requires POSIX advisory file locking") from exc
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                yield False
                return
            yield True
        finally:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _remove_owned_attempt_path(path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return
        parent = path.parent
        try:
            resolved_parent = parent.resolve(strict=True)
        except OSError as exc:
            raise OSError(f"cannot verify owned attempt path parent {parent}: {exc}") from exc
        if resolved_parent != parent.absolute():
            raise OSError(f"refusing to remove attempt path through a symlinked parent: {path}")
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()

    def _recover_prepared_attempts(self) -> tuple[int, list[str]]:
        """Clean intents known never to have reached Scheduler submission."""
        rows = list(self.store.db.execute("SELECT id FROM attempts WHERE state='prepared' ORDER BY created"))
        recovered = 0
        failures: list[str] = []
        for row in rows:
            attempt_id = str(row["id"])
            # Attempt IDs are generated UUIDs; reject malformed database values
            # before deriving paths to remove from them.
            try:
                uuid.UUID(attempt_id)
            except (ValueError, AttributeError):
                self.store.set_attempt(attempt_id, "not_submitted", error="prepared row has a malformed attempt ID; no filesystem paths were removed")
                self.store.delete_setting(f"attempt:{attempt_id}")
                self.store.delete_setting(f"reservation:{attempt_id}")
                recovered += 1
                continue
            staged = self.autobench / "scratch" / attempt_id
            manifest = self.state / "packets" / f"{attempt_id}.json"
            try:
                self._remove_owned_attempt_path(staged)
                self._remove_owned_attempt_path(manifest)
            except OSError as exc:
                failures.append(f"{attempt_id}: {exc}")
                continue
            details = self.store.get(f"attempt:{attempt_id}")
            error = "recovered prepared attempt; Scheduler submission had not begun"
            if not isinstance(details, dict):
                error = "recovered orphan prepared attempt without metadata; Scheduler submission had not begun"
            self.store.set_attempt(attempt_id, "not_submitted", error=error)
            self.store.delete_setting(f"attempt:{attempt_id}")
            self.store.delete_setting(f"reservation:{attempt_id}")
            recovered += 1
        return recovered, failures

    async def doctor(self) -> dict[str, Any]:
        benchmarks = load_registry(self.project)
        missing_tools = []
        for benchmark in benchmarks:
            commands = ([benchmark.build] if benchmark.build else []) + list(benchmark.packets)
            for command in commands:
                joined = " ".join(command.argv).lower()
                needed = set()
                if "ncu" in joined or "kernel-profile" in command.kind:
                    needed.add("ncu")
                if "nsys" in joined or "timeline-profile" in command.kind:
                    needed.add("nsys")
                command_path = command.resources.get("_environment", {}).get("PATH")
                for tool in sorted(needed):
                    if shutil.which(tool, path=command_path or None) is None:
                        missing_tools.append({"benchmark": benchmark.name, "packet": command.name, "tool": tool})
        unit_available = False
        systemctl = shutil.which("systemctl")
        if systemctl:
            try:
                import subprocess
                unit_available = subprocess.run([systemctl, "--user", "cat", "autobench-coordinator.service"],
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
            except OSError:
                pass
        token_file = self.config.get("token_file")
        token_readable = bool(token_file and Path(token_file).expanduser().is_file() and os.access(Path(token_file).expanduser(), os.R_OK))
        missing_configuration = []
        if not self.config.get("scheduler_url"):
            missing_configuration.append("Scheduler URL (--scheduler-url)")
        if not token_readable:
            missing_configuration.append("readable Scheduler token file (--token-file)")
        if not unit_available:
            missing_configuration.append("preinstalled autobench-coordinator.service user unit")
        scheduler_access: dict[str, Any] = {"ready": False, "missing_actions": [], "error": None}
        try:
            access = await self.scheduler.call("access.inspect", {})
            actions = access.get("actions", [])
            actions = [item for item in actions if isinstance(item, str)] if isinstance(actions, list) else []
            required = ["jobs.submit", "jobs.get", "jobs.list", "jobs.control", "resources.forecast",
                        "history.compact:autobench"]
            missing = [needed for needed in required if not any(fnmatch.fnmatchcase(needed, grant) for grant in actions)]
            principal = access.get("principal_id")
            principal_valid = isinstance(principal, str) and bool(principal)
            scheduler_access = {"ready": principal_valid and not missing,
                                "principal_id": principal, "required_actions": required,
                                "missing_actions": missing,
                                "error": None if principal_valid else "access.inspect did not identify the authenticated principal"}
        except Exception as exc:
            scheduler_access["error"] = str(exc)
        return {"project": str(self.project), "initialized": self.autobench.exists(),
                "registry_count": len(benchmarks),
                "scheduler_configured": bool(self.config.get("scheduler_url") and self.config.get("token_file")),
                "scheduler_connected": scheduler_access["error"] is None,
                "scheduler_access": scheduler_access,
                "scheduler_principal": scheduler_access.get("principal_id") or self.config.get("principal_id"),
                "coordinator_unit_available": unit_available, "missing_profiler_tools": missing_tools,
                "missing_configuration": missing_configuration,
                "scheduler_retention_error": self.store.get("scheduler_retention_error"),
                "retained_bytes": _disk_usage(self.autobench),
                "retained_cap_bytes": self.store.get("retained_cap_bytes", 1024**3),
                "scratch_limit_bytes": self.limits["scratch_limit_bytes"]}

    def explain(self, name: str) -> dict[str, Any]:
        benchmark = next((item for item in load_registry(self.project) if item.name == name), None)
        if benchmark is None:
            raise AutobenchError(f"no registered benchmark named {name!r}")
        source_fingerprint, manifest = observe(benchmark)
        autobench_root = _autobench_root(self.config)
        build_fp = None
        build_output = None
        if benchmark.build:
            build_fp = hashlib.sha256(json.dumps({"source": source_fingerprint, "argv": benchmark.build.argv,
                                                   "resources": benchmark.build.resources, "expected": benchmark.build.expected_seconds,
                                                   "kill": benchmark.build.kill_seconds,
                                                   "recipe": _recipe_hash(benchmark.build, autobench_root)}, sort_keys=True, default=str).encode()).hexdigest()
            build_dir = self.autobench / "builds" / build_fp
            if self._build_is_current(build_dir, build_fp):
                build_output = _build_output_identity(build_dir)
        build_retry = self._retry_cooldown(name, -1, build_fp) if build_fp else None
        packets = []
        for index, packet in enumerate(benchmark.packets):
            recipe_fingerprint = _recipe_hash(packet, autobench_root)
            protocol = _measurement_protocol(benchmark, packet, recipe_fingerprint)
            protocol_fingerprint = _protocol_fingerprint(protocol)
            expected = _fingerprint(benchmark, source_fingerprint, packet, manifest, recipe_fingerprint,
                                    hashlib.sha256((str(build_fp) + str(build_output)).encode()).hexdigest() if build_fp and build_output else None)
            actual = self.store.current(benchmark.name, index)
            reasons = []
            if actual is None:
                reasons.append("no successful result has been recorded")
            elif actual != expected:
                reasons.append("source, build, protocol, data, or packet registration changed")
            if build_fp and build_output is None:
                reasons.append("registered build is missing, stale, or incomplete")
            packets.append({"name": packet.name, "kind": packet.kind, "state": "current" if actual == expected and not reasons else "stale_or_missing",
                            "current_fingerprint": actual, "expected_fingerprint": expected, "expected_seconds": packet.expected_seconds,
                            "kill_seconds": packet.kill_seconds, "depends_on": list(packet.depends_on), "reasons": reasons,
                            "protocol_fingerprint": protocol_fingerprint,
                            "retry_cooldown": self._retry_cooldown(name, index, expected)})
            retry = packets[-1]["retry_cooldown"]
            if retry and retry["blocked"]:
                packets[-1]["reasons"].append(retry["reason"])
        latest = [row for row in self.store.attempts_for(name) if row["state"] in {"submitted", "running", "cleanup_unknown", "submit_intent"}]
        return {"benchmark": name, "source_fingerprint": source_fingerprint, "protocol": benchmark.protocol or {},
                "build_fingerprint": build_fp, "build_output_identity": build_output, "packets": packets,
                "build_retry_cooldown": build_retry,
                "active_or_unresolved_attempts": [{k: row[k] for k in ("id", "packet_index", "job_id", "state", "error")} for row in latest[:16]],
                "source_trace": self.store.get(f"seen:{name}", {}).get("trace")}

    def _retry_cooldown(self, benchmark: str, packet_index: int, fingerprint: str | None,
                        now: float | None = None) -> dict[str, Any] | None:
        """Derive bounded same-input backoff from the retained attempt rows."""
        if not fingerprint:
            return None
        now = time.time() if now is None else now
        failures = 0
        newest_failure: dict[str, Any] | None = None
        success_states = {"succeeded", "build_succeeded"}
        reset_states = {"prepared", "submit_intent", "submitted", "running", "cleanup_unknown",
                        "not_submitted", "preempted", "abandoned", "cancelled", "canceled"}
        for row in self.store.attempts_for(benchmark):
            if int(row.get("packet_index", -999)) != packet_index:
                continue
            if row.get("fingerprint") != fingerprint:
                # A source, recipe, registration, or build input change clears
                # this retry streak even if the user later reverts the change.
                break
            state = str(row.get("state", "")).casefold()
            if state in success_states or state in reset_states:
                break
            failures += 1
            if newest_failure is None:
                newest_failure = row
        if failures == 0 or newest_failure is None:
            return None
        delay = min(RETRY_BACKOFF_INITIAL_SECONDS * (2 ** min(failures - 1, 20)),
                    RETRY_BACKOFF_MAX_SECONDS)
        retry_at = float(newest_failure.get("updated", now)) + delay
        remaining = max(0.0, retry_at - now)
        last_error = newest_failure.get("error")
        if not last_error:
            try:
                result = json.loads(newest_failure.get("result_json") or "{}")
                worker = result.get("worker", {}) if isinstance(result, dict) else {}
                last_error = worker.get("status") if isinstance(worker, dict) else None
            except (TypeError, json.JSONDecodeError):
                last_error = None
        reason = (f"{failures} consecutive failure(s) for this input; retry allowed now" if remaining <= 0 else
                  f"{failures} consecutive failure(s) for this input; retry in {int(remaining + 0.999)} seconds")
        return {"fingerprint": fingerprint, "failure_streak": failures, "delay_seconds": delay,
                "retry_at": retry_at, "remaining_seconds": remaining, "blocked": remaining > 0,
                "last_error": last_error, "reason": reason}

    @staticmethod
    def _failure_diagnostics(result: dict[str, Any], staged: Path, job: dict[str, Any]) -> dict[str, Any]:
        sources: list[dict[str, Any]] = []
        attempt = job.get("attempt")
        if isinstance(attempt, dict) and isinstance(attempt.get("result"), dict):
            sources.append(attempt["result"])
        if isinstance(job.get("result"), dict):
            sources.append(job["result"])
        diagnostics: dict[str, Any] = {}
        for stream in ("stdout", "stderr"):
            text, truncated = _read_file_tail(staged / f"{stream}.log")
            if not text:
                for source in sources:
                    logs = source.get("logs") if isinstance(source.get("logs"), dict) else {}
                    value = source.get(stream, logs.get(stream, logs.get("text", "") if stream == "stdout" else ""))
                    text, truncated = _bounded_tail(value)
                    if text:
                        break
            if text or truncated:
                diagnostics[f"{stream}_tail"] = text
                diagnostics[f"{stream}_truncated"] = truncated
        if diagnostics:
            result["diagnostics"] = diagnostics
        return result

    def retry_cooldowns(self) -> list[dict[str, Any]]:
        """Return active cooldowns for status without persisting a second index."""
        active = []
        for benchmark in load_registry(self.project):
            explanation = self.explain(benchmark.name)
            build = explanation.get("build_retry_cooldown")
            if isinstance(build, dict) and build.get("blocked"):
                active.append({"benchmark": benchmark.name, "target": "build", **build})
            for packet in explanation.get("packets", []):
                retry = packet.get("retry_cooldown")
                if isinstance(retry, dict) and retry.get("blocked"):
                    active.append({"benchmark": benchmark.name, "target": packet["name"], **retry})
        return active

    async def _scheduler_health(self) -> bool:
        try:
            await self.scheduler.call("access.inspect", {})
            return True
        except Exception:
            return False

    async def reconcile_once(self) -> dict[str, Any]:
        # Hold a nonblocking interprocess lock across all reads, Scheduler awaits,
        # submit intent, and evidence import. A busy second coordinator does no work.
        with self._workspace_writer_lock() as acquired:
            if not acquired:
                return {"state": "busy", "submitted": 0}
            recovered, failures = self._recover_prepared_attempts()
            if failures:
                return {"state": "waiting_for_prepared_cleanup", "recovered": recovered,
                        "failures": failures, "submitted": 0}
            return await self._reconcile_once_locked()

    async def _reconcile_once_locked(self) -> dict[str, Any]:
        if self.store.get("paused", False):
            return {"state": "paused", "submitted": 0}
        reconciled = await self._reconcile_jobs()
        await self._compact_scheduler_history_if_due()
        # Fail closed on unresolved execution/cleanup: no duplicate submission.
        unresolved = self.store.unresolved()
        if unresolved:
            return {"state": "waiting_for_scheduler_cleanup", "reconciled": reconciled, "submitted": 0}
        benchmarks = load_registry(self.project)
        candidates: list[dict[str, Any]] = []
        storage_blocked: list[str] = []
        retry_blocked: dict[tuple[str, str], dict[str, Any]] = {}
        for benchmark in benchmarks:
            if not benchmark.enabled:
                continue
            try:
                source = capture(benchmark, self.state)
            except Exception as exc:
                self.store.set(f"registration-error:{benchmark.name}", str(exc))
                continue
            self.store.set(f"registration-error:{benchmark.name}", None)
            # Stabilize on the same settled fingerprint across coordinator polls.
            seen_key = f"seen:{benchmark.name}"
            seen = self.store.get(seen_key)
            now = time.time()
            if not isinstance(seen, dict) or seen.get("fingerprint") != source.fingerprint:
                cap = min(int(self.store.get("retained_cap_bytes", 1024**3)), self.limits["retained_cap_bytes"])
                trace_budget = max(0, cap - _disk_usage(self.autobench)) // 3
                trace = self.source_trace.incoming_summary(source.directory, list(source.manifest),
                                                          input_budget_bytes=trace_budget)
                self.store.set(seen_key, {"fingerprint": source.fingerprint, "since": now, "trace": trace})
                continue
            if now - float(seen.get("since", now)) < SETTLE_SECONDS:
                continue
            packet_index_by_name = {packet.name: i for i, packet in enumerate(benchmark.packets)}
            autobench_root = _autobench_root(self.config)
            build_fp = hashlib.sha256(json.dumps({"source": source.fingerprint, "argv": benchmark.build.argv,
                                                   "resources": benchmark.build.resources, "expected": benchmark.build.expected_seconds,
                                                   "kill": benchmark.build.kill_seconds,
                                                   "recipe": _recipe_hash(benchmark.build, autobench_root)}, sort_keys=True, default=str).encode()).hexdigest() if benchmark.build else None
            build_dir = self.autobench / "builds" / (build_fp or "no-build")
            build_output_identity = _build_output_identity(build_dir) if benchmark.build and self._build_is_current(build_dir, build_fp or "") else None
            expected_fingerprints = {packet.name: _fingerprint(
                benchmark, source.fingerprint, packet, source.manifest, _recipe_hash(packet, autobench_root),
                hashlib.sha256((str(build_fp) + str(build_output_identity)).encode()).hexdigest() if build_fp else None)
                for packet in benchmark.packets}
            for index, packet in enumerate(benchmark.packets):
                dependency_indices = [packet_index_by_name[name] for name in packet.depends_on]
                if any(self.store.current(benchmark.name, dep) != expected_fingerprints[benchmark.packets[dep].name] for dep in dependency_indices):
                    continue
                artifacts = {}
                for dep in packet.depends_on:
                    dep_index = packet_index_by_name[dep]
                    artifacts[dep] = self._current_artifact(benchmark.name, dep_index)
                fingerprint = expected_fingerprints[packet.name]
                if self.store.current(benchmark.name, index) == fingerprint:
                    continue
                retry = self._retry_cooldown(benchmark.name, index, fingerprint)
                if retry and retry["blocked"]:
                    retry_blocked[(benchmark.name, packet.name)] = retry
                    continue
                cap_now = min(int(self.store.get("retained_cap_bytes", 1024**3)), self.limits["retained_cap_bytes"])
                packet_error = self.store.get(f"packet-storage-error:{benchmark.name}:{index}")
                if (isinstance(packet_error, dict) and packet_error.get("fingerprint") == fingerprint and
                    packet_error.get("cap") == cap_now and packet_error.get("scratch_limit") == self.limits["scratch_limit_bytes"]):
                    storage_blocked.append(packet.name)
                    continue
                if benchmark.build and not self._build_is_current(build_dir, build_fp or ""):
                    retry = self._retry_cooldown(benchmark.name, -1, build_fp)
                    if retry and retry["blocked"]:
                        retry_blocked[(benchmark.name, "build")] = retry
                        continue
                    prior_error = self.store.get(f"build-storage-error:{benchmark.name}")
                    if (isinstance(prior_error, dict) and prior_error.get("fingerprint") == build_fp and
                        prior_error.get("cap") == cap_now and prior_error.get("scratch_limit") == self.limits["scratch_limit_bytes"]):
                        return {"state": "storage_over_cap", "benchmark": benchmark.name, "reason": "the verified build does not fit the retained-data cap"}
                    self.store.delete_setting(f"build-storage-error:{benchmark.name}")
                    # This registration now needs the new build. If quota pressure
                    # requires it, the older cache is reproducible and can be evicted.
                    self.store.set(f"last-good-build:{benchmark.name}", str(build_dir))
                    build_protocol = _measurement_protocol(benchmark, benchmark.build,
                                                           _recipe_hash(benchmark.build, autobench_root))
                    await self._submit(benchmark, source.directory, build_dir, benchmark.build, -1,
                                       build_fp or "", kind="build",
                                       identity={"protocol": build_protocol,
                                                 "protocol_fingerprint": _protocol_fingerprint(build_protocol),
                                                 "source_fingerprint": source.fingerprint,
                                                 "build_fingerprint": build_fp})
                    return {"state": "submitted", "reconciled": reconciled, "benchmark": benchmark.name, "kind": "build", "submitted": 1}
                data_manifest = {p: source.manifest[p] for p in source.manifest if any(fnmatch.fnmatchcase(p, pat) or Path(p).match(pat) for pat in benchmark.data)}
                recipe_fingerprint = _recipe_hash(packet, autobench_root)
                protocol = _measurement_protocol(benchmark, packet, recipe_fingerprint)
                candidates.append({"benchmark": benchmark, "snapshot": source.directory, "build": build_dir,
                                   "command": packet, "index": index, "fingerprint": fingerprint,
                                   "artifacts": artifacts, "identity": {
                                       "source_fingerprint": source.fingerprint, "source_manifest": source.manifest,
                                       "data_manifest": data_manifest,
                                       "data_fingerprint": hashlib.sha256(json.dumps(data_manifest, sort_keys=True).encode()).hexdigest(),
                                       "protocol": protocol, "protocol_fingerprint": _protocol_fingerprint(protocol),
                                       "source_fingerprint": source.fingerprint, "build_fingerprint": build_fp,
                                       "build_output_identity": build_output_identity,
                                       "setup_key": hashlib.sha256(json.dumps({"build": build_fp, "data": data_manifest}, sort_keys=True).encode()).hexdigest()}})
        if candidates:
            candidate = await self._choose_candidate(candidates)
            requested, _ = _scheduler_resources(candidate["command"].resources)
            if requested and not candidate["identity"].get("forecast_ready"):
                forecast = candidate["identity"].get("availability_forecast")
                reason = candidate["identity"].get("forecast_wait_reason")
                return {"state": "waiting_for_forecast_window" if reason == "window_too_short" else "waiting_for_forecast_resources", "reconciled": reconciled,
                        "benchmark": candidate["benchmark"].name, "packet": candidate["command"].name,
                        "reason": reason, "forecast": forecast, "submitted": 0}
            await self._submit(candidate["benchmark"], candidate["snapshot"], candidate["build"], candidate["command"],
                               candidate["index"], candidate["fingerprint"], kind=candidate["command"].kind,
                               artifacts=candidate["artifacts"], identity=candidate["identity"])
            return {"state": "submitted", "reconciled": reconciled, "benchmark": candidate["benchmark"].name,
                    "kind": candidate["command"].kind, "submitted": 1,
                    "forecast": candidate["identity"].get("availability_forecast")}
        if storage_blocked:
            return {"state": "storage_over_cap", "reconciled": reconciled, "submitted": 0,
                    "packets": storage_blocked}
        if retry_blocked:
            return {"state": "retry_backoff", "reconciled": reconciled, "submitted": 0,
                    "cooldowns": [{"benchmark": benchmark, "target": target, **details}
                                  for (benchmark, target), details in sorted(retry_blocked.items())]}
        return {"state": "idle", "reconciled": reconciled, "submitted": 0}

    async def _owner_id(self) -> str:
        owner = self.config.get("principal_id")
        if not owner:
            access = await self.scheduler.call("access.inspect", {})
            owner = access.get("principal_id") or access.get("principal", {}).get("id")
            if not isinstance(owner, str) or not owner:
                raise SchedulerUnavailable("access.inspect did not identify the authenticated Scheduler principal")
            self.config["principal_id"] = owner
            temp = self.config_path.with_name(self.config_path.name + ".tmp")
            temp.write_text(json.dumps(self.config, indent=2) + "\n", encoding="utf-8")
            temp.replace(self.config_path)
        return owner

    async def _choose_candidate(self, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        scored = []
        forecast_cache: dict[str, dict[str, Any]] = {}
        setup_counts: dict[tuple[str, str], int] = {}
        for candidate in candidates:
            command = candidate["command"]
            requirements, _tags = _scheduler_resources(command.resources)
            estimate = self._expected_duration(candidate["benchmark"], candidate.get("index", 0), command,
                                               candidate["identity"])
            group = (candidate["identity"].get("setup_key", ""), json.dumps(requirements, sort_keys=True))
            setup_counts[group] = setup_counts.get(group, 0) + 1
        for order, candidate in enumerate(candidates):
            command = candidate["command"]
            requirements, _tags = _scheduler_resources(command.resources)
            forecast = None
            fit = 2 if requirements else 0
            earliest = float("inf")
            window = float("inf")
            selected_resource_ids: list[str] = []
            has_gpu = any(item.get("kind") == "gpu" for item in requirements)
            forecast_ready = not requirements
            wait_reason = None
            if requirements:
                try:
                    owner = await self._owner_id()
                    request = {"request_id": str(uuid.uuid4()), "owner_id": owner,
                               "requirements": requirements, "lease_ttl_seconds": float(command.kill_seconds + 30),
                               "owner_context": {"project": str(self.project), "benchmark": candidate["benchmark"].name,
                                                 "packet": command.name}}
                    cache_key = json.dumps({"owner_id": owner, "requirements": requirements,
                                            "lease_ttl_seconds": request["lease_ttl_seconds"],
                                            "horizon_seconds": 600, "max_candidates": 32}, sort_keys=True)
                    if cache_key in forecast_cache:
                        forecast = forecast_cache[cache_key]
                    else:
                        try:
                            forecast = await self.scheduler.call("resources.forecast", {"request": request,
                                                                                          "horizon_seconds": 600,
                                                                                          "max_candidates": 32})
                        except Exception as exc:
                            forecast = {"available": False, "reason": str(exc)}
                        forecast_cache[cache_key] = forecast
                    options = [item for item in forecast.get("candidates", [])
                               if isinstance(item, dict) and item.get("feasible_now") is True]
                    if options:
                        best = min(options, key=lambda item: _timestamp(item.get("earliest_start")))
                        ids = best.get("resource_ids", [])
                        current_resources = forecast.get("current_resources", [])
                        kinds = {item.get("resource_id"): item.get("kind") for item in current_resources
                                 if isinstance(item, dict) and isinstance(item.get("resource_id"), str)}
                        selected_resource_ids = [item for item in ids
                                                 if isinstance(item, str) and
                                                 (kinds.get(item) == "gpu" or item.startswith("gpu:"))] if isinstance(ids, list) else []
                        # Feasible-now timestamps are sampled by separate calls;
                        # using them as a score makes the first call win every
                        # otherwise tied comparison. Ready placements all score
                        # as current, leaving fit and setup grouping meaningful.
                        earliest = 0.0
                        raw_window = best.get("window_seconds")
                        if isinstance(raw_window, (int, float)) and not isinstance(raw_window, bool):
                            window = max(0.0, float(raw_window))
                        else:
                            # Compatibility with an older Scheduler response. Never
                            # treat expected_release (a start estimate) as a window end.
                            window = 0.0
                        expected_gpu_count = sum(1 for item in requirements if item.get("kind") == "gpu")
                        enough_gpu_ids = not has_gpu or len(selected_resource_ids) == expected_gpu_count
                        forecast_ready = enough_gpu_ids and window >= estimate + 5
                        if forecast_ready:
                            fit = 0
                        else:
                            wait_reason = "window_too_short" if enough_gpu_ids else "atomic_gpu_placement_missing"
                            fit = 2
                    else:
                        wait_reason = "no_feasible_now_atomic_placement"
                except Exception as exc:
                    forecast = {"available": False, "reason": str(exc)}
                    wait_reason = "forecast_unavailable"
            candidate["identity"]["selected_resource_ids"] = selected_resource_ids
            candidate["identity"]["availability_forecast"] = forecast
            candidate["identity"]["forecast_ready"] = forecast_ready
            candidate["identity"]["forecast_wait_reason"] = wait_reason
            setup_group = (candidate["identity"].get("setup_key", ""), json.dumps(requirements, sort_keys=True))
            # Prefer an anticipated window that fits, then the earliest start;
            # compatible ready packets with shared verified setup/data stay
            # adjacent in the advisory queue before shorter standalone work.
            scored.append(((fit, earliest, -setup_counts[setup_group], command.expected_seconds, order), candidate))
        return min(scored, key=lambda entry: entry[0])[1]

    async def _compact_scheduler_history_if_due(self) -> None:
        last = float(self.store.get("last_scheduler_compact", 0))
        if time.time() - last < 24 * 60 * 60:
            return
        before = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        try:
            await self.scheduler.call("history.compact", {"before": before, "max_records": 1000})
            self.store.set("last_scheduler_compact", time.time())
            self.store.set("scheduler_retention_error", None)
        except Exception as exc:
            self.store.set("scheduler_retention_error", str(exc))

    def _expected_duration(self, benchmark: Benchmark, packet_index: int, command: Command,
                           identity: dict[str, Any]) -> float:
        protocol = identity.get("protocol_fingerprint")
        if not isinstance(protocol, str):
            protocol = _protocol_fingerprint(identity.get("protocol", {}))
        data = identity.get("data_fingerprint")
        requested_devices = sorted(identity.get("selected_resource_ids", []))
        samples: list[float] = []
        for row in self.store.attempts_for(benchmark.name):
            if int(row.get("packet_index", -99)) != packet_index or row.get("state") not in {"succeeded", "build_succeeded"}:
                continue
            try:
                report = json.loads(row.get("result_json") or "{}")
                observed = report.get("identity", {})
                worker = report.get("worker", {})
                devices = sorted(item.get("resource_id") for item in observed.get("device_identity", [])
                                 if isinstance(item, dict) and item.get("kind") == "gpu"
                                 and isinstance(item.get("resource_id"), str))
                duration = worker.get("duration_seconds")
                if (observed.get("protocol_fingerprint") == protocol and observed.get("data_fingerprint") == data and
                    devices == requested_devices and isinstance(duration, (int, float)) and duration > 0):
                    samples.append(float(duration))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        estimate = float(command.expected_seconds)
        if samples:
            samples.sort()
            p90 = samples[min(len(samples) - 1, math.ceil(0.9 * len(samples)) - 1)]
            estimate = max(estimate, p90 * 1.2)
        # Preserve the declared lower bound and leave the Scheduler's default
        # five-second termination grace where the registered envelope permits.
        # The hard kill bound itself is never modified.
        grace = min(5, command.kill_seconds - command.expected_seconds)
        return min(float(command.kill_seconds - grace), estimate)

    async def _submit(self, benchmark: Benchmark, snapshot: Path, build: Path, command: Command, index: int, fingerprint: str, kind: str, artifacts: dict[str, Path] | None = None, identity: dict[str, Any] | None = None) -> None:
        cap = min(int(self.store.get("retained_cap_bytes", 1024**3)), self.limits["retained_cap_bytes"])
        reservation = int(self.limits["scratch_limit_bytes"])
        current_usage = _disk_usage(self.autobench)
        outstanding = self._reserved_bytes(current_usage)
        if _disk_usage(self.autobench) + outstanding + reservation > cap:
            self._evict_safe(required_free=outstanding + reservation)
            current_usage = _disk_usage(self.autobench)
            outstanding = self._reserved_bytes(current_usage)
        if current_usage + outstanding + reservation > cap:
            raise AutobenchError("retained-data cap reached; scratch reservation cannot fit safely")
        owner_id = await self._owner_id()
        attempt_id = str(uuid.uuid4())
        staged = self.autobench / "scratch" / attempt_id
        staged.mkdir(parents=True, exist_ok=True)
        packet_manifest = self.state / "packets" / f"{attempt_id}.json"
        try:
            argv, environment = _substitute(command, snapshot, build, staged, self.project, artifacts or {}, _autobench_root(self.config))
            selected = (identity or {}).get("selected_resource_ids")
            requested_requirements, _ = _scheduler_resources(command.resources)
            if any(item.get("kind") == "gpu" for item in requested_requirements) and (
                (identity or {}).get("forecast_ready") is not True or not selected
            ):
                raise AutobenchError("GPU work requires a feasible-now forecast-selected stable Scheduler resource ID")
            resource_requirements, resource_tags = _scheduler_resources(command.resources, selected if isinstance(selected, list) else None)
            packet_manifest.parent.mkdir(parents=True, exist_ok=True)
            packet_manifest.write_text(json.dumps({"argv": argv, "working_directory": str(snapshot), "allowed_root": str(self.project),
                                                  "result_directory": str(staged), "kill_seconds": command.kill_seconds,
                                                  "managed_root": str(self.autobench),
                                                  "scratch_limit_bytes": self.limits["scratch_limit_bytes"],
                                                  "retained_cap_bytes": cap,
                                                  "environment": environment, "output_limit_bytes": self.limits["output_limit_bytes"]}), encoding="utf-8")
        except Exception:
            shutil.rmtree(staged, ignore_errors=True)
            packet_manifest.unlink(missing_ok=True)
            raise
        # Scheduler uses expiring owner idempotency tombstones for bounded
        # retention. Persist this exact key with the attempt before submit so
        # recovery always reuses it rather than generating a new identity.
        key_expires = int(time.time()) + 7 * 24 * 60 * 60
        job_idempotency = f"autobench-v1:{key_expires}:{attempt_id}"
        details = {"kind": kind, "snapshot": str(snapshot), "build": str(build), "staged": str(staged), "benchmark": benchmark.name, "packet_index": index, "packet_name": command.name, "fingerprint": fingerprint, "manifest": str(packet_manifest), "idempotency_key": job_idempotency}
        details.update(identity or {})
        try:
            self.store.begin_attempt(benchmark.name, benchmark.revision, index, fingerprint, attempt_id=attempt_id, state="prepared")
            self.store.set(f"attempt:{attempt_id}", details)
            self.store.set(f"reservation:{attempt_id}", {"bytes": reservation, "baseline_usage": _disk_usage(self.autobench)})
            self.store.set_attempt(attempt_id, "submit_intent")
        except Exception:
            shutil.rmtree(staged, ignore_errors=True)
            packet_manifest.unlink(missing_ok=True)
            raise
        spec: dict[str, Any] = {
            "owner_id": owner_id,
            "argv": [sys.executable, "-m", "autobench.worker", str(packet_manifest)],
            "working_directory": str(self.project),
            "resources": resource_requirements,
            "tags": ["autobench", f"benchmark:{benchmark.name}", f"kind:{kind}", *resource_tags],
            "priority_class": "background", "priority": 10,
            "flexible": True,
            "timeout_seconds": command.kill_seconds + 10,
            "expected_duration_seconds": self._expected_duration(benchmark, index, command, identity or {}),
            "kill_timeout_seconds": command.kill_seconds,
            "termination_grace_seconds": 5,
            "preemption_action": "abandon",
            "max_attempts": 1,
            "idempotency_key": job_idempotency,
            "retention_policy": "autobench",
        }
        if any(item.get("kind") == "gpu" for item in resource_requirements):
            spec["preconditions"] = [{"name": "gpu.quiescence", "schema_version": 1,
                                      "parameters": {"timeout_seconds": 30, "interval_seconds": 1,
                                                     "consecutive_samples": 2, "max_utilization_percent": 0}}]
        try:
            response = await self.scheduler.call("jobs.submit", {"spec": spec})
        except Exception as exc:
            # Submission may have succeeded remotely. Keep intent and recover via idempotency key.
            self.store.set_attempt(attempt_id, "submit_intent", error=f"submission outcome unknown: {exc}")
            raise SchedulerUnavailable("scheduler submission outcome is unknown; inspect the durable idempotency key before retry") from exc
        job = job_object(response)
        job_id = job.get("id", job.get("job_id"))
        if not isinstance(job_id, str):
            self.store.set_attempt(attempt_id, "submit_intent", error="scheduler returned no durable job id")
            raise SchedulerUnavailable("scheduler accepted no identifiable durable job")
        self.store.set_job(attempt_id, job_id)

    def _reserved_bytes(self, current_usage: int | None = None) -> int:
        current_usage = _disk_usage(self.autobench) if current_usage is None else current_usage
        total = 0
        for row in self.store.db.execute("SELECT value FROM settings WHERE key LIKE 'reservation:%'"):
            try:
                value = json.loads(row[0])
                if isinstance(value, int) and value > 0:
                    total += value
                elif isinstance(value, dict):
                    amount = value.get("bytes", 0)
                    baseline = value.get("baseline_usage", current_usage)
                    if isinstance(amount, int) and amount > 0 and isinstance(baseline, int):
                        spent = max(0, current_usage - baseline)
                        total += max(0, amount - spent)
            except (TypeError, json.JSONDecodeError):
                continue
        return total

    async def _reconcile_jobs(self) -> int:
        count = 0
        for attempt in self.store.unresolved():
            if not attempt["job_id"]:
                # Recover an accepted submit by its stable idempotency key. An empty or
                # incomplete list does not prove non-acceptance, so never retry here.
                details = self.store.get(f"attempt:{attempt['id']}", {})
                listing = await self.scheduler.call("jobs.list", {"limit": 100})
                jobs = listing.get("jobs", listing.get("items", []))
                if isinstance(jobs, list):
                    found = next((j for j in jobs if isinstance(j, dict) and
                                  (j.get("idempotency_key") == details.get("idempotency_key") or
                                   (isinstance(j.get("spec"), dict) and j["spec"].get("idempotency_key") == details.get("idempotency_key")))), None)
                else:
                    found = None
                if isinstance(found, dict):
                    job_id = found.get("id", found.get("job_id"))
                    if isinstance(job_id, str):
                        self.store.set_job(attempt["id"], job_id)
                continue
            response = await self.scheduler.call("jobs.get", {"job_id": attempt["job_id"]})
            job = job_object(response)
            if not terminal(job):
                self.store.set_attempt(attempt["id"], "running" if str(job.get("state", "")).lower() in {"running", "active"} else "submitted")
                usage = _disk_usage(self.autobench)
                if (_disk_usage(self.autobench / "scratch" / attempt["id"]) > self.limits["scratch_limit_bytes"] or
                    usage + self._reserved_bytes(usage) > min(int(self.store.get("retained_cap_bytes", 1024**3)), self.limits["retained_cap_bytes"])) and not self.store.get(f"cancel_requested:{attempt['id']}", False):
                    await self.scheduler.call("jobs.control", {"job_id": attempt["job_id"], "action": "cancel"})
                    self.store.set(f"cancel_requested:{attempt['id']}", True)
                continue
            if not cleanup_verified(job):
                self.store.set_attempt(attempt["id"], "cleanup_unknown", result={"scheduler_job": _compact_scheduler_job(dict(job))}, error="terminal scheduler job lacks verified cleanup receipt")
                continue
            details = self.store.get(f"attempt:{attempt['id']}", {})
            staged = Path(details.get("staged", ""))
            result_path = staged / "result.json"
            result = json.loads(result_path.read_text()) if result_path.exists() else {"status": "missing_result"}
            scheduler_state = str(job.get("state", "")).casefold()
            scheduler_attempt = job.get("attempt") if isinstance(job.get("attempt"), dict) else {}
            scheduler_attempt_result = scheduler_attempt.get("result") if isinstance(scheduler_attempt.get("result"), dict) else {}
            scheduler_result = job.get("result") if isinstance(job.get("result"), dict) else {}
            preemption = scheduler_attempt.get("preemption") if isinstance(scheduler_attempt.get("preemption"), dict) else {}
            cancellation_reasons = (scheduler_attempt_result.get("cancellation_reason", ""),
                                    scheduler_result.get("cancellation_reason", ""))
            blocked_reason = job.get("blocked_reason", "")
            preempted = (scheduler_state == "preempted" or bool(preemption) or
                         any(isinstance(reason, str) and reason.startswith(("preempted_for:", "priority_lease_demand:"))
                             for reason in cancellation_reasons) or
                         (isinstance(blocked_reason, str) and blocked_reason.startswith("preempted_for:")))
            success = scheduler_state in {"succeeded", "completed"} and result.get("status") == "succeeded"
            if success and details.get("kind") != "build":
                try:
                    _validate_staged_outputs(staged)
                except AutobenchError as exc:
                    result["status"] = "invalid_output"
                    result["error"] = str(exc)
                    success = False
            if not success:
                result = self._failure_diagnostics(result, staged, dict(job))
            if success and details.get("kind") == "build":
                build = Path(details["build"])
                build.mkdir(parents=True, exist_ok=True)
                (build / "SUCCESS.json").write_text(json.dumps({"fingerprint": details["fingerprint"], "attempt": attempt["id"]}), encoding="utf-8")
                output_identity = _build_output_identity(build)
                if output_identity is None:
                    failed_result = dict(result)
                    failed_result["status"] = "build_output_missing"
                    failed_result["error"] = "successful build produced no files"
                    failed_result = self._failure_diagnostics(failed_result, staged, dict(job))
                    self.store.set_attempt(attempt["id"], "failed",
                                           result={"scheduler_job": _compact_scheduler_job(dict(job)), "worker": failed_result, "cleanup_verified": True},
                                           error="successful build produced no files")
                    shutil.rmtree(build, ignore_errors=True)
                    shutil.rmtree(staged, ignore_errors=True)
                    Path(details.get("manifest", "")).unlink(missing_ok=True)
                    self.store.delete_setting(f"attempt:{attempt['id']}")
                    self.store.delete_setting(f"reservation:{attempt['id']}")
                    self.store.compact_history(details["benchmark"], -1, 16)
                    continue
                (build / "SUCCESS.json").write_text(json.dumps({"fingerprint": details["fingerprint"], "output_identity": output_identity, "benchmark": details["benchmark"], "attempt": attempt["id"]}), encoding="utf-8")
                self.store.set(f"last-good-build:{details['benchmark']}", str(build))
                state = "build_succeeded"
            elif success:
                benchmark = details["benchmark"]
                index = int(details["packet_index"])
                self._publish_result(benchmark, index, details["fingerprint"], attempt["id"], staged, details, job)
                self._evict_safe()
                cap = min(int(self.store.get("retained_cap_bytes", 1024**3)), self.limits["retained_cap_bytes"])
                if _disk_usage(self.autobench) <= cap:
                    self.store.mark_current(benchmark, index, details["fingerprint"], attempt["id"])
                    state = "succeeded"
                else:
                    state = "storage_over_cap"
            else:
                state = "preempted" if preempted else result.get("status", "failed")
                if state == "storage_over_cap":
                    failure_record = {
                        "fingerprint": details.get("fingerprint"),
                        "cap": min(int(self.store.get("retained_cap_bytes", 1024**3)), self.limits["retained_cap_bytes"]),
                        "scratch_limit": self.limits["scratch_limit_bytes"],
                    }
                    if details.get("kind") == "build":
                        # The worker stopped this admitted build command itself;
                        # verified Scheduler cleanup now allows deleting its partial cache.
                        shutil.rmtree(Path(details["build"]), ignore_errors=True)
                        self.store.set(f"build-storage-error:{details['benchmark']}", failure_record)
                    else:
                        self.store.set(f"packet-storage-error:{details['benchmark']}:{int(details['packet_index'])}", failure_record)
            device_identity = _device_identity(job)
            protocol_fingerprint = details.get("protocol_fingerprint")
            if not isinstance(protocol_fingerprint, str):
                protocol_fingerprint = _protocol_fingerprint(details.get("protocol", {}))
            diagnostic_error = None
            if state not in {"succeeded", "build_succeeded", "preempted"}:
                diagnostic_error = str(result.get("error") or f"worker result: {result.get('status', state)}")
                stderr_tail = result.get("diagnostics", {}).get("stderr_tail") if isinstance(result.get("diagnostics"), dict) else None
                if isinstance(stderr_tail, str) and stderr_tail:
                    diagnostic_error += ": " + _bounded_tail(stderr_tail, 1024)[0]
            self.store.set_attempt(attempt["id"], state, {"job": _compact_scheduler_job(dict(job)), "worker": result,
                                                            "identity": {"protocol_fingerprint": protocol_fingerprint,
                                                                         "source_fingerprint": details.get("source_fingerprint"),
                                                                         "build_fingerprint": details.get("build_fingerprint"),
                                                                         "data_fingerprint": details.get("data_fingerprint"),
                                                                         "device_identity": device_identity}},
                                   error=diagnostic_error)
            self.store.compact_history(details["benchmark"], int(details.get("packet_index", -1)), 16)
            shutil.rmtree(staged, ignore_errors=True)
            Path(details.get("manifest", "")).unlink(missing_ok=True)
            self._evict_safe()
            if details.get("kind") == "build":
                cap = min(int(self.store.get("retained_cap_bytes", 1024**3)), self.limits["retained_cap_bytes"])
                if _disk_usage(self.autobench) > cap or result.get("status") == "storage_over_cap":
                    build = Path(details["build"])
                    shutil.rmtree(build, ignore_errors=True)
                    self.store.set(f"build-storage-error:{details['benchmark']}", {"fingerprint": details["fingerprint"], "cap": cap,
                                                                                     "scratch_limit": self.limits["scratch_limit_bytes"]})
                    self.store.set_attempt(attempt["id"], "storage_over_cap", result={"scheduler_job": _compact_scheduler_job(dict(job)), "cleanup_verified": True}, error="build exceeded its storage budget")
                else:
                    self.store.delete_setting(f"build-storage-error:{details['benchmark']}")
            self.store.delete_setting(f"attempt:{attempt['id']}")
            self.store.delete_setting(f"reservation:{attempt['id']}")
            count += 1
        return count

    def _build_is_current(self, build: Path, fingerprint: str) -> bool:
        try:
            success = json.loads((build / "SUCCESS.json").read_text(encoding="utf-8"))
            return success.get("fingerprint") == fingerprint and success.get("output_identity") == _build_output_identity(build)
        except (OSError, json.JSONDecodeError):
            return False

    def _current_artifact(self, benchmark: str, packet_index: int) -> Path:
        target = self.autobench / "evidence" / benchmark / str(packet_index)
        pointer = json.loads((target / "current.json").read_text(encoding="utf-8"))
        return target / str(pointer["fingerprint"])

    def _publish_result(self, benchmark: str, packet_index: int, fingerprint: str, attempt_id: str, staged: Path, details: dict[str, Any], job: dict[str, Any]) -> None:
        target = self.autobench / "evidence" / benchmark / str(packet_index)
        target.mkdir(parents=True, exist_ok=True)
        current_file = target / "current.json"
        previous = json.loads(current_file.read_text()) if current_file.exists() else None
        new = target / f"{fingerprint}.tmp"
        shutil.rmtree(new, ignore_errors=True)
        shutil.copytree(staged, new)
        final = target / fingerprint
        shutil.rmtree(final, ignore_errors=True)
        new.replace(final)
        worker_result = json.loads((final / "result.json").read_text())
        metrics_file = final / "metrics.json"
        metrics = json.loads(metrics_file.read_text()) if metrics_file.exists() else {}
        manifest_ref = None
        if details.get("source_fingerprint") and details.get("source_manifest"):
            manifest_path = self.state / "manifests" / f"{details['source_fingerprint']}.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            if not manifest_path.exists():
                temporary = manifest_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(details["source_manifest"], sort_keys=True, separators=(",", ":")), encoding="utf-8")
                temporary.replace(manifest_path)
            manifest_ref = str(manifest_path.relative_to(self.autobench))
        device = _device_identity(job)
        pointer = {"fingerprint": fingerprint, "attempt": attempt_id, "result": worker_result, "metrics": metrics,
                   "source_fingerprint": details.get("source_fingerprint"), "source_manifest_ref": manifest_ref,
                   "data_fingerprint": details.get("data_fingerprint"), "data_manifest": details.get("data_manifest"),
                   "protocol": details.get("protocol", {}),
                   "protocol_fingerprint": details.get("protocol_fingerprint") or _protocol_fingerprint(details.get("protocol", {})),
                   "build_fingerprint": details.get("build_fingerprint"), "build_output_identity": details.get("build_output_identity"),
                   "device_identity": device, "scheduler_job_id": job.get("id", job.get("job_id")),
                   "cleanup_verified": job.get("cleanup_verified") is True}
        if previous and previous.get("fingerprint") != fingerprint:
            (target / "baseline.json").write_text(json.dumps(previous, indent=2), encoding="utf-8")
        current_file.write_text(json.dumps(pointer, indent=2), encoding="utf-8")
        keep_artifacts = {fingerprint}
        if previous and previous.get("fingerprint"):
            keep_artifacts.add(str(previous["fingerprint"]))
        for artifact in target.iterdir():
            if artifact.is_dir() and artifact.name not in keep_artifacts:
                shutil.rmtree(artifact, ignore_errors=True)
        # Keep current + one prior baseline and compact summaries for 16 attempts.
        rows = self.store.attempts_for(benchmark)
        keep = {r["id"] for r in rows[:16]}
        for row in rows:
            if row["id"] not in keep and row.get("result_json"):
                self.store.db.execute("UPDATE attempts SET result_json=NULL WHERE id=?", (row["id"],))
        self.store.db.commit()

    def _evict_safe(self, required_free: int = 0) -> None:
        # Snapshot cache is reproducible from live source. Preserve snapshots referenced by
        # unresolved jobs and the last registered source fingerprints.
        pinned: set[str] = set()
        cap = min(int(self.store.get("retained_cap_bytes", 1024**3)), self.limits["retained_cap_bytes"])
        target = max(0, cap - max(0, required_free))
        for row in self.store.db.execute("SELECT key,value FROM settings WHERE key LIKE 'last-good-build:%'"):
            try:
                path = json.loads(row["value"])
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(path, str):
                pinned.add(path)
        for attempt in self.store.unresolved():
            meta = self.store.get(f"attempt:{attempt['id']}", {})
            snapshot = meta.get("snapshot")
            if snapshot:
                pinned.add(snapshot)
                build = meta.get("build")
                if build:
                    pinned.add(build)
        snapshots = self.state / "snapshots"
        if snapshots.exists():
            dirs = sorted((d for d in snapshots.iterdir() if d.is_dir()), key=lambda p: p.stat().st_mtime)
            for directory in dirs:
                if str(directory) not in pinned and _disk_usage(self.autobench) > target * 4 // 5:
                    shutil.rmtree(directory, ignore_errors=True)
        # Verified builds are reproducible from their registered inputs. Keep those
        # referenced by current or baseline evidence and active work; evict older caches.
        evidence = self.autobench / "evidence"
        if evidence.exists():
            for pointer_path in evidence.rglob("*.json"):
                if pointer_path.name not in {"current.json", "baseline.json"}:
                    continue
                try:
                    identity = json.loads(pointer_path.read_text()).get("build_fingerprint")
                except (OSError, json.JSONDecodeError):
                    identity = None
                if identity:
                    pinned.add(str(self.autobench / "builds" / identity))
        builds = self.autobench / "builds"
        if builds.exists():
            for directory in sorted((p for p in builds.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime):
                if str(directory) not in pinned and _disk_usage(self.autobench) > target * 4 // 5:
                    shutil.rmtree(directory, ignore_errors=True)
        # Under pressure, preserve compact current/baseline metrics and provenance
        # while dropping bulky profiler captures. Raw capture eviction is explicit.
        if _disk_usage(self.autobench) > target:
            for pointer_path in sorted(evidence.rglob("*.json")) if evidence.exists() else []:
                if pointer_path.name not in {"current.json", "baseline.json"}:
                    continue
                try:
                    pointer = json.loads(pointer_path.read_text())
                    artifact = pointer_path.parent / str(pointer.get("fingerprint", ""))
                    if not artifact.is_dir():
                        continue
                    for candidate in artifact.rglob("*"):
                        if candidate.is_file() and candidate.name not in {"result.json", "metrics.json", "stdout.log", "stderr.log"}:
                            candidate.unlink(missing_ok=True)
                    pointer["raw_artifacts_evicted"] = True
                    pointer_path.write_text(json.dumps(pointer, indent=2), encoding="utf-8")
                except (OSError, json.JSONDecodeError):
                    continue
                if _disk_usage(self.autobench) <= target:
                    break

    async def run(self) -> None:
        while True:
            try:
                await self.reconcile_once()
            except (SchedulerUnavailable, OSError):
                pass
            # Observe active packet storage every 500 ms and cancel through Scheduler.
            checks = max(1, int(POLL_SECONDS * 1000 / self.limits["scratch_poll_ms"]))
            for _ in range(checks):
                await asyncio.sleep(self.limits["scratch_poll_ms"] / 1000)
                for attempt in self.store.unresolved():
                    over_scratch = _disk_usage(self.autobench / "scratch" / attempt["id"]) > self.limits["scratch_limit_bytes"]
                    usage = _disk_usage(self.autobench)
                    over_cap = usage + self._reserved_bytes(usage) > min(int(self.store.get("retained_cap_bytes", 1024**3)), self.limits["retained_cap_bytes"])
                    if attempt["job_id"] and (over_scratch or over_cap) and not self.store.get(f"cancel_requested:{attempt['id']}", False):
                        try:
                            await self.scheduler.call("jobs.control", {"job_id": attempt["job_id"], "action": "cancel"})
                            self.store.set(f"cancel_requested:{attempt['id']}", True)
                        except Exception:
                            pass

