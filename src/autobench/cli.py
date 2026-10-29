from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .api import init_project, register_benchmark, register_definition
from .catalog import Catalog
from .engine import Engine
from .errors import AutobenchError
from .instructions import update_agent_instructions
from .registry import load_registry, write_registration
from .scheduler import McpScheduler
from .service import activate_existing_user_unit
from .store import Store


class _UnconfiguredScheduler:
    async def call(self, operation: str, arguments: Any = None) -> dict[str, Any]:
        raise AutobenchError("Device Scheduler is not configured for this project")


def _project(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"project directory does not exist: {path}")
    return path


def _engine(project: Path, require_scheduler: bool = False) -> Engine:
    config = project / ".autobench" / "config.json"
    if not config.exists():
        raise AutobenchError("project is not initialized; run `autobench init` first")
    settings = json.loads(config.read_text(encoding="utf-8"))
    configured = bool(settings.get("scheduler_url") and settings.get("token_file"))
    if require_scheduler and not configured:
        raise AutobenchError("Device Scheduler is not configured for this project")
    scheduler = McpScheduler(settings["scheduler_url"], settings["token_file"]) if configured else _UnconfiguredScheduler()
    return Engine(project, scheduler)


async def _serve(project: Path) -> None:
    engine = _engine(project, require_scheduler=True)
    try:
        await engine.run()
    finally:
        engine.close()


async def _serve_global() -> None:
    while True:
        try:
            catalog = Catalog(create=False)
        except Exception as exc:
            print(f"autobench: workspace catalog unavailable: {exc}", file=sys.stderr)
            await asyncio.sleep(5)
            continue
        try:
            projects = catalog.workspaces()
        finally:
            catalog.close()
        for project in projects:
            config_path = project / ".autobench" / "config.json"
            if not project.is_dir() or not config_path.exists():
                continue
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
                if not config.get("active") or not config.get("scheduler_url") or not config.get("token_file"):
                    continue
                engine = Engine(project, McpScheduler(config["scheduler_url"], config["token_file"]))
                try:
                    await engine.reconcile_once()
                finally:
                    engine.close()
            except Exception as exc:
                print(f"autobench: coordinator could not reconcile {project}: {exc}", file=sys.stderr)
        await asyncio.sleep(5)


def _set_project_active(project: Path, active: bool) -> None:
    autobench = project / ".autobench"
    config_path = autobench / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["active"] = active
    temporary = config_path.with_name(config_path.name + ".tmp")
    temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    temporary.replace(config_path)
    if active:
        enrolled = autobench / "enrolled.json"
        temporary_enrolled = enrolled.with_name(enrolled.name + ".tmp")
        temporary_enrolled.write_text(json.dumps({"active": True, "project": str(project), "activated_at": time.time()}, indent=2) + "\n", encoding="utf-8")
        temporary_enrolled.replace(enrolled)
    else:
        (autobench / "enrolled.json").unlink(missing_ok=True)
    catalog = Catalog(create=False)
    try:
        catalog.enroll(project, active=active)
    finally:
        catalog.close()


async def _cmd_init(args: argparse.Namespace) -> int:
    project = _project(args.project)
    result = init_project(project, scheduler_url=args.scheduler_url, token_file=args.token_file,
                          retained_cap_bytes=int(args.retained_cap_gib * 1024**3),
                          agent_instructions=args.agent_instructions, activate=False)
    access_report: dict[str, Any] = {"ready": False, "missing_actions": [], "error": None}
    if result["scheduler_configured"]:
        try:
            engine = _engine(project, require_scheduler=True)
            try:
                doctor = await engine.doctor()
                access_report = doctor["scheduler_access"]
            finally:
                engine.close()
        except Exception as exc:
            access_report["error"] = str(exc)
    scheduler_ready = bool(access_report.get("ready"))
    if scheduler_ready and args.activate:
        _set_project_active(project, True)
        activated, activation_message = activate_existing_user_unit()
    else:
        _set_project_active(project, False)
        activated = False
        if not result["scheduler_configured"]:
            activation_message = "Scheduler URL and readable token are required; activation was skipped"
        elif not scheduler_ready:
            missing = access_report.get("missing_actions", [])
            if missing:
                activation_message = "Scheduler principal is missing required grants: " + ", ".join(missing)
            else:
                activation_message = "Scheduler access probe failed: " + str(access_report.get("error") or "unknown error")
        else:
            activation_message = "activation was skipped"
    if not scheduler_ready:
        readiness = "incomplete: Scheduler access must succeed and include the required owner-scoped grants"
    elif not args.activate:
        readiness = "configured: Scheduler grants verified; activation skipped"
    elif activated:
        readiness = "ready"
    else:
        readiness = "incomplete: Scheduler grants are valid, but the preinstalled Autobench coordinator service did not start"
    result.update({"initialized": True, "service_activated": activated, "activation": activation_message,
                   "scheduler_access": access_report, "readiness": readiness})
    print(json.dumps(result, indent=2))
    return 0


def _cmd_register(args: argparse.Namespace) -> int:
    project = _project(args.project)
    if not (project / ".autobench").exists():
        raise AutobenchError("run `autobench init` before registering benchmarks")
    if args.file:
        if args.name or args.include or args.argv:
            raise AutobenchError("--file registration cannot be combined with inline name/include/argv")
        path = register_definition(project, args.file, replace_revision=args.replace_revision)
    else:
        if not args.name:
            raise AutobenchError("register requires NAME or --file PATH")
        if args.replace_revision:
            raise AutobenchError("--replace-revision is only valid with --file")
        path = register_benchmark(project, args.name, include=args.include, argv=args.argv,
                                  expected_seconds=args.expected_seconds, kill_seconds=args.kill_seconds, kind=args.kind)
    print(path)
    return 0


async def _async_action(args: argparse.Namespace) -> int:
    if args.command == "serve" and args.project is None:
        await _serve_global()
        return 0
    project = _project(args.project)
    engine = _engine(project, require_scheduler=args.command in {"serve", "reconcile"})
    try:
        if args.command == "doctor":
            result = await engine.doctor()
        elif args.command == "serve":
            await engine.run()
            return 0
        elif args.command == "status":
            result = engine.store.status()
            result["registrations"] = [b.name for b in load_registry(project)]
            result["retry_cooldowns"] = engine.retry_cooldowns()
        elif args.command == "results":
            names = [b.name for b in load_registry(project)] if args.name == "all" else [args.name]
            result = {name: engine.store.attempts_for(name) for name in names}
        elif args.command == "compare":
            result = _compare(project, engine, args.name)
        elif args.command == "explain":
            result = engine.explain(args.name)
        elif args.command == "pause":
            engine.store.set("paused", True)
            catalog = Catalog(create=False)
            try:
                catalog.set_active(project, False)
            finally:
                catalog.close()
            result = {"paused": True}
        elif args.command == "resume":
            engine.store.set("paused", False)
            config = json.loads((project / ".autobench/config.json").read_text(encoding="utf-8"))
            config["active"] = True
            temporary = project / ".autobench/config.json.tmp"
            temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            temporary.replace(project / ".autobench/config.json")
            catalog = Catalog(create=False)
            try:
                catalog.enroll(project, active=True)
            finally:
                catalog.close()
            activated, activation_message = activate_existing_user_unit()
            result = {"paused": False, "service_activated": activated, "activation": activation_message}
        else:
            result = await engine.reconcile_once()
        print(json.dumps(result, indent=2, default=str))
        return 0
    finally:
        engine.close()


def _compare(project: Path, engine: Engine, name: str) -> dict[str, Any]:
    out = project / ".autobench" / "evidence" / name
    comparisons = []
    for packet in sorted((p for p in out.iterdir() if p.is_dir()), key=lambda p: p.name) if out.exists() else []:
        current_file = packet / "current.json"
        baseline_file = packet / "baseline.json"
        if not current_file.exists() or not baseline_file.exists():
            continue
        cur = json.loads(current_file.read_text()).get("metrics", {})
        current = json.loads(current_file.read_text())
        baseline = json.loads(baseline_file.read_text())
        old = baseline.get("metrics", {})
        differences = []
        if current.get("protocol_fingerprint") != baseline.get("protocol_fingerprint"):
            differences.append("measurement protocol differs")
        if current.get("data_fingerprint") != baseline.get("data_fingerprint"):
            differences.append("registered data identity differs")
        current_device = current.get("device_identity")
        baseline_device = baseline.get("device_identity")
        if not current_device or not baseline_device:
            differences.append("device identity is incomplete")
        elif current_device != baseline_device:
            differences.append("device identity differs")
        deltas = {}
        if not differences:
            for key, value in cur.items():
                prior = old.get(key)
                if isinstance(value, (int, float)) and isinstance(prior, (int, float)):
                    deltas[key] = {"baseline": prior, "current": value, "delta": value-prior, "relative": (value-prior)/prior if prior else None}
        comparisons.append({"packet": packet.name, "comparable": not differences, "reasons": differences,
                            "identity": {"current_protocol": current.get("protocol"), "baseline_protocol": baseline.get("protocol"),
                                         "current_build": current.get("build_fingerprint"), "baseline_build": baseline.get("build_fingerprint"),
                                         "current_source": current.get("source_fingerprint"), "baseline_source": baseline.get("source_fingerprint"),
                                         "current_data": current.get("data_fingerprint"), "baseline_data": baseline.get("data_fingerprint"),
                                         "current_device": current_device, "baseline_device": baseline_device},
                            "metrics": deltas})
    return {"benchmark": name, "comparisons": comparisons}


def _explain(engine: Engine, name: str) -> dict[str, Any]:
    status = engine.store.status()
    rows = [a for a in status["attempts"] if a["benchmark"] == name]
    coverage = [c for c in status["coverage"] if c["benchmark"] == name]
    return {"benchmark": name, "coverage": coverage, "recent_attempts": rows[:16],
            "reason": "coverage is current for the recorded source fingerprint" if coverage else "no complete result has been recorded"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autobench", description="Continuously maintain registered benchmark evidence")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="initialize and enroll a project")
    init.add_argument("--project", default=".")
    init.add_argument("--scheduler-url")
    init.add_argument("--token-file")
    init.add_argument("--retained-cap-gib", type=float, default=1.0)
    init.add_argument("--activate", action=argparse.BooleanOptionalAction, default=True)
    init.add_argument("--agent-instructions", choices=("write", "skip"), default="write")
    reg = sub.add_parser("register", help="register a bounded argv benchmark packet")
    reg.add_argument("name", nargs="?")
    reg.add_argument("--project", default=".")
    reg.add_argument("--file", help="import and validate a complete TOML registration")
    reg.add_argument("--replace-revision", action="store_true", help="replace only with a higher explicit TOML revision")
    reg.add_argument("--include", action="append", default=[])
    reg.add_argument("--expected-seconds", type=int, default=5)
    reg.add_argument("--kill-seconds", type=int, default=30)
    reg.add_argument("--kind", default="correctness")
    for name in ("status", "doctor", "serve", "reconcile"):
        p = sub.add_parser(name)
        p.add_argument("--project", default=None if name == "serve" else ".")
    sub.add_parser("workspaces", help="list globally enrolled workspaces")
    for name in ("results", "compare", "explain"):
        p = sub.add_parser(name)
        p.add_argument("name", nargs="?", default="all" if name == "results" else "")
        p.add_argument("--project", default=".")
    for name in ("pause", "resume"):
        p = sub.add_parser(name)
        p.add_argument("--project", default=".")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw_args = list(sys.argv[1:] if argv is None else argv)
    packet_argv: list[str] = []
    parse_args = raw_args
    if raw_args and raw_args[0] == "register":
        try:
            separator = raw_args.index("--")
        except ValueError:
            pass
        else:
            parse_args = raw_args[:separator]
            packet_argv = raw_args[separator + 1:]
    args = parser.parse_args(parse_args)
    if args.command == "register":
        args.argv = packet_argv
    try:
        if args.command == "init":
            return asyncio.run(_cmd_init(args))
        if args.command == "register":
            if args.file and args.argv:
                parser.error("--file registration does not accept trailing argv")
            if not args.file and not args.argv:
                parser.error("register requires argv after --")
            return _cmd_register(args)
        if args.command == "workspaces":
            try:
                catalog = Catalog(create=False)
            except FileNotFoundError:
                print("[]")
                return 0
            try:
                print(json.dumps([str(path) for path in catalog.workspaces(active_only=False)], indent=2))
            finally:
                catalog.close()
            return 0
        return asyncio.run(_async_action(args))
    except (AutobenchError, ValueError) as exc:
        print(f"autobench: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

