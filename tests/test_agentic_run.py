import asyncio
import json
from pathlib import Path

import pytest

from autobench.api import start_run, wait_run
from autobench.engine import Engine
from autobench.store import Store
from autobench.worker import execute


class LocalScheduler:
    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch
        self.jobs = {}
        self.specs = []
        self.complete = True

    async def call(self, operation, arguments):
        if operation == "access.inspect":
            return {"principal_id": "test", "max_priority": 10}
        if operation == "jobs.submit":
            spec = arguments["spec"]
            assert spec["argv"][1:3] == ["-m", "autobench.worker"]
            self.specs.append(spec)
            job_id = str(len(self.specs))
            if self.complete:
                with self.monkeypatch.context() as env:
                    env.setenv("DEVICE_SCHEDULER_JOB_ID", job_id)
                    env.setenv("DEVICE_SCHEDULER_ATTEMPT_ID", f"attempt-{job_id}")
                    assert execute(Path(spec["argv"][3])) == 0
            self.jobs[job_id] = {"id": job_id, "state": "succeeded" if self.complete else "running",
                                 "cleanup_verified": self.complete}
            return {"job": self.jobs[job_id]}
        if operation == "jobs.get":
            return {"job": self.jobs[arguments["job_id"]]}
        raise AssertionError(f"Unexpected Scheduler operation {operation}")


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    managed = root / ".autobench"
    (managed / "registry").mkdir(parents=True)
    (managed / "config.json").write_text(json.dumps(
        {"active": True, "scheduler_url": "http://scheduler", "token_file": "/unused"}
    ))
    Store(managed / "state" / "autobench.sqlite3").close()
    tool = root / "bench"
    tool.write_text("#!/bin/sh\ncat inputs.txt\n")
    tool.chmod(0o755)
    (root / "inputs.txt").write_text("dirty input\n")
    return root


RECIPE = """
[benchmark]
name = "sample"
[source]
include = ["bench", "inputs.txt"]
[[packet]]
name = "timing"
kind = "timing"
argv = ["./bench"]
expected_seconds = 1
kill_seconds = 5
"""


@pytest.mark.parametrize("form", ["registered", "recipe", "argv"])
def test_run_freezes_inputs_and_keeps_explicit_evidence(project, monkeypatch, form):
    scheduler = LocalScheduler(monkeypatch)
    registry = project / ".autobench" / "registry"
    if form == "registered":
        (registry / "recipe.toml").write_text(RECIPE)
        options = {"name": "sample"}
    elif form == "recipe":
        recipe = project / "recipe.toml"
        recipe.write_text(RECIPE)
        options = {"definition": recipe}
    else:
        options = {"argv": ("./bench",), "include": ("bench", "inputs.txt")}

    async def run():
        accepted = await start_run(project, scheduler=scheduler, **options)
        assert accepted["state"] == "accepted", accepted
        run_id = accepted["run_id"]
        assert not scheduler.specs
        (project / "inputs.txt").write_text("new live input\n")
        result = await wait_run(project, run_id, scheduler=scheduler, wait_seconds=6)
        assert result["state"] == "succeeded", result
        assert result["packets"][0]["stdout_tail"] == "dirty input\n"
        assert Path(result["packets"][0]["evidence"], "stdout.log").read_text() == "dirty input\n"
        assert (await wait_run(project, run_id, scheduler=scheduler, wait_seconds=0)) == result
        assert len(scheduler.specs) == 1

    asyncio.run(run())
    assert len(list(registry.glob("*.toml"))) == (1 if form == "registered" else 0)


def test_foreground_denial_submits_no_job(project, monkeypatch):
    scheduler = LocalScheduler(monkeypatch)
    result = asyncio.run(start_run(project, scheduler=scheduler, argv=("./bench",),
                                   include=("bench",), foreground=True))
    assert result["reason_code"] == "foreground_denied"
    assert not scheduler.specs


def test_reconnect_imports_completed_attempt_while_paused(project, monkeypatch):
    scheduler = LocalScheduler(monkeypatch)
    scheduler.complete = False

    async def run():
        accepted = await start_run(project, scheduler=scheduler, argv=("./bench",),
                                   include=("bench", "inputs.txt"))
        run_id = accepted["run_id"]
        assert (await wait_run(project, run_id, scheduler=scheduler,
                               wait_seconds=0.1))["reason_code"] == "wait_expired"
        engine = Engine(project, scheduler)
        try:
            engine.store.set("paused", True)
        finally:
            engine.close()
        scheduler.jobs["1"].update(state="succeeded", cleanup_verified=True)
        manifest = Path(scheduler.specs[0]["argv"][3])
        with monkeypatch.context() as env:
            env.setenv("DEVICE_SCHEDULER_JOB_ID", "1")
            env.setenv("DEVICE_SCHEDULER_ATTEMPT_ID", "attempt-1")
            assert execute(manifest) == 0
        result = await wait_run(project, run_id, scheduler=scheduler, wait_seconds=3)
        assert result["state"] == "succeeded", result
        assert len(scheduler.specs) == 1

    asyncio.run(run())


def test_build_and_packet_dependency_are_reused_without_overwriting_runs(project, monkeypatch):
    scheduler = LocalScheduler(monkeypatch)
    definition = project / "recipe.toml"
    definition.write_text("""
[benchmark]
name = "with-build"
[source]
include = ["inputs.txt"]
[build]
argv = ["/bin/sh", "-c", 'mkdir -p "$1" && cp inputs.txt "$1/built.txt"', "build", "{build}"]
expected_seconds = 1
kill_seconds = 5
[[packet]]
name = "checked"
kind = "correctness"
argv = ["/bin/cp", "{build}/built.txt", "{output}/checked.txt"]
expected_seconds = 1
kill_seconds = 5
[[packet]]
name = "measured"
kind = "timing"
depends_on = ["checked"]
argv = ["/bin/cat", "{artifact.checked}/checked.txt"]
expected_seconds = 1
kill_seconds = 5
""")

    async def run():
        results = []
        for _ in range(2):
            accepted = await start_run(project, scheduler=scheduler, definition=definition)
            assert accepted["state"] == "accepted", accepted
            outcome = await wait_run(project, accepted["run_id"], scheduler=scheduler, wait_seconds=6)
            assert outcome["state"] == "succeeded", outcome
            assert [packet["name"] for packet in outcome["packets"]] == ["checked", "measured"]
            assert outcome["packets"][1]["stdout_tail"] == "dirty input\n"
            results.append(outcome)
        assert len(scheduler.specs) == 5
        assert results[0]["run_id"] != results[1]["run_id"]
        first_evidence = Path(results[0]["packets"][1]["evidence"])
        assert first_evidence != Path(results[1]["packets"][1]["evidence"])
        assert (first_evidence / "stdout.log").read_text() == "dirty input\n"

    asyncio.run(run())


def test_run_acceptance_does_not_wait_for_coordinator_rpc(project, monkeypatch):
    scheduler = LocalScheduler(monkeypatch)
    coordinator = Engine(project, scheduler)
    try:
        with coordinator._workspace_writer_lock() as held:
            assert held
            result = asyncio.run(asyncio.wait_for(
                start_run(project, scheduler=scheduler, argv=("./bench",),
                          include=("bench", "inputs.txt")), timeout=1.0))
        assert result["state"] == "accepted", result
        assert not scheduler.specs
    finally:
        coordinator.close()
