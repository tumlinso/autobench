#!/usr/bin/env python3
"""Create or verify the deterministic synthetic Cellerator sparse fixture."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

from result_utils import write_metrics


EXPECTED = {
    "schema": "cellerator.scaled_sparse_fixture.v1",
    "seed": 20261027,
    "edges": 2176,
    "entities": 256,
    "cells": 64,
    "modules": 16,
    "layout": "identity",
}


def _load_generator(source: Path):
    path = source / "bench/sparse_support/scaled_fixtures.py"
    spec = importlib.util.spec_from_file_location("cellerator_scaled_fixtures", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load fixture generator: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _compact_metadata(metadata: dict) -> dict:
    fields = ("schema", "seed", "edges", "entities", "cells", "modules",
              "layout", "topology_sha256")
    return {key: metadata[key] for key in fields if key in metadata}


def verify(directory: Path, expected: dict | None = None) -> dict:
    manifest_path = directory / "manifest.json"
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    if manifest.get("schema") != "cellerator.scaled_sparse_fixture_bundle.v1":
        raise RuntimeError("unexpected fixture bundle schema")
    metadata = manifest.get("metadata", {})
    for key, value in (expected or {}).items():
        if metadata.get(key) != value:
            raise RuntimeError(f"fixture identity mismatch for {key}: {metadata.get(key)!r}")
    checked: dict[str, str] = {}
    total_bytes = 0
    for name, item in manifest.get("arrays", {}).items():
        filename = item["file"]
        path = directory / filename
        digest = item["sha256"]
        if filename in checked:
            if checked[filename] != digest:
                raise RuntimeError(f"fixture aliases disagree for {filename}")
            continue
        if path.stat().st_size != item["nbytes"] or sha256(path) != digest:
            raise RuntimeError(f"fixture array failed integrity check: {name}")
        checked[filename] = digest
        total_bytes += path.stat().st_size
    return {
        "fixture_manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "fixture_unique_array_files": len(checked),
        "fixture_unique_array_bytes": total_bytes,
        "fixture_metadata": _compact_metadata(metadata),
    }


def create(source: Path, destination: Path, size: str) -> dict:
    generator, path = _load_generator(source)
    generator_digest = sha256(path)
    if size == "registered":
        fixture = generator.make_scaled_fixture(
            edges=2176, entities=256, cells=64, modules=16,
            seed=20261027, layout="identity",
        )
        expected = EXPECTED
    else:
        fixture = generator.make_scaled_fixture(
            edges=96, entities=64, cells=8, modules=8,
            events_per_cell=16, cohort_size=4,
            seed=20261027, layout="identity", query_sizes=(0, 1, 8, 32),
        )
        expected = {"schema": EXPECTED["schema"], "seed": 20261027,
                    "edges": 96, "entities": 64, "cells": 8,
                    "modules": 8, "layout": "identity"}
    for key, value in expected.items():
        if fixture.metadata.get(key) != value:
            raise RuntimeError(f"generator returned unexpected {key}")
    destination = Path(destination)
    if destination.is_symlink():
        raise RuntimeError("refusing a symlinked fixture cache directory")
    destination = destination.resolve()
    sidecar = destination / "autobench-fixture-identity.json"
    if sidecar.is_file():
        try:
            cached = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = {}
        if (cached.get("fixture_generator_sha256") == generator_digest
                and cached.get("fixture_profile") == size):
            try:
                identity = verify(destination, expected)
                identity.update({"fixture_generator_sha256": generator_digest,
                                 "fixture_generator": "bench/sparse_support/scaled_fixtures.py",
                                 "fixture_kind": "synthetic engineering fixture",
                                 "fixture_profile": size,
                                 "cache_reused": True})
                return identity
            except (OSError, KeyError, RuntimeError, ValueError):
                pass
    # Remove only files named by the old fixture manifest. The cache is a
    # dedicated Autobench data directory, and unrelated files are preserved.
    old_manifest = destination / "manifest.json"
    if old_manifest.is_file():
        try:
            previous = json.loads(old_manifest.read_text(encoding="utf-8"))
            for descriptor in previous.get("arrays", {}).values():
                filename = descriptor.get("file", "")
                if Path(filename).name == filename:
                    (destination / filename).unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        old_manifest.unlink(missing_ok=True)
    sidecar.unlink(missing_ok=True)
    destination.mkdir(parents=True, exist_ok=True)
    generator.write_raw_bundle(fixture, destination)
    identity = verify(destination, expected)
    identity["fixture_generator_sha256"] = generator_digest
    identity["fixture_generator"] = "bench/sparse_support/scaled_fixtures.py"
    identity["fixture_kind"] = "synthetic engineering fixture"
    identity["fixture_profile"] = size
    identity["cache_reused"] = False
    sidecar.write_text(json.dumps({"fixture_generator_sha256": generator_digest,
                                   "fixture_profile": size,
                                   "fixture_manifest_sha256": identity[
                                       "fixture_manifest_sha256"]},
                                  sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return identity


def verify_cached(source: Path, directory: Path, size: str = "registered") -> dict:
    _, generator_path = _load_generator(source)
    digest = sha256(generator_path)
    identity_file = directory / "autobench-fixture-identity.json"
    if not identity_file.is_file():
        raise RuntimeError("fixture cache identity sidecar is missing; run prepare-fixture")
    cached = json.loads(identity_file.read_text(encoding="utf-8"))
    if cached.get("fixture_generator_sha256") != digest or cached.get("fixture_profile") != size:
        raise RuntimeError("fixture cache was generated from a different registered recipe")
    expected = EXPECTED if size == "registered" else None
    identity = verify(directory, expected)
    if identity["fixture_manifest_sha256"] != cached.get("fixture_manifest_sha256"):
        raise RuntimeError("fixture cache manifest differs from its identity sidecar")
    identity.update({"fixture_generator_sha256": digest,
                     "fixture_generator": "bench/sparse_support/scaled_fixtures.py",
                     "fixture_kind": "synthetic engineering fixture",
                     "fixture_profile": size,
                     "cache_reused": True})
    return identity


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("create", "verify"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--metrics-output", type=Path)
    parser.add_argument("--size", choices=("registered", "small96"), default="registered")
    args = parser.parse_args()
    if args.action == "create":
        result = create(args.source.resolve(strict=True), args.directory, args.size)
    else:
        result = verify_cached(args.source.resolve(strict=True), args.directory, args.size)
    if args.metrics_output:
        write_metrics(args.metrics_output.parent, "autobench.cellerator-sparse-support.fixture.v1", result)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
