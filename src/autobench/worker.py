"""Scheduler-launched worker. This is the only layer that starts registered argv."""
from __future__ import annotations

import json
import os
import resource
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

LOG_LIMIT = 256 * 1024
MONITOR_INTERVAL_SECONDS = 0.5


def _write_bounded(path: Path, content: bytes) -> None:
    path.write_bytes(content[:LOG_LIMIT])


def _disk_usage(path: Path) -> int:
    total = 0
    if not path.exists():
        return total
    for root, dirs, files in os.walk(path, followlinks=False):
        base = Path(root)
        dirs[:] = [name for name in dirs if not (base / name).is_symlink()]
        for name in files:
            candidate = base / name
            try:
                if not candidate.is_symlink():
                    total += candidate.stat().st_size
            except OSError:
                continue
    return total


def _kill_owned_child(proc: subprocess.Popen[Any]) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass


def _scoped_child_environment(spec: dict[str, Any], inherited: dict[str, str], result_dir: Path) -> tuple[dict[str, str], list[Path]]:
    """Route normal tool temp/cache writes into monitored attempt scratch.

    Explicit registration values take precedence when they resolve inside this
    attempt directory. A registration cannot redirect these managed locations
    outside the storage budget being monitored by the worker.
    """
    registered = spec.get("environment", {})
    if not isinstance(registered, dict):
        raise ValueError("packet environment must be an object")
    env = dict(inherited)
    env.update(registered)
    defaults = {
        "TMPDIR": result_dir / "tmp",
        "TMP": result_dir / "tmp",
        "TEMP": result_dir / "tmp",
        "XDG_CACHE_HOME": result_dir / "cache",
        "CUDA_CACHE_PATH": result_dir / "cache" / "cuda",
    }
    root = result_dir.resolve()
    owned: set[Path] = set()
    for name, default in defaults.items():
        raw = registered.get(name, str(default))
        if not isinstance(raw, str) or not raw:
            raise ValueError(f"packet environment {name} must be a nonempty path")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = result_dir / path
        resolved = path.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"packet environment {name} must stay inside its monitored attempt directory")
        resolved.mkdir(parents=True, exist_ok=True)
        if resolved.is_symlink():
            raise ValueError(f"packet environment {name} cannot use a symlink")
        env[name] = str(resolved)
        owned.add(resolved)
    top_level = [path for path in owned if not any(parent in owned for parent in path.parents if parent != root)]
    return env, sorted(top_level)


def execute(manifest_path: Path) -> int:
    scheduler_job_id = os.environ.get("DEVICE_SCHEDULER_JOB_ID")
    scheduler_attempt_id = os.environ.get("DEVICE_SCHEDULER_ATTEMPT_ID")
    if not scheduler_job_id or not scheduler_attempt_id:
        raise RuntimeError("Autobench worker must run inside an admitted Device Scheduler job attempt")
    spec = json.loads(manifest_path.read_text(encoding="utf-8"))
    argv = spec["argv"]
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
        raise ValueError("invalid packet argv")
    cwd = Path(spec["working_directory"]).resolve()
    allowed = Path(spec["allowed_root"]).resolve()
    if cwd != allowed and allowed not in cwd.parents:
        raise ValueError("packet working directory escaped its snapshot")
    managed_root_input = Path(spec["managed_root"])
    expected_managed = allowed / ".autobench"
    if expected_managed.is_symlink():
        raise ValueError("managed Autobench root must not be a symlink")
    managed_root = managed_root_input.resolve()
    if managed_root != expected_managed.resolve() or allowed not in managed_root.parents:
        raise ValueError("managed storage root escaped the owning project")
    scratch_input = managed_root / "scratch"
    if scratch_input.is_symlink():
        raise ValueError("managed scratch root must not be a symlink")
    result_input = Path(spec["result_directory"])
    if result_input.is_symlink():
        raise ValueError("packet result directory must not be a symlink")
    result_dir = result_input.resolve()
    scratch_root = (managed_root / "scratch").resolve()
    if result_dir == scratch_root or scratch_root not in result_dir.parents:
        raise ValueError("packet scratch directory escaped managed Autobench scratch")
    result_dir.mkdir(parents=True, exist_ok=True)
    output_limit = int(spec.get("output_limit_bytes", 64 * 1024 * 1024))
    scratch_limit = int(spec["scratch_limit_bytes"])
    retained_cap = int(spec["retained_cap_bytes"])
    if output_limit <= 0 or scratch_limit <= 0 or retained_cap <= 0:
        raise ValueError("worker storage limits must be positive")
    preexec = None
    if os.name == "posix":
        def limits() -> None:
            resource.setrlimit(resource.RLIMIT_FSIZE, (output_limit, output_limit))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        preexec = limits
    started = time.monotonic()
    deadline = started + int(spec["kill_seconds"])
    storage_overrun: dict[str, Any] | None = None
    # Direct output to bounded files, avoiding unbounded in-memory PIPE capture.
    with (result_dir / "stdout.log").open("wb") as stdout, (result_dir / "stderr.log").open("wb") as stderr:
        child_env, ephemeral_paths = _scoped_child_environment(spec, os.environ, result_dir)
        # Keep the venv's lexical bin path. Resolving this symlink collapses it
        # to the base interpreter directory and makes child `python3` bypass
        # the installed Autobench/runtime environment.
        worker_bin = str(Path(sys.executable).absolute().parent)
        child_env["PATH"] = worker_bin + os.pathsep + child_env.get("PATH", "")
        child_env["AUTOBENCH_SCHEDULER_JOB_ID"] = scheduler_job_id
        child_env["AUTOBENCH_SCHEDULER_ATTEMPT_ID"] = scheduler_attempt_id
        proc = subprocess.Popen(argv, cwd=cwd, env=child_env, stdout=stdout, stderr=stderr, preexec_fn=preexec, start_new_session=(os.name == "posix"))
        code: int | None = None
        status = "failed"
        while proc.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_owned_child(proc)
                proc.wait()
                code = 124
                status = "timed_out"
                break
            try:
                code = proc.wait(timeout=min(MONITOR_INTERVAL_SECONDS, remaining))
                status = "succeeded" if code == 0 else "failed"
                break
            except subprocess.TimeoutExpired:
                scratch_bytes = _disk_usage(result_dir)
                retained_bytes = _disk_usage(managed_root)
                if scratch_bytes > scratch_limit or retained_bytes > retained_cap:
                    storage_overrun = {"scratch_bytes": scratch_bytes, "scratch_limit_bytes": scratch_limit,
                                       "retained_bytes": retained_bytes, "retained_cap_bytes": retained_cap}
                    _kill_owned_child(proc)
                    proc.wait()
                    code = 125
                    status = "storage_over_cap"
                    break
        if code is None:
            code = proc.returncode if proc.returncode is not None else 1
            status = "succeeded" if code == 0 else "failed"
        if status == "succeeded":
            scratch_bytes = _disk_usage(result_dir)
            retained_bytes = _disk_usage(managed_root)
            if scratch_bytes > scratch_limit or retained_bytes > retained_cap:
                storage_overrun = {"scratch_bytes": scratch_bytes, "scratch_limit_bytes": scratch_limit,
                                   "retained_bytes": retained_bytes, "retained_cap_bytes": retained_cap}
                status = "storage_over_cap"
                code = 125
        for path in ephemeral_paths:
            if path.is_symlink():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path)
    stdout_bytes = (result_dir / "stdout.log").stat().st_size
    stderr_bytes = (result_dir / "stderr.log").stat().st_size
    result: dict[str, Any] = {"status": status, "returncode": code, "duration_seconds": time.monotonic() - started,
                              "scheduler_job_id": scheduler_job_id, "scheduler_attempt_id": scheduler_attempt_id,
                              "stdout_bytes": stdout_bytes, "stderr_bytes": stderr_bytes,
                              "stdout_truncated": stdout_bytes >= output_limit, "stderr_truncated": stderr_bytes >= output_limit}
    if storage_overrun is not None:
        result["storage"] = storage_overrun
    result_path = result_dir / "result.json"
    result_path.write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
    final_scratch_bytes = _disk_usage(result_dir)
    final_retained_bytes = _disk_usage(managed_root)
    if final_scratch_bytes > scratch_limit or final_retained_bytes > retained_cap:
        result["status"] = "storage_over_cap"
        result["returncode"] = 125
        result["storage"] = {"scratch_bytes": final_scratch_bytes, "scratch_limit_bytes": scratch_limit,
                             "retained_bytes": final_retained_bytes, "retained_cap_bytes": retained_cap}
        result_path.write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
    return 0


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m autobench.worker PACKET.json")
    raise SystemExit(execute(Path(sys.argv[1])))


if __name__ == "__main__":
    main()

