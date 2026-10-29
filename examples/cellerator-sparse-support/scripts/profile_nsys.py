#!/usr/bin/env python3
"""Capture the complete registered native sparse-indexing mechanism with Nsys."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

from result_utils import sha256, write_metrics


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nsys", default="nsys", help="explicit installed Nsys executable")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--fixture-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not os.environ.get("AUTOBENCH_SCHEDULER_JOB_ID", "").strip():
        parser.error("profiling requires an Autobench worker Scheduler job context")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    capture_dir = args.output_dir / "nsys"
    capture_dir.mkdir(parents=True, exist_ok=True)
    prefix = capture_dir / "sparse_index_pipeline"
    result_path = capture_dir / "native-result.json"
    runner = Path(__file__).with_name("run_native.py")
    command = [sys.executable, str(runner), "--source", str(args.source),
               "--binary", str(args.binary), "--fixture-dir", str(args.fixture_dir),
               "--output", str(result_path), "--mode", "timing", "--warmup", "1",
               "--samples", "3", "--iterations", "1"]
    argv = [args.nsys, "profile", "--trace=cuda,nvtx,osrt", "--sample=none",
            "--force-overwrite=true", "--output", str(prefix), *command]
    (capture_dir / "nsys-command.txt").write_text("\n".join(argv) + "\n",
                                                    encoding="utf-8")
    proc = subprocess.run(argv, text=True, capture_output=True, check=False)
    (capture_dir / "nsys.stdout.log").write_text(proc.stdout, encoding="utf-8")
    (capture_dir / "nsys.stderr.log").write_text(proc.stderr, encoding="utf-8")
    if proc.returncode:
        return proc.returncode
    reports = list(capture_dir.glob("sparse_index_pipeline.nsys-rep"))
    if not reports or not result_path.is_file():
        raise RuntimeError("Nsys completed without report and native benchmark result")
    report = reports[0]
    version = subprocess.run([args.nsys, "--version"], text=True,
                             capture_output=True, check=True)
    write_metrics(args.output_dir, "autobench.cellerator-sparse-support.nsys-capture.v1", {
        "capture": {"path_name": report.name, "sha256": sha256(report),
                    "bytes": report.stat().st_size},
        "tool": {"executable": args.nsys,
                 "version": (version.stdout + version.stderr).strip()},
        "protocol": {"trace": ["cuda", "nvtx", "osrt"], "sample": "none",
                     "scope": "full registered sparse indexing mechanism"},
        "native_result": "native-result.json",
    })
    print(f"captured {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
