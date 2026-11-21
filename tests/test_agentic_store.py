import sqlite3

import pytest

from autobench.store import Store


def test_run_storage_upgrades_legacy_database_without_losing_attempts(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    old_attempt = store.begin_attempt("old", 1, 0, "legacy")
    store.mark_current("old", 0, "legacy", old_attempt)
    with store.db:
        store.db.execute("DROP TABLE run_attempts")
        store.db.execute("DROP TABLE runs")
    store.close()

    store = Store(path, create=False)
    assert store.current("old", 0) == "legacy"
    assert [row["id"] for row in store.unresolved()] == [old_attempt]
    store.begin_run("fresh", {"benchmark": "new"})
    first = store.begin_attempt("new", 1, 0, "new", run_id="fresh")
    second = store.begin_attempt("new", 1, 1, "new", run_id="fresh")
    store.set_run("fresh", "running")
    reader = Store(path, create=False)
    try:
        active = reader.active_runs()
        assert [(run["id"], run["state"], run["request"]) for run in active] == [
            ("fresh", "running", {"benchmark": "new"})
        ]
        assert [row["id"] for row in reader.run_attempts("fresh")] == [first, second]
        assert reader.attempt_run_id(first) == "fresh"
        assert reader.attempt_run_id(old_attempt) is None
        store.set_run("fresh", "succeeded", {"state": "succeeded"})
        assert reader.active_runs() == []
        assert reader.get_run("fresh")["result"] == {"state": "succeeded"}
    finally:
        reader.close()
        store.close()


def test_attempt_association_rolls_back_if_run_does_not_exist(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            store.begin_attempt("new", 1, 0, "new", attempt_id="missing", run_id="unknown")
        assert store.attempts_for("new") == []
        assert store.unresolved() == []
    finally:
        store.close()


def test_explicit_runs_are_preserved_by_background_compaction_and_unknown_cleanup(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    try:
        store.begin_run("old", {})
        successful = store.begin_attempt("test", 1, 0, "fingerprint", run_id="old")
        store.set_attempt(successful, "succeeded")
        store.set_run("old", "succeeded")
        store.begin_run("unknown", {})
        unknown = store.begin_attempt("test", 1, 0, "fingerprint", run_id="unknown")
        store.set_attempt(unknown, "cleanup_unknown")
        store.set_run("unknown", "blocked")
        store.begin_run("pending", {})
        recent = store.begin_attempt("test", 1, 0, "background")
        store.set_attempt(recent, "succeeded")
        store.compact_history("test", 0, keep=0)
        assert {row["id"] for row in store.attempts_for("test")} == {successful, unknown}
        assert store.compact_runs(keep=0) == ["old"]
        assert store.get_run("old") is None
        assert store.run_attempts("old") == []
        assert [row["id"] for row in store.unresolved()] == [unknown]
        assert store.get_run("pending")["state"] == "pending"
    finally:
        store.close()
