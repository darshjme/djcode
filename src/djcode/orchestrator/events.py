"""Orchestrator Event System — typed events for TUI integration.

Every agent action during orchestration emits an event. The TUI subscribes
to these events via async callbacks to render live dashboards: agent status,
token streams, tool calls, blocking gates, and final synthesis.

Event flow:
  OrchestratorStartEvent
    -> AgentStartEvent (per agent)
        -> AgentTokenEvent (streaming)
        -> AgentToolCallEvent (tool use)
    -> AgentCompleteEvent (per agent)
    -> BlockingGateEvent (if security/risk/legal/SRE fires)
  -> SynthesisEvent (final merged output)
  -> OrchestratorCompleteEvent (summary)
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from djcode.agents.registry import AgentRole


# -- Event Types ---------------------------------------------------------------

class EventType(str, enum.Enum):
    """All event types emitted during orchestration."""

    # Orchestrator lifecycle
    ORCHESTRATOR_START    = "orchestrator_start"
    ORCHESTRATOR_COMPLETE = "orchestrator_complete"
    ORCHESTRATOR_ERROR    = "orchestrator_error"

    # Agent lifecycle
    AGENT_START    = "agent_start"
    AGENT_TOKEN    = "agent_token"
    AGENT_TOOL     = "agent_tool"
    AGENT_COMPLETE = "agent_complete"
    AGENT_ERROR    = "agent_error"

    # Pipeline control
    WAVE_START     = "wave_start"
    WAVE_COMPLETE  = "wave_complete"
    BLOCKING_GATE  = "blocking_gate"

    # Synthesis
    SYNTHESIS_START    = "synthesis_start"
    SYNTHESIS_COMPLETE = "synthesis_complete"

    # Context
    CONTEXT_INJECT  = "context_inject"
    CONTEXT_WRITE   = "context_write"
    CONTEXT_CONFLICT = "context_conflict"


class GateSeverity(str, enum.Enum):
    """Severity levels for blocking gate events."""

    INFO     = "info"
    WARNING  = "warning"
    HIGH     = "high"
    CRITICAL = "critical"


class GateAction(str, enum.Enum):
    """Actions taken by blocking gate agents."""

    PASS     = "pass"
    WARN     = "warn"
    HALT     = "halt"
    ESCALATE = "escalate"


# -- Base Event ----------------------------------------------------------------

@dataclass(frozen=True)
class OrchestratorEvent:
    """Base event emitted during orchestration.

    All events carry a type, optional agent identity, arbitrary typed data,
    and a monotonic timestamp. Frozen for thread-safety across async boundaries.
    """

    event_type: EventType
    agent_name: str = ""
    agent_role: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    @property
    def is_terminal(self) -> bool:
        """Whether this event signals end of orchestration."""
        return self.event_type in (
            EventType.ORCHESTRATOR_COMPLETE,
            EventType.ORCHESTRATOR_ERROR,
        )

    @property
    def is_agent_terminal(self) -> bool:
        """Whether this event signals end of a single agent run."""
        return self.event_type in (
            EventType.AGENT_COMPLETE,
            EventType.AGENT_ERROR,
        )

    def __repr__(self) -> str:
        agent = f" [{self.agent_name}]" if self.agent_name else ""
        return f"<Event {self.event_type.value}{agent}>"


# -- Typed Event Constructors --------------------------------------------------
# Factory functions that produce correctly-typed OrchestratorEvent instances.
# Using factories instead of subclasses keeps the event system flat and
# serialization-friendly (single type to handle everywhere).


def orchestrator_start_event(
    task: str,
    strategy: str,
    agents: list[str],
    complexity: str,
) -> OrchestratorEvent:
    """Emitted when orchestration begins."""
    return OrchestratorEvent(
        event_type=EventType.ORCHESTRATOR_START,
        data={
            "task": task,
            "strategy": strategy,
            "agents": agents,
            "complexity": complexity,
        },
    )


def orchestrator_complete_event(
    task: str,
    agents_used: list[str],
    total_tokens: int,
    total_duration_s: float,
    strategy: str,
) -> OrchestratorEvent:
    """Emitted when orchestration finishes successfully."""
    return OrchestratorEvent(
        event_type=EventType.ORCHESTRATOR_COMPLETE,
        data={
            "task": task,
            "agents_used": agents_used,
            "total_tokens": total_tokens,
            "total_duration_s": round(total_duration_s, 3),
            "strategy": strategy,
        },
    )


def orchestrator_error_event(
    task: str,
    error: str,
    agents_completed: list[str],
) -> OrchestratorEvent:
    """Emitted when orchestration fails."""
    return OrchestratorEvent(
        event_type=EventType.ORCHESTRATOR_ERROR,
        data={
            "task": task,
            "error": error,
            "agents_completed": agents_completed,
        },
    )


def agent_start_event(
    agent_name: str,
    agent_role: str,
    task: str,
    wave: int = 0,
) -> OrchestratorEvent:
    """Emitted when a single agent begins execution."""
    return OrchestratorEvent(
        event_type=EventType.AGENT_START,
        agent_name=agent_name,
        agent_role=agent_role,
        data={"task": task[:500], "wave": wave},
    )


def agent_token_event(
    agent_name: str,
    agent_role: str,
    token: str,
    is_thinking: bool = False,
) -> OrchestratorEvent:
    """Emitted for each streamed token from an agent."""
    return OrchestratorEvent(
        event_type=EventType.AGENT_TOKEN,
        agent_name=agent_name,
        agent_role=agent_role,
        data={"token": token, "thinking": is_thinking},
    )


def agent_tool_event(
    agent_name: str,
    agent_role: str,
    tool_name: str,
    tool_args: dict[str, Any],
    tool_result: str,
    duration_ms: float,
) -> OrchestratorEvent:
    """Emitted when an agent invokes a tool."""
    return OrchestratorEvent(
        event_type=EventType.AGENT_TOOL,
        agent_name=agent_name,
        agent_role=agent_role,
        data={
            "tool_name": tool_name,
            "tool_args": tool_args,
            "tool_result": tool_result[:500],
            "duration_ms": round(duration_ms, 2),
        },
    )


def agent_complete_event(
    agent_name: str,
    agent_role: str,
    result_preview: str,
    confidence: float,
    elapsed_s: float,
    tokens: int,
) -> OrchestratorEvent:
    """Emitted when a single agent finishes successfully."""
    return OrchestratorEvent(
        event_type=EventType.AGENT_COMPLETE,
        agent_name=agent_name,
        agent_role=agent_role,
        data={
            "result_preview": result_preview[:300],
            "confidence": confidence,
            "elapsed_s": round(elapsed_s, 3),
            "tokens": tokens,
        },
    )


def agent_error_event(
    agent_name: str,
    agent_role: str,
    error: str,
) -> OrchestratorEvent:
    """Emitted when a single agent fails."""
    return OrchestratorEvent(
        event_type=EventType.AGENT_ERROR,
        agent_name=agent_name,
        agent_role=agent_role,
        data={"error": error},
    )


def wave_start_event(
    wave_number: int,
    wave_name: str,
    agents: list[str],
) -> OrchestratorEvent:
    """Emitted when a new execution wave begins."""
    return OrchestratorEvent(
        event_type=EventType.WAVE_START,
        data={
            "wave_number": wave_number,
            "wave_name": wave_name,
            "agents": agents,
        },
    )


def wave_complete_event(
    wave_number: int,
    wave_name: str,
    results: dict[str, str],
    elapsed_s: float,
) -> OrchestratorEvent:
    """Emitted when an execution wave finishes."""
    return OrchestratorEvent(
        event_type=EventType.WAVE_COMPLETE,
        data={
            "wave_number": wave_number,
            "wave_name": wave_name,
            "agent_results": {k: v[:200] for k, v in results.items()},
            "elapsed_s": round(elapsed_s, 3),
        },
    )


def blocking_gate_event(
    agent_name: str,
    agent_role: str,
    severity: GateSeverity,
    finding: str,
    action: GateAction,
) -> OrchestratorEvent:
    """Emitted when a blocking agent (Security, Risk, Legal, SRE) reports."""
    return OrchestratorEvent(
        event_type=EventType.BLOCKING_GATE,
        agent_name=agent_name,
        agent_role=agent_role,
        data={
            "severity": severity.value,
            "finding": finding,
            "action": action.value,
        },
    )


def synthesis_start_event(agents_used: list[str]) -> OrchestratorEvent:
    """Emitted when synthesis of multi-agent results begins."""
    return OrchestratorEvent(
        event_type=EventType.SYNTHESIS_START,
        data={"agents_used": agents_used},
    )


def synthesis_complete_event(
    final_response: str,
    agents_used: list[str],
    total_tokens: int,
) -> OrchestratorEvent:
    """Emitted when synthesis finishes."""
    return OrchestratorEvent(
        event_type=EventType.SYNTHESIS_COMPLETE,
        data={
            "final_response_preview": final_response[:500],
            "agents_used": agents_used,
            "total_tokens": total_tokens,
        },
    )


def context_inject_event(
    source: str,
    count: int,
) -> OrchestratorEvent:
    """Emitted when context is injected from vector store."""
    return OrchestratorEvent(
        event_type=EventType.CONTEXT_INJECT,
        data={"source": source, "count": count},
    )


def context_write_event(
    agent_name: str,
    key: str,
    priority: str,
) -> OrchestratorEvent:
    """Emitted when an agent writes to the context bus."""
    return OrchestratorEvent(
        event_type=EventType.CONTEXT_WRITE,
        agent_name=agent_name,
        data={"key": key, "priority": priority},
    )


def context_conflict_event(
    key: str,
    existing_agent: str,
    new_agent: str,
) -> OrchestratorEvent:
    """Emitted when two agents write to the same context key."""
    return OrchestratorEvent(
        event_type=EventType.CONTEXT_CONFLICT,
        data={
            "key": key,
            "existing_agent": existing_agent,
            "new_agent": new_agent,
        },
    )


# -- Event Bus -----------------------------------------------------------------

EventCallback = Callable[[OrchestratorEvent], Coroutine[Any, Any, None]]


class EventBus:
    """Async event bus for orchestrator events.

    TUI components and loggers subscribe to events via callbacks.
    Thread-safe — callbacks are dispatched sequentially within each emit call
    but multiple emits can overlap safely (each creates its own gather).
    """

    def __init__(self) -> None:
        self._callbacks: list[EventCallback] = []
        self._history: list[OrchestratorEvent] = []
        self._max_history: int = 1000

    def subscribe(self, callback: EventCallback) -> None:
        """Register a callback for all events."""
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def unsubscribe(self, callback: EventCallback) -> None:
        """Remove a callback."""
        self._callbacks = [cb for cb in self._callbacks if cb is not callback]

    async def emit(self, event: OrchestratorEvent) -> None:
        """Dispatch an event to all subscribers.

        Errors in individual callbacks are caught and logged, never propagated.
        """
        # Record in history (ring buffer)
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        for cb in self._callbacks:
            try:
                await cb(event)
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    "Event callback error for %s", event.event_type.value
                )

    @property
    def history(self) -> list[OrchestratorEvent]:
        """Recent event history (up to max_history)."""
        return list(self._history)

    def clear_history(self) -> None:
        """Clear event history."""
        self._history.clear()

    @property
    def subscriber_count(self) -> int:
        return len(self._callbacks)
