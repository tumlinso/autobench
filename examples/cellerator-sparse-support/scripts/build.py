#!/usr/bin/env python3
"""Configure and build the minimal captured-source CUDA benchmark target."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


SOURCES = (
    "src/support/logical_support.cc",
    "src/support/adaptive_support.cc",
    "src/support/proposal_discovery.cc",
    "src/indexing/reverse_postings.cc",
    "src/indexing/temporal_occurrences.cc",
    "src/gating/condition_program.cc",
    "src/gating/derivative_sampling.cc",
    "src/gating/cuda_worksets.cu",
    "src/indexing/cuda_postings.cu",
    "bench/sparse_support/cuda_indexing_bench.cu",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tool_version(name: str) -> dict:
    proc = subprocess.run([name, "--version"], text=True,
                          capture_output=True, check=False)
    return {"executable": name, "returncode": proc.returncode,
            "output": (proc.stdout + proc.stderr).strip()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--autobench-root", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    build = args.build.resolve()
    cmake_root = args.autobench_root.resolve(strict=True) / "examples/cellerator-sparse-support/build"
    build.mkdir(parents=True, exist_ok=True)
    configure = ["cmake", "-S", str(cmake_root), "-B", str(build / "cmake"),
                 f"-DCELLERATOR_SOURCE_DIR={source}",
                 "-DCMAKE_CXX_COMPILER=c++", "-DCMAKE_CUDA_COMPILER=nvcc",
                 "-DCMAKE_CUDA_ARCHITECTURES=70", "-DCMAKE_CUDA_FLAGS=-lineinfo",
                 "-DCMAKE_BUILD_TYPE=Release"]
    for command in (configure, ["cmake", "--build", str(build / "cmake"),
                                "--target", "cellerator_sparse_support_cuda_indexing_bench",
                                "--parallel", "2"]):
        proc = subprocess.run(command, text=True, check=False)
        if proc.returncode:
            return proc.returncode
    binary = build / "cmake/cellerator_sparse_support_cuda_indexing_bench"
    if not binary.is_file():
        raise RuntimeError(f"build completed without expected executable: {binary}")
    metadata = {
        "schema": "autobench.cellerator-sparse-support.build.v1",
        "target": "cellerator_sparse_support_cuda_indexing_bench",
        "binary": {"path_name": binary.name, "sha256": sha256(binary),
                   "bytes": binary.stat().st_size},
        "source_files": {name: sha256(source / name) for name in SOURCES},
        "build_recipe_sha256": sha256(Path(__file__).resolve()),
        "cmake_recipe_sha256": sha256(cmake_root / "CMakeLists.txt"),
        "tool_versions": [tool_version("cmake"), tool_version("c++"),
                          tool_version("nvcc")],
        "cuda_architecture": "sm_70",
        "cuda_line_info": True,
    }
    out = build / "build-metadata.json"
    out.write_text(json.dumps(metadata, sort_keys=True, indent=2) + "\n",
                   encoding="utf-8")
    print(json.dumps({"binary_sha256": metadata["binary"]["sha256"],
                      "build_metadata": str(out)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
