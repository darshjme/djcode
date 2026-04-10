"""Agent State Machine — Tracks lifecycle of each PhD agent execution.

States: IDLE -> ASSIGNED -> RESEARCHING -> EXECUTING -> REVIEWING -> DONE -> ERROR

Each agent tracks: state, task, start_time, tokens_used, tools_called, ra_findings.
State transitions emit events for the TUI dashboard via async callbacks.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from djcode.agents.registry import AgentRole, AgentSpec

logger = logging.getLogger(__name__)

__all__ = [
    "AgentState",
    "AgentEvent",
    "AgentEventType",
    "AgentStateError",
    "AgentStateMachine",
]


# -- Valid state transitions --------------------------------------------------

class AgentState(str, enum.Enum):
    """Lifecycle states for an agent execution."""

    IDLE        = "idle"
    ASSIGNED    = "assigned"
    RESEARCHING = "researching"
    EXECUTING   = "executing"
    REVIEWING   = "reviewing"
    DONE        = "done"
    ERROR       = "error"


# Allowed transitions: from_state -> set(to_states)
_TRANSITIONS: dict[AgentState, frozenset[AgentState]] = {
    AgentState.IDLE:        frozenset({AgentState.ASSIGNED, AgentState.ERROR}),
    AgentState.ASSIGNED:    frozenset({AgentState.RESEARCHING, AgentState.EXECUTING, AgentState.ERROR}),
    AgentState.RESEARCHING: frozenset({AgentState.EXECUTING, AgentState.ERROR}),
    AgentState.EXECUTING:   frozenset({AgentState.REVIEWING, AgentState.DONE, AgentState.ERROR}),
    AgentState.REVIEWING:   frozenset({AgentState.DONE, AgentState.ERROR}),
    AgentState.DONE:        frozenset(),
    AgentState.ERROR:       frozenset(),
}


# -- Events -------------------------------------------------------------------

class AgentEventType(str, enum.Enum):
    """Types of events emitted by the state machine."""

    STATE_CHANGE  = "state_change"
    TOKEN         = "token"
    TOOL_CALL     = "tool_call"
    TOOL_RESULT   = "tool_result"
    RA_BRIEFING   = "ra_briefing"
    QUALITY_SCORE = "quality_score"
    ERROR         = "error"
    COMPLETE      = "complete"


@dataclass(frozen=True)
class AgentEvent:
    """Immutable event emitted during agent execution."""

    event_type: AgentEventType
    agent_role: AgentRole
    agent_name: str
    timestamp: float
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.event_type in (AgentEventType.COMPLETE, AgentEventType.ERROR)


# -- Errors -------------------------------------------------------------------

class AgentStateError(Exception):
    """Raised on invalid state transitions."""

    def __init__(self, agent: str, from_state: AgentState, to_state: AgentState) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"Agent '{agent}': invalid transition {from_state.value} -> {to_state.value}"
        )


# -- Callback type alias ------------------------------------------------------

EventCallback = Callable[[AgentEvent], Coroutine[Any, Any, None]]


# -- State Machine ------------------------------------------------------------

@dataclass
class ToolCallRecord:
    """Record of a single tool invocation."""

    tool_name: str
    arguments: dict[str, Any]
    result_preview: str       # first 200 chars of result
    duration_ms: float
    timestamp: float


@dataclass
class AgentStateMachine:
    """Async-friendly state machine tracking a single agent's execution lifecycle.

    Emits AgentEvent instances through registered callbacks so the TUI dashboard
    can render live status, token counts, and tool activity.
    """

    spec: AgentSpec
    state: AgentState = AgentState.IDLE
    task: str = ""
    start_time: float = 0.0
    end_time: float = 0.0
    tokens_used: int = 0
    tools_called: list[ToolCallRecord] = field(default_factory=list)
    ra_findings: str = ""
    confidence_score: float = 0.0
    error_message: str = ""
    _callbacks: list[EventCallback] = field(default_factory=list, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # -- Callback management ---------------------------------------------------

    def on_event(self, callback: EventCallback) -> None:
        """Register an async callback for all events."""
        self._callbacks.append(callback)

    def remove_callback(self, callback: EventCallback) -> None:
        """Remove a previously registered callback."""
        self._callbacks = [cb for cb in self._callbacks if cb is not callback]

    async def _emit(self, event_type: AgentEventType, **data: Any) -> AgentEvent:
        """Create and dispatch an event to all callbacks."""
        event = AgentEvent(
            event_type=event_type,
            agent_role=self.spec.role,
            agent_name=self.spec.name,
            timestamp=time.time(),
            data=data,
        )
        for cb in self._callbacks:
            try:
                await cb(event)
            except Exception:
                logger.exception("Event callback error for %s", self.spec.name)
        return event

    # -- State transitions -----------------------------------------------------

    async def transition(self, to_state: AgentState) -> AgentEvent:
        """Transition to a new state, validating the move and emitting an event.

        Raises AgentStateError if the transition is not allowed.
        """
        async with self._lock:
            allowed = _TRANSITIONS.get(self.state, frozenset())
            if to_state not in allowed:
                raise AgentStateError(self.spec.name, self.state, to_state)

            old_state = self.state
            self.state = to_state
            logger.debug(
                "%s: %s -> %s", self.spec.name, old_state.value, to_state.value
            )

            return await self._emit(
                AgentEventType.STATE_CHANGE,
                from_state=old_state.value,
                to_state=to_state.value,
            )

    # -- Lifecycle helpers (sugar over transition) -----------------------------

    async def assign(self, task: str) -> None:
        """Move from IDLE to ASSIGNED with a task."""
        self.task = task
        self.start_time = time.time()
        await self.transition(AgentState.ASSIGNED)

    async def start_research(self) -> None:
        """Move from ASSIGNED to RESEARCHING."""
        await self.transition(AgentState.RESEARCHING)

    async def start_execution(self) -> None:
        """Move from RESEARCHING or ASSIGNED to EXECUTING."""
        await self.transition(AgentState.EXECUTING)

    async def start_review(self) -> None:
        """Move from EXECUTING to REVIEWING."""
        await self.transition(AgentState.REVIEWING)

    async def complete(self, confidence: float = 0.0) -> None:
        """Move to DONE, recording confidence and end time."""
        self.confidence_score = confidence
        self.end_time = time.time()
        # Can come from EXECUTING or REVIEWING
        await self.transition(AgentState.DONE)
        await self._emit(
            AgentEventType.COMPLETE,
            confidence=confidence,
            duration_s=self.duration_s,
            tokens=self.tokens_used,
            tools=len(self.tools_called),
        )

    async def fail(self, error: str) -> None:
        """Move to ERROR from any non-terminal state."""
        self.error_message = error
        self.end_time = time.time()
        # ERROR is reachable from any non-terminal state
        try:
            await self.transition(AgentState.ERROR)
        except AgentStateError:
            # Force it — ERROR is always reachable except from DONE/ERROR
            self.state = AgentState.ERROR
        await self._emit(AgentEventType.ERROR, error=error)

    # -- Tracking helpers ------------------------------------------------------

    async def record_token(self, token: str) -> None:
        """Record a single token emission and notify listeners."""
        self.tokens_used += 1
        await self._emit(AgentEventType.TOKEN, token=token)

    async def record_tokens_batch(self, count: int) -> None:
        """Record multiple tokens at once (for non-streaming aggregation)."""
        self.tokens_used += count

    async def record_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: str,
        duration_ms: float,
    ) -> None:
        """Record a tool invocation and notify listeners."""
        record = ToolCallRecord(
            tool_name=tool_name,
            arguments=arguments,
            result_preview=result[:200],
            duration_ms=duration_ms,
            timestamp=time.time(),
        )
        self.tools_called.append(record)
        await self._emit(
            AgentEventType.TOOL_CALL,
            tool=tool_name,
            args=arguments,
            duration_ms=duration_ms,
        )

    async def record_ra_briefing(self, briefing: str) -> None:
        """Store research assistant findings and notify listeners."""
        self.ra_findings = briefing
        await self._emit(AgentEventType.RA_BRIEFING, briefing_length=len(briefing))

    async def record_quality_score(self, score: float) -> None:
        """Store quality/confidence score and notify listeners."""
        self.confidence_score = score
        await self._emit(AgentEventType.QUALITY_SCORE, score=score)

    # -- Properties ------------------------------------------------------------

    @property
    def duration_s(self) -> float:
        """Elapsed execution time in seconds."""
        if self.start_time == 0.0:
            return 0.0
        end = self.end_time if self.end_time > 0.0 else time.time()
        return round(end - self.start_time, 3)

    @property
    def is_terminal(self) -> bool:
        return self.state in (AgentState.DONE, AgentState.ERROR)

    @property
    def is_active(self) -> bool:
        return self.state in (
            AgentState.ASSIGNED,
            AgentState.RESEARCHING,
            AgentState.EXECUTING,
            AgentState.REVIEWING,
        )

    @property
    def tool_count(self) -> int:
        return len(self.tools_called)

    def snapshot(self) -> dict[str, Any]:
        """Return a serializable snapshot of the current machine state."""
        return {
            "agent": self.spec.name,
            "role": self.spec.role.value,
            "state": self.state.value,
            "task": self.task,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_s": self.duration_s,
            "tokens_used": self.tokens_used,
            "tools_called": self.tool_count,
            "confidence_score": self.confidence_score,
            "has_ra_briefing": bool(self.ra_findings),
            "error": self.error_message or None,
        }
