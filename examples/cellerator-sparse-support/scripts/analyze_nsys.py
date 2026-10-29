#!/usr/bin/env python3
"""Produce CPU-side Nsight Systems summaries from a captured report."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nsys", default="nsys")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = args.report.resolve(strict=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    version = subprocess.run([args.nsys, "--version"], text=True,
                             capture_output=True, check=True)
    summary = subprocess.run([args.nsys, "stats", "--report",
                              "cuda_gpu_kern_sum,cuda_gpu_mem_time_sum",
                              str(report)], text=True, capture_output=True, check=False)
    (args.output_dir / "nsys-stats.stdout.txt").write_text(summary.stdout,
                                                           encoding="utf-8")
    (args.output_dir / "nsys-stats.stderr.txt").write_text(summary.stderr,
                                                           encoding="utf-8")
    if summary.returncode:
        return summary.returncode
    data = {
        "schema": "autobench.cellerator-sparse-support.nsys-analysis.v1",
        "capture": {"path_name": report.name, "sha256": sha256(report)},
        "tool_version": (version.stdout + version.stderr).strip(),
        "summary_file": "nsys-stats.stdout.txt",
        "summary_sha256": sha256(args.output_dir / "nsys-stats.stdout.txt"),
        "interpretation": "Nsight Systems CUDA kernel and transfer summary; source-bound data only.",
    }
    output = args.output_dir / "analysis.json"
    output.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n",
                      encoding="utf-8")
    print(json.dumps({"analysis": str(output), "report_sha256": data["capture"]["sha256"]},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
