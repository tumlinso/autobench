#!/usr/bin/env python3
"""Collect one focused SM70 NCU report for the existing worksets kernel."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

from result_utils import sha256, write_metrics


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ncu", default="ncu", help="explicit installed NCU executable")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--fixture-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not os.environ.get("AUTOBENCH_SCHEDULER_JOB_ID", "").strip():
        parser.error("profiling requires an Autobench worker Scheduler job context")
    ncu = str(Path(args.ncu).resolve()) if "/" in args.ncu else args.ncu
    version = subprocess.run([ncu, "--version"], text=True, capture_output=True, check=True)
    if not re.search(r"2025\.2(?:\.|\s|$)", version.stdout + version.stderr):
        parser.error("V100 profiling requires the configured Nsight Compute 2025.2.x release")
    for option in ("--set", "--import-source", "--clock-control"):
        help_result = subprocess.run([ncu, "--help"], text=True, capture_output=True, check=True)
        if option not in help_result.stdout + help_result.stderr:
            parser.error(f"configured NCU does not advertise required option {option}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    capture_dir = args.output_dir / "ncu"
    capture_dir.mkdir(parents=True, exist_ok=True)
    report = capture_dir / "stage_worksets_kernel.ncu-rep"
    runner = Path(__file__).with_name("run_native.py")
    result_path = capture_dir / "native-result.json"
    command = [sys.executable, str(runner), "--source", str(args.source),
               "--binary", str(args.binary), "--fixture-dir", str(args.fixture_dir),
               "--output", str(result_path), "--mode", "timing", "--warmup", "0",
               "--samples", "3", "--iterations", "1"]
    # The validated Python runner launches the native benchmark as a child.
    # Capture that process as well as the Python entrypoint itself.
    argv = [ncu, "--target-processes", "all", "--set", "full", "--import-source", "yes", "--clock-control", "base",
            "-k", "regex:stage_worksets_kernel", "-c", "1", "-o", str(report), *command]
    with (capture_dir / "ncu-command.txt").open("w", encoding="utf-8") as stream:
        stream.write("\n".join(argv) + "\n")
    proc = subprocess.run(argv, text=True, capture_output=True, check=False)
    (capture_dir / "ncu.stdout.log").write_text(proc.stdout, encoding="utf-8")
    (capture_dir / "ncu.stderr.log").write_text(proc.stderr, encoding="utf-8")
    if proc.returncode:
        return proc.returncode
    if not report.is_file() or not result_path.is_file():
        raise RuntimeError("NCU completed without the expected report and native result")
    write_metrics(args.output_dir, "autobench.cellerator-sparse-support.ncu-capture.v1", {
        "capture": {"path_name": report.name, "sha256": sha256(report),
                    "bytes": report.stat().st_size},
        "tool": {"executable": ncu, "version": (version.stdout + version.stderr).strip()},
        "target": {"kernel_regex": "stage_worksets_kernel", "launch_count": 1,
                   "metric_set": "full", "source_import": True,
                   "clock_control": "base", "architecture": "sm_70"},
        "native_result": "native-result.json",
    })
    print(f"captured {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
