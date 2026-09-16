"""HookBus -- the veto seam the chokepoint fires, and nothing more.

W3-4 ships the SEAM. It does not ship handlers.

The blueprint's P1-1 (command judges, prompt judges, content-hash trust pinning)
is a later wave. What W3 owes it is a place to hang: one bus, two events, one
result type, and a call in ``dispatch_tool`` at a point where a veto still
matters -- before the handler runs and therefore before any side effect.

Why the seam ships now rather than with the handlers: ``dispatch_tool`` grows
the same forty lines in W3, W5, W6 and P1-1. Building the fire points once, with
no handlers behind them, costs nine lines and saves three rewrites of the
enforcement order.

DESIGN NOTES THAT ARE LOAD-BEARING
----------------------------------
* ``PRE_TOOL_USE`` can veto. ``POST_TOOL_USE`` cannot: the side effect has
  already happened, so ``allow`` on a post result is advisory only. It is
  recorded in ``ToolOutcome.details`` and never used to rewrite history.
* A handler that raises does NOT block the tool. A broken hook must not brick
  every tool call in the session; the failure is logged and recorded in the
  result's ``details``. A hook that wants to block says so by returning
  ``HookResult(allow=False)``. If a later wave wants fail-closed hooks it must
  be an explicit per-handler flag, decided deliberately -- not an accident of
  exception handling.
* ``asyncio.CancelledError`` is re-raised, never absorbed. Same rule as the
  chokepoint: ``Operator.send``'s cancellation bookkeeping depends on it.
* No terminal imports, no ``print``. This module lives in ``djcode.core`` and
  ``tests/test_headless_purity.py`` enforces that.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class HookEvent(StrEnum):
    """The points in a tool call where a hook may observe or intervene."""

    #: Fired before the handler runs. A handler may veto here, and a veto means
    #: the tool never executes -- no side effect, no checkpoint, no diagnostics.
    PRE_TOOL_USE = "PreToolUse"

    #: Fired after the handler returned and the outcome was built. Advisory:
    #: the side effect already happened, so ``allow`` cannot undo anything.
    POST_TOOL_USE = "PostToolUse"


@dataclass(slots=True)
class HookResult:
    """A hook's verdict. Defaults to "allow", so a silent bus never blocks."""

    allow: bool = True
    #: Why a veto happened. Goes back to the MODEL as the tool result, so write
    #: it as an instruction the model can act on, not as a log line.
    reason: str = ""
    #: Structured payload for front-ends. Must stay JSON-serialisable -- it is
    #: merged into ``ToolOutcome.details``, which W10 serialises to JSONL.
    details: dict[str, Any] = field(default_factory=dict)


#: A hook handler takes ``(event, payload)`` and returns a ``HookResult`` (or
#: ``None``, meaning "no opinion"). Sync and async handlers are both accepted:
#: a config-driven matcher is pure CPU and should not be forced to be async.
HookHandler = Callable[[HookEvent, dict[str, Any]], Any]


class HookBus:
    """Fan-out for hook handlers, with first-veto-wins semantics.

    Empty by default. ``fire`` on a bus with no handlers for the event returns
    ``HookResult(allow=True)`` without awaiting anything, which is why
    ``dispatch_tool`` can call it unconditionally.
    """

    __slots__ = ("_handlers",)

    def __init__(self) -> None:
        self._handlers: dict[HookEvent, list[HookHandler]] = {}

    def register(self, event: HookEvent, handler: HookHandler) -> None:
        """Add a handler. Handlers run in registration order."""
        self._handlers.setdefault(HookEvent(event), []).append(handler)

    def handlers(self, event: HookEvent) -> tuple[HookHandler, ...]:
        """The handlers registered for ``event``, in order. Read-only."""
        return tuple(self._handlers.get(HookEvent(event), ()))

    async def fire(self, event: HookEvent, payload: dict[str, Any]) -> HookResult:
        """Run every handler for ``event``; the first veto wins.

        Returns ``HookResult(allow=True)`` when no handler objects -- including
        when no handler is registered at all, which is the W3 default and the
        reason this seam is free to call on every tool.
        """
        event = HookEvent(event)
        handlers = self._handlers.get(event)
        if not handlers:
            return HookResult()

        merged: dict[str, Any] = {}
        for handler in handlers:
            try:
                result = handler(event, payload)
                if inspect.isawaitable(result):
                    result = await result
            except asyncio.CancelledError:
                # Never absorbed. See the module docstring.
                raise
            except Exception as exc:  # a broken hook must not brick the tool
                logger.warning(
                    "%s hook %r raised %s: %s",
                    event.value,
                    getattr(handler, "__name__", handler),
                    type(exc).__name__,
                    exc,
                )
                merged.setdefault("hook_errors", []).append(
                    {"handler": getattr(handler, "__name__", repr(handler)),
                     "exception": type(exc).__name__}
                )
                continue
            if result is None:
                continue
            if result.details:
                merged.update(result.details)
            if not result.allow:
                return HookResult(allow=False, reason=result.reason, details=merged)
        return HookResult(allow=True, details=merged)
