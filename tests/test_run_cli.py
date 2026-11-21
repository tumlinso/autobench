import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from autobench.api import _run_benchmark, start_run, wait_run
from autobench.cli import main
from autobench.errors import RegistrationError
from autobench.store import Store


def test_run_cli_emits_durable_id_before_waiting_and_preserves_argv(tmp_path, monkeypatch, capsys):
    async def observe_wait(project, run_id, *, wait_seconds):
        assert project == str(tmp_path)
        assert run_id == "run-1"
        assert wait_seconds == 12
        assert json.loads(capsys.readouterr().out) == {
            "run_id": "run-1", "state": "accepted", "phase": "accepted",
        }
        return {"run_id": run_id, "state": "succeeded", "phase": "complete"}

    accept = AsyncMock(return_value={"run_id": "run-1", "state": "accepted", "phase": "accepted"})
    monkeypatch.setattr("autobench.cli.start_run", accept)
    monkeypatch.setattr("autobench.cli.wait_run", observe_wait)
    code = main(["run", "--project", str(tmp_path), "--include", "bench/**", "--data", "fixtures/in",
                 "--gpu", "2", "--gpu-memory-mib", "1024", "--foreground", "--wait-seconds", "12",
                 "--", "./bench/run", "--label", "two words", "", "$literal"])

    assert code == 0
    request = accept.await_args.kwargs
    assert request["argv"] == ["./bench/run", "--label", "two words", "", "$literal"]
    assert request["include"] == ["bench/**"]
    assert request["data"] == ["fixtures/in"]
    assert request["gpu"] == 2
    assert request["gpu_memory_mib"] == 1024
    assert request["foreground"] is True
    assert request["admission_seconds"] == 60
    assert json.loads(capsys.readouterr().out)["state"] == "succeeded"


@pytest.mark.parametrize("state", ["blocked", "wait_expired", "failed", "timed_out"])
def test_wait_cli_reconnect_returns_actionable_nonzero(state, monkeypatch, capsys):
    result = {"run_id": "run-1", "state": state, "phase": "admission",
              "reason_code": "gpu_busy", "next_action": "Wait for an exclusive GPU."}
    receive = AsyncMock(return_value=result)
    monkeypatch.setattr("autobench.cli.wait_run", receive)

    assert main(["wait", "run-1", "--wait-seconds", "0"]) == 1
    receive.assert_awaited_once_with(".", "run-1", wait_seconds=0)
    assert json.loads(capsys.readouterr().out) == result


def test_run_cli_preacceptance_blocker_does_not_wait(monkeypatch, capsys):
    blocker = {"state": "blocked", "phase": "setup", "reason_code": "workspace_paused",
               "next_action": "Run autobench resume."}
    accept = AsyncMock(return_value=blocker)
    receive = AsyncMock()
    monkeypatch.setattr("autobench.cli.start_run", accept)
    monkeypatch.setattr("autobench.cli.wait_run", receive)

    assert main(["run", "--file", "probe.toml"]) == 1
    assert accept.await_args.kwargs["definition"] == "probe.toml"
    assert accept.await_args.kwargs["argv"] is None
    receive.assert_not_called()
    assert json.loads(capsys.readouterr().out) == blocker


def test_invalid_client_wait_limit_cannot_submit(monkeypatch):
    accept = AsyncMock()
    monkeypatch.setattr("autobench.cli.start_run", accept)
    with pytest.raises(SystemExit) as failure:
        main(["run", "probe", "--wait-seconds", "nan"])
    assert failure.value.code == 2
    accept.assert_not_called()


def resolve(root, **options):
    arguments = dict(name=None, definition=None, argv=None, include=(), data=(), kind=None,
                     gpu=None, gpu_memory_mib=None, expected_seconds=None, kill_seconds=None)
    return _run_benchmark(root, **(arguments | options))


def test_all_three_input_forms_share_recipe_validation(tmp_path):
    registry = tmp_path / ".autobench" / "registry"
    registry.mkdir(parents=True)
    recipe = tmp_path / "probe.toml"
    recipe.write_text(
        '[benchmark]\nname = "probe"\n[source]\ninclude = ["bench/**"]\n[[packet]]\n'
        'argv = ["./bench/run", ""]\nexpected_seconds = 2\nkill_seconds = 8\n',
        encoding="utf-8",
    )
    (registry / recipe.name).write_bytes(recipe.read_bytes())

    registered = resolve(tmp_path, name="probe")
    one_shot = resolve(tmp_path, definition=recipe)
    raw = resolve(tmp_path, argv=["./bench/run", ""], data=["fixtures/in"], gpu=2, gpu_memory_mib=512)

    assert one_shot == registered
    assert raw.include == () and raw.data == ("fixtures/in",)
    assert raw.packets[0].argv == ("./bench/run", "")
    assert raw.packets[0].kind == "timing"
    assert raw.packets[0].expected_seconds == 5
    assert raw.packets[0].kill_seconds == 30
    assert raw.packets[0].resources == {"gpu_count": 2, "gpu_memory_bytes": 512 * 1024**2}
    assert list(registry.iterdir()) == [registry / recipe.name]


@pytest.mark.parametrize(("options", "expected"), [
    ({"name": "probe", "argv": ["echo"]}, "specify one"),
    ({"name": "probe", "gpu": 1}, "apply to raw argv"),
    ({"argv": ["echo"]}, "must declare"),
    ({"argv": [], "include": ["bench/**"]}, "contain an executable"),
    ({"argv": ["echo"], "include": ["../elsewhere"]}, "cannot escape"),
    ({"argv": ["echo"], "include": ["bench/**"], "gpu": 1}, "together"),
    ({"argv": ["echo"], "include": ["bench/**"], "gpu": 1, "gpu_memory_mib": 0}, "positive integer"),
    ({"argv": ["echo"], "include": ["bench/**"], "kill_seconds": 5}, "kill_seconds"),
])
def test_invalid_run_forms_fail_through_recipe_validation(tmp_path, options, expected):
    with pytest.raises(RegistrationError, match=expected):
        resolve(tmp_path, **options)


def test_uninitialized_run_is_actionable_and_does_not_create_state(tmp_path):
    requested = asyncio.run(start_run(tmp_path, name="probe"))
    waiting = asyncio.run(wait_run(tmp_path, "run-1"))

    assert requested["state"] == waiting["state"] == "blocked"
    assert requested["reason_code"] == waiting["reason_code"] == "workspace_uninitialized"
    assert "autobench init" in requested["next_action"]
    assert waiting["run_id"] == "run-1"
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def configured_workspace(tmp_path):
    managed = tmp_path / ".autobench"
    token = tmp_path / "scheduler.token"
    token.write_text("fixture-token", encoding="utf-8")
    (managed / "registry").mkdir(parents=True)
    (managed / "config.json").write_text(json.dumps({
        "active": True, "scheduler_url": "http://test.invalid", "token_file": str(token),
    }), encoding="utf-8")
    Store(managed / "state" / "autobench.sqlite3").close()
    return tmp_path


def test_denied_foreground_is_an_early_api_blocker(configured_workspace):
    class Scheduler:
        call = AsyncMock(return_value={"principal_id": "test", "max_priority": 10})

    scheduler = Scheduler()
    result = asyncio.run(start_run(configured_workspace, argv=["true"], include=["bench/**"],
                                   foreground=True, scheduler=scheduler))
    assert result["state"] == "blocked"
    assert result["reason_code"] == "foreground_denied"
    assert "foreground" in result["next_action"]
    scheduler.call.assert_awaited_once_with("access.inspect", {})
    assert not (configured_workspace / ".autobench" / "snapshots").exists()


class CompletingScheduler:
    def __init__(self):
        self.submissions = []

    async def call(self, operation, arguments=None):
        if operation == "access.inspect":
            return {"principal_id": "test", "max_priority": 10}
        if operation == "jobs.submit":
            spec = arguments["spec"]
            self.submissions.append(spec)
            manifest = json.loads(Path(spec["argv"][-1]).read_text(encoding="utf-8"))
            executable = Path(manifest["working_directory"]) / manifest["argv"][0]
            assert executable.read_text(encoding="utf-8") == "#!/bin/sh\n# exact dirty bytes\n"
            assert executable.stat().st_mode & 0o111
            artifact_dir = Path(manifest["result_directory"])
            (artifact_dir / "result.json").write_text('{"status":"succeeded","duration_seconds":0.125}', encoding="utf-8")
            (artifact_dir / "metrics.json").write_text('{"elapsed_ms":2}', encoding="utf-8")
            return {"id": "job-1", "state": "queued"}
        if operation == "jobs.get":
            return {"id": "job-1", "state": "succeeded", "cleanup_verified": True}
        raise AssertionError(f"Unexpected Scheduler operation {operation}")


@pytest.mark.parametrize("form", ["name", "recipe", "argv"])
def test_single_cli_command_retains_result_from_snapshot(form, configured_workspace, monkeypatch, capsys):
    project = configured_workspace
    program = project / "bench" / "measure"
    program.parent.mkdir()
    program.write_text("#!/bin/sh\n# exact dirty bytes\n", encoding="utf-8")
    program.chmod(0o755)
    registration = (
        '[benchmark]\nname = "measure"\n[source]\ninclude = ["bench/**"]\n[[packet]]\n'
        'argv = ["./bench/measure"]\nexpected_seconds = 2\nkill_seconds = 8\n'
    )
    if form == "argv":
        selection = ["--include", "bench/**", "--", "./bench/measure"]
    elif form == "recipe":
        recipe = project / "recipe.toml"
        recipe.write_text(registration, encoding="utf-8")
        selection = ["--file", str(recipe)]
    else:
        (project / ".autobench" / "registry" / "measure.toml").write_text(registration, encoding="utf-8")
        selection = ["measure"]
    scheduler = CompletingScheduler()
    monkeypatch.setattr("autobench.scheduler.McpScheduler", lambda *_: scheduler)

    assert main(["run", "--project", str(project), "--wait-seconds", "3", *selection]) == 0
    accepted, completed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert accepted["state"] == "accepted"
    assert completed["run_id"] == accepted["run_id"]
    assert completed["state"] == "succeeded"
    result = completed["packets"][0]
    assert result["metrics"] == {"elapsed_ms": 2}
    assert result["wall_seconds"] == 0.125
    assert result["cleanup_verified"] is True
    assert Path(result["evidence"]).is_dir()
    assert len(scheduler.submissions) == 1
    assert bool(list((project / ".autobench" / "registry").iterdir())) == (form == "name")
    assert main(["wait", accepted["run_id"], "--project", str(project)]) == 0
    assert json.loads(capsys.readouterr().out) == completed
    assert len(scheduler.submissions) == 1
