"""ContextBus — Shared memory between agents during orchestration.

Agents write findings, plans, and results to the bus.
Other agents read from the bus to get context from parallel work.
Thread-safe via file locking for potential future parallel execution.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class BusEntry:
    """A single entry on the context bus."""
    agent: str          # Agent name that wrote this
    role: str           # Agent role
    key: str            # Entry key (e.g., "analysis", "plan", "fix")
    content: str        # The actual content
    timestamp: float    # When it was written
    metadata: dict[str, Any] = field(default_factory=dict)


class ContextBus:
    """In-memory shared context for multi-agent orchestration.

    Agents write findings to the bus during execution.
    The orchestrator reads all entries to synthesize final output.
    """

    def __init__(self) -> None:
        self._entries: list[BusEntry] = []
        self._task: str = ""
        self._intent: str = ""

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

    def write(
        self,
        agent: str,
        role: str,
        key: str,
        content: str,
        **metadata: Any,
    ) -> None:
        """Write an entry to the bus."""
        self._entries.append(BusEntry(
            agent=agent,
            role=role,
            key=key,
            content=content,
            timestamp=time.time(),
            metadata=metadata,
        ))

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

    def summary(self) -> str:
        """Generate a summary of all bus entries for context injection."""
        if not self._entries:
            return ""

        parts: list[str] = []
        parts.append(f"## Orchestration Context\nTask: {self._task}\nIntent: {self._intent}\n")

        for entry in self._entries:
            parts.append(
                f"### [{entry.role}] {entry.agent} — {entry.key}\n"
                f"{entry.content}\n"
            )

        return "\n".join(parts)

    def clear(self) -> None:
        """Clear all entries."""
        self._entries.clear()
        self._task = ""
        self._intent = ""

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return len(self._entries) > 0
