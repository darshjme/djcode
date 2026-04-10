"""Task tracker tool — in-session task management with SQLite persistence.

Provides create/update/list operations for tracking work items within
and across DJcode sessions. Stored in the same SQLite database as sessions.

No new dependencies — uses stdlib sqlite3 only.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from djcode.config import CONFIG_DIR

logger = logging.getLogger(__name__)

DB_PATH = CONFIG_DIR / "sessions.db"

# Valid status transitions
VALID_STATUSES = {"pending", "in_progress", "completed", "blocked", "cancelled"}
VALID_PRIORITIES = {"low", "medium", "high", "critical"}


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


def _connect() -> sqlite3.Connection:
    """Get a database connection with optimal settings."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_table() -> None:
    """Create the tasks table if it doesn't exist."""
    conn = _connect()
    try:
        conn.executescript("""
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

            CREATE INDEX IF NOT EXISTS idx_tasks_session
                ON tasks(session_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_status
                ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_created
                ON tasks(created_at);
        """)
        conn.commit()
    except sqlite3.Error as e:
        logger.error("Failed to create tasks table: %s", e)
    finally:
        conn.close()


# Initialize on import
_ensure_table()


def _generate_id() -> str:
    """Generate a unique task ID."""
    return f"task_{int(time.time() * 1000) % 1_000_000_000}_{id(object()) % 10000}"


async def execute_task_create(
    subject: str,
    description: str = "",
    priority: str = "medium",
    depends_on: str = "",
    tags: str = "",
    session_id: str = "",
) -> str:
    """Create a new task.

    Args:
        subject: Short title for the task (required).
        description: Detailed description of what needs to be done.
        priority: One of: low, medium, high, critical (default: medium).
        depends_on: Comma-separated list of task IDs this depends on.
        tags: Comma-separated tags for categorization.
        session_id: Associate with a specific DJcode session.

    Returns:
        Confirmation message with the created task details.
    """
    if not subject or not subject.strip():
        return "Error: Task subject is required"

    priority = priority.lower().strip()
    if priority not in VALID_PRIORITIES:
        return f"Error: Invalid priority '{priority}'. Must be one of: {', '.join(sorted(VALID_PRIORITIES))}"

    # Validate dependencies exist if specified
    if depends_on:
        dep_ids = [d.strip() for d in depends_on.split(",") if d.strip()]
        missing = _check_deps_exist(dep_ids)
        if missing:
            return f"Error: Dependency task(s) not found: {', '.join(missing)}"

    task_id = _generate_id()
    now = datetime.now().isoformat()

    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO tasks (id, session_id, subject, description, status,
                   priority, depends_on, tags, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)""",
            (
                task_id,
                session_id,
                subject.strip(),
                description.strip(),
                priority,
                depends_on.strip(),
                tags.strip(),
                now,
                now,
            ),
        )
        conn.commit()
    except sqlite3.Error as e:
        return f"Error creating task: {e}"
    finally:
        conn.close()

    # Format output
    lines = [
        f"Task created: {task_id}",
        f"  Subject:  {subject.strip()}",
        f"  Status:   pending",
        f"  Priority: {priority}",
    ]
    if description:
        lines.append(f"  Description: {description.strip()[:100]}")
    if depends_on:
        lines.append(f"  Depends on: {depends_on.strip()}")
    if tags:
        lines.append(f"  Tags: {tags.strip()}")

    return "\n".join(lines)


async def execute_task_update(
    task_id: str,
    status: str | None = None,
    subject: str | None = None,
    description: str | None = None,
    priority: str | None = None,
    depends_on: str | None = None,
    tags: str | None = None,
) -> str:
    """Update an existing task.

    Args:
        task_id: The ID of the task to update (required).
        status: New status: pending, in_progress, completed, blocked, cancelled.
        subject: New subject/title.
        description: New description.
        priority: New priority: low, medium, high, critical.
        depends_on: New dependency list (comma-separated task IDs).
        tags: New tags (comma-separated).

    Returns:
        Confirmation message with updated task details.
    """
    if not task_id or not task_id.strip():
        return "Error: task_id is required"

    task_id = task_id.strip()

    # Verify task exists
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return f"Error: Task '{task_id}' not found"

        # Build update fields
        updates: list[str] = []
        params: list[Any] = []

        if status is not None:
            status = status.lower().strip()
            if status not in VALID_STATUSES:
                return f"Error: Invalid status '{status}'. Must be one of: {', '.join(sorted(VALID_STATUSES))}"

            # Check if dependencies are met before allowing in_progress/completed
            if status in ("in_progress", "completed"):
                blocked_by = _get_blocking_deps(task_id, conn)
                if blocked_by:
                    return (
                        f"Error: Cannot set '{status}' — blocked by incomplete dependencies: "
                        f"{', '.join(blocked_by)}"
                    )

            updates.append("status = ?")
            params.append(status)

            if status == "completed":
                updates.append("completed_at = ?")
                params.append(datetime.now().isoformat())
            elif row["completed_at"] and status != "completed":
                # Reopening a completed task
                updates.append("completed_at = NULL")

        if subject is not None:
            if not subject.strip():
                return "Error: Subject cannot be empty"
            updates.append("subject = ?")
            params.append(subject.strip())

        if description is not None:
            updates.append("description = ?")
            params.append(description.strip())

        if priority is not None:
            priority = priority.lower().strip()
            if priority not in VALID_PRIORITIES:
                return f"Error: Invalid priority '{priority}'. Must be one of: {', '.join(sorted(VALID_PRIORITIES))}"
            updates.append("priority = ?")
            params.append(priority)

        if depends_on is not None:
            if depends_on.strip():
                dep_ids = [d.strip() for d in depends_on.split(",") if d.strip()]
                # Check for self-dependency
                if task_id in dep_ids:
                    return "Error: A task cannot depend on itself"
                missing = _check_deps_exist(dep_ids)
                if missing:
                    return f"Error: Dependency task(s) not found: {', '.join(missing)}"
            updates.append("depends_on = ?")
            params.append(depends_on.strip())

        if tags is not None:
            updates.append("tags = ?")
            params.append(tags.strip())

        if not updates:
            return "Error: No fields to update. Provide at least one of: status, subject, description, priority, depends_on, tags"

        updates.append("updated_at = ?")
        params.append(datetime.now().isoformat())
        params.append(task_id)

        set_clause = ", ".join(updates)
        conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", params)
        conn.commit()

        # Fetch updated task for display
        updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

    except sqlite3.Error as e:
        return f"Error updating task: {e}"
    finally:
        conn.close()

    return _format_single_task(updated)


async def execute_task_list(
    status: str | None = None,
    session_id: str | None = None,
    tag: str | None = None,
    limit: int = 50,
) -> str:
    """List tasks with optional filtering.

    Args:
        status: Filter by status (pending, in_progress, completed, blocked, cancelled).
        session_id: Filter by session ID.
        tag: Filter by tag (matches if tag appears in comma-separated tags field).
        limit: Maximum number of tasks to return (default 50).

    Returns:
        Formatted table of tasks with progress summary.
    """
    limit = max(1, min(200, limit))

    conn = _connect()
    try:
        conditions: list[str] = []
        params: list[Any] = []

        if status:
            status = status.lower().strip()
            if status not in VALID_STATUSES:
                return f"Error: Invalid status '{status}'. Must be one of: {', '.join(sorted(VALID_STATUSES))}"
            conditions.append("status = ?")
            params.append(status)

        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id.strip())

        if tag:
            # Match tag within comma-separated tags field
            conditions.append("(',' || tags || ',' LIKE ?)")
            params.append(f"%,{tag.strip()},%")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        rows = conn.execute(
            f"""SELECT * FROM tasks {where}
                ORDER BY
                    CASE priority
                        WHEN 'critical' THEN 0
                        WHEN 'high' THEN 1
                        WHEN 'medium' THEN 2
                        WHEN 'low' THEN 3
                    END,
                    created_at DESC
                LIMIT ?""",
            params,
        ).fetchall()

        if not rows:
            filter_desc = []
            if status:
                filter_desc.append(f"status={status}")
            if session_id:
                filter_desc.append(f"session={session_id}")
            if tag:
                filter_desc.append(f"tag={tag}")
            filter_str = f" (filters: {', '.join(filter_desc)})" if filter_desc else ""
            return f"No tasks found{filter_str}."

        # Get overall counts for progress summary
        counts = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"
        ).fetchall()

    except sqlite3.Error as e:
        return f"Error listing tasks: {e}"
    finally:
        conn.close()

    # Build progress summary
    status_counts: dict[str, int] = {r["status"]: r["cnt"] for r in counts}
    total = sum(status_counts.values())
    completed = status_counts.get("completed", 0)
    in_progress = status_counts.get("in_progress", 0)
    pending = status_counts.get("pending", 0)
    blocked = status_counts.get("blocked", 0)

    # Progress bar
    pct = (completed / total * 100) if total > 0 else 0
    bar_len = 30
    filled = int(bar_len * completed / total) if total > 0 else 0
    bar = "=" * filled + "-" * (bar_len - filled)

    lines = [
        f"Tasks: {total} total | {completed} done | {in_progress} active | {pending} pending | {blocked} blocked",
        f"Progress: [{bar}] {pct:.0f}%",
        "",
    ]

    # Format each task
    status_icons = {
        "pending": "[ ]",
        "in_progress": "[~]",
        "completed": "[x]",
        "blocked": "[!]",
        "cancelled": "[-]",
    }

    priority_labels = {
        "critical": "CRIT",
        "high": "HIGH",
        "medium": "MED ",
        "low": "LOW ",
    }

    for row in rows:
        icon = status_icons.get(row["status"], "[ ]")
        pri = priority_labels.get(row["priority"], "    ")
        subject = row["subject"]
        if len(subject) > 60:
            subject = subject[:57] + "..."

        line = f"  {icon} {pri} {row['id']:<24} {subject}"

        extras = []
        if row["depends_on"]:
            extras.append(f"deps: {row['depends_on']}")
        if row["tags"]:
            extras.append(f"tags: {row['tags']}")
        if extras:
            line += f"  ({', '.join(extras)})"

        lines.append(line)

    lines.append(f"\n({len(rows)} tasks shown)")
    return "\n".join(lines)


def _check_deps_exist(dep_ids: list[str]) -> list[str]:
    """Check which dependency task IDs don't exist. Returns missing IDs."""
    if not dep_ids:
        return []

    conn = _connect()
    try:
        placeholders = ",".join("?" for _ in dep_ids)
        rows = conn.execute(
            f"SELECT id FROM tasks WHERE id IN ({placeholders})",
            dep_ids,
        ).fetchall()
        found = {r["id"] for r in rows}
        return [d for d in dep_ids if d not in found]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _get_blocking_deps(task_id: str, conn: sqlite3.Connection) -> list[str]:
    """Get list of dependency task IDs that are not yet completed."""
    row = conn.execute(
        "SELECT depends_on FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()

    if not row or not row["depends_on"]:
        return []

    dep_ids = [d.strip() for d in row["depends_on"].split(",") if d.strip()]
    if not dep_ids:
        return []

    placeholders = ",".join("?" for _ in dep_ids)
    incomplete = conn.execute(
        f"SELECT id FROM tasks WHERE id IN ({placeholders}) AND status != 'completed'",
        dep_ids,
    ).fetchall()

    return [r["id"] for r in incomplete]


def _format_single_task(row: sqlite3.Row) -> str:
    """Format a single task row for display."""
    status_icons = {
        "pending": "[ ]",
        "in_progress": "[~]",
        "completed": "[x]",
        "blocked": "[!]",
        "cancelled": "[-]",
    }

    icon = status_icons.get(row["status"], "[ ]")

    lines = [
        f"Task updated: {row['id']}",
        f"  {icon} Subject:     {row['subject']}",
        f"      Status:      {row['status']}",
        f"      Priority:    {row['priority']}",
    ]
    if row["description"]:
        desc = row["description"]
        if len(desc) > 150:
            desc = desc[:147] + "..."
        lines.append(f"      Description: {desc}")
    if row["depends_on"]:
        lines.append(f"      Depends on:  {row['depends_on']}")
    if row["tags"]:
        lines.append(f"      Tags:        {row['tags']}")
    lines.append(f"      Created:     {row['created_at'][:19]}")
    lines.append(f"      Updated:     {row['updated_at'][:19]}")
    if row["completed_at"]:
        lines.append(f"      Completed:   {row['completed_at'][:19]}")

    return "\n".join(lines)
