"""Scheduler-launched worker. This is the only layer that starts registered argv."""
from __future__ import annotations

import json
import os
import resource
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

LOG_LIMIT = 256 * 1024


def _write_bounded(path: Path, content: bytes) -> None:
    path.write_bytes(content[:LOG_LIMIT])


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
    result_dir = Path(spec["result_directory"]).resolve()
    result_dir.mkdir(parents=True, exist_ok=True)
    output_limit = int(spec.get("output_limit_bytes", 64 * 1024 * 1024))
    if output_limit <= 0:
        raise ValueError("output_limit_bytes must be positive")
    preexec = None
    if os.name == "posix":
        def limits() -> None:
            resource.setrlimit(resource.RLIMIT_FSIZE, (output_limit, output_limit))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        preexec = limits
    started = time.monotonic()
    # Direct output to bounded files, avoiding unbounded in-memory PIPE capture.
    with (result_dir / "stdout.log").open("wb") as stdout, (result_dir / "stderr.log").open("wb") as stderr:
        child_env = {**os.environ, **spec.get("environment", {})}
        worker_bin = str(Path(sys.executable).resolve().parent)
        child_env["PATH"] = worker_bin + os.pathsep + child_env.get("PATH", "")
        child_env["AUTOBENCH_SCHEDULER_JOB_ID"] = scheduler_job_id
        child_env["AUTOBENCH_SCHEDULER_ATTEMPT_ID"] = scheduler_attempt_id
        proc = subprocess.Popen(argv, cwd=cwd, env=child_env, stdout=stdout, stderr=stderr, preexec_fn=preexec, start_new_session=(os.name == "posix"))
        try:
            code = proc.wait(timeout=int(spec["kill_seconds"]))
            status = "succeeded" if code == 0 else "failed"
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
            proc.wait()
            code = 124
            status = "timed_out"
    stdout_bytes = (result_dir / "stdout.log").stat().st_size
    stderr_bytes = (result_dir / "stderr.log").stat().st_size
    result: dict[str, Any] = {"status": status, "returncode": code, "duration_seconds": time.monotonic() - started,
                              "scheduler_job_id": scheduler_job_id, "scheduler_attempt_id": scheduler_attempt_id,
                              "stdout_bytes": stdout_bytes, "stderr_bytes": stderr_bytes,
                              "stdout_truncated": stdout_bytes >= output_limit, "stderr_truncated": stderr_bytes >= output_limit}
    (result_dir / "result.json").write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
    return 0


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m autobench.worker PACKET.json")
    raise SystemExit(execute(Path(sys.argv[1])))


if __name__ == "__main__":
    main()

