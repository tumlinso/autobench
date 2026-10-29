from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
from pathlib import Path

from .errors import UnsafeWorkspace
from .models import Benchmark, Snapshot

EXCLUDED = {".git", ".autobench", "__pycache__", ".venv", "venv", "node_modules"}


def _files(root: Path, patterns: tuple[str, ...]) -> list[Path]:
    found: set[Path] = set()
    ordinary = tuple(p for p in patterns if not p.startswith(".autobench/data/"))
    data_patterns = tuple(p for p in patterns if p.startswith(".autobench/data/"))
    for current, dirs, files in os.walk(root, followlinks=False):
        base = Path(current)
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not (base / d).is_symlink())
        for name in files:
            path = base / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                continue
            if any(fnmatch.fnmatch(rel, pat) or Path(rel).match(pat) for pat in ordinary):
                found.add(path)
    # Generated datasets are opt-in and live only under this dedicated Autobench
    # data namespace. Do not traverse registry, state, builds, or evidence.
    if data_patterns:
        data_root = root / ".autobench" / "data"
        if data_root.exists():
            for current, dirs, files in os.walk(data_root, followlinks=False):
                base = Path(current)
                dirs[:] = sorted(d for d in dirs if not (base / d).is_symlink())
                for name in files:
                    path = base / name
                    rel = path.relative_to(root).as_posix()
                    if not path.is_symlink() and any(fnmatch.fnmatch(rel, pat) or Path(rel).match(pat) for pat in data_patterns):
                        found.add(path)
    return sorted(found)


def fingerprint_manifest(manifest: dict[str, str]) -> str:
    return hashlib.sha256("\n".join(f"{key}\0{manifest[key]}" for key in sorted(manifest)).encode()).hexdigest()


def observe(benchmark: Benchmark) -> tuple[str, dict[str, str]]:
    """Read current scoped identities without materializing a snapshot."""
    manifest: dict[str, str] = {}
    for path in _files(benchmark.root, benchmark.include + benchmark.data):
        manifest[path.relative_to(benchmark.root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return fingerprint_manifest(manifest), manifest


def capture(benchmark: Benchmark, state_dir: Path) -> Snapshot:
    root = benchmark.root
    patterns = benchmark.include + benchmark.data
    paths = _files(root, patterns)
    manifest: dict[str, str] = {}
    before: dict[str, tuple[int, int]] = {}
    for p in paths:
        rel = p.relative_to(root).as_posix()
        st = p.stat()
        before[rel] = (st.st_size, st.st_mtime_ns)
        manifest[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    digest = fingerprint_manifest(manifest)
    target = state_dir / "snapshots" / digest
    if not target.exists():
        temp = target.with_name(target.name + ".tmp")
        shutil.rmtree(temp, ignore_errors=True)
        temp.mkdir(parents=True)
        for p in paths:
            rel = p.relative_to(root)
            try:
                dest = temp / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, dest)
                if hashlib.sha256(dest.read_bytes()).hexdigest() != manifest[rel.as_posix()]:
                    raise UnsafeWorkspace(f"source changed while snapshotting: {rel}")
            except OSError as exc:
                shutil.rmtree(temp, ignore_errors=True)
                raise UnsafeWorkspace(f"cannot snapshot {rel}: {exc}") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        temp.replace(target)
    after_paths = _files(root, patterns)
    after_names = [p.relative_to(root).as_posix() for p in after_paths]
    if after_names != sorted(manifest):
        shutil.rmtree(target, ignore_errors=True)
        raise UnsafeWorkspace("source scope changed while taking snapshot; retry after it settles")
    after = {p.relative_to(root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns) for p in after_paths}
    if before != after:
        shutil.rmtree(target, ignore_errors=True)
        raise UnsafeWorkspace("source scope changed while taking snapshot; retry after it settles")
    # Re-read all live bytes after materialization. This catches additions, removals,
    # and same-size replacements that the initial enumeration cannot see.
    after_manifest = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in after_paths}
    if after_manifest != manifest:
        shutil.rmtree(target, ignore_errors=True)
        raise UnsafeWorkspace("source scope changed while taking snapshot; retry after it settles")
    return Snapshot(digest, manifest, target)

