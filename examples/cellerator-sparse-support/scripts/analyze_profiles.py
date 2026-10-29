#!/usr/bin/env python3
"""Run CPU-only NCU/Nsys report analysis after capture jobs release the GPU."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from result_utils import write_metrics


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ncu", default="ncu")
    parser.add_argument("--nsys", default="nsys")
    parser.add_argument("--ncu-report", type=Path, required=True)
    parser.add_argument("--nsys-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    here = Path(__file__).resolve().parent
    commands = [
        [sys.executable, str(here / "analyze_ncu.py"), "--ncu", args.ncu,
         "--report", str(args.ncu_report), "--output", str(args.output_dir / "ncu-analysis.json")],
        [sys.executable, str(here / "analyze_nsys.py"), "--nsys", args.nsys,
         "--report", str(args.nsys_report), "--output-dir", str(args.output_dir / "nsys")],
    ]
    for index, command in enumerate(commands):
        proc = subprocess.run(command, text=True, capture_output=True, check=False)
        (args.output_dir / f"analysis-{index}.stdout.txt").write_text(proc.stdout,
                                                                        encoding="utf-8")
        (args.output_dir / f"analysis-{index}.stderr.txt").write_text(proc.stderr,
                                                                        encoding="utf-8")
        if proc.returncode:
            return proc.returncode
    summary = {
        "schema": "autobench.cellerator-sparse-support.analysis-bundle.v1",
        "ncu_report_sha256": sha256(args.ncu_report),
        "nsys_report_sha256": sha256(args.nsys_report),
        "ncu_analysis": "ncu-analysis.json",
        "nsys_analysis": "nsys/analysis.json",
        "analysis_recipe_sha256": sha256(Path(__file__).resolve()),
        "scope": "synthetic sparse-support indexing/gating mechanism; no biological interpretation or speedup claim",
    }
    target = args.output_dir / "analysis-bundle.json"
    target.write_text(json.dumps(summary, sort_keys=True, indent=2) + "\n",
                      encoding="utf-8")
    ncu_data = json.loads((args.output_dir / "ncu-analysis.json").read_text(encoding="utf-8"))
    write_metrics(args.output_dir, "autobench.cellerator-sparse-support.profile-analysis.v1", {
        "capture_identities": {
            "ncu_report_sha256": summary["ncu_report_sha256"],
            "nsys_report_sha256": summary["nsys_report_sha256"],
        },
        "ncu_action_count": len(ncu_data["actions"]),
        "ncu_metric_count": sum(len(action["metrics"]) for action in ncu_data["actions"]),
        "analysis_files": ["ncu-analysis.json", "nsys/analysis.json"],
        "scope": summary["scope"],
    })
    print(json.dumps({"analysis_bundle": str(target),
                      "ncu_report_sha256": summary["ncu_report_sha256"],
                      "nsys_report_sha256": summary["nsys_report_sha256"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
