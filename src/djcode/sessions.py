"""SQLite Session Persistence for DJcode.

Replaces the JSON-based stats system with a proper SQLite database.
Enables: session resume, conversation search, faster aggregation.

Zero new dependencies — stdlib sqlite3 only.
Database: ~/.djcode/sessions.db
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from djcode.config import CONFIG_DIR

logger = logging.getLogger(__name__)

DB_PATH = CONFIG_DIR / "sessions.db"

GOLD = "#FFD700"

# Schema version — bump when adding migrations
SCHEMA_VERSION = 1


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


class ConversationMessage:
    """A single message in a conversation."""

    __slots__ = ("role", "content", "timestamp", "tool_calls_json")

    def __init__(
        self,
        role: str,
        content: str,
        timestamp: str = "",
        tool_calls_json: str = "",
    ) -> None:
        self.role = role
        self.content = content
        self.timestamp = timestamp or datetime.now().isoformat()
        self.tool_calls_json = tool_calls_json


class SessionDB:
    """SQLite-backed session persistence.

    Thread-safe via connection-per-call with WAL mode.
    All public methods handle their own connections and errors.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or DB_PATH
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
        """Create tables if they don't exist."""
        conn = self._connect()
        try:
            conn.executescript("""
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
                    INSERT INTO conversations_fts(conversations_fts, rowid, content, session_id, role)
                    VALUES ('delete', old.id, old.content, old.session_id, old.role);
                END;

                CREATE TRIGGER IF NOT EXISTS conversations_au AFTER UPDATE ON conversations BEGIN
                    INSERT INTO conversations_fts(conversations_fts, rowid, content, session_id, role)
                    VALUES ('delete', old.id, old.content, old.session_id, old.role);
                    INSERT INTO conversations_fts(rowid, content, session_id, role)
                    VALUES (new.id, new.content, new.session_id, new.role);
                END;

                -- Schema version tracking
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
            """)

            # Set schema version
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to initialize sessions database: %s", e)
        finally:
            conn.close()

    # ── Session CRUD ──────────────────────────────────────────────────────

    def create_session(self, model: str, provider: str, cwd: str = "") -> str:
        """Create a new session. Returns session_id."""
        import os

        session_id = f"s_{int(time.time())}_{id(self) % 10000}"
        now = datetime.now().isoformat()
        cwd = cwd or os.getcwd()

        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO sessions (id, model, provider, start_time, cwd)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, model, provider, now, cwd),
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
                       tools_used = tools_used + ?
                   WHERE id = ?""",
                (tokens_in, tokens_out, messages, tools_used, session_id),
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
                "UPDATE sessions SET end_time = ?, summary = ? WHERE id = ?",
                (now, summary, session_id),
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
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
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
    ) -> None:
        """Save a single conversation message."""
        conn = self._connect()
        try:
            now = datetime.now().isoformat()
            tc_json = json.dumps(tool_calls) if tool_calls else ""
            conn.execute(
                """INSERT INTO conversations (session_id, role, content, timestamp, tool_calls_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, role, content, now, tc_json),
            )
            conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to save message: %s", e)
        finally:
            conn.close()

    def save_conversation(self, session_id: str, messages: list[Any]) -> None:
        """Bulk save a conversation (list of Message objects or dicts).

        Replaces any existing conversation for this session.
        """
        conn = self._connect()
        try:
            # Clear existing
            conn.execute("DELETE FROM conversations WHERE session_id = ?", (session_id,))

            now = datetime.now().isoformat()
            for msg in messages:
                if hasattr(msg, "role"):
                    role = msg.role
                    content = msg.content or ""
                    tc = getattr(msg, "tool_calls", None)
                elif isinstance(msg, dict):
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    tc = msg.get("tool_calls")
                else:
                    continue

                tc_json = json.dumps(tc) if tc else ""
                conn.execute(
                    """INSERT INTO conversations (session_id, role, content, timestamp, tool_calls_json)
                       VALUES (?, ?, ?, ?, ?)""",
                    (session_id, role, content, now, tc_json),
                )

            conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to save conversation: %s", e)
        finally:
            conn.close()

    def load_conversation(self, session_id: str) -> list[dict[str, Any]]:
        """Load a conversation for session resume.

        Returns list of dicts with role, content, tool_calls keys.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT role, content, tool_calls_json, timestamp
                   FROM conversations
                   WHERE session_id = ?
                   ORDER BY id ASC""",
                (session_id,),
            ).fetchall()

            messages = []
            for r in rows:
                msg: dict[str, Any] = {
                    "role": r["role"],
                    "content": r["content"],
                    "timestamp": r["timestamp"],
                }
                if r["tool_calls_json"]:
                    try:
                        msg["tool_calls"] = json.loads(r["tool_calls_json"])
                    except json.JSONDecodeError:
                        pass
                messages.append(msg)

            return messages
        except sqlite3.Error as e:
            logger.error("Failed to load conversation: %s", e)
            return []
        finally:
            conn.close()

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
                {where + ' AND' if where else 'WHERE'} end_time IS NOT NULL
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

    def _compute_streaks(
        self, conn: sqlite3.Connection, where: str = ""
    ) -> tuple[int, int]:
        """Compute longest and current streaks from active days."""
        try:
            rows = conn.execute(f"""
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
            rows = conn.execute("""
                SELECT DATE(start_time) as day, SUM(tokens_in + tokens_out) as total
                FROM sessions
                WHERE start_time >= ?
                GROUP BY day
                ORDER BY day ASC
            """, (cutoff,)).fetchall()

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
        from djcode.config import CONFIG_DIR as _cd

        json_path = json_path or (_cd / "stats.json")
        if not json_path.exists():
            return 0

        try:
            data = json.loads(json_path.read_text())
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
                existing = conn.execute(
                    "SELECT id FROM sessions WHERE id = ?", (sid,)
                ).fetchone()
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


# ── Rendering helpers ─────────────────────────────────────────────────────

def render_session_list(console: Any, sessions: list[Session]) -> None:
    """Render a formatted list of sessions for /history."""
    from rich.table import Table

    if not sessions:
        console.print(f"[{GOLD}]No sessions found.[/]")
        return

    table = Table(
        show_header=True,
        header_style=f"bold {GOLD}",
        border_style="dim",
        title=f"[bold {GOLD}]Session History[/]",
        title_justify="left",
    )
    table.add_column("ID", style="dim", max_width=20)
    table.add_column("Date", style="white")
    table.add_column("Model", style=f"bold {GOLD}")
    table.add_column("Messages", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Duration", justify="right", style="dim")

    for s in sessions:
        try:
            dt = datetime.fromisoformat(s.start)
            date_str = dt.strftime("%b %d %H:%M")
        except (ValueError, TypeError):
            date_str = "?"

        dur = s.duration_seconds
        if dur > 0:
            if dur < 60:
                dur_str = f"{int(dur)}s"
            elif dur < 3600:
                dur_str = f"{int(dur // 60)}m"
            else:
                dur_str = f"{int(dur // 3600)}h {int((dur % 3600) // 60)}m"
        else:
            dur_str = "active" if not s.end else "?"

        tokens = s.total_tokens
        tok_str = f"{tokens // 1000}k" if tokens >= 1000 else str(tokens)

        table.add_row(
            s.id,
            date_str,
            s.model,
            str(s.messages_count),
            tok_str,
            dur_str,
        )

    console.print()
    console.print(table)
    console.print()
    console.print("[dim]/resume <id> to resume a session  |  /history search <query>[/]")
    console.print()
