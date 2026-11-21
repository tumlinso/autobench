from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any, TYPE_CHECKING
import uuid

from .errors import AutobenchError, SchedulerUnavailable
from .models import Benchmark, Command
from .snapshot import capture

if TYPE_CHECKING:
    from .engine import Engine

ACTIVE_ATTEMPTS = {"prepared", "submit_intent", "submitted", "running", "cleanup_unknown"}
WAIT_INTERVAL_SECONDS = 0.5
CLEANUP_SECONDS = 30


class RunBlocked(AutobenchError):
    def __init__(self, code: str, reason: str, next_action: str, *, phase: str = "acceptance"):
        super().__init__(reason)
        self.result = {"state": "blocked", "phase": phase, "reason_code": code, "reason": reason,
                       "next_action": next_action}


def _benchmark(request: dict[str, Any], root: Path) -> Benchmark:
    frozen = dict(request["benchmark"])
    frozen["root"] = root
    frozen["build"] = Command(**frozen["build"]) if frozen["build"] else None
    frozen["packets"] = tuple(Command(**packet) for packet in frozen["packets"])
    return Benchmark(**frozen)


def run_evidence(engine: Engine, run_id: str, packet: str = "") -> Path:
    return engine.autobench / "evidence" / "runs" / run_id / packet


async def start_run(engine: Engine, benchmark: Benchmark, *, foreground: bool = False,
                    admission_seconds: int = 60) -> str:
    from .engine import _autobench_root, _build_fingerprint, _data_manifest, _disk_usage, _recipe_hash

    if type(admission_seconds) is not int or not 1 <= admission_seconds <= 3600:
        raise RunBlocked("invalid_admission_budget", "Admission seconds must be between 1 and 3600.",
                         "Set --admission-seconds to a value in that range.")
    if not benchmark.enabled:
        raise RunBlocked("benchmark_disabled", f"{benchmark.name} is disabled.",
                         "Enable the registration or use a different recipe.")
    if engine.store.get("paused", False):
        raise RunBlocked("workspace_paused", "Autobench submissions are paused for this workspace.",
                         "Run autobench resume for this workspace.")
    if not engine.config.get("active", False):
        raise RunBlocked("workspace_inactive", "This workspace is not enrolled for execution.",
                         "Run autobench resume for this workspace.")
    if foreground:
        try:
            grant = await engine.scheduler.call("access.inspect", {})
        except Exception as exc:
            raise RunBlocked("scheduler_unavailable", str(exc),
                             "Check the configured Scheduler connection and retry.") from exc
        priority = grant.get("max_priority")
        if type(priority) is not int or priority < 80:
            raise RunBlocked("foreground_denied", "The configured Scheduler principal lacks interactive priority.",
                             "Run without --foreground or use a principal already granted interactive priority.")

    until = time.monotonic() + 10
    while True:
        with engine._workspace_writer_lock("capture.lock") as acquired:
            if acquired:
                if engine.store.get("paused", False):
                    raise RunBlocked("workspace_paused", "Autobench submissions were paused.",
                                     "Run autobench resume for this workspace.")
                try:
                    snapshot = capture(benchmark, engine.state)
                    root = _autobench_root(engine.config)
                    recipes = {packet.name: _recipe_hash(packet, root) for packet in benchmark.packets}
                    if benchmark.build:
                        recipes["@build"] = _recipe_hash(benchmark.build, root)
                except (OSError, AutobenchError) as exc:
                    raise RunBlocked("snapshot_failed", str(exc),
                                     "Check declared inputs and retry once the workspace is stable.") from exc
                if not snapshot.manifest:
                    raise RunBlocked("inputs_missing", "The declared input paths matched no files.",
                                     "Declare existing workspace inputs with --include or --data.")
                engine._evict_safe_locked(extra_pins={str(snapshot.directory)})
                cap = min(int(engine.store.get("retained_cap_bytes", 1024**3)),
                          engine.limits["retained_cap_bytes"])
                if _disk_usage(engine.autobench) > cap:
                    engine._evict_safe_locked()
                    raise RunBlocked("storage_over_cap", "The frozen inputs exceed available benchmark storage.",
                                     "Reduce declared inputs or free space within the configured cap.")
                run_id = str(uuid.uuid4())
                frozen = asdict(benchmark)
                del frozen["root"]
                data = _data_manifest(benchmark, snapshot.manifest)
                engine.store.begin_run(run_id, {
                    "benchmark": frozen, "snapshot": str(snapshot.directory),
                    "source_fingerprint": snapshot.fingerprint, "source_manifest": snapshot.manifest,
                    "recipes": recipes, "foreground": foreground, "admission_seconds": admission_seconds,
                    "build_fingerprint": _build_fingerprint(benchmark, snapshot.fingerprint, root, recipe_hash=recipes.get("@build")),
                    "data_manifest": data,
                    "data_fingerprint": hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest(),
                })
                return run_id
        if time.monotonic() >= until:
            raise RunBlocked("capture_busy", "Another input capture or storage operation is still in progress.",
                             "Retry after that workspace operation finishes.")
        await asyncio.sleep(0.1)


def _attempt_report(row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(row.get("result_json") or "{}")


def _report_for_attempt(row: dict[str, Any]) -> dict[str, Any]:
    from .engine import _bounded_tail

    report = _attempt_report(row)
    worker = report.get("worker") or {}
    state = row["state"]
    builds = row["packet_index"] == -1
    phase = report.get("phase") or ("build" if builds else "execution")
    result = {"attempt_id": row["id"], "scheduler_job_id": row["job_id"],
              "state": state, "phase": phase, "packet_index": row["packet_index"]}
    for key in ("reason_code", "reason", "next_action", "evidence"):
        if report.get(key) is not None:
            result[key] = report[key]
    if row.get("error") and "reason" not in result:
        result["reason"] = row["error"]
    if isinstance(worker.get("duration_seconds"), (int, float)):
        result["wall_seconds"] = worker["duration_seconds"]
    for key in ("stderr_tail", "stdout_tail"):
        tail = (worker.get("diagnostics") or {}).get(key)
        if isinstance(tail, str) and tail:
            result[key] = _bounded_tail(tail, 2048)[0]
    job = report.get("job") or report.get("scheduler_job") or {}
    cleanup = report.get("cleanup_verified", job.get("cleanup_verified"))
    if cleanup is not None:
        result["cleanup_verified"] = cleanup
    return result


def _fail(engine: Engine, run: dict[str, Any], code: str, reason: str, next_action: str, **details: Any) -> None:
    engine.store.set_run(run["id"], "blocked", {
        **details, "state": "blocked", "reason_code": code, "reason": reason, "next_action": next_action,
    })


def _finish_attempt_failure(engine: Engine, run: dict[str, Any], row: dict[str, Any]) -> None:
    result = _report_for_attempt(row)
    code = result.get("reason_code") or row["state"]
    result["attempt_state"] = row["state"]
    result["state"] = "blocked" if result["phase"] in {"admission", "submission", "cleanup"} else "failed"
    result["reason_code"] = code
    result.setdefault("reason", f"The benchmark packet ended in {row['state']}.")
    actions = {
        "deadline_expired": "Wait for compatible capacity or increase --admission-seconds and submit a new run.",
        "runtime_timeout": "Check the command or increase its configured hard runtime before retrying.",
        "execution_precondition_unverified": "Check GPU process, memory, and telemetry availability before retrying.",
        "permission_denied": "Use a configured principal authorized for this workspace and request.",
        "storage_over_cap": "Reduce the requested output or free space within the configured cap.",
    }
    result.setdefault("next_action", actions.get(code, "Inspect the reported diagnostics before retrying."))
    engine.store.set_run(run["id"], result["state"], result)


def _packets(engine: Engine, run: dict[str, Any], successful: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    from .api import _compact_metrics

    result = []
    for index, command in enumerate(run["request"]["benchmark"]["packets"]):
        directory = run_evidence(engine, run["id"], command["name"])
        try:
            pointer = json.loads((directory / "current.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            result.append({**_report_for_attempt(successful[index]), "name": command["name"],
                           "artifacts_evicted": True})
            continue
        artifact = directory / pointer["fingerprint"]
        item = {**_report_for_attempt(successful[index]), "name": command["name"],
                "evidence": str(artifact), "metrics": _compact_metrics(pointer["metrics"]),
                "device_identity": pointer["device_identity"]}
        from .engine import _read_file_tail
        for log in ("stdout", "stderr"):
            tail, truncated = _read_file_tail(artifact / f"{log}.log", 2048)
            if tail:
                item[f"{log}_tail"] = tail
                if truncated:
                    item[f"{log}_tail_truncated"] = True
        result.append(item)
    return result


async def advance_runs(engine: Engine, *, allow_submit: bool = True) -> dict[str, Any] | None:
    """Advance accepted runs with the same packet executor as background work."""
    from .engine import (_autobench_root, _build_output_identity, _fingerprint,
                         _measurement_protocol, _protocol_fingerprint, _recipe_hash)

    for run in engine.store.active_runs():
        request = run["request"]
        attempts = engine.store.run_attempts(run["id"])
        # An active job or uncertain cleanup can never be replaced by a new job.
        if any(row["state"] in ACTIVE_ATTEMPTS for row in attempts):
            continue
        failures = [row for row in attempts if row["state"] not in {"succeeded", "build_succeeded"}]
        if failures:
            _finish_attempt_failure(engine, run, failures[0])
            continue
        successful = {row["packet_index"]: row for row in attempts}
        benchmark = _benchmark(request, engine.project)
        if all(index in successful for index in range(len(benchmark.packets))):
            engine.store.set_run(run["id"], "succeeded", {
                "state": "succeeded", "phase": "complete", "source_fingerprint": request["source_fingerprint"],
                "packets": _packets(engine, run, successful),
            })
            continue
        if not allow_submit:
            continue
        snapshot = Path(request["snapshot"])
        if not snapshot.is_dir():
            _fail(engine, run, "snapshot_missing", "The accepted input snapshot is unavailable.",
                  "Check managed storage, then submit a new run.", phase="preparation")
            continue
        autobench_root = _autobench_root(engine.config)
        build_fp = request["build_fingerprint"]
        build = engine.autobench / "builds" / (build_fp or "no-build")
        build_current = benchmark.build and engine._build_is_current(build, build_fp or "")
        index = -1
        command = benchmark.build
        artifacts: dict[str, Path] = {}
        build_output = None
        if build_fp is None or build_current:
            if build_current:
                build_output = _build_output_identity(build)
            indices = {packet.name: i for i, packet in enumerate(benchmark.packets)}
            index, command = next((i, packet) for i, packet in enumerate(benchmark.packets)
                                  if i not in successful and all(indices[name] in successful for name in packet.depends_on))
            for name in command.depends_on:
                destination = run_evidence(engine, run["id"], name)
                pointer = json.loads((destination / "current.json").read_text(encoding="utf-8"))
                artifacts[name] = destination / pointer["fingerprint"]
        elif -1 in successful:
            _fail(engine, run, "verified_build_missing", "The verified build is no longer available.",
                  "Restore managed build storage or submit a new run.", phase="build")
            continue
        assert command is not None
        recipe = request["recipes"]["@build" if index == -1 else command.name]
        if _recipe_hash(command, autobench_root) != recipe:
            _fail(engine, run, "recipe_changed", "An installed recipe changed after the run was accepted.",
                  "Submit a new run against the updated recipe.", phase="preparation")
            continue
        source_fp = request["source_fingerprint"]
        manifest = request["source_manifest"]
        fingerprint = build_fp if index == -1 else _fingerprint(
            benchmark, source_fp, command, manifest, recipe,
            hashlib.sha256((str(build_fp) + str(build_output)).encode()).hexdigest() if build_fp else None,
        )
        assert fingerprint is not None
        # A shared build directory is never written by two jobs. Explicit
        # packet attempts use separate scratch and evidence, so repeats are safe.
        if index == -1 and any(row["packet_index"] == -1 and row["fingerprint"] == build_fp
                               for row in engine.store.unresolved()):
            continue
        protocol = _measurement_protocol(benchmark, command, recipe)
        identity = {
            "run_id": run["id"], "foreground": request["foreground"],
            "admission_seconds": request["admission_seconds"],
            "source_fingerprint": source_fp, "source_manifest": manifest,
            "data_manifest": request.get("data_manifest", {}),
            "data_fingerprint": request.get("data_fingerprint"),
            "protocol": protocol, "protocol_fingerprint": _protocol_fingerprint(protocol),
            "build_fingerprint": build_fp, "build_output_identity": build_output,
        }
        try:
            await engine._submit(benchmark, snapshot, build, command, index, fingerprint,
                                 kind="build" if index == -1 else command.kind,
                                 artifacts=artifacts, identity=identity)
        except SchedulerUnavailable:
            if engine.store.run_attempts(run["id"]) == attempts:
                _fail(engine, run, "scheduler_unavailable", "The Scheduler could not accept the packet.",
                      "Check the configured Scheduler connection, then submit a new run.", phase="submission")
            continue
        except (OSError, AutobenchError) as exc:
            _fail(engine, run, "packet_preparation_failed", str(exc),
                  "Resolve the reported preparation or storage problem and retry.", phase="preparation")
            continue
        engine.store.set_run(run["id"], "running")
        return {"state": "submitted", "submitted": 1, "run_id": run["id"], "kind": identity["protocol"]["kind"]}
    return None


def run_status(engine: Engine, run_id: str) -> dict[str, Any]:
    run = engine.store.get_run(run_id)
    if run is None:
        raise RunBlocked("run_not_found", f"No retained run has ID {run_id}.",
                         "Check the run ID and workspace.", phase="wait")
    base = {"run_id": run_id, "benchmark": run["request"]["benchmark"]["name"]}
    if run["result"] is not None:
        result = run["result"]
        for packet in result.get("packets", []):
            if packet.get("evidence") and not Path(packet["evidence"]).exists():
                packet["artifacts_evicted"] = True
        return {**base, **result}
    attempts = engine.store.run_attempts(run_id)
    report = _report_for_attempt(attempts[-1]) if attempts else {}
    if not report or report["state"] in {"succeeded", "build_succeeded"}:
        report = {"state": "queued", "phase": "dispatch"}
    return {**base, **report}


async def wait_run(engine: Engine, run_id: str, *, wait_seconds: float | None = None) -> dict[str, Any]:
    run = engine.store.get_run(run_id)
    if run is None:
        return run_status(engine, run_id)
    if wait_seconds is not None and (isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float))
                                     or not math.isfinite(wait_seconds) or wait_seconds < 0):
        raise RunBlocked("invalid_wait_budget", "Wait seconds must be a finite non-negative number.",
                         "Use --wait-seconds with a valid duration.", phase="wait")
    if wait_seconds is None:
        request = run["request"]
        recipe = request["benchmark"]
        packets = [*recipe["packets"]]
        if recipe["build"]:
            packets.append(recipe["build"])
        wait_seconds = sum(request["admission_seconds"] + packet["kill_seconds"] + CLEANUP_SECONDS
                           for packet in packets)
    until = time.monotonic() + wait_seconds
    transport_error = None
    while True:
        overview = run_status(engine, run_id)
        current = engine.store.get_run(run_id)
        if current is None or current["result"] is not None:
            return overview
        if overview["state"] == "cleanup_unknown":
            return {**overview, "state": "blocked", "phase": "cleanup",
                    "reason_code": "cleanup_unverified",
                    "reason": overview.get("reason", "Scheduler cleanup remains unverified."),
                    "next_action": f"Run autobench wait {run_id} when Scheduler resolves cleanup.",
                    "work_active": True}
        if engine.store.get("paused", False) and overview["state"] not in ACTIVE_ATTEMPTS:
            return {**overview, "state": "blocked", "reason_code": "workspace_paused",
                    "reason": "The workspace is paused before the next packet.",
                    "next_action": f"Resume the workspace, then run autobench wait {run_id}.", "work_active": False}
        remaining = until - time.monotonic()
        if remaining <= 0:
            code = overview.get("reason_code")
            reason = overview.get("reason")
            if transport_error:
                code, reason = "scheduler_unavailable", transport_error
            return {**overview, "attempt_state": overview["state"], "state": "blocked",
                    "reason_code": code or "wait_expired",
                    "reason": reason or "The client wait expired. The durable run remains queued or active.",
                    "next_action": f"Run autobench wait {run_id} to reconnect.", "work_active": True}
        try:
            await asyncio.wait_for(engine.reconcile_once(explicit_only=True), timeout=remaining)
            transport_error = None
        except TimeoutError:
            pass
        except (AutobenchError, OSError) as exc:
            transport_error = str(exc)
        if (engine.store.get_run(run_id) or {}).get("result") is not None:
            continue
        await asyncio.sleep(min(WAIT_INTERVAL_SECONDS, max(0, until - time.monotonic())))
