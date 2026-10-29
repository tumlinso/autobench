#!/usr/bin/env python3
"""Run the existing native sparse-support oracle or timing harness."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from fixture import verify_cached
from result_utils import write_metrics


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--fixture-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("oracle", "timing"), required=True)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--iterations", type=int, default=1)
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    binary = args.binary.resolve(strict=True)
    fixture_dir = args.fixture_dir.resolve()
    fixture_before = verify_cached(source, fixture_dir, "registered")
    command = [str(source / "bench/sparse_support/run_cuda_indexing_bench.py"),
               "--binary", str(binary), "--fixture-dir", str(fixture_dir),
               "--case-id", "anchor_e2176_b64", "--edges", "2176",
               "--cells", "64", "--entities", "256"]
    env = os.environ.copy()
    if args.mode == "oracle":
        command.extend(["--host-oracle-only", "--output", str(args.output)])
    else:
        job_id = env.get("AUTOBENCH_SCHEDULER_JOB_ID", "").strip()
        if not job_id:
            parser.error("timing requires an Autobench worker Scheduler job context")
        # This compatibility flag enables the existing harness. Scheduler
        # admission and receipt validation remain the service's responsibility.
        env["CELLERATOR_SPARSE_SUPPORT_BENCH_ADMITTED"] = "1"
        command.extend(["--warmup", str(args.warmup), "--samples", str(args.samples),
                        "--iterations", str(args.iterations), "--output", str(args.output)])
    actual_command = [sys.executable, *command]
    proc = subprocess.run(actual_command, cwd=source, env=env,
                          text=True, capture_output=True, check=False)
    if proc.returncode:
        sys.stderr.write(proc.stderr)
        return proc.returncode
    result = json.loads(args.output.read_text(encoding="utf-8"))
    fixture_after = verify_cached(source, fixture_dir, "registered")
    if fixture_before["fixture_manifest_sha256"] != fixture_after["fixture_manifest_sha256"]:
        raise RuntimeError("fixture changed during benchmark execution")
    build_metadata = binary.parent.parent / "build-metadata.json"
    if not build_metadata.is_file():
        raise RuntimeError("build metadata is missing beside the content-addressed binary")
    result["autobench_provenance"] = {
        "fixture_kind": "synthetic engineering fixture",
        "fixture_profile": "registered-2176-256-64-16-seed-20261027-identity",
        "fixture_manifest_sha256": fixture_before["fixture_manifest_sha256"],
        "fixture_cache_reused": fixture_before["cache_reused"],
        "fixture_generator_sha256": sha256(source / "bench/sparse_support/scaled_fixtures.py"),
        "benchmark_binary_sha256": sha256(binary),
        "build_metadata_sha256": sha256(build_metadata),
        "benchmark_source_sha256": sha256(source / "bench/sparse_support/cuda_indexing_bench.cu"),
        "recipe_sha256": sha256(Path(__file__).resolve()),
        "mode": args.mode,
        "argv": actual_command,
        "scheduler_job_id": os.environ.get("AUTOBENCH_SCHEDULER_JOB_ID") if args.mode == "timing" else None,
    }
    args.output.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n",
                           encoding="utf-8")
    write_metrics(args.output.parent, "autobench.cellerator-sparse-support.native.v1", result)
    print(json.dumps({"result": str(args.output), "case_id": "anchor_e2176_b64",
                      "mode": args.mode, "fixture_manifest_sha256": fixture_before[
                          "fixture_manifest_sha256"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
