from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
from pathlib import Path
from pathlib import PurePosixPath

from .errors import UnsafeWorkspace
from .models import Benchmark, Snapshot

EXCLUDED = {".git", ".autobench", "__pycache__", ".venv", "venv", "node_modules"}


def _scope(root: Path, pattern: str, *, allow_autobench_data: bool) -> tuple[Path | None, bool]:
    """Resolve the deepest literal path prefix, or an exact file pattern.

    Registration globs are relative to the workspace root. A wildcard-free
    pattern can only match that exact file; other patterns need a walk below
    the path preceding their first wildcard component.
    """
    parts = PurePosixPath(pattern).parts
    if not parts or PurePosixPath(pattern).is_absolute() or ".." in parts:
        return None, False
    prefix: list[str] = []
    wildcard = False
    for part in parts:
        if any(char in part for char in "*?["):
            wildcard = True
            break
        prefix.append(part)
    candidate = root.joinpath(*prefix)
    if not allow_autobench_data and any(part in EXCLUDED for part in prefix):
        return None, False
    # A literal glob matches one exact file and does not justify a workspace
    # walk. Literal directories alone do not match any files.
    if not wildcard:
        if (candidate.is_symlink() or not candidate.is_file() or
                any(part.is_symlink() for part in _parents_to(root, candidate))):
            return None, False
        return candidate, True
    if not candidate.is_dir() or any(part.is_symlink() for part in _parents_to(root, candidate)):
        return None, False
    return candidate, False


def _parents_to(root: Path, path: Path):
    """Yield path components from root's child to path for symlink checks."""
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        yield current


def _walk_scopes(root: Path, patterns: tuple[str, ...], *, allow_autobench_data: bool = False):
    exact_files: set[Path] = set()
    directories: set[Path] = set()
    excluded = set() if allow_autobench_data else EXCLUDED
    for pattern in patterns:
        scope, exact = _scope(root, pattern, allow_autobench_data=allow_autobench_data)
        if scope is None:
            continue
        if exact:
            exact_files.add(scope)
        else:
            directories.add(scope)

    # One broad scope subsumes narrower scopes. Avoid enumerating the same
    # files twice when a registration declares both a directory and a file.
    roots: list[Path] = []
    for candidate in sorted(directories, key=lambda path: (len(path.parts), str(path))):
        if not any(candidate == parent or candidate.is_relative_to(parent) for parent in roots):
            roots.append(candidate)

    yield from exact_files
    for scope in roots:
        for current, dirs, files in os.walk(scope, followlinks=False):
            base = Path(current)
            dirs[:] = sorted(d for d in dirs if d not in excluded and not (base / d).is_symlink())
            for name in files:
                path = base / name
                if not path.is_symlink():
                    yield path


def _files(root: Path, patterns: tuple[str, ...]) -> list[Path]:
    """Return files matched by workspace-root-relative globs.

    Patterns are anchored at the project root. This keeps `src/**` scoped to
    the project's `src` tree instead of accidentally matching historical
    `bench/.../source/src` copies elsewhere in the repository.
    """
    found: set[Path] = set()
    ordinary = tuple(p for p in patterns if not p.startswith(".autobench/data/"))
    data_patterns = tuple(p for p in patterns if p.startswith(".autobench/data/"))
    for path in _walk_scopes(root, ordinary):
        rel = path.relative_to(root).as_posix()
        if any(fnmatch.fnmatchcase(rel, pat) for pat in ordinary):
            found.add(path)
    # Generated datasets are opt-in and live only under this dedicated Autobench
    # data namespace. Do not traverse registry, state, builds, or evidence.
    if data_patterns:
        for path in _walk_scopes(root, data_patterns, allow_autobench_data=True):
            rel = path.relative_to(root).as_posix()
            if any(fnmatch.fnmatchcase(rel, pat) for pat in data_patterns):
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

