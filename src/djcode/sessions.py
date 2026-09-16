"""SQLite Session Persistence for DJcode.

Replaces the JSON-based stats system with a proper SQLite database.
Enables: session resume, conversation search, faster aggregation.

Zero new dependencies — stdlib sqlite3 only.
Database: ~/.djcode/sessions.db
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from djcode.config import CONFIG_DIR

logger = logging.getLogger(__name__)

DB_PATH = CONFIG_DIR / "sessions.db"

GOLD = "#FFD700"

# Schema version — bump when adding migrations
SCHEMA_VERSION = 3

# ``conversations.entry_type`` domain. SQLite carries no CHECK constraint for
# this (a CHECK would need a table rebuild, and a rebuild silently repoints the
# FTS5 triggers at the orphaned table — see W4-1), so the domain is enforced in
# Python, here.
ENTRY_MESSAGE = "message"
ENTRY_COMPACTION = "compaction"
ENTRY_CHECKPOINT = "checkpoint"
ENTRY_TYPES = frozenset({ENTRY_MESSAGE, ENTRY_COMPACTION, ENTRY_CHECKPOINT})

# Synthetic messages that live only in the model's working set. They are
# rebuilt from scratch on every turn, so persisting them as transcript rows
# would make a transient artefact permanent.
SUMMARY_PREFIX = "[Conversation summary]"
INJECTED_PREFIX = "[Injected context]"


class SessionSchemaError(RuntimeError):
    """The sessions database cannot be opened or migrated safely.

    Raised instead of logging-and-continuing: a half-migrated database that
    presents as healthy degrades every later read to ``[]``/``None``, which
    reads to the user as "no history" rather than "the database is broken".
    """


# Ordered, additive-only migration steps. ``ALTER TABLE ADD COLUMN`` exclusively:
# a 12-step rebuild rewrites the conversations_ai/ad/au trigger bodies to follow
# the renamed table, after which FTS silently stops indexing new rows and
# ``integrity-check`` still reports clean.
_SCHEMA_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # v1 -> v2 (kept outside the version gate so a database whose columns were
    # dropped out from under it heals on the next open).
    ("conversations", "tool_call_id", "tool_call_id TEXT"),
    ("conversations", "name", "name TEXT"),
    ("conversations", "images_json", "images_json TEXT"),
    # v2 -> v3
    ("conversations", "parent_id", "parent_id INTEGER"),
    ("conversations", "entry_type", "entry_type TEXT NOT NULL DEFAULT 'message'"),
    ("conversations", "checkpoint_blob", "checkpoint_blob TEXT"),
    ("conversations", "first_kept_entry_id", "first_kept_entry_id INTEGER"),
    ("sessions", "parent_session_id", "parent_session_id TEXT"),
    ("sessions", "forked_at_entry_id", "forked_at_entry_id INTEGER"),
    ("sessions", "last_interaction_at", "last_interaction_at TEXT"),
    ("sessions", "updated_at", "updated_at TEXT"),
)


# Every entry is written through this one statement, so no column can be
# silently dropped by one writer and carried by another (pre-W4, save_message
# omitted images_json while save_conversation wrote it, and vision attachments
# vanished whenever the former ran).
_INSERT_ENTRY_SQL = """
    INSERT INTO conversations (
        session_id, role, content, timestamp, tool_calls_json, tool_call_id,
        name, images_json, entry_type, first_kept_entry_id, checkpoint_blob, parent_id
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _loads_list(raw: Any) -> list:
    """Parse a JSON list column, tolerating NULL, '' and corrupt values."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _canonical_json(raw: Any) -> str:
    """Stable text for a JSON list column, so fingerprints compare by value."""
    items = _loads_list(raw)
    return json.dumps(items) if items else ""


def _fingerprint(
    role: str, content: str, tool_calls_json: str, tool_call_id: str, name: str, images_json: str
) -> str:
    payload = "\x1f".join((role, content, tool_calls_json, tool_call_id, name, images_json))
    return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()


def _parse_message(msg: Any) -> dict[str, Any] | None:
    """Normalise a Message object or dict into the columns of one log entry.

    Returns None for anything that must not become a transcript row: an
    unrecognised object, or one of the synthetic system messages the context
    manager rebuilds from scratch on every turn ([Injected context]) and the
    compressor re-creates on every compaction ([Conversation summary], which is
    persisted once, as its own compaction entry, by record_compaction).
    """
    if hasattr(msg, "role"):
        role = msg.role or ""
        content = msg.content or ""
        tool_calls = getattr(msg, "tool_calls", None)
        tool_call_id = getattr(msg, "tool_call_id", None)
        name = getattr(msg, "name", None)
        images = getattr(msg, "images", None)
    elif isinstance(msg, dict):
        role = msg.get("role", "") or ""
        content = msg.get("content", "") or ""
        tool_calls = msg.get("tool_calls")
        tool_call_id = msg.get("tool_call_id")
        name = msg.get("name")
        images = msg.get("images")
    else:
        return None

    if role == "system" and content.startswith((INJECTED_PREFIX, SUMMARY_PREFIX)):
        return None

    tool_calls_json = json.dumps(tool_calls) if tool_calls else ""
    images_json = json.dumps(list(images)) if images else "[]"
    return {
        "role": role,
        "content": content,
        "tool_calls_json": tool_calls_json,
        "tool_call_id": tool_call_id,
        "name": name,
        "images_json": images_json,
        "fp": _fingerprint(
            role,
            content,
            _canonical_json(tool_calls_json),
            tool_call_id or "",
            name or "",
            _canonical_json(images_json),
        ),
    }


def _row_fingerprint(row: sqlite3.Row) -> str:
    return _fingerprint(
        row["role"] or "",
        row["content"] or "",
        _canonical_json(row["tool_calls_json"]),
        row["tool_call_id"] or "",
        row["name"] or "",
        _canonical_json(row["images_json"]),
    )


def _row_to_message(row: sqlite3.Row) -> dict[str, Any]:
    """Shape a stored entry the way every resume path already expects it."""
    msg: dict[str, Any] = {
        "role": row["role"],
        "content": row["content"],
        "timestamp": row["timestamp"],
        "tool_call_id": row["tool_call_id"],
        "name": row["name"],
        "images": _loads_list(row["images_json"]),
    }
    if row["tool_calls_json"]:
        try:
            msg["tool_calls"] = json.loads(row["tool_calls_json"])
        except json.JSONDecodeError:
            pass
    return msg


_BASE_SCHEMA_SQL = """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    model TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '',
                    start_time TEXT NOT NULL,
                    end_time TEXT,
                    tokens_in INTEGER DEFAULT 0,
                    tokens_out INTEGER DEFAULT 0,
                    messages_count INTEGER DEFAULT 0,
                    tools_used INTEGER DEFAULT 0,
                    cwd TEXT DEFAULT '',
                    summary TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    timestamp TEXT NOT NULL,
                    tool_calls_json TEXT DEFAULT '',
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS stats_daily (
                    date TEXT PRIMARY KEY,
                    sessions INTEGER DEFAULT 0,
                    tokens_in INTEGER DEFAULT 0,
                    tokens_out INTEGER DEFAULT 0,
                    messages INTEGER DEFAULT 0,
                    models_used_json TEXT DEFAULT '[]'
                );

                CREATE INDEX IF NOT EXISTS idx_conversations_session
                    ON conversations(session_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_start
                    ON sessions(start_time);
                CREATE INDEX IF NOT EXISTS idx_conversations_content
                    ON conversations(content);

                -- FTS virtual table for full-text search on conversations
                CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts USING fts5(
                    content,
                    session_id UNINDEXED,
                    role UNINDEXED,
                    content='conversations',
                    content_rowid='id'
                );

                -- Triggers to keep FTS in sync
                CREATE TRIGGER IF NOT EXISTS conversations_ai AFTER INSERT ON conversations BEGIN
                    INSERT INTO conversations_fts(rowid, content, session_id, role)
                    VALUES (new.id, new.content, new.session_id, new.role);
                END;

                CREATE TRIGGER IF NOT EXISTS conversations_ad AFTER DELETE ON conversations BEGIN
                    INSERT INTO conversations_fts(conversations_fts, rowid, content, \
session_id, role)
                    VALUES ('delete', old.id, old.content, old.session_id, old.role);
                END;

                CREATE TRIGGER IF NOT EXISTS conversations_au AFTER UPDATE ON conversations BEGIN
                    INSERT INTO conversations_fts(conversations_fts, rowid, content, \
session_id, role)
                    VALUES ('delete', old.id, old.content, old.session_id, old.role);
                    INSERT INTO conversations_fts(rowid, content, session_id, role)
                    VALUES (new.id, new.content, new.session_id, new.role);
                END;

                -- Schema version tracking
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                -- W5-1: checkpoints (P0-1). Purely additive tables, so they do
                -- NOT bump SCHEMA_VERSION: a version bump would make every
                -- older build refuse to open the database
                -- (_init_db raises on `found > SCHEMA_VERSION`), and locking a
                -- user out of their own history is too high a price for three
                -- CREATE TABLE IF NOT EXISTS statements that heal themselves on
                -- every open exactly the way _SCHEMA_COLUMNS does.
                --
                -- They live here, in executescript, and not in _migrate,
                -- because _migrate forbids executescript (implicit COMMIT) and
                -- because this keeps sessions.py the ONE ddl writer for this
                -- file. djcode/core/checkpoints.py owns only the logic.
                CREATE TABLE IF NOT EXISTS blobs (
                    sha256 TEXT PRIMARY KEY,
                    body BLOB NOT NULL,
                    size INTEGER NOT NULL DEFAULT 0
                );

                -- `seq` is the `#7` DESIGN-CLI's tool cards print and the
                -- argument `/undo 7` takes; a uuid primary key alone cannot be
                -- rendered or typed. `turn_id` groups the checkpoints of one
                -- turn so /undo reverts a five-file turn as one action instead
                -- of five. `entry_id` stays NULL: checkpoints are captured
                -- mid-round, and the conversations row for that round does not
                -- exist until on_checkpoint fires at the end of it.
                -- No FOREIGN KEY on session_id on purpose -- headless and
                -- one-shot paths dispatch tools with a session_id that has no
                -- sessions row, and an FK would turn those into hard failures.
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL DEFAULT '',
                    seq INTEGER NOT NULL DEFAULT 0,
                    entry_id INTEGER,
                    created_at TEXT NOT NULL,
                    tool_name TEXT,
                    label TEXT,
                    coverage TEXT DEFAULT ''
                );

                -- `kind` distinguishes a file from a directory the tool created
                -- (file_write does parent.mkdir(parents=True), and without this
                -- undo leaves empty directories behind). `mode` carries the
                -- POSIX permission bits so `chmod +x` survives a round trip.
                CREATE TABLE IF NOT EXISTS checkpoint_files (
                    checkpoint_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    pre_sha TEXT,
                    post_sha TEXT,
                    existed INTEGER NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'file',
                    mode INTEGER,
                    FOREIGN KEY (checkpoint_id) REFERENCES checkpoints(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_checkpoints_session
                    ON checkpoints(session_id, seq);
                CREATE INDEX IF NOT EXISTS idx_checkpoints_created
                    ON checkpoints(created_at);
                CREATE INDEX IF NOT EXISTS idx_checkpoint_files_cp
                    ON checkpoint_files(checkpoint_id);
                CREATE INDEX IF NOT EXISTS idx_checkpoint_files_pre
                    ON checkpoint_files(pre_sha);
            """

@dataclass
class Session:
    """A single DJcode session."""

    id: str
    model: str
    provider: str
    start: str  # ISO datetime
    end: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    messages_count: int = 0
    tools_used: int = 0
    cwd: str = ""
    summary: str = ""
    parent_session_id: str | None = None
    forked_at_entry_id: int | None = None
    last_interaction_at: str | None = None
    updated_at: str | None = None

    @property
    def duration_seconds(self) -> float:
        if not self.end:
            return 0.0
        try:
            s = datetime.fromisoformat(self.start)
            e = datetime.fromisoformat(self.end)
            return (e - s).total_seconds()
        except (ValueError, TypeError):
            return 0.0

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out


@dataclass
class SessionStats:
    """Aggregated statistics."""

    total_sessions: int = 0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_messages: int = 0
    total_tools: int = 0
    active_days: int = 0
    favorite_model: str = "unknown"
    longest_session_seconds: float = 0.0
    current_streak: int = 0
    longest_streak: int = 0
    most_active_day: str = ""


@dataclass
class ConversationEntry:
    """One row of the immutable session log."""

    id: int
    session_id: str
    role: str
    content: str
    timestamp: str
    entry_type: str = ENTRY_MESSAGE
    tool_calls: list[dict] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    images: list[str] = field(default_factory=list)
    parent_id: int | None = None
    first_kept_entry_id: int | None = None
    checkpoint_blob: str | None = None


class SessionDB:
    """SQLite-backed session persistence.

    Thread-safe via connection-per-call with WAL mode.
    All public methods handle their own connections and errors.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or DB_PATH
        # session_id -> ((row count, max id), fingerprints of persisted entries)
        self._append_cache: dict[str, tuple[tuple[int, int], Counter[str]]] = {}
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """Get a database connection with optimal settings."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create the base schema, then run the real migration gate.

        Unlike the pre-W4 version this **raises** on failure. A SessionDB whose
        schema never got created is indistinguishable from an empty history to
        every caller, so swallowing the error here is how a user silently loses
        a session.
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: CPython's sqlite3 never opens an implicit
        # transaction around DDL, so each ALTER would otherwise commit by itself
        # and a failure halfway through would leave an unrepairable database.
        # Explicit BEGIN IMMEDIATE in _migrate makes the whole thing atomic.
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")

            # Probe BEFORE creating anything: a future-version database must be
            # left byte-identical, and "file exists but holds no sessions table"
            # (the shape produced by tools/task_tracker.py, which shares this
            # file) has to be distinguishable from "v1".
            found = self._probe_version(conn)
            if found is not None and found > SCHEMA_VERSION:
                raise SessionSchemaError(
                    f"{self.db_path} was written by a newer DJcode (schema version "
                    f"{found}); this build supports {SCHEMA_VERSION}. Refusing to open it."
                )

            conn.executescript(_BASE_SCHEMA_SQL)
            self._migrate(conn, SCHEMA_VERSION if found is None else found)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except sqlite3.Error as e:
            raise SessionSchemaError(
                f"Failed to initialize sessions database {self.db_path}: {e}"
            ) from e
        finally:
            conn.close()

    @staticmethod
    def _probe_version(conn: sqlite3.Connection) -> int | None:
        """Return the on-disk schema version, or None when there is no schema yet.

        Tolerates every shape reachable today: a file holding only the
        task-tracker's tables, a v2 database whose meta row was deleted, and a
        meta value that is not an integer (``int()`` on garbage raises
        ValueError, which is not a sqlite3.Error and would escape the caller).
        """
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "sessions" not in names:
            return None

        raw: Any = None
        if "meta" in names:
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            raw = row[0] if row is not None else None
        if raw is not None:
            try:
                return int(str(raw).strip())
            except (TypeError, ValueError):
                logger.warning("Unparseable schema_version %r; inferring from table shape", raw)

        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if user_version:
            return int(user_version)

        columns = {r[1] for r in conn.execute("PRAGMA table_info(conversations)")}
        if "entry_type" in columns and "parent_id" in columns:
            return 3
        if "tool_call_id" in columns:
            return 2
        return 1

    def _migrate(self, conn: sqlite3.Connection, found: int) -> None:
        """Bring the database to SCHEMA_VERSION in a single transaction.

        Additive only, and every step is keyed on ``PRAGMA table_info`` rather
        than on the version number: ``schema_version = '2'`` on disk means
        either "with images_json" or "without it", because the release that
        added that column did not bump the version.

        No ``executescript`` may appear below — it issues an implicit COMMIT,
        which would silently end the transaction and make a partial migration
        durable.
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table, column, ddl in _SCHEMA_COLUMNS:
                columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

            if found < SCHEMA_VERSION:
                # Deterministic backfill from data already on disk. Lineage
                # columns stay NULL on purpose: every pre-v3 transcript was
                # DELETEd and re-INSERTed on each checkpoint, so historical row
                # ids carry no ancestry and any guess would be fabrication.
                conn.execute(
                    f"UPDATE conversations SET entry_type = '{ENTRY_MESSAGE}' "
                    "WHERE entry_type IS NULL OR entry_type = ''"
                )
                conn.execute(
                    "UPDATE sessions SET updated_at = COALESCE(end_time, start_time) "
                    "WHERE updated_at IS NULL"
                )
                conn.execute(
                    "UPDATE sessions SET last_interaction_at = COALESCE(end_time, start_time) "
                    "WHERE last_interaction_at IS NULL"
                )

            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - rollback of a dead handle
                pass
            raise

    # ── Session CRUD ──────────────────────────────────────────────────────

    def create_session(self, model: str, provider: str, cwd: str = "") -> str:
        """Create a new session. Returns session_id."""
        import os
        import uuid

        session_id = f"s_{uuid.uuid4().hex}"
        now = datetime.now().isoformat()
        cwd = cwd or os.getcwd()

        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO sessions (id, model, provider, start_time, cwd,
                                         updated_at, last_interaction_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (session_id, model, provider, now, cwd, now, now),
            )
            # Update daily stats
            today = datetime.now().strftime("%Y-%m-%d")
            conn.execute(
                """INSERT INTO stats_daily (date, sessions, models_used_json)
                   VALUES (?, 1, ?)
                   ON CONFLICT(date) DO UPDATE SET
                       sessions = sessions + 1,
                       models_used_json = (
                           SELECT json_group_array(DISTINCT value)
                           FROM (
                               SELECT value FROM json_each(stats_daily.models_used_json)
                               UNION SELECT ?
                           )
                       )""",
                (today, json.dumps([model]), model),
            )
            conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to create session: %s", e)
        finally:
            conn.close()

        return session_id

    def update_session(
        self,
        session_id: str,
        *,
        tokens_in: int = 0,
        tokens_out: int = 0,
        messages: int = 0,
        tools_used: int = 0,
    ) -> None:
        """Update a running session's counters (additive)."""
        conn = self._connect()
        try:
            conn.execute(
                """UPDATE sessions SET
                       tokens_in = tokens_in + ?,
                       tokens_out = tokens_out + ?,
                       messages_count = messages_count + ?,
                       tools_used = tools_used + ?,
                       updated_at = ?
                   WHERE id = ?""",
                (
                    tokens_in,
                    tokens_out,
                    messages,
                    tools_used,
                    datetime.now().isoformat(),
                    session_id,
                ),
            )

            # Update daily stats
            today = datetime.now().strftime("%Y-%m-%d")
            conn.execute(
                """INSERT INTO stats_daily (date, tokens_in, tokens_out, messages)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(date) DO UPDATE SET
                       tokens_in = tokens_in + ?,
                       tokens_out = tokens_out + ?,
                       messages = messages + ?""",
                (today, tokens_in, tokens_out, messages, tokens_in, tokens_out, messages),
            )
            conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to update session: %s", e)
        finally:
            conn.close()

    def end_session(self, session_id: str, summary: str = "") -> None:
        """Mark a session as ended."""
        conn = self._connect()
        try:
            now = datetime.now().isoformat()
            conn.execute(
                "UPDATE sessions SET end_time = ?, summary = ?, updated_at = ? WHERE id = ?",
                (now, summary, now, session_id),
            )
            conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to end session: %s", e)
        finally:
            conn.close()

    def get_session(self, session_id: str) -> Session | None:
        """Get a single session by ID."""
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row:
                return self._row_to_session(row)
            return None
        except sqlite3.Error as e:
            logger.error("Failed to get session: %s", e)
            return None
        finally:
            conn.close()

    def list_sessions(self, limit: int = 50, offset: int = 0) -> list[Session]:
        """List recent sessions, most recent first."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY start_time DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [self._row_to_session(r) for r in rows]
        except sqlite3.Error as e:
            logger.error("Failed to list sessions: %s", e)
            return []
        finally:
            conn.close()

    def search_sessions(self, query: str, limit: int = 20) -> list[Session]:
        """Full-text search across conversation content. Returns matching sessions."""
        conn = self._connect()
        try:
            # Search FTS table, get distinct session IDs
            rows = conn.execute(
                """SELECT DISTINCT c.session_id
                   FROM conversations_fts fts
                   JOIN conversations c ON c.id = fts.rowid
                   WHERE conversations_fts MATCH ?
                     AND c.entry_type != 'checkpoint'
                   ORDER BY fts.rank
                   LIMIT ?""",
                (query, limit),
            ).fetchall()

            session_ids = [r["session_id"] for r in rows]
            if not session_ids:
                return []

            placeholders = ",".join("?" for _ in session_ids)
            session_rows = conn.execute(
                f"SELECT * FROM sessions WHERE id IN ({placeholders}) ORDER BY start_time DESC",
                session_ids,
            ).fetchall()

            return [self._row_to_session(r) for r in session_rows]
        except sqlite3.Error as e:
            # FTS might not be available on all SQLite builds
            logger.debug("FTS search failed, falling back to LIKE: %s", e)
            try:
                rows = conn.execute(
                    """SELECT DISTINCT s.*
                       FROM sessions s
                       JOIN conversations c ON c.session_id = s.id
                       WHERE c.content LIKE ?
                         AND c.entry_type != 'checkpoint'
                       ORDER BY s.start_time DESC
                       LIMIT ?""",
                    (f"%{query}%", limit),
                ).fetchall()
                return [self._row_to_session(r) for r in rows]
            except sqlite3.Error:
                return []
        finally:
            conn.close()

    # ── Conversation Persistence ──────────────────────────────────────────

    def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: list[dict] | None = None,
        tool_call_id: str | None = None,
        name: str | None = None,
        images: list[str] | None = None,
        *,
        entry_type: str = ENTRY_MESSAGE,
        first_kept_entry_id: int | None = None,
        checkpoint_blob: str | None = None,
        parent_id: int | None = None,
    ) -> int | None:
        """Append a single log entry. Returns its entry id, or None on failure.

        Nothing in this module ever deletes or rewrites an entry; the log is
        append-only. ``images`` is written here too -- the pre-W4 version of
        this method silently dropped it, which is why it could not be the sole
        writer.
        """
        if entry_type not in ENTRY_TYPES:
            raise ValueError(
                f"unknown entry_type {entry_type!r}; expected one of {sorted(ENTRY_TYPES)}"
            )
        conn = self._connect()
        try:
            now = datetime.now().isoformat()
            tc_json = json.dumps(tool_calls) if tool_calls else ""
            cur = conn.execute(
                _INSERT_ENTRY_SQL,
                (
                    session_id,
                    role,
                    content,
                    now,
                    tc_json,
                    tool_call_id,
                    name,
                    json.dumps(list(images or [])),
                    entry_type,
                    first_kept_entry_id,
                    checkpoint_blob,
                    parent_id,
                ),
            )
            entry_id = int(cur.lastrowid or 0)
            self._touch(conn, session_id, now)
            conn.commit()
            self._append_cache.pop(session_id, None)
            return entry_id or None
        except sqlite3.Error as e:
            logger.error("Failed to save message: %s", e)
            return None
        finally:
            conn.close()

    def append_messages(
        self,
        session_id: str,
        messages: list[Any],
        from_index: int | None = None,
    ) -> list[int]:
        """Append whatever in ``messages`` is not on disk yet. Returns new entry ids.

        This is the single migration target for every former
        ``save_conversation`` call site, including the ``on_checkpoint`` hot
        path that fires once per tool round with the whole (growing, and after a
        compaction *shrinking*) message list.

        ``from_index`` is the fast path for a caller that genuinely knows how
        many of its messages are already persisted. When it is None -- which is
        every real call site, because nothing tracks that index today -- the
        already-persisted entries are matched by content fingerprint and
        skipped. An integer cursor cannot be used on its own: the operator's
        message list is *reassigned* wholesale by compaction, which invalidates
        any index into it.
        """
        parsed = [p for p in (_parse_message(m) for m in messages) if p is not None]
        if not session_id or not parsed:
            return []

        conn = self._connect()
        try:
            counter: Counter[str] | None = None
            if from_index is not None:
                pending = parsed[max(0, from_index) :]
            else:
                # The cached counter is the authority for what is on disk, so
                # matching runs against a throwaway copy; only a real INSERT is
                # allowed to change it.
                counter = self._fingerprint_counter(conn, session_id)
                remaining = counter.copy()
                pending = []
                for entry in parsed:
                    if remaining[entry["fp"]] > 0:
                        remaining[entry["fp"]] -= 1
                    else:
                        pending.append(entry)

            if not pending:
                return []

            now = datetime.now().isoformat()
            ids: list[int] = []
            for entry in pending:
                cur = conn.execute(
                    _INSERT_ENTRY_SQL,
                    (
                        session_id,
                        entry["role"],
                        entry["content"],
                        now,
                        entry["tool_calls_json"],
                        entry["tool_call_id"],
                        entry["name"],
                        entry["images_json"],
                        ENTRY_MESSAGE,
                        None,
                        None,
                        None,
                    ),
                )
                ids.append(int(cur.lastrowid or 0))
                if counter is not None:
                    counter[entry["fp"]] += 1
            self._touch(conn, session_id, now)
            conn.commit()

            if counter is not None:
                self._remember_fingerprints(conn, session_id, counter)
            else:
                self._append_cache.pop(session_id, None)
            return ids
        except sqlite3.Error as e:
            logger.error("Failed to append messages: %s", e)
            self._append_cache.pop(session_id, None)
            return []
        finally:
            conn.close()

    def record_compaction(
        self,
        session_id: str,
        *,
        summary: str = "",
        kept_messages: list[Any] | None = None,
        strategy: str = "",
        messages_removed: int = 0,
    ) -> int | None:
        """Record a compaction as an entry. Nothing is deleted.

        ``first_kept_entry_id`` is resolved by walking the persisted transcript
        and the surviving in-memory tail backwards together, so it is exact even
        though entry ids have gaps. It is then widened backwards to the nearest
        tool-group boundary: replaying from the middle of an assistant's
        tool_calls and its tool replies produces a protocol-invalid transcript
        that every provider rejects.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT id, role, content, tool_calls_json, tool_call_id, name, images_json
                   FROM conversations
                   WHERE session_id = ? AND entry_type = ?
                   ORDER BY id ASC""",
                (session_id, ENTRY_MESSAGE),
            ).fetchall()

            kept = [p for p in (_parse_message(m) for m in (kept_messages or [])) if p is not None]
            first_kept: int | None = None
            i, k = len(rows) - 1, len(kept) - 1
            while i >= 0 and k >= 0 and _row_fingerprint(rows[i]) == kept[k]["fp"]:
                first_kept = int(rows[i]["id"])
                i -= 1
                k -= 1

            if first_kept is not None:
                idx = next(j for j, r in enumerate(rows) if int(r["id"]) == first_kept)
                while idx > 0 and rows[idx]["role"] == "tool":
                    idx -= 1
                first_kept = int(rows[idx]["id"])

            body = summary.strip()
            if body:
                content = body if body.startswith(SUMMARY_PREFIX) else SUMMARY_PREFIX + "\n" + body
            else:
                # TRIM and SELECTIVE produce no summary text at all, and
                # SELECTIVE is the first strategy picked for every session, so
                # an empty compaction row is the common case, not the edge one.
                content = (
                    SUMMARY_PREFIX
                    + f"\n<compaction: {strategy or 'trim'}, {messages_removed} earlier "
                    "entries elided from the model's view; the full transcript is preserved>"
                )

            now = datetime.now().isoformat()
            cur = conn.execute(
                _INSERT_ENTRY_SQL,
                (
                    session_id,
                    "system",
                    content,
                    now,
                    "",
                    None,
                    None,
                    "[]",
                    ENTRY_COMPACTION,
                    first_kept,
                    None,
                    None,
                ),
            )
            self._touch(conn, session_id, now)
            conn.commit()
            self._append_cache.pop(session_id, None)
            return int(cur.lastrowid or 0) or None
        except sqlite3.Error as e:
            logger.error("Failed to record compaction: %s", e)
            self._append_cache.pop(session_id, None)
            return None
        finally:
            conn.close()

    def fork_session(self, session_id: str, before_entry_id: int | None = None) -> str | None:
        """Branch a session. The parent stays open and keeps every entry.

        Columns are enumerated explicitly on both inserts: a ``SELECT *`` would
        carry the parent's primary keys into the child and collide with the
        AUTOINCREMENT sequence. Counters are zeroed rather than copied, or the
        parent's tokens would be double-counted by /stats, and the session row
        is written before its conversation rows so no orphan is ever visible to
        ``PRAGMA foreign_key_check``.
        """
        import uuid

        parent = self.get_session(session_id)
        if parent is None:
            return None

        fork_id = f"s_{uuid.uuid4().hex}"
        now = datetime.now().isoformat()
        conn = self._connect()
        try:
            cut = before_entry_id
            if cut is None:
                row = conn.execute(
                    "SELECT COALESCE(MAX(id), 0) + 1 FROM conversations WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                cut = int(row[0])

            conn.execute(
                """INSERT INTO sessions (id, model, provider, start_time, end_time,
                                         tokens_in, tokens_out, messages_count, tools_used,
                                         cwd, summary, parent_session_id, forked_at_entry_id,
                                         last_interaction_at, updated_at)
                   SELECT ?, model, provider, ?, NULL, 0, 0, 0, 0, cwd, '', id, ?, ?, ?
                   FROM sessions WHERE id = ?""",
                (fork_id, now, cut, now, now, session_id),
            )
            conn.execute(
                """INSERT INTO conversations (session_id, role, content, timestamp,
                                              tool_calls_json, tool_call_id, name, images_json,
                                              entry_type, first_kept_entry_id, checkpoint_blob,
                                              parent_id)
                   SELECT ?, role, content, timestamp, tool_calls_json, tool_call_id, name,
                          images_json, entry_type, first_kept_entry_id, checkpoint_blob, id
                   FROM conversations
                   WHERE session_id = ? AND id < ?
                   ORDER BY id ASC""",
                (fork_id, session_id, cut),
            )
            today = datetime.now().strftime("%Y-%m-%d")
            conn.execute(
                """INSERT INTO stats_daily (date, sessions, models_used_json)
                   VALUES (?, 1, ?)
                   ON CONFLICT(date) DO UPDATE SET sessions = sessions + 1""",
                (today, json.dumps([parent.model])),
            )
            conn.commit()
            return fork_id
        except sqlite3.Error as e:
            logger.error("Failed to fork session: %s", e)
            return None
        finally:
            conn.close()

    def list_entries(self, session_id: str) -> list[ConversationEntry]:
        """The raw, immutable log for a session -- every entry type, in id order."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
            return [
                ConversationEntry(
                    id=int(r["id"]),
                    session_id=r["session_id"],
                    role=r["role"],
                    content=r["content"],
                    timestamp=r["timestamp"],
                    entry_type=r["entry_type"] or ENTRY_MESSAGE,
                    tool_calls=_loads_list(r["tool_calls_json"]),
                    tool_call_id=r["tool_call_id"],
                    name=r["name"],
                    images=_loads_list(r["images_json"]),
                    parent_id=r["parent_id"],
                    first_kept_entry_id=r["first_kept_entry_id"],
                    checkpoint_blob=r["checkpoint_blob"],
                )
                for r in rows
            ]
        except sqlite3.Error as e:
            logger.error("Failed to list entries: %s", e)
            return []
        finally:
            conn.close()

    def load_conversation(self, session_id: str, *, view: str = "model") -> list[dict[str, Any]]:
        """Load a conversation for session resume.

        ``view="model"`` replays what the model should see: the most recent
        compaction summary followed by every message from its
        ``first_kept_entry_id`` onward. ``view="full"`` returns the immutable
        transcript, compaction markers excluded. Checkpoint entries (W5) never
        appear in either view.

        The model sees less; the user loses nothing.
        """
        if view not in {"model", "full"}:
            raise ValueError(f"unknown view {view!r}; expected 'model' or 'full'")
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT id, role, content, tool_calls_json, timestamp, tool_call_id, name,
                          images_json, entry_type, first_kept_entry_id
                   FROM conversations
                   WHERE session_id = ? AND entry_type IN (?, ?)
                   ORDER BY id ASC""",
                (session_id, ENTRY_MESSAGE, ENTRY_COMPACTION),
            ).fetchall()

            messages_only = [r for r in rows if r["entry_type"] == ENTRY_MESSAGE]
            if view == "full":
                return [_row_to_message(r) for r in messages_only]

            marker = None
            for r in rows:
                if r["entry_type"] == ENTRY_COMPACTION:
                    marker = r
            if marker is None:
                return [_row_to_message(r) for r in messages_only]

            floor = marker["first_kept_entry_id"]
            floor = int(marker["id"]) if floor is None else int(floor)
            replay = [_row_to_message(marker)]
            replay.extend(_row_to_message(r) for r in messages_only if int(r["id"]) >= floor)
            return replay
        except sqlite3.Error as e:
            logger.error("Failed to load conversation: %s", e)
            return []
        finally:
            conn.close()

    # ── Append bookkeeping ───────────────────────

    @staticmethod
    def _touch(conn: sqlite3.Connection, session_id: str, now: str) -> None:
        """W4-5: every append moves last_interaction_at and updated_at."""
        conn.execute(
            "UPDATE sessions SET last_interaction_at = ?, updated_at = ? WHERE id = ?",
            (now, now, session_id),
        )

    @staticmethod
    def _session_shape(conn: sqlite3.Connection, session_id: str) -> tuple[int, int]:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM conversations WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row[0]), int(row[1])

    def _fingerprint_counter(self, conn: sqlite3.Connection, session_id: str) -> Counter[str]:
        """Fingerprints of everything already persisted for this session.

        Cached against (row count, max id) so the once-per-tool-round hot path
        does not re-read a whole transcript every time, while a write from any
        other process still invalidates it.
        """
        shape = self._session_shape(conn, session_id)
        cached = self._append_cache.get(session_id)
        if cached is not None and cached[0] == shape:
            return cached[1]

        counter: Counter[str] = Counter()
        for r in conn.execute(
            """SELECT role, content, tool_calls_json, tool_call_id, name, images_json, entry_type
               FROM conversations WHERE session_id = ?""",
            (session_id,),
        ):
            if r["entry_type"] == ENTRY_CHECKPOINT:
                continue
            counter[_row_fingerprint(r)] += 1
        self._append_cache[session_id] = (shape, counter)
        return counter

    def _remember_fingerprints(
        self, conn: sqlite3.Connection, session_id: str, counter: Counter[str]
    ) -> None:
        self._append_cache[session_id] = (self._session_shape(conn, session_id), counter)

    # ── Statistics ────────────────────────────────────────────────────────

    def get_stats(self, period: str = "all") -> SessionStats:
        """Compute aggregated statistics for /stats command."""
        conn = self._connect()
        try:
            # Period filter
            if period == "7d":
                cutoff = (datetime.now() - timedelta(days=7)).isoformat()
                where = f"WHERE start_time >= '{cutoff}'"
            elif period == "30d":
                cutoff = (datetime.now() - timedelta(days=30)).isoformat()
                where = f"WHERE start_time >= '{cutoff}'"
            else:
                where = ""

            # Aggregate query
            row = conn.execute(f"""
                SELECT
                    COUNT(*) as total_sessions,
                    COALESCE(SUM(tokens_in), 0) as total_tokens_in,
                    COALESCE(SUM(tokens_out), 0) as total_tokens_out,
                    COALESCE(SUM(messages_count), 0) as total_messages,
                    COALESCE(SUM(tools_used), 0) as total_tools
                FROM sessions {where}
            """).fetchone()

            stats = SessionStats(
                total_sessions=row["total_sessions"],
                total_tokens_in=row["total_tokens_in"],
                total_tokens_out=row["total_tokens_out"],
                total_messages=row["total_messages"],
                total_tools=row["total_tools"],
            )

            # Favorite model
            model_row = conn.execute(f"""
                SELECT model, SUM(tokens_in + tokens_out) as total
                FROM sessions {where}
                GROUP BY model
                ORDER BY total DESC
                LIMIT 1
            """).fetchone()
            if model_row:
                stats.favorite_model = model_row["model"]

            # Active days
            days_row = conn.execute(f"""
                SELECT COUNT(DISTINCT DATE(start_time)) as active_days
                FROM sessions {where}
            """).fetchone()
            stats.active_days = days_row["active_days"] if days_row else 0

            # Longest session
            dur_row = conn.execute(f"""
                SELECT MAX(
                    CAST((julianday(end_time) - julianday(start_time)) * 86400 AS INTEGER)
                ) as max_dur
                FROM sessions
                {where + " AND" if where else "WHERE"} end_time IS NOT NULL
            """).fetchone()
            if dur_row and dur_row["max_dur"]:
                stats.longest_session_seconds = float(dur_row["max_dur"])

            # Most active day
            active_row = conn.execute(f"""
                SELECT DATE(start_time) as day, SUM(tokens_in + tokens_out) as total
                FROM sessions {where}
                GROUP BY day
                ORDER BY total DESC
                LIMIT 1
            """).fetchone()
            if active_row and active_row["day"]:
                stats.most_active_day = active_row["day"]

            # Streaks
            stats.longest_streak, stats.current_streak = self._compute_streaks(conn, where)

            return stats

        except sqlite3.Error as e:
            logger.error("Failed to compute stats: %s", e)
            return SessionStats()
        finally:
            conn.close()

    def _compute_streaks(self, conn: sqlite3.Connection, where: str = "") -> tuple[int, int]:
        """Compute longest and current streaks from active days."""
        try:
            rows = conn.execute("""
                SELECT DISTINCT DATE(start_time) as day
                FROM sessions
                ORDER BY day ASC
            """).fetchall()

            if not rows:
                return 0, 0

            dates = [datetime.strptime(r["day"], "%Y-%m-%d").date() for r in rows if r["day"]]
            if not dates:
                return 0, 0

            # Longest streak
            longest = 1
            current = 1
            for i in range(1, len(dates)):
                if (dates[i] - dates[i - 1]).days == 1:
                    current += 1
                    longest = max(longest, current)
                elif (dates[i] - dates[i - 1]).days > 1:
                    current = 1

            # Current streak
            today = datetime.now().date()
            active_set = set(d.isoformat() for d in dates)
            current_streak = 0
            check = today
            while check.isoformat() in active_set:
                current_streak += 1
                check -= timedelta(days=1)

            return longest, current_streak

        except (sqlite3.Error, ValueError):
            return 0, 0

    def get_daily_tokens(self, days: int = 365) -> dict[str, int]:
        """Get token counts per day for heatmap rendering."""
        conn = self._connect()
        try:
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            rows = conn.execute(
                """
                SELECT DATE(start_time) as day, SUM(tokens_in + tokens_out) as total
                FROM sessions
                WHERE start_time >= ?
                GROUP BY day
                ORDER BY day ASC
            """,
                (cutoff,),
            ).fetchall()

            return {r["day"]: r["total"] for r in rows if r["day"]}
        except sqlite3.Error:
            return {}
        finally:
            conn.close()

    # ── Migration from JSON stats ─────────────────────────────────────────

    def migrate_from_json(self, json_path: Path | None = None) -> int:
        """Import sessions from the old stats.json format.

        Returns number of sessions imported.
        """
        json_path = json_path or (CONFIG_DIR / "stats.json")
        if not json_path.exists():
            return 0

        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return 0

        sessions = data.get("sessions", [])
        if not sessions:
            return 0

        conn = self._connect()
        count = 0
        try:
            for s in sessions:
                sid = s.get("id", f"s_migrated_{count}")
                # Skip if already exists
                existing = conn.execute("SELECT id FROM sessions WHERE id = ?", (sid,)).fetchone()
                if existing:
                    continue

                conn.execute(
                    """INSERT INTO sessions (id, model, provider, start_time, end_time,
                           tokens_out, messages_count, tools_used)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        sid,
                        s.get("model", ""),
                        s.get("provider", ""),
                        s.get("start", datetime.now().isoformat()),
                        s.get("end"),
                        s.get("tokens", 0),
                        s.get("messages", 0),
                        s.get("tools_used", 0),
                    ),
                )
                count += 1

            conn.commit()
            logger.info("Migrated %d sessions from stats.json", count)
        except sqlite3.Error as e:
            logger.error("Migration failed: %s", e)
        finally:
            conn.close()

        return count

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> Session:
        keys = {k: row[k] for k in row.keys()}
        return Session(
            id=row["id"],
            model=row["model"],
            provider=row["provider"],
            start=row["start_time"],
            end=row["end_time"],
            tokens_in=row["tokens_in"],
            tokens_out=row["tokens_out"],
            messages_count=row["messages_count"],
            tools_used=row["tools_used"],
            cwd=row["cwd"],
            summary=row["summary"] or "",
            parent_session_id=keys.get("parent_session_id"),
            forked_at_entry_id=keys.get("forked_at_entry_id"),
            last_interaction_at=keys.get("last_interaction_at"),
            updated_at=keys.get("updated_at"),
        )

    def vacuum(self) -> None:
        """Reclaim disk space."""
        conn = self._connect()
        try:
            conn.execute("VACUUM")
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and its conversation."""
        conn = self._connect()
        try:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
            return conn.total_changes > 0
        except sqlite3.Error:
            return False
        finally:
            conn.close()




