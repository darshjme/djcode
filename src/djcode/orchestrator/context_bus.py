"""ContextBus v2 — Thread-safe shared state for multi-agent execution.

Agents write findings, plans, code, reviews, and results to the bus.
Other agents read from the bus to get context from parallel work.

v2 features over v1:
  - asyncio.Lock for thread-safety under parallel agent execution
  - Typed entries with priority levels
  - Agent attribution and timestamps
  - Conflict detection (two agents writing same key)
  - Versioned history (all writes retained, not just latest)
  - Event emission on write (for TUI updates)
  - Summary generation with priority ordering for prompt injection
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


# -- Entry Types and Priorities ------------------------------------------------

class EntryType(str, Enum):
    """Typed categories for bus entries."""

    CODE            = "code"
    PLAN            = "plan"
    REVIEW          = "review"
    TEST            = "test"
    DEPLOYMENT      = "deployment"
    SECURITY_AUDIT  = "security_audit"
    ANALYSIS        = "analysis"
    DOCUMENTATION   = "documentation"
    RISK_ASSESSMENT = "risk_assessment"
    COST_ANALYSIS   = "cost_analysis"
    INTEGRATION     = "integration"
    RESULT          = "result"
    MEMORY          = "memory"
    GENERAL         = "general"


class Priority(str, Enum):
    """Priority levels for bus entries. Higher priority = shown first in summaries."""

    CRITICAL = "critical"
    HIGH     = "high"
    NORMAL   = "normal"
    LOW      = "low"


_PRIORITY_ORDER: dict[Priority, int] = {
    Priority.CRITICAL: 0,
    Priority.HIGH: 1,
    Priority.NORMAL: 2,
    Priority.LOW: 3,
}


# -- Bus Entry -----------------------------------------------------------------

@dataclass(frozen=True)
class BusEntry:
    """A single immutable entry on the context bus.

    Frozen so entries can safely be shared across async agent boundaries.
    """

    agent: str
    role: str
    key: str
    content: str
    timestamp: float
    entry_type: EntryType = EntryType.GENERAL
    priority: Priority = Priority.NORMAL
    version: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def age_s(self) -> float:
        """Seconds since this entry was written."""
        return round(time.time() - self.timestamp, 3)


# -- Write Callback type -------------------------------------------------------

BusWriteCallback = Callable[[BusEntry], Coroutine[Any, Any, None]]


# -- Context Bus ---------------------------------------------------------------

class ContextBus:
    """Thread-safe shared context for multi-agent orchestration.

    Agents write findings to the bus during execution. The orchestrator
    reads all entries to synthesize final output. Safe for concurrent
    use under asyncio.gather() parallel agent execution.

    Backwards compatible with v1 API (write/read_all/read_by_agent/summary/clear).
    """

    def __init__(self) -> None:
        self._entries: list[BusEntry] = []
        self._task: str = ""
        self._intent: str = ""
        self._lock = asyncio.Lock()
        self._version_counters: dict[str, int] = {}  # key -> version
        self._write_callbacks: list[BusWriteCallback] = []
        self._conflicts: list[dict[str, str]] = []

    # -- Task management -------------------------------------------------------

    def set_task(self, task: str, intent: str) -> None:
        """Set the current orchestration task."""
        self._task = task
        self._intent = intent

    @property
    def task(self) -> str:
        return self._task

    @property
    def intent(self) -> str:
        return self._intent

    # -- Callback management ---------------------------------------------------

    def on_write(self, callback: BusWriteCallback) -> None:
        """Register a callback invoked on every bus write."""
        self._write_callbacks.append(callback)

    def remove_write_callback(self, callback: BusWriteCallback) -> None:
        """Remove a write callback."""
        self._write_callbacks = [cb for cb in self._write_callbacks if cb is not callback]

    # -- Write operations (thread-safe) ----------------------------------------

    async def write_async(
        self,
        agent: str,
        role: str,
        key: str,
        content: str,
        entry_type: EntryType = EntryType.GENERAL,
        priority: Priority = Priority.NORMAL,
        **metadata: Any,
    ) -> BusEntry:
        """Write an entry to the bus (async, thread-safe).

        Detects conflicts when another agent already wrote to the same key.
        Increments version counter for duplicate keys.
        """
        async with self._lock:
            # Version tracking
            version_key = f"{role}:{key}"
            version = self._version_counters.get(version_key, 0) + 1
            self._version_counters[version_key] = version

            # Conflict detection
            existing = [e for e in self._entries if e.key == key and e.agent != agent]
            if existing:
                conflict = {
                    "key": key,
                    "existing_agent": existing[-1].agent,
                    "new_agent": agent,
                    "timestamp": str(time.time()),
                }
                self._conflicts.append(conflict)
                logger.warning(
                    "Context bus conflict on key '%s': %s vs %s",
                    key, existing[-1].agent, agent,
                )

            entry = BusEntry(
                agent=agent,
                role=role,
                key=key,
                content=content,
                timestamp=time.time(),
                entry_type=entry_type,
                priority=priority,
                version=version,
                metadata=dict(metadata),
            )
            self._entries.append(entry)

        # Notify callbacks (outside lock to avoid deadlocks)
        for cb in self._write_callbacks:
            try:
                await cb(entry)
            except Exception:
                logger.exception("Bus write callback error")

        return entry

    def write(
        self,
        agent: str,
        role: str,
        key: str,
        content: str,
        entry_type: EntryType = EntryType.GENERAL,
        priority: Priority = Priority.NORMAL,
        **metadata: Any,
    ) -> BusEntry:
        """Synchronous write for backwards compatibility with v1.

        Safe for single-threaded execution. For parallel agents, use write_async.
        """
        version_key = f"{role}:{key}"
        version = self._version_counters.get(version_key, 0) + 1
        self._version_counters[version_key] = version

        # Conflict detection
        existing = [e for e in self._entries if e.key == key and e.agent != agent]
        if existing:
            conflict = {
                "key": key,
                "existing_agent": existing[-1].agent,
                "new_agent": agent,
                "timestamp": str(time.time()),
            }
            self._conflicts.append(conflict)

        entry = BusEntry(
            agent=agent,
            role=role,
            key=key,
            content=content,
            timestamp=time.time(),
            entry_type=entry_type,
            priority=priority,
            version=version,
            metadata=dict(metadata),
        )
        self._entries.append(entry)
        return entry

    # -- Read operations -------------------------------------------------------

    def read_all(self) -> list[BusEntry]:
        """Read all entries in chronological order."""
        return list(self._entries)

    def read_by_agent(self, agent: str) -> list[BusEntry]:
        """Read all entries from a specific agent."""
        return [e for e in self._entries if e.agent == agent]

    def read_by_role(self, role: str) -> list[BusEntry]:
        """Read all entries from agents with a specific role."""
        return [e for e in self._entries if e.role == role]

    def read_by_key(self, key: str) -> list[BusEntry]:
        """Read all entries with a specific key."""
        return [e for e in self._entries if e.key == key]

    def read_by_type(self, entry_type: EntryType) -> list[BusEntry]:
        """Read all entries of a specific type."""
        return [e for e in self._entries if e.entry_type == entry_type]

    def read_by_priority(self, priority: Priority) -> list[BusEntry]:
        """Read all entries at a specific priority level."""
        return [e for e in self._entries if e.priority == priority]

    def read_latest(self, key: str) -> BusEntry | None:
        """Read the most recent entry for a given key."""
        matches = [e for e in self._entries if e.key == key]
        return matches[-1] if matches else None

    def read_history(self, key: str) -> list[BusEntry]:
        """Read all versions of a key, oldest first."""
        return [e for e in self._entries if e.key == key]

    # -- Conflict tracking -----------------------------------------------------

    @property
    def conflicts(self) -> list[dict[str, str]]:
        """All detected write conflicts."""
        return list(self._conflicts)

    @property
    def has_conflicts(self) -> bool:
        return len(self._conflicts) > 0

    # -- Summary generation ----------------------------------------------------

    def summary(self, max_entries: int = 20, max_content_len: int = 800) -> str:
        """Generate a summary of bus entries for context injection into agent prompts.

        Entries are sorted by priority (critical first), then by recency.
        Content is truncated to keep prompt injection reasonable.
        """
        if not self._entries:
            return ""

        parts: list[str] = []
        parts.append(f"## Orchestration Context\nTask: {self._task}\nIntent: {self._intent}\n")

        # Sort: priority first, then newest first within priority
        sorted_entries = sorted(
            self._entries,
            key=lambda e: (
                _PRIORITY_ORDER.get(e.priority, 2),
                -e.timestamp,
            ),
        )

        seen_keys: set[str] = set()
        shown = 0

        for entry in sorted_entries:
            if shown >= max_entries:
                remaining = len(sorted_entries) - shown
                parts.append(f"\n... and {remaining} more entries on the bus.")
                break

            # For duplicate keys, show only the latest version
            dedup_key = f"{entry.agent}:{entry.key}"
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            priority_tag = ""
            if entry.priority in (Priority.CRITICAL, Priority.HIGH):
                priority_tag = f" [{entry.priority.value.upper()}]"

            content = entry.content
            if len(content) > max_content_len:
                content = content[:max_content_len] + "\n... (truncated)"

            parts.append(
                f"### [{entry.role}] {entry.agent} -- {entry.key}{priority_tag}\n"
                f"{content}\n"
            )
            shown += 1

        if self._conflicts:
            parts.append(f"\n**Conflicts detected:** {len(self._conflicts)} key collision(s)")

        return "\n".join(parts)

    def summary_for_agent(self, exclude_agent: str, max_len: int = 2000) -> str:
        """Generate a summary excluding a specific agent's own entries.

        Used when injecting context into an agent's prompt to avoid circular reference.
        """
        if not self._entries:
            return ""

        parts: list[str] = []
        parts.append(f"## Context from other agents\nTask: {self._task}\n")

        other_entries = [e for e in self._entries if e.agent != exclude_agent]
        if not other_entries:
            return ""

        # Sort by priority then recency
        other_entries.sort(
            key=lambda e: (_PRIORITY_ORDER.get(e.priority, 2), -e.timestamp)
        )

        total_len = 0
        for entry in other_entries:
            content = entry.content
            if len(content) > 500:
                content = content[:500] + "..."

            block = (
                f"### [{entry.role}] {entry.agent} -- {entry.key}\n"
                f"{content}\n"
            )
            if total_len + len(block) > max_len:
                parts.append("... (remaining context truncated for prompt budget)")
                break

            parts.append(block)
            total_len += len(block)

        return "\n".join(parts)

    # -- State management ------------------------------------------------------

    def clear(self) -> None:
        """Clear all entries and reset state."""
        self._entries.clear()
        self._task = ""
        self._intent = ""
        self._version_counters.clear()
        self._conflicts.clear()

    def snapshot(self) -> dict[str, Any]:
        """Return a serializable snapshot of the bus state."""
        return {
            "task": self._task,
            "intent": self._intent,
            "entry_count": len(self._entries),
            "agents": list({e.agent for e in self._entries}),
            "keys": list({e.key for e in self._entries}),
            "conflicts": len(self._conflicts),
            "entries": [
                {
                    "agent": e.agent,
                    "role": e.role,
                    "key": e.key,
                    "type": e.entry_type.value,
                    "priority": e.priority.value,
                    "version": e.version,
                    "content_len": len(e.content),
                    "timestamp": e.timestamp,
                }
                for e in self._entries
            ],
        }

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return len(self._entries) > 0
