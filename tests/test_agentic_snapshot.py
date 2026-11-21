import hashlib
import subprocess

import pytest

from autobench import snapshot
from autobench.errors import UnsafeWorkspace
from autobench.models import Benchmark


def _recipe(workspace):
    return Benchmark("test", 1, workspace, ("measure",), (), None, ())


def test_snapshot_preserves_execute_bits_and_rejects_preupgrade_cache(tmp_path):
    program = tmp_path / "measure"
    payload = b"#!/bin/sh\nprintf 'ran\n'\n"
    program.write_bytes(payload)
    program.chmod(0o751)
    recipe = _recipe(tmp_path)
    state = tmp_path / "state"
    prior_digest = snapshot.fingerprint_manifest({"measure": hashlib.sha256(payload).hexdigest()})
    prior = state / "snapshots" / prior_digest
    prior.mkdir(parents=True)
    (prior / "measure").write_bytes(payload)

    frozen = snapshot.capture(recipe, state)
    assert frozen.fingerprint != prior_digest
    assert snapshot.observe(recipe) == (frozen.fingerprint, frozen.manifest)
    assert (frozen.directory / "measure").stat().st_mode & 0o111 == 0o111
    assert subprocess.check_output([frozen.directory / "measure"], text=True) == "ran\n"

    program.chmod(0o740)
    revised = snapshot.capture(recipe, state)
    assert revised.fingerprint not in {frozen.fingerprint, prior_digest}
    assert snapshot.observe(recipe) == (revised.fingerprint, revised.manifest)
    assert (revised.directory / "measure").stat().st_mode & 0o111 == 0o100

    program.chmod(0o640)
    assert snapshot.capture(recipe, state).directory == prior


def test_snapshot_never_publishes_mode_changed_during_copy(tmp_path, monkeypatch):
    program = tmp_path / "measure"
    program.write_text("sample")
    program.chmod(0o700)
    state = tmp_path / "state"
    copy_file = snapshot.shutil.copyfile

    def copy_then_change(src, dst):
        result = copy_file(src, dst)
        program.chmod(0o600)
        return result

    monkeypatch.setattr(snapshot.shutil, "copyfile", copy_then_change)
    with pytest.raises(UnsafeWorkspace, match="source scope changed"):
        snapshot.capture(_recipe(tmp_path), state)
    assert not list((state / "snapshots").iterdir())
