#!/usr/bin/env python3
"""Parse a real NCU report through Nsight Compute's Python report API."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_api(ncu: str):
    extra = os.environ.get("NCU_REPORT_PYTHON_PATH")
    if extra:
        sys.path.insert(0, extra)
    executable = shutil.which(ncu) or ncu
    resolved = Path(executable).resolve()
    candidates = [resolved.parent.parent / "extras/python",
                  resolved.parent / "extras/python"]
    for path in candidates:
        if path.is_dir() and str(path) not in sys.path:
            sys.path.insert(0, str(path))
    try:
        return importlib.import_module("ncu_report")
    except ImportError as exc:
        raise RuntimeError("cannot import ncu_report; set NCU_REPORT_PYTHON_PATH to the configured NCU extras/python directory") from exc


def metric_value(action, name):
    try:
        metric = action[name]
        return {"value": metric.value(),
                "unit": metric.unit() if hasattr(metric, "unit") else None,
                "kind": str(metric.kind()) if hasattr(metric, "kind") else None}
    except Exception as exc:
        return {"unavailable": type(exc).__name__}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--ncu", default="ncu")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report_path = args.report.resolve(strict=True)
    api = load_api(args.ncu)
    report = api.load_report(str(report_path))
    actions = []
    for range_index in range(report.num_ranges()):
        range_obj = report.range_by_idx(range_index)
        for action_index in range(range_obj.num_actions()):
            action = range_obj.action_by_idx(action_index)
            try:
                name = action.name(action.NameBase_FUNCTION)
            except Exception:
                name = action.name()
            metrics = {metric_name: metric_value(action, metric_name)
                       for metric_name in sorted(action.metric_names())}
            rules = []
            if hasattr(action, "rule_results"):
                for rule in action.rule_results():
                    item = {"section": rule.section_identifier(),
                            "rule": rule.rule_identifier()}
                    if rule.has_rule_message():
                        item["message"] = rule.rule_message()
                    if rule.has_speedup_estimation():
                        item["speedup_estimation"] = rule.speedup_estimation()
                    rules.append(item)
            actions.append({"name": name, "metrics": metrics, "rules": rules})
    if not actions:
        raise RuntimeError("NCU report contains no actions")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "schema": "autobench.cellerator-sparse-support.ncu-analysis.v1",
        "capture": {"path_name": report_path.name, "sha256": sha256(report_path)},
        "reader": {"module": "ncu_report", "version": getattr(api, "__version__", None),
                   "path": getattr(api, "__file__", None)},
        "actions": actions,
        "interpretation": "Raw NCU API metrics and rules; this record does not assert a speedup or bottleneck diagnosis.",
    }
    args.output.write_text(json.dumps(data, sort_keys=True, indent=2, default=str) + "\n",
                           encoding="utf-8")
    print(json.dumps({"analysis": str(args.output), "action_count": len(actions),
                      "report_sha256": data["capture"]["sha256"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
