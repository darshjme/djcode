"""Real process ownership regressions, without inference or executing jobs."""

import subprocess
import sys

import pytest

from djcode.scheduler import Scheduler


def test_crashed_worker_is_interrupted_and_never_replayed(tmp_path):
    path = tmp_path / "schedule.db"
    store = Scheduler(path)
    ident = store.create("echo must-not-replay", cwd=tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os,sys; from pathlib import Path; from djcode.scheduler import Scheduler; "
            "s=Scheduler(Path(sys.argv[1])); assert s.claim(); os._exit(9)",
            str(path),
        ],
        check=False,
    )
    assert result.returncode == 9
    assert store.claim() is None
    assert store.list()[0]["state"] == "interrupted"
    store.cancel(ident)
    assert store.list()[0]["state"] == "cancelled"


def test_other_workers_cannot_recover_or_cancel_a_live_claim(tmp_path):
    path = tmp_path / "schedule.db"
    first, second = Scheduler(path), Scheduler(path)
    ident = first.create("echo active", cwd=tmp_path)
    try:
        assert first.claim()["id"] == ident
        assert second.claim() is None
        assert second.list()[0]["state"] == "running"
        with pytest.raises(ValueError):
            second.cancel(ident)
        first.close()
        assert second.list()[0]["state"] == "interrupted"
    finally:
        first.close()
        second.close()


def test_migration_does_not_steal_from_lockless_legacy_worker(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE schedules (id TEXT PRIMARY KEY, command TEXT, cwd TEXT, "
            "due REAL, interval REAL, state TEXT, result TEXT)"
        )
        db.execute(
            "INSERT INTO schedules VALUES ('old', 'echo old', ?, 0, 0, 'running', '')",
            (str(tmp_path),),
        )
    store = Scheduler(path)
    assert store.list()[0]["state"] == "running"
    assert store.claim() is None
    store.recover_legacy("old")
    assert store.list()[0]["state"] == "interrupted"
    assert store.claim() is None
