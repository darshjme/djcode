"""Durable command schedules for an explicitly launched DJcode worker.

Use `djcode --scheduler` under a service manager on the machine that owns the
workspace. No daemon is silently installed. A claimed run is never replayed
following a worker crash; its status remains interrupted for review.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

from djcode.config import CONFIG_DIR


class Scheduler:
    def __init__(self, path=None):
        self.path = Path(path or CONFIG_DIR / "schedules.db")
        self._claims = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS schedules (id TEXT PRIMARY KEY, "
                "command TEXT, cwd TEXT, due REAL, interval REAL, state TEXT, "
                "result TEXT, lease INTEGER NOT NULL DEFAULT 0)"
            )
            db.execute("BEGIN IMMEDIATE")
            if "lease" not in {row["name"] for row in db.execute("PRAGMA table_info(schedules)")}:
                db.execute("ALTER TABLE schedules ADD COLUMN lease INTEGER NOT NULL DEFAULT 0")

    def _lock(self, ident):
        # Never unlink lock files: replacing an inode could admit two owners.
        root = self.path.with_name(self.path.name + ".locks")
        root.mkdir(exist_ok=True)
        handle = (root / hashlib.sha256(ident.encode()).hexdigest()).open("a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
        return handle

    def _recover(self, db):
        # A process death releases flock, unlike persistent SQLite state.
        # The transaction serializes claiming and recovery across workers.
        for row in db.execute(
            "SELECT id FROM schedules WHERE state='running' AND lease=1"
        ).fetchall():
            handle = self._lock(row["id"])
            if handle is not None:
                try:
                    db.execute(
                        "UPDATE schedules SET state='interrupted', result=? WHERE id=?",
                        (
                            "Worker lost; review effects before creating a new schedule. "
                            "Not retried.",
                            row["id"],
                        ),
                    )
                finally:
                    handle.close()

    def close(self):
        """Release claims when a worker is disposed; never replay abandoned work."""
        for handle in self._claims.values():
            handle.close()
        self._claims.clear()

    def __del__(self):
        self.close()

    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def create(self, command, delay=0, interval=0, cwd=None):
        if (
            not command.strip()
            or not 0 <= delay <= 31536000
            or not (interval == 0 or 60 <= interval <= 31536000)
        ):
            raise ValueError(
                "Require a command, delay 0..1 year, and interval 0 or 60 seconds..1 year"
            )
        cwd = str(Path(cwd or Path.cwd()).resolve())
        if not Path(cwd).is_dir():
            raise ValueError("Schedule workspace does not exist")
        ident = uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute(
                "INSERT INTO schedules (id, command, cwd, due, interval, state, result) "
                "VALUES (?, ?, ?, ?, ?, 'pending', '')",
                (ident, command, cwd, time.time() + delay, interval),
            )
        return ident

    def list(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._recover(db)
            return [dict(row) for row in db.execute("SELECT * FROM schedules ORDER BY due")]

    def cancel(self, ident):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._recover(db)
            changed = db.execute(
                "UPDATE schedules SET state='cancelled' WHERE id=? "
                "AND state IN ('pending', 'interrupted')",
                (ident,),
            ).rowcount
        if not changed:
            raise ValueError(
                "Only pending or interrupted schedules can be cancelled; a running command must "
                "finish or its worker be stopped"
            )

    def recover_legacy(self, ident):
        """Explicit operator recovery after stopping an older, lockless worker."""
        with self.connect() as db:
            changed = db.execute(
                "UPDATE schedules SET state='interrupted', result=? "
                "WHERE id=? AND state='running' AND lease=0",
                ("Legacy worker declared stopped by operator; not retried", ident),
            ).rowcount
        if not changed:
            raise ValueError("Only a running job from a legacy worker requires manual recovery")

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._recover(db)
            row = db.execute(
                "SELECT * FROM schedules WHERE state='pending' AND due <= ? ORDER BY due LIMIT 1",
                (time.time(),),
            ).fetchone()
            if row:
                handle = self._lock(row["id"])
                if handle is None:
                    return None
                self._claims[row["id"]] = handle
                db.execute("UPDATE schedules SET state='running', lease=1 WHERE id=?", (row["id"],))
                return dict(row)
        return None

    async def run_once(self):
        job = self.claim()
        if not job:
            return False
        from djcode.tools.bash import execute_bash
        from djcode.workflow import WorkflowEngine

        try:

            async def dispatch(name, arguments):
                return await execute_bash(**arguments)

            result = await WorkflowEngine(mode="daf").one(
                "bash", {"command": job["command"], "cwd": job["cwd"]}, dispatch
            )
            failed = result.startswith(("Error", "[exit code", "Command timed out"))
            state = "failed" if failed else "pending" if job["interval"] else "completed"
            with self.connect() as db:
                db.execute(
                    "UPDATE schedules SET state=?, result=?, due=? WHERE id=?",
                    (state, result[-16000:], time.time() + job["interval"], job["id"]),
                )
        except BaseException:
            with self.connect() as db:
                db.execute(
                    (
                        "UPDATE schedules SET state='interrupted', result='Worker "
                        "interrupted or engine unavailable; not automatically retried' "
                        "WHERE id=?"
                    ),
                    (job["id"],),
                )
            raise
        finally:
            handle = self._claims.pop(job["id"], None)
            if handle is not None:
                handle.close()
        return True

    async def serve(self):
        while True:
            if not await self.run_once():
                await asyncio.sleep(1)


async def schedule_tool(action="list", command="", delay=0, interval=0, cwd="", schedule_id=""):
    store = Scheduler()
    if action == "list":
        return json.dumps(store.list())
    if action == "create":
        ident = store.create(command, delay, interval, cwd or None)
        return (
            f"Saved schedule {ident}. Runs only while an explicitly started "
            "djcode --scheduler worker is running on this machine."
        )
    if action == "recover":
        store.recover_legacy(schedule_id)
        return "Legacy run marked interrupted; review command effects before rescheduling"
    if action == "cancel":
        store.cancel(schedule_id)
        return "Schedule cancelled"
    raise ValueError("Use schedule create/list/cancel/recover; stop a legacy worker before recover")
