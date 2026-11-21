import asyncio
import copy
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from autobench.engine import Engine
from autobench.models import Benchmark, Command
from autobench.store import Store


class SchedulerDouble:
    def __init__(self):
        self.jobs = {}
        self.requests = []
        self.forecast = None
        self.lose_response = False
        self.rejection = None

    async def call(self, operation, arguments=None):
        if operation == "access.inspect":
            return {"principal_id": "bench-agent", "max_priority": 80}
        if operation == "resources.forecast":
            return self.forecast
        if operation == "jobs.submit":
            self.requests.append(copy.deepcopy(arguments))
            if self.rejection:
                return {"error": self.rejection}
            spec = arguments["spec"]
            job = self.jobs.setdefault(spec["idempotency_key"], {
                "id": f"job-{len(self.jobs)}",
                "state": "queued",
                "idempotency_key": spec["idempotency_key"],
            })
            if self.lose_response:
                self.lose_response = False
                raise TimeoutError("connection lost after submit")
            return {"job": job}
        if operation == "jobs.list":
            return {"jobs": list(self.jobs.values())}
        if operation == "jobs.get":
            return {"job": next(job for job in self.jobs.values() if job["id"] == arguments["job_id"])}
        raise AssertionError(operation)


@pytest.fixture
def workspace(tmp_path):
    state = tmp_path / ".autobench"
    state.mkdir()
    (state / "config.json").write_text("{}")
    Store(state / "state/autobench.sqlite3").close()
    return tmp_path


def command(resources=None, kind="correctness"):
    return Command(("/usr/bin/true",), 5, 30, resources or {}, kind=kind, name="measure")


def benchmark(project, packet):
    return Benchmark("example", 1, project, ("input",), (), None, (packet,))


async def submit(engine, packet=None, identity=None):
    packet = packet or command()
    return await engine._submit(
        benchmark(engine.project, packet), engine.project,
        engine.autobench / "builds/no-build", packet, 0, "input-id", packet.kind, identity=identity,
    )


def attempt(engine, attempt_id):
    row = next(row for row in engine.store.attempts_for("example") if row["id"] == attempt_id)
    return row["state"], json.loads(row["result_json"])


def test_reversed_forecast_ranks_without_changing_the_declared_selectors(workspace):
    original = [
        {"kind": "gpu", "selectors": {"uuid": "GPU-A"}, "quantities": {"memory_bytes": 4096}},
        {"kind": "gpu", "selectors": {"uuid": "GPU-B"}, "quantities": {"memory_bytes": 1024}},
    ]
    scheduler = SchedulerDouble()
    scheduler.forecast = {
        "inventory": {"stale": False},
        "candidates": [{"feasible_now": True, "window_seconds": 40,
                        "resource_ids": ["gpu:host:GPU-B", "gpu:host:GPU-A"]}],
    }
    engine = Engine(workspace, scheduler)
    packet = command({"scheduler": original}, kind="timing")
    candidate = {"command": packet, "benchmark": benchmark(workspace, packet), "identity": {}}
    try:
        asyncio.run(engine._choose_candidate([candidate]))
        assert candidate["identity"]["forecast_ready"]
        asyncio.run(submit(engine, packet, candidate["identity"]))
        spec = scheduler.requests[-1]["spec"]
        assert spec["resources"] == original
        assert spec["preconditions"][0]["parameters"]["max_utilization_percent"] == 0

        scheduler.forecast["inventory"]["stale"] = True
        asyncio.run(engine._choose_candidate([candidate]))
        assert not candidate["identity"]["forecast_ready"]
        assert candidate["identity"]["forecast_wait_reason"] == "inventory_stale"
    finally:
        engine.close()


def test_explicit_gpu_request_sets_admission_deadline_and_correctness_gate(workspace):
    scheduler = SchedulerDouble()
    engine = Engine(workspace, scheduler)
    try:
        engine.store.begin_run("run-1", {})
        start = datetime.now(timezone.utc)
        attempt_id = asyncio.run(submit(engine, command({"gpu_memory_bytes": 4096}),
                                       {"run_id": "run-1", "admission_seconds": 7, "foreground": True}))
        spec = scheduler.requests[-1]["spec"]
        assert 7 <= (datetime.fromisoformat(spec["deadline"]) - start).total_seconds() < 8
        assert spec["priority_class"] == "interactive"
        assert spec["priority"] == 80
        assert spec["preconditions"][0]["parameters"]["max_utilization_percent"] == 100
        assert spec["resources"] == [{"kind": "gpu", "quantities": {"memory_bytes": 4096},
                                      "mode": "exclusive", "same_host": True, "topology_constraints": {}}]
        assert engine.store.run_attempts("run-1")[0]["id"] == attempt_id
    finally:
        engine.close()


def test_lost_submit_response_replays_exact_request_after_reopen(workspace):
    scheduler = SchedulerDouble()
    scheduler.lose_response = True
    engine = Engine(workspace, scheduler)
    try:
        attempt_id = asyncio.run(submit(engine))
        assert attempt(engine, attempt_id)[1]["reason_code"] == "submission_unknown"
        request = engine.store.get(f"attempt:{attempt_id}")["scheduler_request"]
        assert request == scheduler.requests[0]
    finally:
        engine.close()
    engine = Engine(workspace, scheduler)
    try:
        asyncio.run(engine._reconcile_jobs())
        assert scheduler.requests == [request, request]
        assert len(scheduler.jobs) == 1
        assert engine.store.unresolved()[0]["job_id"] == "job-0"
    finally:
        engine.close()


def test_paused_reconciliation_observes_existing_intent_without_replay(workspace):
    scheduler = SchedulerDouble()
    scheduler.lose_response = True
    engine = Engine(workspace, scheduler)
    try:
        attempt_id = asyncio.run(submit(engine))
        request = scheduler.requests[0]
        asyncio.run(engine._reconcile_jobs(replay=False))
        assert scheduler.requests == [request]
        assert engine.store.unresolved()[0]["job_id"] == "job-0"
        assert attempt(engine, attempt_id)[0] == "submitted"
    finally:
        engine.close()


def test_authoritative_rejection_cleans_only_an_unaccepted_first_submission(workspace):
    scheduler = SchedulerDouble()
    scheduler.rejection = {"code": "validation_error", "message": "GPU selectors conflict"}
    engine = Engine(workspace, scheduler)
    try:
        attempt_id = asyncio.run(submit(engine))
        state, report = attempt(engine, attempt_id)
        assert state == "not_submitted"
        assert report["reason_code"] == "validation_error"
        assert report["reason"] == "GPU selectors conflict"
        assert not (engine.autobench / "scratch" / attempt_id).exists()
        assert engine.store.unresolved() == []

        scheduler.rejection = None
        scheduler.lose_response = True
        attempt_id = asyncio.run(submit(engine))
        scheduler.rejection = {"code": "permission_denied", "message": "The token was revoked"}
        asyncio.run(engine._reconcile_jobs())
        state, report = attempt(engine, attempt_id)
        assert state == "submit_intent"
        assert report["reason_code"] == "submission_unknown"
        assert engine._reserved_bytes() > 0
    finally:
        engine.close()


@pytest.mark.parametrize(("outcome", "expected_state", "reason"), [
    ({"reason": "deadline_expired"}, "admission_timeout", "deadline_expired"),
    ({"reason": "execution_precondition_unverified"}, "admission_failed", "execution_precondition_unverified"),
    ({"result": "timeout"}, "timed_out", "runtime_timeout"),
])
def test_scheduler_outcome_is_preserved_when_worker_never_reports(workspace, outcome, expected_state, reason):
    scheduler = SchedulerDouble()
    engine = Engine(workspace, scheduler)
    try:
        attempt_id = asyncio.run(submit(engine))
        job = next(iter(scheduler.jobs.values()))
        job.update(state="failed", result=outcome, cleanup_verified=False)
        assert asyncio.run(engine._reconcile_jobs()) == 0
        assert attempt(engine, attempt_id)[1]["reason_code"] == "cleanup_unverified"
        assert engine._reserved_bytes() > 0
        job["cleanup_verified"] = True
        assert asyncio.run(engine._reconcile_jobs()) == 1
        state, report = attempt(engine, attempt_id)
        assert state == expected_state
        assert report["reason_code"] == reason
        assert report["cleanup_verified"]
        assert engine._reserved_bytes() == 0
    finally:
        engine.close()


def test_parallel_attempts_only_credit_their_own_scratch_bytes(workspace):
    scheduler = SchedulerDouble()
    engine = Engine(workspace, scheduler)
    try:
        first = asyncio.run(submit(engine))
        second = asyncio.run(submit(engine))
        scratch = engine.autobench / "scratch"
        (scratch / first / "first.data").write_bytes(b"a" * 81)
        (scratch / second / "second.data").write_bytes(b"b" * 19)
        (engine.autobench / "other.data").write_bytes(b"c" * 100)
        assert engine._reserved_bytes() == 2 * engine.limits["scratch_limit_bytes"] - 100
    finally:
        engine.close()


def test_invalid_worker_metrics_dont_hide_an_independent_result(workspace):
    scheduler = SchedulerDouble()
    engine = Engine(workspace, scheduler)
    try:
        failed = asyncio.run(submit(engine))
        passed = asyncio.run(submit(engine))
        for attempt_id, job in zip((failed, passed), scheduler.jobs.values()):
            staged = engine.autobench / "scratch" / attempt_id
            (staged / "result.json").write_text('{"status":"succeeded"}')
            job.update(state="succeeded", cleanup_verified=True)
        (engine.autobench / "scratch" / failed / "metrics.json").write_text("invalid json")
        assert asyncio.run(engine._reconcile_jobs()) == 2
        assert attempt(engine, failed)[0] == "invalid_output"
        assert attempt(engine, passed)[0] == "succeeded"
    finally:
        engine.close()


def test_run_publication_charges_only_one_copy_of_staging(workspace):
    scheduler = SchedulerDouble()
    engine = Engine(workspace, scheduler)
    try:
        cap = 2 * 1024**2
        engine.store.set("retained_cap_bytes", cap)
        engine.limits = {**engine.limits, "scratch_limit_bytes": cap - 512 * 1024}
        engine.store.begin_run("valid-run", {"snapshot": str(workspace)})
        posted = asyncio.run(submit(engine, identity={"run_id": "valid-run"}))
        stage = engine.autobench / "scratch" / posted
        (stage / "result.json").write_text('{"status":"succeeded"}')
        (stage / "payload.dat").write_bytes(b"a" * (cap * 3 // 4))
        next(iter(scheduler.jobs.values())).update(state="succeeded", cleanup_verified=True)

        assert asyncio.run(engine._reconcile_jobs()) == 1
        assert attempt(engine, posted)[0] == "succeeded"
        target = engine.autobench / "evidence/runs/valid-run/measure"
        assert (target / "input-id/payload.dat").stat().st_size == cap * 3 // 4
        assert not stage.exists()
    finally:
        engine.close()


def test_over_cap_run_discards_only_its_failed_publication(workspace):
    scheduler = SchedulerDouble()
    engine = Engine(workspace, scheduler)
    try:
        cap = 1024**2
        engine.store.set("retained_cap_bytes", cap)
        engine.limits = {**engine.limits, "scratch_limit_bytes": cap // 2}
        engine.store.begin_run("over-cap", {"snapshot": str(workspace)})
        posted = asyncio.run(submit(engine, identity={"run_id": "over-cap"}))
        stage = engine.autobench / "scratch" / posted
        (stage / "result.json").write_text('{"status":"succeeded"}')
        (stage / "stderr.log").write_text("probe diagnostic")
        (stage / "payload.dat").write_bytes(b"a" * cap)
        next(iter(scheduler.jobs.values())).update(state="succeeded", cleanup_verified=True)
        unrelated = engine.autobench / "evidence/other/result.json"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text("retain this")

        assert asyncio.run(engine._reconcile_jobs()) == 1
        state, report = attempt(engine, posted)
        assert state == "storage_over_cap"
        assert unrelated.read_text() == "retain this"
        target = engine.autobench / "evidence/runs/over-cap/measure"
        assert not (target / "input-id").exists()
        assert not (target / "current.json").exists()
        assert (Path(report["evidence"]) / "stderr.log").read_text() == "probe diagnostic"
    finally:
        engine.close()
