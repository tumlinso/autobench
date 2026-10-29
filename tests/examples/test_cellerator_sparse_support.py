from __future__ import annotations

import ast
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples/cellerator-sparse-support"
CELLERATOR = Path(os.environ.get("CELLERATOR_SOURCE_DIR", "/home/tumlinson/Cellerator"))
sys.path.insert(0, str(ROOT / "src"))

from autobench.registry import parse_registration
from autobench.snapshot import capture


class CelleratorSparseSupportExampleTests(unittest.TestCase):
    def test_registration_is_bounded_and_describes_independent_packets(self) -> None:
        raw = tomllib.loads((EXAMPLE / "registration.toml").read_text(encoding="utf-8"))
        registration = parse_registration(EXAMPLE / "registration.toml", CELLERATOR)
        self.assertEqual(registration.name, "cellerator-sparse-support-indexing")
        self.assertIsNotNone(registration.build)
        assert registration.build is not None
        self.assertEqual(registration.build.expected_seconds, 60)
        self.assertEqual(registration.build.kill_seconds, 300)
        self.assertTrue(all(p.expected_seconds <= p.kill_seconds <= 600
                            for p in registration.packets))
        packets = {packet.name: packet for packet in registration.packets}
        self.assertTrue({"host-oracle", "cuda-oracle", "mechanism-timing", "profile-nsys",
                         "profile-ncu", "analyze-profiles"}.issubset(packets))
        self.assertEqual(packets["analyze-profiles"].depends_on,
                         ("profile-nsys", "profile-ncu"))
        self.assertIn("{artifact.profile-ncu}", " ".join(packets["analyze-profiles"].argv))
        self.assertIn("{artifact.profile-nsys}", " ".join(packets["analyze-profiles"].argv))
        self.assertEqual(packets["mechanism-timing"].expected_seconds, 5)
        self.assertEqual(packets["mechanism-timing"].kill_seconds, 30)
        self.assertEqual(packets["profile-ncu"].expected_seconds, 30)
        self.assertEqual(packets["profile-ncu"].kill_seconds, 120)
        self.assertTrue(raw["source"]["include"])
        self.assertEqual(raw["data"]["paths"],
                         [".autobench/data/cellerator-sparse-support-indexing/**"])
        self.assertTrue(all("evidence" not in pattern for pattern in raw["source"]["include"]))
        self.assertTrue(all("{autobench}" in arg or "{snapshot}" in arg or "{build}" in arg
                            or "{output}" in arg or "{artifact." in arg or not arg.startswith("/")
                            for packet in registration.packets for arg in packet.argv))


    def test_captured_source_globs_match_only_the_narrow_build_closure(self) -> None:
        if not CELLERATOR.is_dir():
            self.skipTest("Cellerator source checkout is unavailable")
        registration = parse_registration(EXAMPLE / "registration.toml", CELLERATOR)
        matched: set[Path] = set()
        for pattern in registration.include:
            matched.update(CELLERATOR.glob(pattern))
        relative = {path.relative_to(CELLERATOR).as_posix() for path in matched}
        expected = {
            "src/gating/cuda_worksets.cu",
            "src/indexing/cuda_postings.cu",
            "bench/sparse_support/cuda_indexing_bench.cu",
            "bench/sparse_support/scaled_fixtures.py",
        }
        self.assertTrue(expected <= relative)
        self.assertFalse(any(path.startswith("bench/sparse_support/evidence/") for path in relative))
        self.assertFalse(any(path.startswith("external/") for path in relative))


    def test_fixture_is_deterministic_integrity_checked_and_small(self) -> None:
        if not CELLERATOR.is_dir():
            self.skipTest("Cellerator source checkout is unavailable")
        if importlib.util.find_spec("numpy") is None:
            self.skipTest("the optional example dependency numpy is not installed")
        script = EXAMPLE / "scripts/fixture.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            results = []
            for index, destination in enumerate((root / "first", root / "second", root / "first")):
                metrics_output = root / f"run-{index}" / "metrics.json"
                proc = subprocess.run([sys.executable, str(script), "create", "--source",
                                       str(CELLERATOR), "--directory", str(destination),
                                       "--size", "registered", "--metrics-output",
                                       str(metrics_output)], check=True,
                                      text=True, capture_output=True)
                results.append(json.loads(proc.stdout))
            self.assertEqual(results[0]["fixture_manifest_sha256"],
                             results[1]["fixture_manifest_sha256"])
            self.assertEqual(results[0]["fixture_manifest_sha256"],
                             results[2]["fixture_manifest_sha256"])
            self.assertFalse(results[0]["cache_reused"])
            self.assertTrue(results[2]["cache_reused"])
            self.assertEqual(results[0]["fixture_kind"], "synthetic engineering fixture")
            for key, value in (("edges", 2176), ("entities", 256),
                               ("cells", 64), ("modules", 16), ("layout", "identity")):
                self.assertEqual(results[0]["fixture_metadata"][key], value)
            self.assertLess(results[0]["fixture_unique_array_bytes"], 16 * 1024 * 1024)
            self.assertTrue((root / "first/manifest.json").is_file())
            self.assertTrue((root / "run-0/metrics.json").is_file())
            verify = subprocess.run([sys.executable, str(script), "verify", "--source",
                                     str(CELLERATOR), "--directory", str(root / "first"),
                                     "--size", "registered"], check=True, text=True,
                                    capture_output=True)
            self.assertTrue(json.loads(verify.stdout)["cache_reused"])


    def test_example_python_sources_parse(self) -> None:
        for path in (EXAMPLE / "scripts").glob("*.py"):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_registered_autobench_data_is_captured_without_other_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            data = project / ".autobench/data/cellerator-sparse-support-indexing"
            data.mkdir(parents=True)
            (data / "manifest.json").write_text("fixture manifest\n", encoding="utf-8")
            (project / ".autobench/registry").mkdir(parents=True)
            (project / ".autobench/state").mkdir(parents=True)
            (project / ".autobench/state/private.json").write_text("not captured\n", encoding="utf-8")
            benchmark = parse_registration(EXAMPLE / "registration.toml", project)
            snapshot = capture(benchmark, Path(temporary) / "state")
            self.assertTrue((snapshot.directory / ".autobench/data/cellerator-sparse-support-indexing/manifest.json").is_file())
            self.assertFalse((snapshot.directory / ".autobench/state/private.json").exists())
            self.assertIn(".autobench/data/cellerator-sparse-support-indexing/manifest.json",
                          snapshot.manifest)


if __name__ == "__main__":
    unittest.main()
