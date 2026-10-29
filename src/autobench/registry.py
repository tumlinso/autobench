from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from .errors import RegistrationError
from .models import Benchmark, Command
from .settings import host_limits

_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")


def _command(raw: dict[str, Any] | None, default_kind: str) -> Command | None:
    if raw is None:
        return None
    argv = raw.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
        raise RegistrationError("command.argv must be a non-empty array of strings")
    expected = raw.get("expected_seconds")
    kill = raw.get("kill_seconds")
    if not isinstance(expected, int) or expected < 1 or not isinstance(kill, int) or kill <= expected:
        raise RegistrationError("each command needs expected_seconds >= 1 and kill_seconds > expected_seconds")
    if kill > host_limits()["max_kill_seconds"]:
        raise RegistrationError("kill_seconds exceeds the configured host ceiling")
    resources = raw.get("resources", {})
    if not isinstance(resources, dict):
        raise RegistrationError("command.resources must be a TOML table")
    environment = raw.get("environment", {})
    if not isinstance(environment, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in environment.items()):
        raise RegistrationError("command.environment must map strings to strings")
    resources = dict(resources)
    if environment:
        resources["_environment"] = environment
    depends = raw.get("depends_on", [])
    if not isinstance(depends, list) or not all(isinstance(x, str) for x in depends):
        raise RegistrationError("packet.depends_on must be an array of packet names")
    name = raw.get("name", default_kind)
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise RegistrationError("packet.name must be a simple identifier")
    return Command(tuple(argv), expected, kill, resources, str(raw.get("kind", default_kind)), name, tuple(depends))


def parse_registration(path: Path, root: Path) -> Benchmark:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RegistrationError(f"cannot read {path}: {exc}") from exc
    meta = raw.get("benchmark", {})
    benchmark_name = meta.get("name")
    if not isinstance(benchmark_name, str) or not _NAME.fullmatch(benchmark_name):
        raise RegistrationError(f"invalid benchmark name in {path}")
    protocol = raw.get("protocol", {})
    if not isinstance(protocol, dict):
        raise RegistrationError("[protocol] must be a TOML table")
    includes = raw.get("source", {}).get("include", [])
    data = raw.get("data", {}).get("paths", [])
    if not includes and not data:
        raise RegistrationError("registration must declare source.include or data.paths")
    for label, paths in (("source.include", includes), ("data.paths", data)):
        if not isinstance(paths, list) or not all(isinstance(x, str) and x for x in paths):
            raise RegistrationError(f"{label} must be an array of relative path globs")
        for entry in paths:
            if Path(entry).is_absolute() or ".." in Path(entry).parts:
                raise RegistrationError(f"{label} cannot escape the project root: {entry}")
            if label == "source.include" and entry.startswith(".autobench/"):
                raise RegistrationError("source.include cannot watch Autobench-owned data or generated state")
            if label == "data.paths" and entry.startswith(".autobench/") and not entry.startswith(".autobench/data/"):
                raise RegistrationError("only .autobench/data may be registered as a generated input")
    build = _command(raw.get("build"), "build")
    packets_raw = raw.get("packet", [])
    if not isinstance(packets_raw, list) or not packets_raw:
        raise RegistrationError("at least one [[packet]] is required")
    packets = tuple(_command(p, "correctness") for p in packets_raw)
    packet_names = [p.name for p in packets if p]
    if len(set(packet_names)) != len(packet_names):
        raise RegistrationError("packet names must be unique")
    if any(dep not in packet_names for p in packets if p for dep in p.depends_on):
        raise RegistrationError("packet.depends_on refers to an unknown packet name")
    for p in packets:
        if p and p.name in p.depends_on:
            raise RegistrationError("a packet cannot depend on itself")
    by_name = {p.name: p for p in packets if p}
    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(name: str) -> None:
        if name in visiting:
            raise RegistrationError("packet dependencies must be acyclic")
        if name in visited:
            return
        visiting.add(name)
        for dependency in by_name[name].depends_on:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)
    for packet_name in by_name:
        visit(packet_name)
    return Benchmark(benchmark_name, int(meta.get("revision", 1)), root.resolve(), tuple(includes), tuple(data), build, packets, protocol=protocol)  # type: ignore[arg-type]


def load_registry(project: Path) -> list[Benchmark]:
    directory = project / ".autobench" / "registry"
    if not directory.exists():
        return []
    items = [parse_registration(p, project) for p in sorted(directory.glob("*.toml"))]
    names = [item.name for item in items]
    if len(set(names)) != len(names):
        raise RegistrationError("duplicate benchmark names in registry")
    return items


def write_registration(project: Path, name: str, include: list[str], argv: list[str], expected: int, kill: int, kind: str = "correctness") -> Path:
    if not _NAME.fullmatch(name):
        raise RegistrationError("name must use letters, numbers, dot, underscore, or dash")
    directory = project / ".autobench" / "registry"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{name}.toml"
    if target.exists():
        raise RegistrationError(f"registration already exists: {target}; edit it explicitly to revise")
    for entry in include:
        if Path(entry).is_absolute() or ".." in Path(entry).parts:
            raise RegistrationError(f"source scope must remain inside the project: {entry}")
    if not argv or expected < 1 or kill <= expected or kill > host_limits()["max_kill_seconds"]:
        raise RegistrationError("invalid argv or timing envelope (kill must exceed expected and stay within the host ceiling)")
    import json
    encoded_argv = ", ".join(json.dumps(arg) for arg in argv)
    encoded_include = ", ".join(json.dumps(pattern) for pattern in include)
    text = f'''[benchmark]\nname = {json.dumps(name)}\nrevision = 1\n\n[source]\ninclude = [{encoded_include}]\n\n[[packet]]\nkind = {json.dumps(kind)}\nargv = [{encoded_argv}]\nexpected_seconds = {expected}\nkill_seconds = {kill}\n'''
    target.write_text(text, encoding="utf-8")
    return target

