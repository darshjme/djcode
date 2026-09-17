"""Core event system — the one typed event stream every front-end consumes.

Moved here from ``djcode.orchestrator.events`` in Wave 2. The event set stopped
being orchestrator-specific the moment a single-agent turn started emitting it,
so the dataclass is now ``CoreEvent``; ``OrchestratorEvent`` remains as an alias
so existing imports and ``isinstance`` checks keep working unchanged.

Two families of events share the one flat type:

Orchestration (multi-agent, ``djcode.orchestrator.engine``)::

  ORCHESTRATOR_START
    -> AGENT_START (per agent)
        -> AGENT_TOKEN (streaming)
        -> AGENT_TOOL (tool use)
    -> AGENT_COMPLETE (per agent)
    -> BLOCKING_GATE (if security/risk/legal/SRE fires)
  -> SYNTHESIS_COMPLETE (final merged output)
  -> ORCHESTRATOR_COMPLETE (summary)

Session (single-agent turn, what a GUI or the REPL renders)::

  TOKEN / THINKING (streaming)
    -> TOOL_CALL (parsed out of the stream, BEFORE approval)
    -> PERMISSION_REQUEST / PERMISSION_DECIDED
    -> TOOL_RESULT -> DIFF -> CHECKPOINT
  -> COMPLETE, or ERROR

This module is part of the headless core: it imports only the standard library
and must never import rich, questionary, prompt_toolkit, textual or click, nor
call print()/input()/sys.stdout.write()/sys.stderr.write().

NOTE - deliberately no ``__all__``. ``djcode/orchestrator/events.py`` is a
star-re-export shim for this module; adding a partial ``__all__`` here would
silently narrow that shim and break ``orchestrator/engine.py``'s 17-name import
at import time. If you add one, it must name every public symbol below.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps this module standalone
    from djcode.core.outcome import ToolOutcome

logger = logging.getLogger(__name__)

# -- Event Types ---------------------------------------------------------------


class EventType(enum.StrEnum):
    """All event types emitted during orchestration."""

    # Orchestrator lifecycle
    ORCHESTRATOR_START = "orchestrator_start"
    ORCHESTRATOR_COMPLETE = "orchestrator_complete"
    ORCHESTRATOR_ERROR = "orchestrator_error"

    # Agent lifecycle
    AGENT_START = "agent_start"
    AGENT_TOKEN = "agent_token"
    AGENT_TOOL = "agent_tool"
    AGENT_COMPLETE = "agent_complete"
    AGENT_ERROR = "agent_error"

    # Pipeline control
    WAVE_START = "wave_start"
    WAVE_COMPLETE = "wave_complete"
    BLOCKING_GATE = "blocking_gate"

    # Synthesis
    SYNTHESIS_START = "synthesis_start"
    SYNTHESIS_COMPLETE = "synthesis_complete"

    # Context
    CONTEXT_INJECT = "context_inject"
    CONTEXT_WRITE = "context_write"
    CONTEXT_CONFLICT = "context_conflict"

    # ── Session events (W2) ────────────────────────────────────────────────
    # The single-agent turn. These are what a GUI, the REPL renderer and the
    # --json headless surface consume. Deliberately NOT prefixed AGENT_*: the
    # AGENT_* family above belongs to multi-agent orchestration and carries an
    # agent identity, while these describe the user's own turn.
    #
    # Every value below was checked against the orchestration values above for
    # collisions -- an enum.StrEnum silently aliases two members that share a
    # value, which would make one of them unreachable. There are none.
    TOKEN = "token"
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    DIFF = "diff"
    CHECKPOINT = "checkpoint"
    COMPLETE = "complete"
    ERROR = "error"
    PERMISSION_REQUEST = "permission_request"
    PERMISSION_DECIDED = "permission_decided"
    STEER = "steer"
    QUEUE = "queue"


class GateSeverity(enum.StrEnum):
    """Severity levels for blocking gate events."""

    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"


class GateAction(enum.StrEnum):
    """Actions taken by blocking gate agents."""

    PASS = "pass"
    WARN = "warn"
    HALT = "halt"
    ESCALATE = "escalate"


# -- Base Event ----------------------------------------------------------------


@dataclass(frozen=True)
class CoreEvent:
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


#: Backwards-compatible alias. ``OrchestratorEvent`` was the original name and
#: is still imported by ``orchestrator/engine.py``, ``orchestrator/__init__.py``
#: and ``tests/test_v4.py``. It is the SAME class object, so the single
#: ``isinstance(r, OrchestratorEvent)`` check at ``engine.py:517`` is unaffected.
OrchestratorEvent = CoreEvent


# -- Typed Event Constructors --------------------------------------------------
# Factory functions that produce correctly-typed CoreEvent instances.
# Using factories instead of subclasses keeps the event system flat and
# serialization-friendly (single type to handle everywhere).


def orchestrator_start_event(
    task: str,
    strategy: str,
    agents: list[str],
    complexity: str,
    *,
    intent: str = "",
    route_method: str = "",
    context_docs: int = 0,
) -> CoreEvent:
    """Emitted when orchestration begins.

    ``intent``, ``route_method`` and ``context_docs`` existed only inside a
    ``console.print`` before W2. They are carried here so removing that print
    loses nothing a front-end could previously show.
    """
    return CoreEvent(
        event_type=EventType.ORCHESTRATOR_START,
        data={
            "task": task,
            "strategy": strategy,
            "agents": agents,
            "complexity": complexity,
            "intent": intent,
            "route_method": route_method,
            "context_docs": context_docs,
        },
    )


def orchestrator_complete_event(
    task: str,
    agents_used: list[str],
    total_tokens: int,
    total_duration_s: float,
    strategy: str,
    *,
    bus_entries: int = 0,
    context_stored: int = 0,
    context_backend: str = "",
) -> CoreEvent:
    """Emitted when orchestration finishes successfully.

    ``bus_entries``, ``context_stored`` and ``context_backend`` were previously
    only in a ``console.print``; they ride here so the print can go.
    """
    return CoreEvent(
        event_type=EventType.ORCHESTRATOR_COMPLETE,
        data={
            "task": task,
            "agents_used": agents_used,
            "total_tokens": total_tokens,
            "total_duration_s": round(total_duration_s, 3),
            "strategy": strategy,
            "bus_entries": bus_entries,
            "context_stored": context_stored,
            "context_backend": context_backend,
        },
    )


def orchestrator_error_event(
    task: str,
    error: str,
    agents_completed: list[str],
) -> CoreEvent:
    """Emitted when orchestration fails."""
    return CoreEvent(
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
) -> CoreEvent:
    """Emitted when a single agent begins execution."""
    return CoreEvent(
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
) -> CoreEvent:
    """Emitted for each streamed token from an agent."""
    return CoreEvent(
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
) -> CoreEvent:
    """Emitted when an agent invokes a tool."""
    return CoreEvent(
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
) -> CoreEvent:
    """Emitted when a single agent finishes successfully."""
    return CoreEvent(
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
) -> CoreEvent:
    """Emitted when a single agent fails."""
    return CoreEvent(
        event_type=EventType.AGENT_ERROR,
        agent_name=agent_name,
        agent_role=agent_role,
        data={"error": error},
    )


def wave_start_event(
    wave_number: int,
    wave_name: str,
    agents: list[str],
) -> CoreEvent:
    """Emitted when a new execution wave begins."""
    return CoreEvent(
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
) -> CoreEvent:
    """Emitted when an execution wave finishes."""
    return CoreEvent(
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
) -> CoreEvent:
    """Emitted when a blocking agent (Security, Risk, Legal, SRE) reports."""
    return CoreEvent(
        event_type=EventType.BLOCKING_GATE,
        agent_name=agent_name,
        agent_role=agent_role,
        data={
            "severity": severity.value,
            "finding": finding,
            "action": action.value,
        },
    )


def synthesis_start_event(agents_used: list[str]) -> CoreEvent:
    """Emitted when synthesis of multi-agent results begins."""
    return CoreEvent(
        event_type=EventType.SYNTHESIS_START,
        data={"agents_used": agents_used},
    )


def synthesis_complete_event(
    final_response: str,
    agents_used: list[str],
    total_tokens: int,
) -> CoreEvent:
    """Emitted when synthesis finishes."""
    return CoreEvent(
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
) -> CoreEvent:
    """Emitted when context is injected from vector store."""
    return CoreEvent(
        event_type=EventType.CONTEXT_INJECT,
        data={"source": source, "count": count},
    )


def context_write_event(
    agent_name: str,
    key: str,
    priority: str,
) -> CoreEvent:
    """Emitted when an agent writes to the context bus."""
    return CoreEvent(
        event_type=EventType.CONTEXT_WRITE,
        agent_name=agent_name,
        data={"key": key, "priority": priority},
    )


def context_conflict_event(
    key: str,
    existing_agent: str,
    new_agent: str,
) -> CoreEvent:
    """Emitted when two agents write to the same context key."""
    return CoreEvent(
        event_type=EventType.CONTEXT_CONFLICT,
        data={
            "key": key,
            "existing_agent": existing_agent,
            "new_agent": new_agent,
        },
    )


# -- Session Event Constructors (W2) -------------------------------------------
# The single-agent turn. Same flat CoreEvent type, same data-dict discipline:
# everything a renderer needs lives in `data`, and `data` stays JSON-friendly so
# the --json headless surface (W10) can serialise an event without special cases.


def token_event(text: str, *, agent: str | None = None) -> CoreEvent:
    """Emitted for each chunk of visible response text in a session turn.

    This is the hot path. Publish it with :meth:`EventBus.emit_nowait` so a slow
    subscriber cannot backpressure the model stream.
    """
    return CoreEvent(
        event_type=EventType.TOKEN,
        agent_name=agent or "",
        data={"text": text},
    )


def thinking_event(text: str, *, agent: str | None = None) -> CoreEvent:
    """Emitted for each chunk of model thinking (the inside of a <think> block).

    Thinking is *produced* unconditionally and *shown* by the front-end. Whether
    the user sees it is a rendering decision (`show_thinking`), never a reason to
    destroy the content inside core -- that is what made thinking unavailable to
    the TUI and to any future GUI.
    """
    return CoreEvent(
        event_type=EventType.THINKING,
        agent_name=agent or "",
        data={"text": text},
    )


def tool_call_event(
    name: str,
    args: dict[str, Any],
    call_id: str,
    agent: str | None = None,
) -> CoreEvent:
    """Emitted the moment a tool call is parsed out of the stream.

    Fired BEFORE approval and BEFORE execution, so a UI can show a pending card
    while the arguments are still arriving. Pair it with the matching
    :func:`tool_result_event` on ``call_id``.
    """
    return CoreEvent(
        event_type=EventType.TOOL_CALL,
        agent_name=agent or "",
        data={"name": name, "args": args, "call_id": call_id},
    )


def tool_result_event(
    call_id: str,
    outcome: ToolOutcome | str,
    *,
    name: str = "",
    agent: str | None = None,
) -> CoreEvent:
    """Emitted when a tool call finishes, successfully or not.

    ``outcome`` is a :class:`djcode.core.outcome.ToolOutcome` once W3 rewires
    ``dispatch_tool``. Until then every dispatch path still yields a plain ``str``
    (``tools/__init__.py:58`` and ``workflow.py:234`` both stringify), so this
    factory is duck-typed and degrades cleanly:

    * ``content``  -- ``str(outcome)`` either way (``ToolOutcome.__str__``).
    * ``ok``       -- the real flag when present. For a bare ``str`` there is no
      honest answer, so it is ``True``; a renderer that needs better than that in
      the W2->W3 interim keeps its own "error"/"traceback" prefix sniff and drops
      it the day ``ToolOutcome`` arrives. Do NOT sniff here: computing ``ok`` from
      the content string is exactly the bug W3-1 removes.
    * ``details`` / ``spill_path`` / ``duration_ms`` -- empty until W3.
    """
    return CoreEvent(
        event_type=EventType.TOOL_RESULT,
        agent_name=agent or "",
        data={
            "call_id": call_id,
            "name": name,
            "content": str(outcome),
            "ok": bool(getattr(outcome, "ok", True)),
            "details": dict(getattr(outcome, "details", None) or {}),
            "spill_path": getattr(outcome, "spill_path", None),
            "duration_ms": int(getattr(outcome, "duration_ms", 0) or 0),
        },
    )


def diff_event(diff: Any, *, agent: str | None = None) -> CoreEvent:
    """Emitted when a tool call changed a file.

    ``diff`` is a ``FileDiff`` (``djcode.core.diff``), which exists since W7.
    Still duck-typed rather than imported: this module sits underneath
    ``core.diff`` in the import order and a hard dependency here would put a
    cycle between the event factory and the data it carries, for no gain.

    ``data["diff"]`` carries the object itself, so an in-process renderer gets
    ``before_text``/``after_text`` and can lex a hunk with the leading context
    it needs. ``data["summary"]`` is the same thing through ``as_dict()``:
    bounded, body-free and JSON-serialisable, which is what W10 writes to JSONL
    and what a socket-attached GUI receives. ``data["path"]`` stays for the
    callers written against the original shape.
    """
    as_dict = getattr(diff, "as_dict", None)
    summary = as_dict() if callable(as_dict) else {}
    return CoreEvent(
        event_type=EventType.DIFF,
        agent_name=agent or "",
        data={
            "path": str(getattr(diff, "path", "") or ""),
            "diff": diff,
            "summary": summary,
        },
    )


def checkpoint_event(checkpoint: Any, *, agent: str | None = None) -> CoreEvent:
    """Emitted when a checkpoint is captured or restored.

    ``checkpoint`` is a ``Checkpoint`` from W5 (``djcode.core.checkpoints``),
    which does not exist yet -- duck-typed for the same reason as
    :func:`diff_event`.
    """
    ident = getattr(checkpoint, "checkpoint_id", None) or getattr(checkpoint, "id", "")
    return CoreEvent(
        event_type=EventType.CHECKPOINT,
        agent_name=agent or "",
        data={"checkpoint_id": str(ident or ""), "checkpoint": checkpoint},
    )


def complete_event(
    response: str = "",
    *,
    agent: str | None = None,
    tool_rounds: int = 0,
    elapsed_s: float = 0.0,
) -> CoreEvent:
    """Emitted once, at the end of a session turn that finished normally."""
    return CoreEvent(
        event_type=EventType.COMPLETE,
        agent_name=agent or "",
        data={
            "response": response,
            "tool_rounds": tool_rounds,
            "elapsed_s": round(elapsed_s, 3),
        },
    )


def error_event(
    error: str,
    *,
    agent: str | None = None,
    kind: str = "",
    recoverable: bool = False,
) -> CoreEvent:
    """Emitted when a session turn fails.

    ``kind`` is a free-form classification string (``djcode.errors.classify_error``
    produces suitable values). Core never raises a front-end framework's exception
    type -- errors leave core as events or as ``djcode.errors`` types, never as
    ``click.ClickException``.
    """
    return CoreEvent(
        event_type=EventType.ERROR,
        agent_name=agent or "",
        data={"error": error, "kind": kind, "recoverable": recoverable},
    )


# -- Event Bus -----------------------------------------------------------------

EventCallback = Callable[[CoreEvent], Coroutine[Any, Any, None]]


class EventBus:
    """Async event bus for orchestrator events.

    TUI components and loggers subscribe to events via callbacks.
    Thread-safe — callbacks are dispatched sequentially within each emit call
    but multiple emits can overlap safely (each creates its own gather).
    """

    def __init__(self) -> None:
        self._callbacks: list[EventCallback] = []
        self._history: list[CoreEvent] = []
        self._max_history: int = 1000
        # Strong references to tasks spawned by emit_nowait. asyncio only keeps a
        # weak reference to a running task, so without this a fire-and-forget
        # callback can be garbage-collected mid-flight.
        self._pending: set[asyncio.Task[None]] = set()

    def subscribe(self, callback: EventCallback) -> None:
        """Register a callback for all events."""
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def unsubscribe(self, callback: EventCallback) -> None:
        """Remove a callback."""
        self._callbacks = [cb for cb in self._callbacks if cb is not callback]

    async def emit(self, event: CoreEvent) -> None:
        """Dispatch an event to all subscribers.

        Errors in individual callbacks are caught and logged, never propagated.
        """
        self._record(event)

        for cb in self._callbacks:
            try:
                await cb(event)
            except Exception:
                logger.exception("Event callback error for %s", event.event_type.value)

    def emit_nowait(self, event: CoreEvent) -> None:
        """Dispatch an event without waiting for subscribers.

        For the hot paths -- TOKEN and THINKING, one event per model chunk -- and
        for the places where awaiting is actively unsafe: a cancelled task's
        ``except asyncio.CancelledError`` handler, where an ``await`` can raise a
        second ``CancelledError`` and abort the tool-protocol repair half-done.

        History is recorded synchronously, so ``history`` is authoritative the
        instant this returns. Callbacks are scheduled as independent tasks and
        their exceptions are logged, never propagated -- identical error
        semantics to :meth:`emit`, minus the ordering guarantee.

        Contract differences from :meth:`emit`, both deliberate:

        * **No ordering guarantee between subscribers.** Each callback runs in
          its own task. Per-subscriber ordering IS preserved for a single-threaded
          event loop, because the tasks are created in emit order and each
          callback's first step runs in that order.
        * **No running loop -> no dispatch.** Outside an event loop the event is
          still recorded in history and the callbacks are skipped rather than
          raising. A synchronous caller has no loop to schedule on, and an
          exception here would take down whatever is streaming.
        """
        self._record(event)
        if not self._callbacks:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "emit_nowait outside an event loop; %s recorded but not dispatched",
                event.event_type.value,
            )
            return
        for cb in tuple(self._callbacks):
            task = loop.create_task(self._safe_call(cb, event))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    async def _safe_call(self, cb: EventCallback, event: CoreEvent) -> None:
        """Run one subscriber, swallowing its failure the way emit() does."""
        try:
            await cb(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Event callback error for %s", event.event_type.value)

    def _record(self, event: CoreEvent) -> None:
        """Append to the bounded history ring buffer."""
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history :]

    @property
    def history(self) -> list[CoreEvent]:
        """Recent event history (up to max_history)."""
        return list(self._history)

    def clear_history(self) -> None:
        """Clear event history."""
        self._history.clear()

    @property
    def subscriber_count(self) -> int:
        return len(self._callbacks)
