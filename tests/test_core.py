from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import venv
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from autobench.engine import (Engine, _device_identity, _measurement_protocol,
                              _protocol_fingerprint, _scheduler_resources)
from autobench.errors import RegistrationError, UnsafeWorkspace
from autobench.instructions import BLOCK, update_agent_instructions
from autobench.registry import load_registry, parse_registration
from autobench.snapshot import capture
from autobench.store import Store
from autobench.worker import execute
from autobench.cli import _compare
from autobench.cli import main as cli_main
from autobench.api import (init_project, pause_project, reconcile_project, register_benchmark,
                           register_definition, resume_project, workspace_compare,
                           workspace_explain, workspace_results, workspace_status)
from autobench.catalog import Catalog
from autobench.models import Benchmark, Command
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
            if packet in self.forecast_by_packet:
                return self.forecast_by_packet[packet]
            resources = []
            current = []
            for requirement in arguments["request"].get("requirements", []):
                kind = requirement.get("kind")
                resource_id = requirement.get("selectors", {}).get("resource_id")
                if not resource_id:
                    resource_id = "gpu:worker-a:GPU-123" if kind == "gpu" else f"host:worker-a:{kind}"
                resources.append(resource_id)
                current.append({"resource_id": resource_id, "kind": kind})
            return {"candidates": [{"candidate_id": "atomic-request", "feasible": True,
                                    "feasible_now": True, "resource_ids": resources,
                                    "earliest_start": datetime.now(timezone.utc).isoformat(),
                                    "expected_release": None, "available_until": time.time() + 600,
                                    "window_seconds": 600, "uncertainty_seconds": 0,
                                    "window_basis": "forecast_horizon_only"}], "current_resources": current}
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

    def test_worker_kills_oversized_child_within_storage_poll_interval(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "src").mkdir()
            managed = project / ".autobench"
            staged = managed / "scratch" / "attempt-1"
            staged.mkdir(parents=True)
            pid_file = project / "child.pid"
            payload = staged / "large-output.bin"
            code = "import os,pathlib,time; pathlib.Path(%r).write_bytes(b'x'*1000000); pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(10)" % (str(payload), str(pid_file))
            manifest = project / "packet.json"
            manifest.write_text(json.dumps({
                "argv": [sys.executable, "-c", code],
                "working_directory": str(project / "src"),
                "allowed_root": str(project),
                "managed_root": str(managed),
                "result_directory": str(staged),
                "scratch_limit_bytes": 64 * 1024,
                "retained_cap_bytes": 10 * 1024 * 1024,
                "output_limit_bytes": 2 * 1024 * 1024,
                "kill_seconds": 20,
            }))
            started = time.monotonic()
            with patch.dict(os.environ, {"DEVICE_SCHEDULER_JOB_ID": "job-test",
                                         "DEVICE_SCHEDULER_ATTEMPT_ID": "attempt-test"}):
                self.assertEqual(execute(manifest), 0)
            elapsed = time.monotonic() - started
            result = json.loads((staged / "result.json").read_text())
            self.assertEqual(result["status"], "storage_over_cap")
            self.assertLess(elapsed, 3)
            child_pid = int(pid_file.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)

    def test_worker_scopes_and_removes_temporary_and_cache_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "src").mkdir()
            staged = project / ".autobench/scratch/attempt-1"
            staged.mkdir(parents=True)
            observed = project / "observed.json"
            code = ("import json,os,pathlib; names=['TMPDIR','TMP','TEMP','XDG_CACHE_HOME','CUDA_CACHE_PATH']; "
                    "paths={n:os.environ[n] for n in names}; "
                    "[pathlib.Path(v,'marker').write_text('ok') for v in paths.values()]; "
                    "pathlib.Path(%r).write_text(json.dumps(paths))") % str(observed)
            manifest = project / "packet.json"
            manifest.write_text(json.dumps({
                "argv": [sys.executable, "-c", code],
                "working_directory": str(project / "src"), "allowed_root": str(project),
                "managed_root": str(project / ".autobench"), "result_directory": str(staged),
                "scratch_limit_bytes": 1024 * 1024, "retained_cap_bytes": 8 * 1024 * 1024,
                "output_limit_bytes": 1024 * 1024, "kill_seconds": 5,
                "environment": {"TMPDIR": str(staged / "explicit-temp")},
            }))
            with patch.dict(os.environ, {"DEVICE_SCHEDULER_JOB_ID": "job-test",
                                         "DEVICE_SCHEDULER_ATTEMPT_ID": "attempt-test",
                                         "TMPDIR": "/tmp", "XDG_CACHE_HOME": "/tmp"}):
                self.assertEqual(execute(manifest), 0)
            paths = json.loads(observed.read_text())
            self.assertEqual(Path(paths["TMPDIR"]), staged / "explicit-temp")
            for path in paths.values():
                self.assertTrue(Path(path).is_relative_to(staged))
                self.assertFalse(Path(path).exists(), path)

    def test_worker_child_python_uses_symlink_venv_and_its_site_packages(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            venv_root = root / "runtime-venv"
            venv.EnvBuilder(with_pip=False, symlinks=True).create(venv_root)
            child_python = venv_root / "bin/python3"
            self.assertTrue(child_python.is_symlink())
            py_version = f"python{sys.version_info.major}.{sys.version_info.minor}"
            site_packages = venv_root / "lib" / py_version / "site-packages"
            site_packages.mkdir(parents=True, exist_ok=True)
            (site_packages / "autobench_venv_probe.py").write_text("VALUE = 'venv-site-package'\n")

            (root / "src").mkdir()
            staged = root / ".autobench/scratch/attempt-venv"
            staged.mkdir(parents=True)
            observed = staged / "import-result.txt"
            code = "import autobench_venv_probe,pathlib; pathlib.Path(%r).write_text(autobench_venv_probe.VALUE)" % str(observed)
            manifest = root / "packet.json"
            manifest.write_text(json.dumps({
                "argv": ["python3", "-c", code], "working_directory": str(root / "src"),
                "allowed_root": str(root), "managed_root": str(root / ".autobench"),
                "result_directory": str(staged), "scratch_limit_bytes": 1024 * 1024,
                "retained_cap_bytes": 8 * 1024 * 1024, "output_limit_bytes": 1024 * 1024,
                "kill_seconds": 5, "environment": {"PATH": "/usr/bin:/bin", "PYTHONPATH": ""},
            }))
            with patch("autobench.worker.sys.executable", str(child_python)), \
                 patch.dict(os.environ, {"DEVICE_SCHEDULER_JOB_ID": "job-test",
                                         "DEVICE_SCHEDULER_ATTEMPT_ID": "attempt-test"}):
                self.assertEqual(execute(manifest), 0)
            self.assertEqual(observed.read_text(), "venv-site-package")
            result = json.loads((staged / "result.json").read_text())
            self.assertEqual(result["status"], "succeeded")

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

    def test_public_workspace_api_queries_controls_and_reconciliation_match_cli(self):
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "XDG_STATE_HOME": str(Path(temp) / "state-home"),
            "XDG_CONFIG_HOME": str(Path(temp) / "config-home"),
        }):
            project = Path(temp) / "project"
            project.mkdir()
            (project / "src").mkdir()
            (project / "src/input.py").write_text("value=1\n")
            init_project(project, agent_instructions="skip", activate=False)
            definition = project / "definition.toml"
            definition.write_text('''[benchmark]
name="demo"
revision=1
[source]
include=["src/**"]
[[packet]]
name="run"
argv=["/usr/bin/true"]
expected_seconds=2
kill_seconds=30
resources={cpu_cores=1}
''')
            register_definition(project, definition)
            scheduler = FakeScheduler()
            self.assertEqual(asyncio.run(reconcile_project(project, scheduler=scheduler))["state"], "idle")
            store = Store(project / ".autobench/state/autobench.sqlite3", create=False)
            try:
                seen = store.get("seen:demo")
                seen["since"] = time.time() - 3
                store.set("seen:demo", seen)
            finally:
                store.close()
            self.assertEqual(asyncio.run(reconcile_project(project, scheduler=scheduler))["state"], "submitted")

            status = workspace_status(project)
            self.assertIn("demo", status["registrations"])
            results = workspace_results(project, "demo")
            self.assertEqual(len(results["demo"]), 1)
            explanation = workspace_explain(project, "demo")
            self.assertEqual(explanation["benchmark"], "demo")
            self.assertEqual(explanation["packets"][0]["name"], "run")

            packet_dir = project / ".autobench/evidence/demo/0"
            packet_dir.mkdir(parents=True)
            registered = load_registry(project)[0]
            protocol = _measurement_protocol(registered, registered.packets[0], "recipe-v1")
            protocol_fp = _protocol_fingerprint(protocol)
            identity = {"protocol": protocol, "protocol_fingerprint": protocol_fp,
                        "source_fingerprint": "source-current", "data_fingerprint": "fixture-v1",
                        "device_identity": [{"resource_id": "gpu:host:GPU-test"}]}
            baseline_identity = {**identity, "source_fingerprint": "source-baseline"}
            for filename, value, record in (("current.json", 2.0, identity), ("baseline.json", 1.0, baseline_identity)):
                (packet_dir / filename).write_text(json.dumps({**record, "metrics": {"median_ms": value}}))
            comparison = workspace_compare(project, "demo")
            self.assertTrue(comparison["comparisons"][0]["comparable"])
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(cli_main(["compare", "demo", "--project", str(project)]), 0)
            self.assertEqual(json.loads(stdout.getvalue()), comparison)

            paused = pause_project(project)
            self.assertTrue(paused["paused"])
            self.assertTrue(workspace_status(project)["paused"])
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(cli_main(["pause", "--project", str(project)]), 0)
            self.assertTrue(json.loads(stdout.getvalue())["paused"])

            resumed = resume_project(project)
            self.assertFalse(resumed["paused"])
            self.assertTrue(resumed["active"])
            with patch("autobench.cli.activate_existing_user_unit", return_value=(True, "started")), \
                 redirect_stdout(stdout := io.StringIO()):
                self.assertEqual(cli_main(["resume", "--project", str(project)]), 0)
            cli_resumed = json.loads(stdout.getvalue())
            self.assertFalse(cli_resumed["paused"])
            self.assertTrue(cli_resumed["active"])

    def test_workspace_results_compacts_receipts_and_cli_details_are_explicit(self):
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "XDG_STATE_HOME": str(Path(temp) / "state-home"),
            "XDG_CONFIG_HOME": str(Path(temp) / "config-home"),
        }):
            project = Path(temp) / "project"
            project.mkdir()
            (project / "src").mkdir()
            (project / "src/input.py").write_text("value=1\n")
            init_project(project, agent_instructions="skip", activate=False)
            registration = project / "definition.toml"
            registration.write_text('''[benchmark]
name="demo"
revision=1
[source]
include=["src/**"]
[[packet]]
name="profile"
argv=["/usr/bin/true"]
expected_seconds=1
kill_seconds=5
''')
            register_definition(project, registration)
            store = Store(project / ".autobench/state/autobench.sqlite3", create=False)
            try:
                fingerprint = "f" * 64
                attempt = store.begin_attempt("demo", 1, 0, fingerprint)
                huge_manifest = {f"src/file-{index}.cpp": "a" * 256 for index in range(200)}
                raw_result = {
                    "job": {"receipt": {"payload": "r" * 50_000}, "logs": {"stdout": "o" * 20_000}},
                    "source_manifest": huge_manifest,
                    "worker": {"duration_seconds": 12.5, "diagnostics": {"stderr_tail": "private raw"}},
                    "identity": {"source_fingerprint": "source-fp", "build_fingerprint": "build-fp",
                                 "data_fingerprint": "data-fp", "protocol_fingerprint": "protocol-fp",
                                 "device_identity": [{"resource_id": "gpu:host:GPU-1", "uuid": "GPU-1",
                                                      "host_id": "host", "kind": "gpu", "attributes": {"large": "x" * 2000}}]},
                }
                store.set_job(attempt, "job-1", "succeeded")
                store.set_attempt(attempt, "succeeded", raw_result)
                store.mark_current("demo", 0, fingerprint, attempt)
            finally:
                store.close()
            artifact = project / ".autobench/evidence/demo/0" / fingerprint
            artifact.mkdir(parents=True)
            early_numeric_fields = {f"a_fixture_{index:02d}": index for index in range(50)}
            (artifact / "metrics.json").write_text(json.dumps({
                **early_numeric_fields, "median_ms": 2.5, "sample_count": 40,
                "timings_ms": {"cuda_complete_postings_bitmap_workset": {
                    "median_ms": 0.662527978, "samples_ms": [0.6, 0.7]}},
                "capture": {"bytes": 48192},
                "argv": ["python", "profile.py"], "label": "discard nonnumeric", "truth": True,
            }))
            (artifact / "result.json").write_text(json.dumps({"scheduler_job_id": "job-1", "status": "succeeded"}))
            (artifact.parent / "current.json").write_text(json.dumps({
                "fingerprint": fingerprint, "attempt": attempt,
                "metrics": {"median_ms": 2.5, "sample_count": 40},
                "source_fingerprint": "source-fp", "build_fingerprint": "build-fp",
                "data_fingerprint": "data-fp", "protocol_fingerprint": "protocol-fp",
                "device_identity": [{"resource_id": "gpu:host:GPU-1", "uuid": "GPU-1",
                                     "host_id": "host", "kind": "gpu"}],
            }))

            compact = workspace_results(project, "demo")["demo"][0]
            self.assertEqual({key: compact[key] for key in ("id", "job_id", "packet_index", "state")},
                             {"id": attempt, "job_id": "job-1", "packet_index": 0, "state": "succeeded"})
            self.assertEqual(compact["packet_name"], "profile")
            self.assertLessEqual(len(compact["metrics"]), 64)
            self.assertEqual(compact["metrics"]["timings_ms.cuda_complete_postings_bitmap_workset.median_ms"], 0.662527978)
            self.assertEqual(compact["metrics"]["capture.bytes"], 48192)
            self.assertEqual(compact["duration_seconds"], 12.5)
            self.assertEqual(compact["identity"]["source_fingerprint"], "source-fp")
            self.assertEqual(compact["identity"]["build_fingerprint"], "build-fp")
            self.assertEqual(compact["identity"]["device_identity"], [{"resource_id": "gpu:host:GPU-1",
                                                                        "uuid": "GPU-1", "host_id": "host", "kind": "gpu"}])
            self.assertEqual(compact["evidence_role"], "selected_current")
            self.assertTrue(compact["artifact_ref"].endswith(f"demo/0/{fingerprint}"))
            encoded_compact = json.dumps(compact)
            self.assertNotIn("source_manifest", encoded_compact)
            self.assertNotIn("receipt", encoded_compact)
            self.assertNotIn("private raw", encoded_compact)
            self.assertLess(len(encoded_compact), 3000)

            (artifact / "result.json").write_text(json.dumps({"scheduler_job_id": "another-job"}))
            unverified_artifact = workspace_results(project, "demo")["demo"][0]
            self.assertEqual(unverified_artifact["metrics"], {})
            self.assertEqual(unverified_artifact["evidence_role"], "selected_current")
            self.assertNotIn("artifact_ref", unverified_artifact)
            (artifact / "result.json").write_text(json.dumps({"scheduler_job_id": "job-1", "status": "succeeded"}))

            detailed = workspace_results(project, "demo", detailed=True)["demo"][0]
            self.assertIn("source_manifest", detailed["result_json"])
            self.assertIn("receipt", detailed["result_json"])
            for args, expected in ((["results", "demo", "--project", str(project)], {"demo": [compact]}),
                                   (["results", "demo", "--details", "--project", str(project)], {"demo": [detailed]})):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    self.assertEqual(cli_main(args), 0)
                self.assertEqual(json.loads(stdout.getvalue()), expected)

            # A later failed attempt may share the same fingerprint, but must
            # not inherit the successful attempt's selected evidence or metrics.
            store = Store(project / ".autobench/state/autobench.sqlite3", create=False)
            try:
                failed = store.begin_attempt("demo", 1, 0, fingerprint)
                store.set_job(failed, "job-2", "preempted")
                store.set_attempt(failed, "preempted", {"worker": {"status": "missing_result"}})
            finally:
                store.close()
            failed_compact = workspace_results(project, "demo")["demo"][0]
            self.assertEqual(failed_compact["id"], failed)
            self.assertEqual(failed_compact["metrics"], {})
            self.assertIsNone(failed_compact["evidence_role"])
            self.assertNotIn("artifact_ref", failed_compact)

    def test_compact_results_default_to_sixteen_with_explicit_bounded_limit(self):
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "XDG_STATE_HOME": str(Path(temp) / "state-home"),
            "XDG_CONFIG_HOME": str(Path(temp) / "config-home"),
        }):
            project = Path(temp) / "project"
            project.mkdir()
            (project / "src").mkdir()
            (project / "src/input.py").write_text("value=1\n")
            init_project(project, agent_instructions="skip", activate=False)
            registration = project / "definition.toml"
            registration.write_text('''[benchmark]
name="demo"
revision=1
[source]
include=["src/**"]
[[packet]]
name="run"
argv=["/usr/bin/true"]
expected_seconds=1
kill_seconds=5
''')
            register_definition(project, registration)
            store = Store(project / ".autobench/state/autobench.sqlite3", create=False)
            try:
                for index in range(20):
                    attempt = store.begin_attempt("demo", 1, 0, f"fp-{index:02d}")
                    store.set_attempt(attempt, "failed", error=f"failure-{index}")
            finally:
                store.close()

            self.assertEqual(len(workspace_results(project, "demo")["demo"]), 16)
            self.assertEqual(len(workspace_results(project, "demo", limit=5)["demo"]), 5)
            self.assertEqual(len(workspace_results(project, "demo", detailed=True)["demo"]), 20)
            with self.assertRaisesRegex(ValueError, "1 to 100"):
                workspace_results(project, "demo", limit=101)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(cli_main(["results", "demo", "--limit", "5", "--project", str(project)]), 0)
            self.assertEqual(len(json.loads(stdout.getvalue())["demo"]), 5)

    def test_cli_init_does_not_activate_when_scheduler_grants_are_missing_or_unreachable(self):
        import io
        from contextlib import redirect_stdout

        class ProbeClient:
            def __init__(self, response=None, error=None):
                self.response = response
                self.error = error

            async def call(self, operation, arguments=None):
                if operation != "access.inspect":
                    raise AssertionError(operation)
                if self.error:
                    raise RuntimeError(self.error)
                return self.response

        required = {"jobs.submit", "jobs.get", "jobs.list", "jobs.control", "resources.forecast",
                    "history.compact:autobench"}
        for case, response, error in (
            ("denied", {"principal_id": "agent:test", "actions": []}, None),
            ("unreachable", None, "connection refused"),
            ("allowed", {"principal_id": "agent:test", "actions": sorted(required)}, None),
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
                "XDG_STATE_HOME": str(Path(temp) / "state-home"),
                "XDG_CONFIG_HOME": str(Path(temp) / "config-home"),
            }):
                project = Path(temp) / "project"
                project.mkdir()
                token = project / "scheduler.token"
                token.write_text("test-token")
                stdout = io.StringIO()
                with patch("autobench.cli.McpScheduler", return_value=ProbeClient(response, error)), \
                     patch("autobench.cli.activate_existing_user_unit", return_value=(True, "started")) as activate, \
                     redirect_stdout(stdout):
                    code = cli_main(["init", "--project", str(project), "--scheduler-url", "http://scheduler",
                                     "--token-file", str(token), "--agent-instructions=skip"])
                result = json.loads(stdout.getvalue())
                self.assertEqual(code, 0)
                self.assertTrue(result["initialized"])
                if case == "allowed":
                    self.assertEqual(result["readiness"], "ready")
                    self.assertTrue(result["service_activated"])
                    self.assertTrue(json.loads((project / ".autobench/config.json").read_text())["active"])
                    activate.assert_called_once()
                else:
                    self.assertTrue(result["readiness"].startswith("incomplete:"))
                    self.assertFalse(result["service_activated"])
                    self.assertFalse(json.loads((project / ".autobench/config.json").read_text())["active"])
                    activate.assert_not_called()
                if case == "denied":
                    self.assertEqual(set(result["scheduler_access"]["missing_actions"]), required)
                elif case == "unreachable":
                    self.assertIn("connection refused", result["scheduler_access"]["error"])

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
                self.assertEqual(scheduler.job_spec["termination_grace_seconds"], 5)
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
                worker_manifest = json.loads(Path(details["manifest"]).read_text())
                self.assertEqual(worker_manifest["managed_root"], str(autobench))
                self.assertEqual(worker_manifest["scratch_limit_bytes"], engine.limits["scratch_limit_bytes"])
                self.assertGreater(worker_manifest["retained_cap_bytes"], 0)
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

    def test_failed_packet_keeps_only_bounded_log_tails_after_scratch_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "src").mkdir()
            (project / "src/input.py").write_text("value=1\n")
            autobench = project / ".autobench"
            (autobench / "registry").mkdir(parents=True)
            (autobench / "config.json").write_text("{}")
            (autobench / "registry/demo.toml").write_text('''[benchmark]
name="demo"
revision=1
[source]
include=["src/**"]
[[packet]]
name="run"
argv=["/usr/bin/true"]
expected_seconds=2
kill_seconds=30
resources={cpu_cores=1}
''')
            create_test_store(project).close()
            scheduler = FakeScheduler()
            engine = Engine(project, scheduler)
            try:
                self.assertEqual(asyncio.run(engine.reconcile_once())["state"], "idle")
                seen = engine.store.get("seen:demo")
                seen["since"] = time.time() - 3
                engine.store.set("seen:demo", seen)
                self.assertEqual(asyncio.run(engine.reconcile_once())["state"], "submitted")
                attempt = engine.store.unresolved()[0]
                details = engine.store.get(f"attempt:{attempt['id']}")
                staged = Path(details["staged"])
                staged.mkdir(parents=True, exist_ok=True)
                (staged / "result.json").write_text(json.dumps({"status": "failed", "returncode": 1}))
                (staged / "stdout.log").write_bytes(b"out-start" + b"o" * 12_000 + b"out-tail")
                (staged / "stderr.log").write_bytes(b"err-start" + b"e" * 20_000 + b"fixture-diagnostic-tail")
                scheduler.job = {"id": "job-1", "state": "failed", "cleanup_verified": True,
                                 "attempt": {"result": {"stdout": "s" * 20_000,
                                                         "logs": {"stderr": "t" * 30_000}}}}
                self.assertEqual(asyncio.run(engine._reconcile_jobs()), 1)
                self.assertFalse(staged.exists())
                row = next(item for item in engine.store.attempts_for("demo") if item["id"] == attempt["id"])
                report = json.loads(row["result_json"])
                diagnostics = report["worker"]["diagnostics"]
                self.assertTrue(diagnostics["stdout_truncated"])
                self.assertTrue(diagnostics["stderr_truncated"])
                self.assertLessEqual(len(diagnostics["stdout_tail"].encode()), 8 * 1024)
                self.assertLessEqual(len(diagnostics["stderr_tail"].encode()), 8 * 1024)
                self.assertTrue(diagnostics["stderr_tail"].endswith("fixture-diagnostic-tail"))
                self.assertIn("fixture-diagnostic-tail", row["error"])
                scheduler_result = report["job"]["attempt"]["result"]
                self.assertNotIn("stdout", scheduler_result)
                self.assertNotIn("logs", scheduler_result)
                self.assertEqual(scheduler_result["stdout_bytes"], 20_000)
                self.assertEqual(scheduler_result["logs_summary"]["stderr"], 30_000)

                fallback = Engine._failure_diagnostics(
                    {"status": "missing_result"}, staged,
                    {"attempt": {"result": {"logs": {"stdout": "scheduler-out", "stderr": "s" * 20_000 + "scheduler-stderr"}}}},
                )
                self.assertTrue(fallback["diagnostics"]["stderr_truncated"])
                self.assertLessEqual(len(fallback["diagnostics"]["stderr_tail"].encode()), 8 * 1024)
                self.assertTrue(fallback["diagnostics"]["stderr_tail"].endswith("scheduler-stderr"))
            finally:
                engine.close()

    def test_priority_lease_demand_cancellation_is_preemption_not_packet_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "src").mkdir()
            (project / "src/input.py").write_text("value=1\n")
            autobench = project / ".autobench"
            (autobench / "registry").mkdir(parents=True)
            (autobench / "config.json").write_text("{}")
            (autobench / "registry/demo.toml").write_text('''[benchmark]
name="demo"
revision=1
[source]
include=["src/**"]
[[packet]]
name="run"
argv=["/usr/bin/true"]
expected_seconds=2
kill_seconds=30
resources={cpu_cores=1}
''')
            create_test_store(project).close()
            scheduler = FakeScheduler()
            engine = Engine(project, scheduler)
            try:
                self.assertEqual(asyncio.run(engine.reconcile_once())["state"], "idle")
                seen = engine.store.get("seen:demo")
                seen["since"] = time.time() - 3
                engine.store.set("seen:demo", seen)
                self.assertEqual(asyncio.run(engine.reconcile_once())["state"], "submitted")
                attempt = engine.store.unresolved()[0]
                scheduler.job = {
                    "id": "job-1", "state": "cancelled", "cleanup_verified": True,
                    "result": {"cancellation_reason": "priority_lease_demand:interactive-request"},
                    "attempt": {"result": {"cancellation_reason": "priority_lease_demand:interactive-request",
                                             "cleanup_verified": True}},
                }
                self.assertEqual(asyncio.run(engine._reconcile_jobs()), 1)
                row = next(item for item in engine.store.attempts_for("demo") if item["id"] == attempt["id"])
                self.assertEqual(row["state"], "preempted")
                self.assertIsNone(engine._retry_cooldown("demo", 0, attempt["fingerprint"]))
                self.assertIsNone(engine.store.current("demo", 0))
                self.assertFalse((autobench / "evidence/demo/0/current.json").exists())
            finally:
                engine.close()

    def test_worker_storage_stop_cancels_unknown_scheduler_job_until_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "src").mkdir()
            (project / "src/input.py").write_text("value=1\n")
            autobench = project / ".autobench"
            (autobench / "registry").mkdir(parents=True)
            (autobench / "config.json").write_text("{}")
            (autobench / "registry/demo.toml").write_text('''[benchmark]
name="demo"
revision=1
[source]
include=["src/**"]
[[packet]]
name="run"
argv=["/usr/bin/true"]
expected_seconds=2
kill_seconds=30
resources={cpu_cores=1}
''')
            create_test_store(project).close()
            scheduler = FakeScheduler()
            engine = Engine(project, scheduler)
            engine.limits["scratch_limit_bytes"] = 1024 * 1024
            engine.store.set("retained_cap_bytes", 10 * 1024 * 1024)
            try:
                self.assertEqual(asyncio.run(engine.reconcile_once())["state"], "idle")
                seen = engine.store.get("seen:demo")
                seen["since"] = time.time() - 3
                engine.store.set("seen:demo", seen)
                self.assertEqual(asyncio.run(engine.reconcile_once())["state"], "submitted")
                attempt = engine.store.unresolved()[0]
                staged = autobench / "scratch" / attempt["id"]
                staged.mkdir(parents=True, exist_ok=True)
                (staged / "result.json").write_text(json.dumps({"status": "storage_over_cap", "returncode": 125}))
                usage = sum(path.stat().st_size for path in autobench.rglob("*") if path.is_file())
                self.assertLess(usage + engine._reserved_bytes(usage), 10 * 1024 * 1024)

                self.assertEqual(asyncio.run(engine._reconcile_jobs()), 0)
                self.assertEqual([args["action"] for operation, args in scheduler.calls if operation == "jobs.control"], ["cancel"])
                self.assertTrue(engine.store.get(f"cancel_requested:{attempt['id']}"))
                self.assertGreater(engine._reserved_bytes(), 0)
                self.assertEqual(engine.store.current("demo", 0), None)

                # A still-running Scheduler record must not trigger duplicate cancel requests.
                self.assertEqual(asyncio.run(engine._reconcile_jobs()), 0)
                self.assertEqual(sum(operation == "jobs.control" for operation, _ in scheduler.calls), 1)
                self.assertGreater(engine._reserved_bytes(), 0)

                scheduler.job = {"id": "job-1", "state": "cancelled", "cleanup_verified": True}
                self.assertEqual(asyncio.run(engine._reconcile_jobs()), 1)
                row = next(item for item in engine.store.attempts_for("demo") if item["id"] == attempt["id"])
                self.assertEqual(row["state"], "storage_over_cap")
                self.assertEqual(engine._reserved_bytes(), 0)
                self.assertIsNone(engine.store.current("demo", 0))
                self.assertFalse((autobench / "evidence/demo/0/current.json").exists())
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
                "candidate_id": "atomic-request", "feasible": True, "feasible_now": True,
                "resource_ids": ["host:worker-a:cpu", "host:worker-a:memory", "gpu:worker-a:GPU-123"],
                "earliest_start": datetime.now(timezone.utc).isoformat(), "expected_release": None,
                "available_until": datetime.now(timezone.utc).timestamp() + 600,
                "window_seconds": 600, "uncertainty_seconds": 0,
                "window_basis": "forecast_horizon_only",
            }], "current_resources": [{"resource_id": "gpu:worker-a:GPU-123", "kind": "gpu"}]}
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
                self.assertEqual(spec["preconditions"][0]["parameters"], {
                    "timeout_seconds": 30, "interval_seconds": 1,
                    "consecutive_samples": 2, "max_utilization_percent": 0,
                })
                from device_scheduler_core import JobSpec
                JobSpec.from_dict(spec)
            finally:
                engine.close()

    def test_reconcile_uses_live_scheduler_atomic_forecast_wire(self):
        from device_scheduler_adapters.sqlite import Ledger as SchedulerLedger
        from device_scheduler_core.allocator import ResourceAllocator
        from device_scheduler_core.application import Scheduler as CoreScheduler, SchedulerConfig
        from device_scheduler_core.models import Principal, Resource

        class ObservedInventory:
            adapter_id = "autobench.test.inventory"

            def snapshot(self):
                stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                return {
                    "state": "observed", "observed_at": stamp,
                    "gpu_inventory": {"state": "observed"},
                    "process_telemetry": {"state": "observed", "processes": []},
                    "resources": [
                        Resource("host:local:cpu", "host:local", "cpu", {"cores": 16}),
                        Resource("host:local:memory", "host:local", "memory", {"bytes": 32 * 1024**3}),
                        Resource("gpu:host:GPU-test", "host:local", "gpu", {"memory_bytes": 8 * 1024**3},
                                 attributes={"uuid": "GPU-test", "memory_free_bytes_observation": 7 * 1024**3,
                                             "process_count_observation": 0,
                                             "process_telemetry": "compute-apps-visible"}),
                    ],
                }

        class DispatchClient:
            def __init__(self, scheduler, principal):
                self.scheduler = scheduler
                self.principal = principal
                self.calls = []

            async def call(self, operation, arguments=None):
                args = dict(arguments or {})
                self.calls.append((operation, args))
                return self.scheduler.dispatch(operation, args, self.principal)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            (root / "src").mkdir()
            (root / "src/kernel.cu").write_text("__global__ void k() {}\n")
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(Path(temp) / "autobench-state")}):
                init_project(root, agent_instructions="skip")
            (root / ".autobench/registry/live.toml").write_text('''[benchmark]
name="live-wire"
revision=1
protocol={runner="test-v1"}
[source]
include=["src/**"]
[[packet]]
name="profile"
kind="timeline-profile"
argv=["/usr/bin/true"]
expected_seconds=2
kill_seconds=30
resources={cpu_cores=1,host_memory_bytes=1048576,gpu_memory_bytes=1048576}
''', encoding="utf-8")

            ledger = SchedulerLedger(Path(temp) / "scheduler.sqlite3")
            core = CoreScheduler(
                ledger, ResourceAllocator(ledger), resource_adapters=(ObservedInventory(),),
                config=SchedulerConfig(state_root=Path(temp) / "scheduler-state",
                                       execution_root=root, artifact_root=Path(temp) / "scheduler-artifacts"),
            )
            principal = Principal(
                "agent:one", actions=frozenset({"jobs.submit", "jobs.read", "jobs.control", "jobs.list",
                                                 "resources.forecast", "history.compact:autobench"}),
                execution_roots=(str(root),), max_priority=90,
            )
            client = DispatchClient(core, principal)
            engine = Engine(root, client)
            try:
                self.assertEqual(asyncio.run(engine.reconcile_once())["state"], "idle")
                seen = engine.store.get("seen:live-wire")
                seen["since"] = time.time() - 3
                engine.store.set("seen:live-wire", seen)
                result = asyncio.run(engine.reconcile_once())
                self.assertEqual(result["state"], "submitted", result)
                forecast_calls = [args for operation, args in client.calls if operation == "resources.forecast"]
                self.assertEqual(len(forecast_calls), 1)
                self.assertEqual(forecast_calls[0]["horizon_seconds"], 600)
                self.assertEqual(len(forecast_calls[0]["request"]["requirements"]), 3)
                forecast = core.dispatch("resources.forecast", forecast_calls[0], principal)
                candidate, = forecast["candidates"]
                self.assertTrue(candidate["feasible_now"])
                self.assertEqual(set(candidate["resource_ids"]), {
                    "host:local:cpu", "host:local:memory", "gpu:host:GPU-test",
                })
                submitted = [args["spec"] for operation, args in client.calls if operation == "jobs.submit"]
                self.assertEqual(len(submitted), 1)
                self.assertEqual(submitted[0]["resources"][-1]["selectors"], {"resource_id": "gpu:host:GPU-test"})
                self.assertEqual(submitted[0]["preconditions"][0]["name"], "gpu.quiescence")
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
            protocol = {"schema_version": 1, "argv": list(command.argv), "kind": command.kind,
                        "resources": {}, "environment_sha256": {}, "recipe_fingerprint": "recipe-v1"}
            protocol_fp = _protocol_fingerprint(protocol)
            actual_grant = _device_identity({"attempt": {"execution_context": {
                "allocations": [
                    {"resource_id": "gpu:worker-a:GPU-1", "quantities": {"memory_bytes": 100}},
                    {"resource_id": "host:worker-a:cpu", "quantities": {"cores": 4}},
                    {"resource_id": "host:worker-a:memory", "quantities": {"bytes": 4096}},
                ],
                "resources": [
                    {"resource_id": "gpu:worker-a:GPU-1", "kind": "gpu", "attributes": {"uuid": "GPU-1"}},
                    {"resource_id": "host:worker-a:cpu", "kind": "cpu"},
                    {"resource_id": "host:worker-a:memory", "kind": "memory"},
                ],
            }}})
            for i, (duration, data, state) in enumerate(((10, "d1", "succeeded"), (20, "d1", "succeeded"), (100, "other", "succeeded"), (90, "d1", "failed"))):
                attempt_id = engine.store.begin_attempt("demo", 1, 0, f"fp{i}")
                engine.store.set_attempt(attempt_id, state, {"worker": {"duration_seconds": duration},
                                                             "identity": {"protocol_fingerprint": protocol_fp,
                                                                          "data_fingerprint": data,
                                                                          "device_identity": actual_grant}})
            try:
                estimate = engine._expected_duration(benchmark, 0, command,
                                                     {"protocol": protocol, "protocol_fingerprint": protocol_fp,
                                                      "data_fingerprint": "d1",
                                                      "selected_resource_ids": ["gpu:worker-a:GPU-1"]})
                self.assertEqual(estimate, 24)
            finally:
                engine.close()

    def test_measurement_protocol_tracks_command_recipe_and_not_source(self):
        benchmark = Benchmark(name="demo", revision=1, root=Path("."), include=("src/**",),
                              data=(), build=None, packets=(), protocol={"warmup": 2, "samples": 7})
        first = Command(("python3", "bench.py", "--warmup", "2", "--samples", "7"), 5, 30,
                        {"cpu_cores": 1, "_environment": {"API_TOKEN": "secret-a"}}, kind="timing")
        same = Command(first.argv, 5, 30,
                       {"cpu_cores": 1, "_environment": {"API_TOKEN": "secret-a"}}, kind="timing")
        warmup_changed = Command(("python3", "bench.py", "--warmup", "3", "--samples", "7"), 5, 30,
                                 {"cpu_cores": 1, "_environment": {"API_TOKEN": "secret-a"}}, kind="timing")
        protocol = _measurement_protocol(benchmark, first, "recipe-a")
        self.assertEqual(_protocol_fingerprint(protocol),
                         _protocol_fingerprint(_measurement_protocol(benchmark, same, "recipe-a")))
        self.assertNotEqual(_protocol_fingerprint(protocol),
                            _protocol_fingerprint(_measurement_protocol(benchmark, warmup_changed, "recipe-a")))
        self.assertNotEqual(_protocol_fingerprint(protocol),
                            _protocol_fingerprint(_measurement_protocol(benchmark, first, "recipe-b")))
        self.assertNotIn("secret-a", json.dumps(protocol))
        self.assertNotEqual(_protocol_fingerprint(protocol), _protocol_fingerprint({}))
        # Source/build/data are independent identity dimensions; changing source
        # does not alter the measurement protocol itself.
        self.assertEqual(_protocol_fingerprint(protocol),
                         _protocol_fingerprint(_measurement_protocol(benchmark, first, "recipe-a")))

    def test_compare_rejects_legacy_empty_protocol_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            target = project / ".autobench/evidence/bench/0"
            target.mkdir(parents=True)
            (project / ".autobench/config.json").write_text("{}")
            create_test_store(project).close()
            modern = {"schema_version": 1, "registered": {}, "argv": ["python3", "bench.py"],
                      "kind": "timing", "resources": {}, "environment_sha256": {},
                      "recipe_fingerprint": "recipe-v1"}
            current = {"metrics": {"median_ms": 2.0}, "protocol": modern,
                       "protocol_fingerprint": _protocol_fingerprint(modern),
                       "data_fingerprint": "fixture", "device_identity": [{"resource_id": "gpu:host:GPU-test"}]}
            legacy = {**current, "metrics": {"median_ms": 1.0}, "protocol": {},
                      "protocol_fingerprint": _protocol_fingerprint({})}
            (target / "current.json").write_text(json.dumps(current))
            (target / "baseline.json").write_text(json.dumps(legacy))
            result = workspace_compare(project, "bench")["comparisons"][0]
            self.assertFalse(result["comparable"])
            self.assertIn("measurement protocol identity is incomplete", result["reasons"])

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

    def test_two_engine_instances_cannot_submit_same_workspace_concurrently(self):
        class SlowForecastScheduler(FakeScheduler):
            async def call(self, operation, arguments=None):
                if operation == "resources.forecast":
                    await asyncio.sleep(0.05)
                return await super().call(operation, arguments)

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "src").mkdir()
            (project / "src/input.cu").write_text("kernel")
            autobench = project / ".autobench"
            (autobench / "registry").mkdir(parents=True)
            (autobench / "config.json").write_text("{}")
            (autobench / "registry/bench.toml").write_text('''[benchmark]\nname="demo"\nrevision=1\n[source]\ninclude=["src/**"]\n[[packet]]\nname="timing"\nargv=["python3","runner.py"]\nexpected_seconds=1\nkill_seconds=5\nresources={cpu_cores=1}\n''')
            create_test_store(project).close()
            scheduler = SlowForecastScheduler()
            first = Engine(project, scheduler)
            second = Engine(project, scheduler)
            try:
                self.assertEqual(asyncio.run(first.reconcile_once())["state"], "idle")
                seen = first.store.get("seen:demo")
                seen["since"] = time.time() - 3
                first.store.set("seen:demo", seen)
                async def run_both():
                    return await asyncio.gather(first.reconcile_once(), second.reconcile_once())

                results = asyncio.run(run_both())
                self.assertEqual(sum(result.get("submitted", 0) for result in results), 1)
                self.assertEqual(sum(result["state"] == "busy" for result in results), 1)
                self.assertEqual(sum(operation == "jobs.submit" for operation, _ in scheduler.calls), 1)
                self.assertEqual(len(first.store.unresolved()), 1)
            finally:
                first.close()
                second.close()

    def test_separate_process_respects_workspace_writer_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / ".autobench/registry").mkdir(parents=True)
            (project / ".autobench/config.json").write_text("{}")
            create_test_store(project).close()
            engine = Engine(project, FakeScheduler())
            code = ("from autobench.engine import Engine; from pathlib import Path; "
                    "import asyncio; e=Engine(Path(r'%s'), object()); "
                    "print(asyncio.run(e.reconcile_once())['state']); e.close()") % str(project)
            try:
                with engine._workspace_writer_lock() as acquired:
                    self.assertTrue(acquired)
                    child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
                    self.assertEqual(child.returncode, 0, child.stderr)
                    self.assertEqual(child.stdout.strip(), "busy")
            finally:
                engine.close()

    def test_prepared_attempts_are_cleaned_and_orphans_terminalized(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / ".autobench/registry").mkdir(parents=True)
            (project / ".autobench/config.json").write_text("{}")
            create_test_store(project).close()
            engine = Engine(project, FakeScheduler())
            owned = str(__import__("uuid").uuid4())
            stage = project / f".autobench/scratch/{owned}"
            stage.mkdir(parents=True)
            (stage / "partial").write_text("partial")
            manifest = project / f".autobench/state/packets/{owned}.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text("{}")
            engine.store.begin_attempt("demo", 1, 0, "fp", attempt_id=owned, state="prepared")
            engine.store.set(f"attempt:{owned}", {"staged": str(stage), "manifest": str(manifest)})
            engine.store.set(f"reservation:{owned}", {"bytes": 4096, "baseline_usage": 0})
            orphan = str(__import__("uuid").uuid4())
            engine.store.begin_attempt("demo", 1, 1, "orphan", attempt_id=orphan, state="prepared")
            engine.store.set(f"reservation:{orphan}", 2048)
            try:
                asyncio.run(engine.reconcile_once())
                states = {row["id"]: row["state"] for row in engine.store.db.execute("SELECT id,state FROM attempts")}
                self.assertEqual(states[owned], "not_submitted")
                self.assertEqual(states[orphan], "not_submitted")
                self.assertFalse(stage.exists())
                self.assertFalse(manifest.exists())
                self.assertEqual(engine._reserved_bytes(), 0)
                self.assertIsNone(engine.store.get(f"attempt:{owned}"))
                self.assertIsNone(engine.store.get(f"attempt:{orphan}"))
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
            (project / ".autobench/config.json").write_text("{}")
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
                "long": {"candidates": [{"feasible": True, "feasible_now": True, "earliest_start": now,
                                           "window_seconds": 10, "uncertainty_seconds": 0,
                                           "window_basis": "forecast_horizon_only"}]},
                "short": {"candidates": [{"feasible": True, "feasible_now": True, "earliest_start": now,
                                            "window_seconds": 10, "uncertainty_seconds": 0,
                                            "window_basis": "forecast_horizon_only"}]},
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

    def test_ready_candidates_normalize_forecast_time_and_prefer_shared_setup_group(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / ".autobench/registry").mkdir(parents=True)
            (project / ".autobench/config.json").write_text("{}")
            create_test_store(project).close()
            scheduler = FakeScheduler()
            now = datetime.now(timezone.utc).timestamp()
            scheduler.forecast_by_packet = {
                "cpu": {"candidates": [{"feasible_now": True, "resource_ids": ["host:worker-a:cpu"],
                                        "earliest_start": now - 100, "window_seconds": 600}],
                        "current_resources": [{"resource_id": "host:worker-a:cpu", "kind": "cpu"}]},
                "gpu-one": {"candidates": [{"feasible_now": True, "resource_ids": ["gpu:worker-a:GPU-1", "gpu:worker-a:GPU-2"],
                                             "earliest_start": now, "window_seconds": 600}],
                            "current_resources": [{"resource_id": "gpu:worker-a:GPU-1", "kind": "gpu"},
                                                  {"resource_id": "gpu:worker-a:GPU-2", "kind": "gpu"}]},
                "gpu-two": {"candidates": [{"feasible_now": True, "resource_ids": ["gpu:worker-a:GPU-1", "gpu:worker-a:GPU-2"],
                                             "earliest_start": now + 2, "window_seconds": 600}],
                            "current_resources": [{"resource_id": "gpu:worker-a:GPU-1", "kind": "gpu"},
                                                  {"resource_id": "gpu:worker-a:GPU-2", "kind": "gpu"}]},
            }
            engine = Engine(project, scheduler)
            benchmark = type("B", (), {"name": "demo", "revision": 1, "protocol": {}})()
            candidates = [
                {"benchmark": benchmark, "command": Command(("python3", "cpu.py"), 2, 30,
                                                               {"cpu_cores": 1}, name="cpu"),
                 "identity": {"setup_key": "cpu-setup"}},
                {"benchmark": benchmark, "command": Command(("python3", "gpu1.py"), 10, 30,
                                                               {"gpu_memory_bytes": 1024, "gpu_count": 2}, name="gpu-one"),
                 "identity": {"setup_key": "gpu-setup"}},
                {"benchmark": benchmark, "command": Command(("python3", "gpu2.py"), 10, 30,
                                                               {"gpu_memory_bytes": 1024, "gpu_count": 2}, name="gpu-two"),
                 "identity": {"setup_key": "gpu-setup"}},
            ]
            try:
                chosen = asyncio.run(engine._choose_candidate(candidates))
                self.assertEqual(chosen["command"].name, "gpu-one")
                forecast_calls = [args for op, args in scheduler.calls if op == "resources.forecast"]
                self.assertEqual(len(forecast_calls), 2)
                self.assertEqual({args["request"]["lease_ttl_seconds"] for args in forecast_calls}, {60.0})
                self.assertEqual(len(chosen["identity"]["selected_resource_ids"]), 2)
            finally:
                engine.close()

    def test_identical_forecast_cache_does_not_conflate_different_lease_ttls(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / ".autobench/registry").mkdir(parents=True)
            (project / ".autobench/config.json").write_text("{}")
            create_test_store(project).close()
            scheduler = FakeScheduler()
            response = {"candidates": [{"feasible_now": True, "resource_ids": ["gpu:worker-a:GPU-1"],
                                         "earliest_start": time.time(), "window_seconds": 600}],
                        "current_resources": [{"resource_id": "gpu:worker-a:GPU-1", "kind": "gpu"}]}
            scheduler.forecast_by_packet = {"same-a": response, "same-b": response, "longer": response}
            engine = Engine(project, scheduler)
            benchmark = type("B", (), {"name": "demo", "revision": 1, "protocol": {}})()
            candidates = [
                {"benchmark": benchmark, "command": Command(("python3", "a.py"), 1, 30,
                                                               {"gpu_memory_bytes": 1024}, name="same-a"), "identity": {}},
                {"benchmark": benchmark, "command": Command(("python3", "b.py"), 1, 30,
                                                               {"gpu_memory_bytes": 1024}, name="same-b"), "identity": {}},
                {"benchmark": benchmark, "command": Command(("python3", "c.py"), 1, 40,
                                                               {"gpu_memory_bytes": 1024}, name="longer"), "identity": {}},
            ]
            try:
                asyncio.run(engine._choose_candidate(candidates))
                calls = [args for op, args in scheduler.calls if op == "resources.forecast"]
                self.assertEqual(len(calls), 2)
                self.assertEqual({args["request"]["lease_ttl_seconds"] for args in calls}, {60.0, 70.0})
            finally:
                engine.close()

    def test_future_only_forecast_never_selects_gpu_or_admits_packet(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / ".autobench/registry").mkdir(parents=True)
            (project / ".autobench/config.json").write_text("{}")
            create_test_store(project).close()
            scheduler = FakeScheduler()
            scheduler.forecast_by_packet["profile"] = {"candidates": [{
                "candidate_id": "atomic-request", "feasible": False, "feasible_now": False,
                "projected_feasible": True, "resource_ids": ["gpu:worker-a:GPU-123"],
                "earliest_start": datetime.now(timezone.utc).isoformat(),
                "expected_release": datetime.now(timezone.utc).isoformat(),
                "available_until": datetime.now(timezone.utc).timestamp() + 600,
                "window_seconds": 600, "window_basis": "verified_active_job_release",
            }], "current_resources": [{"resource_id": "gpu:worker-a:GPU-123", "kind": "gpu"}]}
            engine = Engine(project, scheduler)
            benchmark = type("B", (), {"name": "demo", "revision": 1, "protocol": {}})()
            packet = Command(("python3", "profile.py"), 2, 30,
                             {"gpu_memory_bytes": 1024}, kind="timeline-profile", name="profile")
            candidate = {"benchmark": benchmark, "command": packet, "index": 0,
                         "identity": {"data_fingerprint": "fixture-v1"}, "snapshot": project,
                         "build": project / ".autobench/builds/x", "fingerprint": "fp", "artifacts": {}}
            try:
                chosen = asyncio.run(engine._choose_candidate([candidate]))
                self.assertFalse(chosen["identity"]["forecast_ready"])
                self.assertEqual(chosen["identity"]["selected_resource_ids"], [])
                with self.assertRaisesRegex(Exception, "feasible-now forecast-selected"):
                    asyncio.run(engine._submit(benchmark, project, project / ".autobench/builds/x", packet, 0,
                                               "fp", "timeline-profile", identity=chosen["identity"]))
                self.assertFalse(any(operation == "jobs.submit" for operation, _args in scheduler.calls))
            finally:
                engine.close()

    def test_failure_backoff_blocks_same_packet_fingerprint_and_input_change_resets_it(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "src").mkdir()
            (project / "src/input.py").write_text("value = 1\n")
            (project / ".autobench/registry").mkdir(parents=True)
            (project / ".autobench/config.json").write_text("{}")
            (project / ".autobench/registry/demo.toml").write_text('''[benchmark]
name="demo"
revision=1
[source]
include=["src/**"]
[[packet]]
name="run"
argv=["/usr/bin/true"]
expected_seconds=2
kill_seconds=30
resources={cpu_cores=1}
''')
            create_test_store(project).close()
            scheduler = FakeScheduler()
            engine = Engine(project, scheduler)
            try:
                self.assertEqual(asyncio.run(engine.reconcile_once())["state"], "idle")
                seen = engine.store.get("seen:demo")
                seen["since"] = time.time() - 3
                engine.store.set("seen:demo", seen)
                expected = engine.explain("demo")["packets"][0]["expected_fingerprint"]
                failed = engine.store.begin_attempt("demo", 1, 0, expected)
                engine.store.set_attempt(failed, "missing_result", result={"worker": {"status": "missing_result"}},
                                         error="worker result missing")

                result = asyncio.run(engine.reconcile_once())
                self.assertEqual(result["state"], "retry_backoff")
                self.assertFalse(any(op == "jobs.submit" for op, _ in scheduler.calls))
                retry = engine.explain("demo")["packets"][0]["retry_cooldown"]
                self.assertTrue(retry["blocked"])
                self.assertEqual(retry["failure_streak"], 1)
                self.assertIn("retry in", retry["reason"])
                self.assertEqual(engine.retry_cooldowns()[0]["target"], "run")

                (project / "src/input.py").write_text("value = 2\n")
                self.assertEqual(asyncio.run(engine.reconcile_once())["state"], "idle")
                seen = engine.store.get("seen:demo")
                seen["since"] = time.time() - 3
                engine.store.set("seen:demo", seen)
                changed = asyncio.run(engine.reconcile_once())
                self.assertEqual(changed["state"], "submitted", changed)
                self.assertEqual(sum(op == "jobs.submit" for op, _ in scheduler.calls), 1)
            finally:
                engine.close()

    def test_backoff_resets_after_success_and_scheduler_preemption_for_build_and_packet(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / ".autobench/registry").mkdir(parents=True)
            (project / ".autobench/config.json").write_text("{}")
            create_test_store(project).close()
            engine = Engine(project, FakeScheduler())
            try:
                for packet_index in (-1, 0):
                    failure = engine.store.begin_attempt("demo", 1, packet_index, f"fp-{packet_index}")
                    engine.store.set_attempt(failure, "failed", error="runner failed")
                    cooldown = engine._retry_cooldown("demo", packet_index, f"fp-{packet_index}")
                    self.assertTrue(cooldown["blocked"])
                    self.assertEqual(cooldown["failure_streak"], 1)

                    engine.store.set_attempt(failure, "succeeded", result={"worker": {"status": "succeeded"}})
                    self.assertIsNone(engine._retry_cooldown("demo", packet_index, f"fp-{packet_index}"))

                    engine.store.set_attempt(failure, "failed", error="runner failed")
                    preempted = engine.store.begin_attempt("demo", 1, packet_index, f"fp-{packet_index}")
                    engine.store.set_attempt(preempted, "preempted", result={"worker": {"status": "missing_result"}})
                    self.assertIsNone(engine._retry_cooldown("demo", packet_index, f"fp-{packet_index}"))
            finally:
                engine.close()

    def test_failed_build_fingerprint_is_cooled_down_before_resubmission(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "src").mkdir()
            (project / "src/input.py").write_text("value = 1\n")
            (project / ".autobench/registry").mkdir(parents=True)
            (project / ".autobench/config.json").write_text("{}")
            (project / ".autobench/registry/demo.toml").write_text('''[benchmark]
name="demo"
revision=1
[source]
include=["src/**"]
[build]
argv=["/usr/bin/true"]
expected_seconds=2
kill_seconds=30
resources={cpu_cores=1}
[[packet]]
name="run"
argv=["/usr/bin/true"]
expected_seconds=2
kill_seconds=30
''')
            create_test_store(project).close()
            scheduler = FakeScheduler()
            engine = Engine(project, scheduler)
            try:
                self.assertEqual(asyncio.run(engine.reconcile_once())["state"], "idle")
                seen = engine.store.get("seen:demo")
                seen["since"] = time.time() - 3
                engine.store.set("seen:demo", seen)
                build_fp = engine.explain("demo")["build_fingerprint"]
                failed = engine.store.begin_attempt("demo", 1, -1, build_fp)
                engine.store.set_attempt(failed, "missing_result", error="build produced no result")
                result = asyncio.run(engine.reconcile_once())
                self.assertEqual(result["state"], "retry_backoff")
                self.assertEqual(result["cooldowns"][0]["target"], "build")
                self.assertFalse(any(op == "jobs.submit" for op, _ in scheduler.calls))
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

