from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from autobench.engine import Engine, _device_identity, _scheduler_resources
from autobench.errors import RegistrationError, UnsafeWorkspace
from autobench.instructions import BLOCK, update_agent_instructions
from autobench.registry import load_registry, parse_registration
from autobench.snapshot import capture
from autobench.store import Store
from autobench.worker import execute
from autobench.cli import _compare
from autobench.cli import main as cli_main
from autobench.api import init_project, register_benchmark, register_definition
from autobench.catalog import Catalog
from autobench.models import Command
from autobench.source_trace import SourceTrace


def create_test_store(project: Path) -> Store:
    path = project / ".autobench/state/autobench.sqlite3"
    store = Store(path, create=True)
    store.close()
    return Store(path, create=False)


class FakeScheduler:
    def __init__(self):
        self.calls = []
        self.job = {"id": "job-1", "state": "running"}
        self.job_spec = None
        self.deny_access = False
        self.forecast_by_packet = {}

    async def call(self, operation, arguments=None):
        self.calls.append((operation, arguments or {}))
        if operation == "access.inspect":
            if self.deny_access:
                raise RuntimeError("access denied")
            return {"principal_id": "agent-principal"}
        if operation == "jobs.submit":
            self.job_spec = arguments["spec"]
            return {"job": {"id": "job-1", "state": "queued"}}
        if operation == "jobs.get":
            return {"job": self.job}
        if operation == "jobs.control":
            return {"job_id": arguments.get("job_id"), "accepted": True}
        if operation == "resources.list":
            return {"resources": []}
        if operation == "resources.forecast":
            packet = arguments["request"]["owner_context"]["packet"]
            return self.forecast_by_packet.get(packet, {"candidates": []})
        raise AssertionError(f"unexpected Scheduler operation {operation}")


class AutobenchCoreTests(unittest.TestCase):
    def test_agent_block_insert_update_and_surrounding_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "AGENTS.md"
            prefix = "# Agent rules\r\nkeep exact\n"
            suffix = "\nOther tool block.\n"
            path.write_bytes((prefix + suffix).encode())
            update_agent_instructions(path)
            first = path.read_bytes().decode()
            self.assertTrue(first.startswith(prefix))
            self.assertIn(suffix, first)
            update_agent_instructions(path)
            self.assertEqual(path.read_bytes().decode(), first)
            path.write_bytes(first.replace("Route benchmark", "Route benchmark").encode())
            self.assertEqual(update_agent_instructions(path), first)

    def test_agent_block_duplicate_and_malformed_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "AGENTS.md"
            path.write_text(BLOCK + "\n" + BLOCK, encoding="utf-8")
            with self.assertRaises(ValueError):
                update_agent_instructions(path)
            path.write_text("<!-- agent-interface:autobench:start -->\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                update_agent_instructions(path)

    def test_registry_requires_argv_and_bounded_expected_kill(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "bad.toml"
            path.write_text('''[benchmark]\nname="bad"\n[source]\ninclude=["src/**"]\n[[packet]]\nname="x"\nargv="echo hi"\nexpected_seconds=2\nkill_seconds=3\n''')
            with self.assertRaises(RegistrationError):
                parse_registration(path, root)
            path.write_text('''[benchmark]\nname="bad"\n[source]\ninclude=["../outside"]\n[[packet]]\nname="x"\nargv=["true"]\nexpected_seconds=2\nkill_seconds=3\n''')
            with self.assertRaises(RegistrationError):
                parse_registration(path, root)
            path.write_text('''[benchmark]\nname="bad"\n[source]\ninclude=["src/**"]\n[[packet]]\nname="x"\nargv=["true"]\nexpected_seconds=3\nkill_seconds=3\n''')
            with self.assertRaises(RegistrationError):
                parse_registration(path, root)

    def test_snapshot_captures_declared_files_excluding_autobench_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "src").mkdir()
            (root / "src/a.py").write_text("one")
            (root / ".autobench/registry").mkdir(parents=True)
            (root / ".autobench/registry/example.toml").write_text("data")
            try:
                (root / "src/link.py").symlink_to(root / "src/a.py")
            except OSError:
                pass
            registration = root / "bench.toml"
            registration.write_text('''[benchmark]\nname="snapshot"\n[source]\ninclude=["src/**"]\n[[packet]]\nname="run"\nargv=["python3","-c","pass"]\nexpected_seconds=1\nkill_seconds=2\n''')
            benchmark = parse_registration(registration, root)
            snap = capture(benchmark, root / ".autobench/state")
            self.assertIn("src/a.py", snap.manifest)
            self.assertNotIn(".autobench/registry/example.toml", snap.manifest)
            self.assertEqual((snap.directory / "src/a.py").read_text(), "one")

    def test_snapshot_rejects_matching_file_added_during_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "src").mkdir()
            (root / "src/a.cu").write_text("one")
            registration = root / "bench.toml"
            registration.write_text('''[benchmark]\nname="race"\n[source]\ninclude=["src/**"]\n[[packet]]\nname="run"\nargv=["true"]\nexpected_seconds=1\nkill_seconds=2\n''')
            benchmark = parse_registration(registration, root)
            import autobench.snapshot as snapshot_module
            original = snapshot_module.shutil.copyfile
            added = False
            def copy_then_add(src, dst):
                nonlocal added
                result = original(src, dst)
                if not added:
                    (root / "src/new.cu").write_text("two")
                    added = True
                return result
            with patch.object(snapshot_module.shutil, "copyfile", side_effect=copy_then_add):
                with self.assertRaises(UnsafeWorkspace):
                    capture(benchmark, root / ".autobench/state")

    def test_registered_autobench_data_is_snapshotted_without_scanning_other_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".autobench/data/fixture").mkdir(parents=True)
            (root / ".autobench/data/fixture/input.bin").write_bytes(b"fixture")
            (root / ".autobench/state").mkdir()
            (root / ".autobench/state/other.bin").write_bytes(b"state")
            registration = root / "bench.toml"
            registration.write_text('''[benchmark]\nname="data"\n[data]\npaths=[".autobench/data/fixture/**"]\n[[packet]]\nname="run"\nargv=["python3","runner.py"]\nexpected_seconds=1\nkill_seconds=2\n''')
            benchmark = parse_registration(registration, root)
            snap = capture(benchmark, root / ".autobench/state")
            self.assertIn(".autobench/data/fixture/input.bin", snap.manifest)
            self.assertNotIn(".autobench/state/other.bin", snap.manifest)
            self.assertEqual((snap.directory / ".autobench/data/fixture/input.bin").read_bytes(), b"fixture")

    def test_store_keeps_durable_submit_intent_and_compact_coverage(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "state.sqlite3")
            attempt = store.begin_attempt("x", 1, 0, "fingerprint")
            self.assertEqual(store.unresolved()[0]["state"], "submit_intent")
            store.set_job(attempt, "job-1")
            store.mark_current("x", 0, "fingerprint", attempt)
            self.assertEqual(store.current("x", 0), "fingerprint")
            store.close()

    def test_worker_requires_scheduler_job_context(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "packet.json"
            manifest.write_text(json.dumps({"argv": ["python3", "-c", "pass"], "working_directory": str(root),
                                            "allowed_root": str(root), "result_directory": str(root / "out"), "kill_seconds": 2}))
            with self.assertRaises(RuntimeError):
                execute(manifest)

    def test_public_api_initializes_enrolls_and_registers_without_cli_duplication(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "XDG_STATE_HOME": str(Path(temp) / "state-home"),
            "XDG_CONFIG_HOME": str(Path(temp) / "config-home"),
        }):
            project = Path(temp) / "project"
            project.mkdir()
            result = init_project(project, agent_instructions="skip", activate=True)
            self.assertTrue(result["active"])
            self.assertEqual((project / ".autobench/.gitignore").read_text(), "*\n!.gitignore\n!registry/\n!registry/**\n")
            self.assertFalse((project / "AGENTS.md").exists())
            catalog = Catalog()
            try:
                self.assertEqual(catalog.workspaces(), [project.resolve()])
            finally:
                catalog.close()
            (project / "src").mkdir()
            registration = register_benchmark(project, "api", include=["src/**"], argv=["python3", "-c", "pass"], expected_seconds=1, kill_seconds=2)
            self.assertTrue(registration.exists())
            self.assertEqual(load_registry(project)[0].name, "api")

    def test_init_preserves_existing_autobench_gitignore(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "XDG_STATE_HOME": str(Path(temp) / "state-home"),
            "XDG_CONFIG_HOME": str(Path(temp) / "config-home"),
        }):
            project = Path(temp) / "project"
            project.mkdir()
            autobench = project / ".autobench"
            autobench.mkdir()
            ignore = autobench / ".gitignore"
            ignore.write_bytes(b"custom local rules\r\n")
            init_project(project, agent_instructions="skip", activate=False)
            self.assertEqual(ignore.read_bytes(), b"custom local rules\r\n")

    def test_reconcile_submits_only_scheduler_worker_and_imports_after_verified_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "src").mkdir()
            (project / "src/input.cu").write_text("kernel")
            autobench = project / ".autobench"
            (autobench / "registry").mkdir(parents=True)
            (autobench / "config.json").write_text(json.dumps({"scheduler_url": "http://scheduler", "token_file": "/tmp/token"}))
            (autobench / "registry/bench.toml").write_text('''[benchmark]\nname="demo"\nrevision=1\n[source]\ninclude=["src/**"]\n[[packet]]\nname="timing"\nkind="timing"\nargv=["python3","runner.py","{snapshot}","{output}"]\nexpected_seconds=5\nkill_seconds=30\nresources={cpu_cores=1}\n''')
            scheduler = FakeScheduler()
            create_test_store(project).close()
            engine = Engine(project, scheduler)
            try:
                first = asyncio.run(engine.reconcile_once())
                self.assertEqual(first["state"], "idle")
                seen = engine.store.get("seen:demo")
                seen["since"] = time.time() - 3
                engine.store.set("seen:demo", seen)
                second = asyncio.run(engine.reconcile_once())
                self.assertEqual(second["state"], "submitted")
                self.assertEqual(scheduler.job_spec["priority_class"], "background")
                self.assertTrue(scheduler.job_spec["flexible"])
                self.assertEqual(scheduler.job_spec["expected_duration_seconds"], 5)
                self.assertEqual(scheduler.job_spec["kill_timeout_seconds"], 30)
                self.assertEqual(scheduler.job_spec["owner_id"], "agent-principal")
                self.assertEqual(scheduler.job_spec["argv"][1:3], ["-m", "autobench.worker"])
                key_version, key_expiry, key_unique = scheduler.job_spec["idempotency_key"].split(":", 2)
                self.assertEqual(key_version, "autobench-v1")
                self.assertGreater(int(key_expiry), int(time.time()) + 6 * 24 * 60 * 60)
                self.assertTrue(key_unique)
                self.assertGreater(engine._reserved_bytes(), 0)
                attempt = engine.store.unresolved()[0]
                details = engine.store.get(f"attempt:{attempt['id']}")
                staged = Path(details["staged"])
                staged.mkdir(parents=True, exist_ok=True)
                (staged / "result.json").write_text(json.dumps({"status": "succeeded", "returncode": 0}))
                (staged / "metrics.json").write_text(json.dumps({"median_ms": 1.25}))
                scheduler.job = {"id": "job-1", "state": "succeeded", "cleanup_verified": True}
                self.assertEqual(asyncio.run(engine._reconcile_jobs()), 1)
                self.assertEqual(engine._reserved_bytes(), 0)
                self.assertEqual(engine.store.current("demo", 0), attempt["fingerprint"])
                pointer = json.loads((autobench / "evidence/demo/0/current.json").read_text())
                self.assertEqual(pointer["metrics"]["median_ms"], 1.25)
                (project / "src/input.cu").write_text("kernel changed")
                explanation = engine.explain("demo")
                self.assertEqual(explanation["packets"][0]["state"], "stale_or_missing")
                self.assertIn("changed", explanation["packets"][0]["reasons"][0])
            finally:
                engine.close()

    def test_scheduler_wire_requirements_match_actual_inventory_dto_and_allocator(self):
        try:
            from device_scheduler_adapters import Ledger
            from device_scheduler_core import Resource, ResourceAllocator, ResourceRequirement
        except ImportError:
            self.skipTest("optional Device Scheduler packages are unavailable")
        with tempfile.TemporaryDirectory() as temp:
            ledger = Ledger(Path(temp) / "scheduler.sqlite3")
            allocator = ResourceAllocator(ledger)
            host = "host-test"
            gpu_id = f"gpu:{host}:GPU-TEST"
            for resource in (
                Resource(f"host:{host}:cpu", host, "cpu", {"cores": 8}),
                Resource(f"host:{host}:memory", host, "memory", {"bytes": 16 * 1024**3}),
                Resource(gpu_id, host, "gpu", {"memory_bytes": 8 * 1024**3}, attributes={
                             "uuid": "GPU-TEST", "inventory_observed_at": datetime.now(timezone.utc).isoformat(),
                             "process_observed_at": datetime.now(timezone.utc).isoformat(),
                             "memory_free_bytes_observation": 8 * 1024**3,
                             "process_telemetry": "compute-apps-visible", "process_telemetry_state": "observed",
                             "process_count_observation": 0, "process_identity_unknown_count": 0,
                             "unmanaged_process_count": 0,
                         },
                         conflict_domains=frozenset({f"gpu-exclusive:{host}:GPU-TEST"})),
            ):
                allocator.register_resource(resource)
            raw = {"cpu_cores": 2, "host_memory_bytes": 1024**3, "gpu_memory_bytes": 512 * 1024**2}
            requirements, tags = _scheduler_resources(raw, [gpu_id])
            self.assertEqual([(item["kind"], item["quantities"]) for item in requirements], [
                ("cpu", {"cores": 2}), ("memory", {"bytes": 1024**3}),
                ("gpu", {"memory_bytes": 512 * 1024**2}),
            ])
            self.assertEqual(requirements[-1]["selectors"], {"resource_id": gpu_id})
            self.assertEqual(requirements[-1]["mode"], "exclusive")
            self.assertIn("gpu.exclusive", tags)
            allocation = allocator.preview_allocation([ResourceRequirement.from_dict(item) for item in requirements])
            self.assertEqual({item.resource_id for item in allocation}, {
                f"host:{host}:cpu", f"host:{host}:memory", gpu_id,
            })

    def test_device_identity_reads_attempt_execution_context_grant(self):
        job = {"attempt": {"execution_context": {
            "allocations": [{"resource_id": "gpu:host:GPU-1", "quantities": {"memory_bytes": 100}, "mode": "exclusive"}],
            "resources": [{"resource_id": "gpu:host:GPU-1", "host_id": "host", "kind": "gpu",
                           "attributes": {"uuid": "GPU-1", "name": "test", "pci_bus_id": "0000:01:00.0"}}],
        }}}
        identity = _device_identity(job)
        self.assertEqual(identity[0]["resource_id"], "gpu:host:GPU-1")
        self.assertEqual(identity[0]["uuid"], "GPU-1")
        self.assertEqual(identity[0]["host_id"], "host")

    def test_gpu_forecast_binds_uuid_and_quiescence_to_scheduler_job(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / ".autobench/registry").mkdir(parents=True)
            (project / ".autobench/config.json").write_text("{}")
            create_test_store(project).close()
            scheduler = FakeScheduler()
            scheduler.forecast_by_packet["profile"] = {"candidates": [{
                "feasible": True, "resource_ids": ["gpu:worker-a:GPU-123"],
                "earliest_start": time.time(), "expected_release": time.time() + 120,
                "uncertainty_seconds": 0,
            }]}
            engine = Engine(project, scheduler)
            benchmark = type("B", (), {"name": "demo", "revision": 1, "protocol": {}})()
            packet = Command(("python3", "profile.py"), 30, 120,
                             {"gpu_memory_bytes": 1_000_000, "mode": "exclusive"},
                             kind="timeline-profile", name="profile")
            candidate = {"benchmark": benchmark, "command": packet, "index": 0,
                         "identity": {"data_fingerprint": "fixture-v1"},
                         "snapshot": project, "build": project / ".autobench/builds/x",
                         "fingerprint": "fp", "artifacts": {}}
            try:
                chosen = asyncio.run(engine._choose_candidate([candidate]))
                self.assertEqual(chosen["identity"]["selected_resource_ids"], ["gpu:worker-a:GPU-123"])
                asyncio.run(engine._submit(benchmark, project, project / ".autobench/builds/x", packet, 0,
                                           "fp", "timeline-profile", identity=chosen["identity"]))
                spec = scheduler.job_spec
                self.assertEqual(spec["resources"][0]["selectors"], {"resource_id": "gpu:worker-a:GPU-123"})
                self.assertEqual(spec["resources"][0]["quantities"], {"memory_bytes": 1_000_000})
                self.assertEqual(spec["preconditions"][0]["name"], "gpu.quiescence")
                from device_scheduler_core import JobSpec
                JobSpec.from_dict(spec)
            finally:
                engine.close()

    def test_duration_estimate_uses_only_compatible_successful_history(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / ".autobench/registry").mkdir(parents=True)
            (project / ".autobench/config.json").write_text("{}")
            create_test_store(project).close()
            engine = Engine(project, FakeScheduler())
            benchmark = type("B", (), {"name": "demo"})()
            command = Command(("python3", "run.py"), 5, 30, {}, name="timing")
            protocol_fp = __import__("hashlib").sha256(b"{}").hexdigest()
            for i, (duration, data, state) in enumerate(((10, "d1", "succeeded"), (20, "d1", "succeeded"), (100, "other", "succeeded"), (90, "d1", "failed"))):
                attempt_id = engine.store.begin_attempt("demo", 1, 0, f"fp{i}")
                engine.store.set_attempt(attempt_id, state, {"worker": {"duration_seconds": duration},
                                                             "identity": {"protocol_fingerprint": protocol_fp,
                                                                          "data_fingerprint": data,
                                                                          "device_identity": []}})
            try:
                estimate = engine._expected_duration(benchmark, 0, command,
                                                     {"protocol": {}, "data_fingerprint": "d1", "selected_resource_ids": []})
                self.assertEqual(estimate, 24)
            finally:
                engine.close()

    def test_retained_cap_reserves_scratch_before_admission(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / ".autobench/registry").mkdir(parents=True)
            (project / ".autobench/config.json").write_text("{}")
            create_test_store(project).close()
            scheduler = FakeScheduler()
            engine = Engine(project, scheduler)
            engine.limits["scratch_limit_bytes"] = 4096
            used = sum(path.stat().st_size for path in (project / ".autobench").rglob("*") if path.is_file())
            engine.store.set("retained_cap_bytes", used + 4095)
            source = project / "registration.toml"
            source.write_text('''[benchmark]\nname="quota"\n[source]\ninclude=["src/**"]\n[[packet]]\nname="run"\nargv=["true"]\nexpected_seconds=1\nkill_seconds=2\n''')
            benchmark = parse_registration(source, project)
            try:
                with self.assertRaisesRegex(Exception, "scratch reservation"):
                    asyncio.run(engine._submit(benchmark, project, project / ".autobench/builds/x", benchmark.packets[0], 0, "fp", "correctness"))
                self.assertFalse(any(operation == "jobs.submit" for operation, _ in scheduler.calls))
                self.assertEqual(engine._reserved_bytes(), 0)
            finally:
                engine.close()

    def test_full_toml_registration_import_is_atomic_and_revision_gated(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(project / "global-state")}):
                init_project(project, agent_instructions="skip", activate=False)
            source = project / "complete.toml"
            source.write_text('''[benchmark]\nname="complete"\nrevision=1\n[source]\ninclude=["src/**"]\n[[packet]]\nname="run"\nargv=["python3","run.py"]\nexpected_seconds=1\nkill_seconds=2\n''')
            target = register_definition(project, source)
            self.assertEqual(target.read_bytes(), source.read_bytes())
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                register_definition(project, source)
            with self.assertRaisesRegex(RuntimeError, "replacement revision"):
                register_definition(project, source, replace_revision=True)
            source.write_text(source.read_text().replace("revision=1", "revision=2"))
            self.assertTrue(register_definition(project, source, replace_revision=True).exists())
            cli_source = project / "via-cli.toml"
            cli_source.write_text('''[benchmark]\nname="via-cli"\nrevision=1\n[source]\ninclude=["src/**"]\n[[packet]]\nname="run"\nargv=["python3","run.py"]\nexpected_seconds=1\nkill_seconds=2\n''')
            with patch("sys.stdout", new_callable=__import__("io").StringIO):
                self.assertEqual(cli_main(["register", "--project", str(project), "--file", str(cli_source)]), 0)
            self.assertTrue((project / ".autobench/registry/via-cli.toml").is_file())

    def test_compare_refuses_incompatible_protocol_device_or_data_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            target = project / ".autobench/evidence/bench/0"
            target.mkdir(parents=True)
            current = {"metrics": {"median_ms": 2.0}, "protocol_fingerprint": "protocol-new",
                       "protocol": {"revision": 2}, "data_fingerprint": "fixture-v2",
                       "device_identity": [{"device_uuid": "GPU-L40"}], "build_fingerprint": "build-new"}
            baseline = {"metrics": {"median_ms": 1.0}, "protocol_fingerprint": "protocol-old",
                        "protocol": {"revision": 1}, "data_fingerprint": "fixture-v1",
                        "device_identity": [{"device_uuid": "GPU-V100"}], "build_fingerprint": "build-old"}
            (target / "current.json").write_text(json.dumps(current))
            (target / "baseline.json").write_text(json.dumps(baseline))
            store = Store(project / ".autobench/state/autobench.sqlite3")
            engine = type("E", (), {"store": store})()
            result = _compare(project, engine, "bench")["comparisons"][0]
            self.assertFalse(result["comparable"])
            self.assertEqual(result["metrics"], {})
            self.assertEqual(len(result["reasons"]), 3)
            store.close()

    def test_build_output_over_total_project_cap_requests_scheduler_cancel(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            autobench = project / ".autobench"
            (autobench / "builds/current").mkdir(parents=True)
            (autobench / "builds/current/large.bin").write_bytes(b"x" * 4096)
            create_test_store(project).close()
            scheduler = FakeScheduler()
            engine = Engine(project, scheduler)
            try:
                engine.store.set("retained_cap_bytes", 1024)
                attempt = engine.store.begin_attempt("builder", 1, -1, "build-fingerprint")
                engine.store.set_job(attempt, "job-1", "running")
                engine.store.set(f"attempt:{attempt}", {"snapshot": str(project / ".autobench/state/snapshots/empty"),
                                                          "build": str(autobench / "builds/current")})
                asyncio.run(engine._reconcile_jobs())
                self.assertTrue(any(op == "jobs.control" for op, _args in scheduler.calls))
                self.assertTrue(engine.store.get(f"cancel_requested:{attempt}"))
            finally:
                engine.close()

    def test_safe_eviction_keeps_latest_verified_registered_build(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            build = project / ".autobench/builds/build-current"
            build.mkdir(parents=True)
            (build / "binary").write_bytes(b"x" * 160_000)
            create_test_store(project).close()
            engine = Engine(project, FakeScheduler())
            try:
                engine.store.set("retained_cap_bytes", 300_000)
                engine.store.set("last-good-build:demo", str(build))
                engine._evict_safe()
                self.assertTrue((build / "binary").exists())
            finally:
                engine.close()

    def test_access_failure_before_submission_does_not_leave_unresolved_intent(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / ".autobench/registry").mkdir(parents=True)
            (project / ".autobench/config.json").write_text("{}")
            create_test_store(project).close()
            scheduler = FakeScheduler()
            scheduler.deny_access = True
            engine = Engine(project, scheduler)
            registration = project / "registration.toml"
            registration.write_text('''[benchmark]\nname="test"\n[source]\ninclude=["src/**"]\n[[packet]]\nname="run"\nargv=["true"]\nexpected_seconds=1\nkill_seconds=2\n''')
            benchmark = parse_registration(registration, project)
            try:
                with self.assertRaises(RuntimeError):
                    asyncio.run(engine._submit(benchmark, project, project / ".autobench/builds/x", benchmark.packets[0], 0, "fingerprint", "correctness"))
                self.assertEqual(engine.store.unresolved(), [])
                self.assertFalse(any(op == "jobs.submit" for op, _args in scheduler.calls))
            finally:
                engine.close()

    def test_forecast_prefers_ready_packet_that_fits_predicted_availability(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / ".autobench/registry").mkdir(parents=True)
            (project / ".autobench/config.json").write_text("{}")
            create_test_store(project).close()
            scheduler = FakeScheduler()
            now = datetime.now(timezone.utc).timestamp()
            scheduler.forecast_by_packet = {
                "long": {"candidates": [{"feasible": True, "earliest_start": now,
                                           "expected_release": now + 10, "uncertainty_seconds": 0}]},
                "short": {"candidates": [{"feasible": True, "earliest_start": now,
                                            "expected_release": now + 10, "uncertainty_seconds": 0}]},
            }
            engine = Engine(project, scheduler)
            long = Command(("python3", "long.py"), 15, 30, {"cpu_cores": 1}, name="long")
            short = Command(("python3", "short.py"), 2, 5, {"cpu_cores": 1}, name="short")
            candidates = [
                {"benchmark": type("B", (), {"name": "demo"})(), "command": long, "identity": {}},
                {"benchmark": type("B", (), {"name": "demo"})(), "command": short, "identity": {}},
            ]
            try:
                chosen = asyncio.run(engine._choose_candidate(candidates))
                self.assertEqual(chosen["command"].name, "short")
                self.assertTrue(any(op == "resources.forecast" for op, _args in scheduler.calls))
                self.assertEqual(chosen["identity"]["availability_forecast"]["candidates"][0]["feasible"], True)
            finally:
                engine.close()

    def test_missing_state_authority_is_not_silently_recreated(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / ".autobench/registry").mkdir(parents=True)
            (project / ".autobench/config.json").write_text('{"active": true}')
            with self.assertRaises(RuntimeError):
                init_project(project, agent_instructions="skip")
            with self.assertRaises(Exception):
                Engine(project, FakeScheduler())
            self.assertFalse((project / ".autobench/state/autobench.sqlite3").exists())

    def test_optional_source_trace_indexes_only_declared_snapshot_not_local_state(self):
        try:
            import project_source  # noqa: F401
        except ImportError:
            self.skipTest("optional project-source package is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "non-git-project"
            (project / "src").mkdir(parents=True)
            (project / "src/input.py").write_text("def input_value():\n    return 1\n")
            (project / ".autobench/state").mkdir(parents=True)
            (project / ".autobench/state/private-state.py").write_text("def private_state():\n    return 2\n")
            (project / ".autobench/evidence/history").mkdir(parents=True)
            (project / ".autobench/evidence/history/large-history.py").write_text("def historical_evidence():\n    return 3\n")
            (project / ".autobench/registry").mkdir(parents=True)
            registration = project / ".autobench/registry/trace.toml"
            registration.write_text('''[benchmark]\nname="trace"\n[source]\ninclude=["src/**"]\n[[packet]]\nname="run"\nargv=["true"]\nexpected_seconds=1\nkill_seconds=2\n''')
            benchmark = parse_registration(registration, project)
            captured = capture(benchmark, project / ".autobench/state")
            trace = SourceTrace(project, project / ".autobench/state")
            summary = trace.incoming_summary(captured.directory, list(captured.manifest))
            self.assertIsNotNone(summary)
            self.assertEqual(summary["coverage"], "declared input scope")
            with sqlite3.connect(trace.index.db_path) as db:
                indexed = {row[0] for row in db.execute("SELECT path FROM files")}
            self.assertEqual(indexed, {"src/input.py"})
            bounded = SourceTrace(project, project / ".autobench/state/bounded-trace")
            skipped = bounded.incoming_summary(captured.directory, list(captured.manifest), input_budget_bytes=0)
            self.assertFalse(skipped["complete"])
            self.assertIn("no retained-data headroom", skipped["omissions"][0])
            self.assertIsNone(bounded.index)


if __name__ == "__main__":
    unittest.main()

