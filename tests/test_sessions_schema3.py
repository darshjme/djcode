"""W4 — the v2 -> v3 sessions migration, proven lossless.

Every fixture below is a **frozen DDL literal**, never a database produced by
calling ``SessionDB``. Once SCHEMA_VERSION is 3, a fixture built by the product
itself would make ``test_v2b_migrates_to_v3`` a v3 -> v3 no-op that passes
forever while proving nothing. The literals are diffable against
``git show 0d42da2:src/djcode/sessions.py`` and ``git show 57c6f6b:...``.

Three measurements drive the shape of these tests, and each is named in the
docstring of the test that guards it:

* ``ALTER TABLE ... ADD COLUMN`` leaves the FTS5 triggers working; a table
  *rebuild* silently repoints them at the orphaned table, after which search of
  old content still works, ``integrity-check`` still says OK, and every new
  message is invisible forever. Only B2 catches that.
* ``integrity-check`` with no argument does not compare the index against the
  content table. Only ``integrity-check, rank=1`` does (SQLite >= 3.37).
* ``executescript`` issues an implicit COMMIT, so one such call inside the
  migration transaction would make C1 and C2 vacuous. C3 is a static guard.
"""

import ast
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import textwrap
import time

import pytest

from djcode import sessions as sessions_module
from djcode.provider import Message
from djcode.sessions import SCHEMA_VERSION, SessionDB, SessionSchemaError
from tests.test_memory_recovery import assert_complete_tools

# ── Frozen fixtures ───────────────────────────────────────────────────────
# Dumped from sqlite_master of a database created by SessionDB at HEAD 1966404.
# SQLite rewrote `conversations` in place, so the three ALTERed columns sit
# before the FOREIGN KEY clause. Reproduced exactly.

_SESSIONS_DDL = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, model TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT '',
    start_time TEXT NOT NULL, end_time TEXT, tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0, messages_count INTEGER DEFAULT 0, tools_used INTEGER DEFAULT 0,
    cwd TEXT DEFAULT '', summary TEXT DEFAULT '');
"""

_CONVERSATIONS_DDL = """
CREATE TABLE conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '', timestamp TEXT NOT NULL,
    tool_calls_json TEXT DEFAULT ''{extra},
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE);
"""

_REST_DDL = """
CREATE TABLE stats_daily (
    date TEXT PRIMARY KEY, sessions INTEGER DEFAULT 0, tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0, messages INTEGER DEFAULT 0, models_used_json TEXT DEFAULT '[]');
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX idx_conversations_session ON conversations(session_id);
CREATE INDEX idx_sessions_start ON sessions(start_time);
CREATE INDEX idx_conversations_content ON conversations(content);
CREATE VIRTUAL TABLE conversations_fts USING fts5(
    content, session_id UNINDEXED, role UNINDEXED,
    content='conversations', content_rowid='id');
CREATE TRIGGER conversations_ai AFTER INSERT ON conversations BEGIN
    INSERT INTO conversations_fts(rowid, content, session_id, role)
    VALUES (new.id, new.content, new.session_id, new.role); END;
CREATE TRIGGER conversations_ad AFTER DELETE ON conversations BEGIN
    INSERT INTO conversations_fts(conversations_fts, rowid, content, session_id, role)
    VALUES ('delete', old.id, old.content, old.session_id, old.role); END;
CREATE TRIGGER conversations_au AFTER UPDATE ON conversations BEGIN
    INSERT INTO conversations_fts(conversations_fts, rowid, content, session_id, role)
    VALUES ('delete', old.id, old.content, old.session_id, old.role);
    INSERT INTO conversations_fts(rowid, content, session_id, role)
    VALUES (new.id, new.content, new.session_id, new.role); END;
"""

# tools/task_tracker.py points DB_PATH at this same sessions.db file.
_TASKS_DDL = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    session_id TEXT DEFAULT '',
    subject TEXT NOT NULL,
    description TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    priority TEXT NOT NULL DEFAULT 'medium',
    depends_on TEXT DEFAULT '',
    tags TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
"""

_SHAPE_EXTRA = {
    "v1": "",
    "v2a": ", tool_call_id TEXT, name TEXT",
    "v2b": ", tool_call_id TEXT, name TEXT, images_json TEXT",
}
_SHAPE_VERSION = {"v1": "1", "v2a": "2", "v2b": "2"}

V2_COLUMNS = {
    "v1": ("id", "session_id", "role", "content", "timestamp", "tool_calls_json"),
    "v2a": (
        "id", "session_id", "role", "content", "timestamp", "tool_calls_json",
        "tool_call_id", "name",
    ),
    "v2b": (
        "id", "session_id", "role", "content", "timestamp", "tool_calls_json",
        "tool_call_id", "name", "images_json",
    ),
}

V3_CONVERSATION_COLUMNS = ("parent_id", "entry_type", "checkpoint_blob", "first_kept_entry_id")
V3_SESSION_COLUMNS = (
    "parent_session_id", "forked_at_entry_id", "last_interaction_at", "updated_at",
)

# F4: unicode, control characters, a 200 KB body, embedded quotes, RTL text, emoji.
NASTY = (
    "\u0645\u0631\u062d\u0628\u0627 RTL \u2014 quote ' and \" \x01\x1f "
    "\U0001f9ea \u4f60\u597d nastytok " + ("\u00e9pais " * 34000)
)


def _msg_rows(shape):
    """(session_id, role, content, ts, tool_calls_json, tool_call_id, name, images_json)."""
    tc = json.dumps(
        [
            {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]
    )
    rows = [
        # F2 — a complete tool round trip in session A.
        ("s_a", "user", "alphatok please read", "t0", "", None, None, None),
        ("s_a", "assistant", "betatok working", "t1", tc, None, None, None),
        ("s_a", "tool", "gammatok one", "t2", "", "c1", "read_file", None),
        ("s_a", "tool", "deltatok two", "t3", "", "c2", "read_file", None),
        ("s_a", "assistant", "epsilontok both read", "t4", "", None, None, None),
        # F3 — images_json round trip (v2b only).
        ("s_b", "user", "zetatok look at this", "t5", "", None, None, '["C:/x/a.png"]'),
        # F4/F5 — encoding hazards, and the NOT NULL DEFAULT '' columns.
        ("s_c", "user", NASTY, "t6", "", None, None, None),
        ("s_c", "assistant", "", "t7", "", None, None, None),
        ("s_c", "user", "etatok keep me", "t8", "", None, None, None),
        # F6 — these two get deleted to leave a gap with the high-water mark intact.
        ("s_c", "user", "thetatok doomed", "t9", "", None, None, None),
        ("s_c", "user", "iotatok doomed", "t10", "", None, None, None),
        ("s_c", "assistant", "kappatok after the gap", "t11", "", None, None, None),
    ]
    width = len(V2_COLUMNS[shape]) - 1  # minus the autoincrement id
    return [r[:width] for r in rows]


def build_v2_db(path, shape="v2b"):
    """Write a frozen pre-v3 database carrying every hazard F1..F11."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        _SESSIONS_DDL
        + _CONVERSATIONS_DDL.format(extra=_SHAPE_EXTRA[shape])
        + _REST_DDL
        + _TASKS_DDL
    )
    # F1 — one ended, one open, one carrying a summary.
    conn.executemany(
        "INSERT INTO sessions (id, model, provider, start_time, end_time, tokens_in, "
        "tokens_out, messages_count, tools_used, cwd, summary) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("s_a", "m1", "p1", "2026-01-01T00:00:00", "2026-01-01T01:00:00",
             10, 20, 3, 2, "/a", ""),
            ("s_b", "m2", "p2", "2026-01-02T00:00:00", None, 5, 6, 1, 0, "/b", ""),
            ("s_c", "m1", "p1", "2026-01-03T00:00:00", None, 0, 0, 0, 0, "/c", "wrap up"),
        ],
    )
    cols = ",".join(V2_COLUMNS[shape][1:])
    marks = ",".join("?" for _ in V2_COLUMNS[shape][1:])
    # F11 — inserted through the live triggers, so conversations_fts is real.
    conn.executemany(
        f"INSERT INTO conversations ({cols}) VALUES ({marks})", _msg_rows(shape)
    )
    # F6 — delete two rows; sqlite_sequence keeps the high-water mark of 12.
    conn.execute("DELETE FROM conversations WHERE content LIKE '%doomed%'")
    # F7 — one orphan, written the way an older release or a third-party tool could.
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        f"INSERT INTO conversations ({cols}) VALUES ({marks})",
        ("s_ghost", "user", "lambdatok orphan", "t99")
        + (None,) * (len(V2_COLUMNS[shape]) - 5),
    )
    # F8
    conn.executemany(
        "INSERT INTO stats_daily (date, sessions, tokens_in, tokens_out, messages) "
        "VALUES (?,?,?,?,?)",
        [("2026-01-01", 1, 10, 20, 3), ("2026-01-02", 1, 5, 6, 1)],
    )
    # F9
    conn.executemany(
        "INSERT INTO tasks (id, session_id, subject, created_at, updated_at) VALUES (?,?,?,?,?)",
        [("t1", "s_a", "ship W4", "c", "u"), ("t2", "s_b", "prove losslessness", "c", "u")],
    )
    # F10 — the version stamp plus an unrelated key that must survive.
    conn.executemany(
        "INSERT INTO meta (key, value) VALUES (?,?)",
        [("schema_version", _SHAPE_VERSION[shape]), ("last_vacuum", "2026-01-01T00:00:00")],
    )
    conn.commit()
    conn.close()
    return path


def snapshot(path, shape="v2b"):
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=OFF")
    snap = {
        "conversations": {
            r[0]: r
            for r in conn.execute(
                f"SELECT {','.join(V2_COLUMNS[shape])} FROM conversations ORDER BY id"
            )
        },
        "sessions": list(conn.execute("SELECT * FROM sessions ORDER BY id")),
        "stats_daily": list(conn.execute("SELECT * FROM stats_daily ORDER BY date")),
        "tasks": list(conn.execute("SELECT * FROM tasks ORDER BY id")),
        "seq": conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='conversations'"
        ).fetchone()[0],
        "triggers": sorted(
            conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger'")
        ),
        "indexes": sorted(
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            )
        ),
        "fk_check": list(conn.execute("PRAGMA foreign_key_check")),
        "meta": sorted(conn.execute("SELECT key, value FROM meta")),
    }
    conn.close()
    return snap


def columns(path, table):
    conn = sqlite3.connect(str(path))
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def meta_version(path):
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return row[0] if row else None
    finally:
        conn.close()


@pytest.fixture
def v2db(tmp_path):
    path = tmp_path / "sessions.db"
    build_v2_db(path)
    return path


# ── Group A — losslessness ────────────────────────────────────────────────


def test_v2b_migrates_to_v3_with_zero_row_loss(v2db):
    before = snapshot(v2db)
    SessionDB(v2db)
    after = snapshot(v2db)
    assert meta_version(v2db) == "3"
    for table in ("conversations", "sessions", "stats_daily", "tasks"):
        assert len(after[table]) == len(before[table]), table
    # Equal, not >=. And the F6 gap at ids 10, 11 is still a gap.
    assert list(after["conversations"]) == list(before["conversations"])
    assert columns(v2db, "conversations") >= set(V3_CONVERSATION_COLUMNS)
    assert columns(v2db, "sessions") >= set(V3_SESSION_COLUMNS)


def test_v2b_migration_preserves_every_column_value(v2db):
    before = snapshot(v2db)
    SessionDB(v2db)
    after = snapshot(v2db)
    assert after["conversations"] == before["conversations"]
    assert after["stats_daily"] == before["stats_daily"]
    # sessions gains four trailing columns; the v2 prefix must be untouched.
    assert [row[:11] for row in after["sessions"]] == [row[:11] for row in before["sessions"]]
    # The 200 KB unicode body, compared in full rather than by length.
    body = next(r[3] for r in after["conversations"].values() if "nastytok" in r[3])
    assert body == NASTY


def test_migration_preserves_message_ordering(v2db):
    before = snapshot(v2db)
    db = SessionDB(v2db)
    for sid in ("s_a", "s_b", "s_c"):
        expected = [r[3] for r in before["conversations"].values() if r[1] == sid]
        assert [m["content"] for m in db.load_conversation(sid, view="full")] == expected
    # The row after the F6 gap did not move.
    assert db.load_conversation("s_c", view="full")[-1]["content"] == "kappatok after the gap"


def test_migration_preserves_tool_call_pairing(v2db):
    db = SessionDB(v2db)
    rows = db.load_conversation("s_a", view="full")
    messages = [Message(**{k: v for k, v in row.items() if k != "timestamp"}) for row in rows]
    assert_complete_tools(messages)
    assert messages[2].name == "read_file"
    assert messages[2].tool_call_id == "c1"


def test_migration_preserves_images_json(v2db):
    db = SessionDB(v2db)
    assert db.load_conversation("s_b", view="full")[0]["images"] == ["C:/x/a.png"]


def test_migration_preserves_sqlite_sequence_high_water_mark(v2db):
    before = snapshot(v2db)
    assert before["seq"] == 13  # 12 message rows + the orphan; 2 were deleted
    db = SessionDB(v2db)
    assert snapshot(v2db)["seq"] == before["seq"]
    new_id = db.save_message("s_a", "user", "after migration")
    # 10 and 11 were deleted; reusing them would map stale FTS entries onto a
    # live row. This is the assertion that fails if anyone rebuilds the table.
    assert new_id == before["seq"] + 1


def test_migration_preserves_schema_objects(v2db):
    before = snapshot(v2db)
    SessionDB(v2db)
    after = snapshot(v2db)
    assert after["triggers"] == before["triggers"]
    assert [name for name, _ in after["triggers"]] == [
        "conversations_ad", "conversations_ai", "conversations_au",
    ]
    assert set(after["indexes"]) >= {
        "idx_conversations_session", "idx_sessions_start", "idx_conversations_content",
        "idx_tasks_session", "idx_tasks_status", "idx_tasks_created",
    }
    conn = sqlite3.connect(str(v2db))
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {
        "conversations_fts", "conversations_fts_config", "conversations_fts_data",
        "conversations_fts_docsize", "conversations_fts_idx",
    } <= tables


def test_migration_leaves_orphan_rows_alone(v2db):
    before = snapshot(v2db)
    assert len(before["fk_check"]) == 1
    SessionDB(v2db)
    after = snapshot(v2db)
    assert after["fk_check"] == before["fk_check"]
    assert any(r[3] == "lambdatok orphan" for r in after["conversations"].values())


def test_migration_preserves_the_tasks_table(v2db):
    before = snapshot(v2db)
    SessionDB(v2db)
    assert snapshot(v2db)["tasks"] == before["tasks"]


def test_migration_preserves_unrelated_meta_keys(v2db):
    SessionDB(v2db)
    assert dict(snapshot(v2db)["meta"]) == {
        "schema_version": "3",
        "last_vacuum": "2026-01-01T00:00:00",
    }


def test_migration_backfills_timestamps_without_inventing_lineage(v2db):
    db = SessionDB(v2db)
    ended = db.get_session("s_a")
    assert ended.updated_at == ended.end == "2026-01-01T01:00:00"
    assert ended.last_interaction_at == "2026-01-01T01:00:00"
    open_one = db.get_session("s_b")
    assert open_one.updated_at == open_one.start == "2026-01-02T00:00:00"
    # Pre-v3 ids carry no ancestry: every transcript was DELETEd and reinserted
    # on each checkpoint. Backfilling a guess would be the data loss this
    # migration exists to prevent.
    for entry in db.list_entries("s_a"):
        assert entry.parent_id is None
        assert entry.first_kept_entry_id is None
        assert entry.entry_type == "message"
    assert ended.parent_session_id is None
    assert ended.forked_at_entry_id is None


# ── Group B — FTS5 ────────────────────────────────────────────────────────


def _fts_ids(path, token):
    conn = sqlite3.connect(str(path))
    try:
        return [
            r[0]
            for r in conn.execute(
                "SELECT rowid FROM conversations_fts WHERE conversations_fts MATCH ?", (token,)
            )
        ]
    finally:
        conn.close()


def test_fts_finds_pre_migration_content_after_migration(v2db):
    before = snapshot(v2db)
    tokens = {
        row[0]: next(w for w in row[3].split() if w.endswith("tok"))
        for row in before["conversations"].values()
        if any(w.endswith("tok") for w in row[3].split())
    }
    assert len(tokens) >= 8
    SessionDB(v2db)
    for row_id, token in tokens.items():
        assert _fts_ids(v2db, token) == [row_id], token


def test_fts_indexes_content_written_after_migration(v2db):
    db = SessionDB(v2db)
    db.save_message("s_a", "assistant", "mutok written after the migration")
    # The only test that fails when a table rebuild drops the triggers: search
    # of pre-migration content keeps working and integrity-check stays clean.
    assert [s.id for s in db.search_sessions("mutok")] == ["s_a"]


def test_fts_delete_trigger_still_fires_after_migration(v2db):
    db = SessionDB(v2db)
    new_id = db.save_message("s_a", "assistant", "nutok transient")
    assert _fts_ids(v2db, "nutok") == [new_id]
    conn = sqlite3.connect(str(v2db))
    try:
        conn.execute("DELETE FROM conversations WHERE id = ?", (new_id,))
        conn.commit()
    finally:
        conn.close()
    assert _fts_ids(v2db, "nutok") == []
    _assert_fts_content_integrity(v2db)


def _assert_fts_content_integrity(path):
    if sqlite3.sqlite_version_info < (3, 37):
        pytest.skip("integrity-check with rank=1 needs SQLite >= 3.37")
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "INSERT INTO conversations_fts(conversations_fts, rank) VALUES('integrity-check', 1)"
        )
    finally:
        conn.close()


def test_fts_content_integrity_check_is_clean_after_migration(v2db):
    """The unargumented form is NOT sufficient and is asserted only as a floor.

    Measured: after an insert that bypassed the triggers, ``integrity-check``
    returned OK while the row was unsearchable; ``integrity-check, rank=1``
    raised ``database disk image is malformed`` on the same database.
    """
    SessionDB(v2db)
    conn = sqlite3.connect(str(v2db))
    try:
        conn.execute("INSERT INTO conversations_fts(conversations_fts) VALUES('integrity-check')")
    finally:
        conn.close()
    _assert_fts_content_integrity(v2db)


def test_fts_survives_a_compaction_entry(v2db):
    db = SessionDB(v2db)
    before = [m["content"] for m in db.load_conversation("s_a", view="full")]
    kept = [Message("assistant", "epsilontok both read")]
    db.record_compaction(
        "s_a", summary="xitok we read two files", kept_messages=kept,
        strategy="summary", messages_removed=4,
    )
    assert [s.id for s in db.search_sessions("xitok")] == ["s_a"]
    # The whole point of P0-4: the transcript, not the summary.
    assert [m["content"] for m in db.load_conversation("s_a", view="full")] == before
    model = db.load_conversation("s_a", view="model")
    assert model[0]["content"].endswith("xitok we read two files")
    assert [m["content"] for m in model[1:]] == ["epsilontok both read"]
    _assert_fts_content_integrity(v2db)


# ── Group C — atomicity ───────────────────────────────────────────────────


def test_migration_is_one_transaction_and_rolls_back_whole(v2db, monkeypatch):
    before = snapshot(v2db)
    # ADD COLUMN with NOT NULL and no default is rejected by SQLite, so this
    # fails after the legitimate ALTERs have already run in the transaction.
    monkeypatch.setattr(
        sessions_module,
        "_SCHEMA_COLUMNS",
        sessions_module._SCHEMA_COLUMNS + (("conversations", "boom", "boom TEXT NOT NULL"),),
    )
    with pytest.raises(SessionSchemaError):
        SessionDB(v2db)

    assert meta_version(v2db) == "2"
    conv = columns(v2db, "conversations")
    assert not (conv & set(V3_CONVERSATION_COLUMNS)), conv
    assert not (columns(v2db, "sessions") & set(V3_SESSION_COLUMNS))
    after = snapshot(v2db)
    assert after["conversations"] == before["conversations"]
    assert after["sessions"] == before["sessions"]
    assert after["tasks"] == before["tasks"]


_KILL_SCRIPT = textwrap.dedent(
    """
    import os, sqlite3, sys
    conn = sqlite3.connect(sys.argv[1], isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("ALTER TABLE conversations ADD COLUMN parent_id INTEGER")
    conn.execute("ALTER TABLE conversations ADD COLUMN entry_type TEXT NOT NULL DEFAULT 'message'")
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '3')")
    conn.execute("DELETE FROM conversations WHERE id = 1")
    os._exit(1)
    """
)


def test_migration_survives_a_hard_process_kill(v2db):
    """os._exit, not sys.exit: the latter runs atexit and closes the handle."""
    before = snapshot(v2db)
    script = pathlib.Path(v2db).parent / "killer.py"
    script.write_text(_KILL_SCRIPT, encoding="utf-8")
    assert subprocess.run([sys.executable, str(script), str(v2db)]).returncode == 1

    # Either cleanly v2 (rolled back) or cleanly v3 — never in between.
    assert meta_version(v2db) in {"2", "3"}
    db = SessionDB(v2db)
    assert meta_version(v2db) == "3"
    after = snapshot(v2db)
    assert after["conversations"] == before["conversations"]
    assert [m["content"] for m in db.load_conversation("s_a", view="full")] == [
        r[3] for r in before["conversations"].values() if r[1] == "s_a"
    ]


def test_migration_never_calls_executescript_inside_a_transaction():
    """executescript issues an implicit COMMIT, which would make C1/C2 vacuous."""
    source = pathlib.Path(sessions_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    migrate = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_migrate"
    )
    offenders = [
        node.func.attr
        for node in ast.walk(migrate)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "executescript"
    ]
    assert offenders == []


def test_failed_migration_raises_instead_of_returning_a_broken_db(v2db, monkeypatch):
    def boom(self, conn, found):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SessionDB, "_migrate", boom)
    with pytest.raises(SessionSchemaError) as excinfo:
        SessionDB(v2db)
    assert "database is locked" in str(excinfo.value)


_HOLD_SCRIPT = textwrap.dedent(
    """
    import pathlib, sqlite3, sys, time
    conn = sqlite3.connect(sys.argv[1], isolation_level=None, timeout=30)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE meta SET value = value WHERE key = 'last_vacuum'")
    pathlib.Path(sys.argv[2]).write_text("held", encoding="utf-8")
    time.sleep(float(sys.argv[3]))
    conn.execute("COMMIT")
    """
)


@pytest.mark.skipif(os.environ.get("DJCODE_SKIP_SLOW") == "1", reason="slow lock test")
def test_concurrent_open_during_migration_does_not_corrupt(v2db):
    before = snapshot(v2db)
    script = pathlib.Path(v2db).parent / "holder.py"
    script.write_text(_HOLD_SCRIPT, encoding="utf-8")
    flag = pathlib.Path(v2db).parent / "held.flag"
    proc = subprocess.Popen([sys.executable, str(script), str(v2db), str(flag), "9"])
    try:
        deadline = time.monotonic() + 20
        while not flag.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert flag.exists(), "lock holder never started"
        # Measured pre-W4: this returned after 5.6s WITHOUT raising, handing back
        # a SessionDB bound to a database it had not migrated.
        with pytest.raises(SessionSchemaError):
            SessionDB(v2db)
        assert meta_version(v2db) == "2"
    finally:
        proc.wait(timeout=60)

    db = SessionDB(v2db)
    assert meta_version(v2db) == "3"
    after = snapshot(v2db)
    assert after["conversations"] == before["conversations"]
    assert len(db.load_conversation("s_a", view="full")) == 5


# ── Group D — version gate ────────────────────────────────────────────────


def test_migrating_an_already_v3_database_is_a_no_op(v2db, caplog):
    SessionDB(v2db)
    baseline = snapshot(v2db)
    for _ in range(3):
        with caplog.at_level("ERROR"):
            SessionDB(v2db)
        assert "duplicate column" not in caplog.text
        assert snapshot(v2db) == baseline
        assert meta_version(v2db) == "3"
    conv = list(columns(v2db, "conversations"))
    assert len(conv) == len(set(conv))


def test_v2a_without_images_json_migrates(tmp_path):
    path = build_v2_db(tmp_path / "v2a.db", shape="v2a")
    before = snapshot(path, shape="v2a")
    db = SessionDB(path)
    assert meta_version(path) == "3"
    # schema_version '2' means "with images_json" OR "without" — the release
    # that added the column never bumped the version, so the step has to be
    # keyed on PRAGMA table_info, not on the number.
    assert "images_json" in columns(path, "conversations")
    assert columns(path, "conversations") >= set(V3_CONVERSATION_COLUMNS)
    assert snapshot(path, shape="v2a")["conversations"] == before["conversations"]
    assert len(db.load_conversation("s_a", view="full")) == 5


def test_v1_database_migrates_through_to_v3(tmp_path):
    path = build_v2_db(tmp_path / "v1.db", shape="v1")
    before = snapshot(path, shape="v1")
    db = SessionDB(path)
    assert meta_version(path) == "3"
    assert columns(path, "conversations") >= {"tool_call_id", "name", "images_json"}
    assert columns(path, "conversations") >= set(V3_CONVERSATION_COLUMNS)
    assert snapshot(path, shape="v1")["conversations"] == before["conversations"]
    rows = db.load_conversation("s_a", view="full")
    # v1 had no tool_call_id/name columns at all, so there is no pairing to
    # recover — the added columns are NULL and the content is intact. Anything
    # else here would be the migration inventing data.
    assert [r["tool_call_id"] for r in rows] == [None] * 5
    assert [r["name"] for r in rows] == [None] * 5
    assert [r["role"] for r in rows] == ["user", "assistant", "tool", "tool", "assistant"]
    assert rows[1]["tool_calls"][0]["id"] == "c1"
    for row_id, row in before["conversations"].items():
        token = next((w for w in row[3].split() if w.endswith("tok")), None)
        if token:
            assert _fts_ids(path, token) == [row_id], token


def test_a_future_version_database_refuses_to_open(v2db):
    conn = sqlite3.connect(str(v2db))
    conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    before = snapshot(v2db)

    with pytest.raises(SessionSchemaError) as excinfo:
        SessionDB(v2db)
    assert "99" in str(excinfo.value) and str(SCHEMA_VERSION) in str(excinfo.value)
    # Measured pre-W4: the unconditional INSERT OR REPLACE stamped a v99
    # database back down to '2'. A newer DJcode's data must never be mangled.
    assert meta_version(v2db) == "99"
    assert not (columns(v2db, "conversations") & set(V3_CONVERSATION_COLUMNS))
    assert snapshot(v2db) == before


@pytest.mark.parametrize("damage", ["empty_meta", "garbage_value", "no_meta_table"])
def test_missing_or_garbage_schema_version_is_handled(v2db, damage):
    conn = sqlite3.connect(str(v2db))
    if damage == "empty_meta":
        conn.execute("DELETE FROM meta")
    elif damage == "garbage_value":
        conn.execute("UPDATE meta SET value='v2.1-beta' WHERE key='schema_version'")
    else:
        conn.execute("DROP TABLE meta")
    conn.commit()
    conn.close()
    before = snapshot(v2db) if damage != "no_meta_table" else None

    # int() on garbage raises ValueError, which is not a sqlite3.Error and would
    # escape the constructor uncaught.
    db = SessionDB(v2db)
    assert meta_version(v2db) == "3"
    assert columns(v2db, "conversations") >= set(V3_CONVERSATION_COLUMNS)
    assert len(db.load_conversation("s_a", view="full")) == 5
    if before is not None:
        assert snapshot(v2db)["conversations"] == before["conversations"]


def test_version_gate_keys_steps_on_table_info_not_only_on_the_version(v2db):
    """A v3 database whose meta row was reverted must not raise duplicate column."""
    conn = sqlite3.connect(str(v2db))
    conn.execute("ALTER TABLE conversations ADD COLUMN parent_id INTEGER")
    conn.execute("ALTER TABLE sessions ADD COLUMN updated_at TEXT")
    conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    conn.commit()
    conn.close()

    db = SessionDB(v2db)
    assert meta_version(v2db) == "3"
    assert columns(v2db, "conversations") >= set(V3_CONVERSATION_COLUMNS)
    assert len(db.load_conversation("s_a", view="full")) == 5


def test_a_file_holding_only_the_task_tracker_tables_is_not_treated_as_v1(tmp_path):
    """The real ~/.djcode/sessions.db on a fresh box holds only `tasks`."""
    path = tmp_path / "tasks-only.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_TASKS_DDL)
    conn.execute(
        "INSERT INTO tasks (id, session_id, subject, created_at, updated_at) VALUES (?,?,?,?,?)",
        ("t9", "", "survive", "c", "u"),
    )
    conn.commit()
    conn.close()

    db = SessionDB(path)
    assert meta_version(path) == "3"
    conn = sqlite3.connect(str(path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    finally:
        conn.close()
    sid = db.create_session("m", "p")
    assert db.append_messages(sid, [Message("user", "hello")])


# ── Group E — the W4 behaviour the migration exists to enable ─────────────


def test_compaction_never_deletes_the_transcript(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    sid = db.create_session("m", "p")
    msgs = [Message("system", "rules")] + [Message("user", f"turn {i}") for i in range(6)]
    db.append_messages(sid, msgs)
    kept = msgs[-2:]
    db.record_compaction(sid, summary="earlier turns", kept_messages=kept, messages_removed=5)

    full = [m["content"] for m in db.load_conversation(sid, view="full")]
    assert full == ["rules"] + [f"turn {i}" for i in range(6)]
    model = [m["content"] for m in db.load_conversation(sid)]
    assert model[0].startswith("[Conversation summary]")
    assert model[1:] == ["turn 4", "turn 5"]


def test_compaction_floor_never_splits_a_tool_group(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    sid = db.create_session("m", "p")
    calls = [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]
    msgs = [
        Message("user", "go"),
        Message("assistant", "", tool_calls=calls),
        Message("tool", "result", tool_call_id="c1", name="read_file"),
        Message("assistant", "done"),
    ]
    db.append_messages(sid, msgs)
    # Ask to keep only the tool reply and what follows it — a transcript every
    # provider would reject, because the assistant that issued c1 is missing.
    db.record_compaction(sid, summary="s", kept_messages=msgs[2:], messages_removed=2)
    replay = db.load_conversation(sid)[1:]
    assert_complete_tools(
        [Message(**{k: v for k, v in r.items() if k != "timestamp"}) for r in replay]
    )
    assert replay[0]["role"] == "assistant"


def test_append_messages_is_idempotent_and_never_deletes(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    sid = db.create_session("m", "p")
    msgs = [Message("system", "rules"), Message("user", "one")]
    assert db.append_messages(sid, msgs) == [1, 2]
    assert db.append_messages(sid, msgs) == []
    msgs.append(Message("assistant", "two"))
    assert db.append_messages(sid, msgs) == [3]
    # A second instance has no warm cache and must reach the same conclusion.
    assert SessionDB(tmp_path / "s.db").append_messages(sid, msgs) == []
    # Duplicate user input is real content, not a re-save.
    msgs.append(Message("user", "one"))
    assert len(db.append_messages(sid, msgs)) == 1
    assert len(db.load_conversation(sid, view="full")) == 4


def test_append_messages_carries_images_and_moves_the_timestamps(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    sid = db.create_session("m", "p")
    created = db.get_session(sid).last_interaction_at
    db.append_messages(sid, [Message("user", "look", images=["C:/x/a.png"])])
    assert db.load_conversation(sid)[0]["images"] == ["C:/x/a.png"]
    session = db.get_session(sid)
    assert session.last_interaction_at >= created
    assert session.updated_at == session.last_interaction_at


def test_append_messages_skips_synthetic_context_messages(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    sid = db.create_session("m", "p")
    db.append_messages(
        sid,
        [
            Message("system", "[Injected context]\nrecalled notes"),
            Message("system", "[Conversation summary]\nearlier"),
            Message("user", "real"),
        ],
    )
    assert [m["content"] for m in db.load_conversation(sid, view="full")] == ["real"]


def test_fork_leaves_the_parent_open_and_copies_the_cut(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    parent = db.create_session("m", "p")
    db.update_session(parent, tokens_in=100, tokens_out=200, messages=3, tools_used=1)
    ids = db.append_messages(
        parent, [Message("user", f"m{i}") for i in range(4)]
    )
    fork = db.fork_session(parent, before_entry_id=ids[2])

    assert db.get_session(parent).end is None  # the parent stays alive
    child = db.get_session(fork)
    assert child.parent_session_id == parent
    assert child.forked_at_entry_id == ids[2]
    # Counters are zeroed, or /stats double-counts every token the parent spent.
    counters = (child.tokens_in, child.tokens_out, child.messages_count, child.tools_used)
    assert counters == (0, 0, 0, 0)
    assert [m["content"] for m in db.load_conversation(fork, view="full")] == ["m0", "m1"]
    assert [m["content"] for m in db.load_conversation(parent, view="full")] == [
        "m0", "m1", "m2", "m3",
    ]
    # New rowids, so the copies are independently searchable.
    assert {s.id for s in db.search_sessions("m1")} == {parent, fork}
    conn = sqlite3.connect(str(tmp_path / "s.db"))
    try:
        assert list(conn.execute("PRAGMA foreign_key_check")) == []
    finally:
        conn.close()


def test_delete_session_still_cascades_and_leaves_the_fork_intact(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    parent = db.create_session("m", "p")
    db.append_messages(parent, [Message("user", "shared")])
    fork = db.fork_session(parent)
    assert db.delete_session(parent)
    assert db.get_session(parent) is None
    # parent_session_id is deliberately NOT a foreign key: with NO ACTION the
    # delete would fail outright on any forked session.
    assert db.get_session(fork) is not None
    assert [m["content"] for m in db.load_conversation(fork, view="full")] == ["shared"]


def test_entry_type_domain_is_enforced_in_python(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    sid = db.create_session("m", "p")
    with pytest.raises(ValueError):
        db.save_message(sid, "user", "x", entry_type="nonsense")


def test_checkpoint_entries_are_invisible_to_both_views(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    sid = db.create_session("m", "p")
    db.append_messages(sid, [Message("user", "real")])
    db.save_message(sid, "system", "w5 blob", entry_type="checkpoint", checkpoint_blob="{}")
    assert [m["content"] for m in db.load_conversation(sid, view="full")] == ["real"]
    assert [m["content"] for m in db.load_conversation(sid)] == ["real"]
    assert [e.entry_type for e in db.list_entries(sid)] == ["message", "checkpoint"]


def test_load_conversation_rejects_an_unknown_view(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    with pytest.raises(ValueError):
        db.load_conversation("nope", view="raw")
