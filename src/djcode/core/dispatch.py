"""DispatchContext -- everything the one chokepoint is allowed to know.

``dispatch_tool`` is the single enforcement point for all 24 tools. To enforce
anything it needs a caller-supplied environment: which hooks are registered,
which permission engine to consult, where to record checkpoints, which session
owns the spill directory, and which directory the call is relative to.

EVERY FIELD IS OPTIONAL AND EVERY FIELD DEFAULTS TO A NO-OP. That is the whole
compatibility story for W3: ``dispatch_tool(name, args)`` with no context still
runs the tool, still catches exceptions, still returns a ``ToolOutcome``. The
13 direct call sites in ``src/`` and the two test patches keep working with no
signature edit at all.

HOW A CONTEXT REACHES THE CHOKEPOINT
------------------------------------
Two ways, and the second is the one that works on the production path:

1. ``dispatch_tool(name, args, ctx=ctx)`` -- explicit; what the tests use.
2. ``with dispatch_context(ctx): ...`` -- a ``ContextVar``, the same pattern
   ``capabilities.capability_context`` and ``agent_spawn.agent_context``
   already use in this codebase.

(2) exists because the production path does NOT call ``dispatch_tool``
directly: ``Operator`` hands the bare function to ``WorkflowEngine.one``, which
calls it positionally. Binding a ``partial(dispatch_tool, ctx=...)`` there would
break ``tests/test_capabilities.py``'s cancellation guard, which monkeypatches
``dispatch_tool`` with ``async def hang(*args)`` -- a signature that accepts no
keyword arguments at all, so ``hang(name, args, ctx=ctx)`` raises ``TypeError``.
A ContextVar changes no call signature, so the monkeypatch keeps working and
the context still flows.

ContextVar inheritance is deliberate: a task created inside the ``with`` block
(a background agent, one of ``parallel_execute``'s children) inherits the same
``DispatchContext`` OBJECT and therefore shares the doom-loop window. For the
doom loop that is the safe direction -- interleaved distinct calls can only
*prevent* the breaker from firing, never cause a false positive, because the
breaker requires three byte-identical consecutive signatures.

WHAT IS NOT HERE YET
--------------------
``permissions`` is typed ``Any`` because ``djcode.core.permissions`` is W6, and
``checkpoints`` because ``djcode.core.checkpoints`` is W5. W2's rule applies:
do not annotate against a module that does not exist. Both slots are read by
``dispatch_tool``'s numbered steps, which are no-ops until those waves land.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from djcode.core.hooks import HookBus

#: Three byte-identical consecutive calls trip the breaker -- the third one is
#: refused, per the W3 green gate ("doom-loop triggers on the third identical
#: call"). Calls one and two run normally: a model reading a file twice is not
#: yet a loop.
DOOM_LOOP_THRESHOLD = 3


def _opaque(obj: Any) -> str:
    """Stand-in for an argument value ``json`` cannot encode.

    Deliberately type-only. ``repr()`` was the obvious choice and is wrong:
    ``repr(object())`` embeds the address, so two passes of the same loop could
    produce different signatures and the breaker would never fire. Type-only
    makes two DIFFERENT objects of the same type compare equal, which can only
    make the breaker more eager -- the safe direction. In practice this never
    runs: every real argument dict arrives from ``json.loads`` of the model's
    ``function.arguments``.
    """
    return f"<{type(obj).__module__}.{type(obj).__qualname__}>"


def canonical_arguments(arguments: Any) -> str:
    """A stable text form of a call's arguments, for identity comparison only.

    ``sort_keys`` is required, not cosmetic: a model that emits
    ``{"path": ..., "content": ...}`` and then ``{"content": ..., "path": ...}``
    is making the SAME call, and without sorting the loop would run forever.
    Lists stay order-sensitive, which is correct -- ``parallel_execute`` with
    reordered ``tool_calls`` is a different call.

    Serialisation failure FAILS OPEN. A circular reference raises ``ValueError``
    even with ``default=`` set, and deep nesting raises ``RecursionError``; both
    return a fresh uuid that can never match anything, so the call loses
    doom-loop protection rather than being wrongly blocked or crashing the
    chokepoint.
    """
    try:
        return json.dumps(
            arguments,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=_opaque,
        )
    except (TypeError, ValueError, RecursionError):
        return uuid.uuid4().hex


def call_signature(name: str, arguments: Any) -> str:
    """Hash of ``(name, canonical args)``.

    Hashed rather than stored because ``file_write``'s ``content`` and
    ``file_edit``'s ``old_string`` are unbounded; keeping three raw canonical
    strings alive would retain megabytes of user content in a long session for
    no reason.
    """
    digest = hashlib.sha256()
    digest.update(name.encode("utf-8", "replace"))
    digest.update(b"\x00")
    digest.update(canonical_arguments(arguments).encode("utf-8", "replace"))
    return digest.hexdigest()


@dataclass(slots=True)
class DispatchContext:
    """The chokepoint's view of its caller. Every field optional, every default a no-op."""

    #: Hook seam (W3-4). ``None`` means no hooks fire at all.
    hooks: HookBus | None = None
    #: ``PermissionEngine`` once W6 exists. Read by ``dispatch_tool``'s step 2,
    #: which is a no-op stub until then.
    permissions: Any | None = None
    #: ``CheckpointStore`` once W5 exists. Read by steps 3 and 5, both stubs.
    checkpoints: Any | None = None
    #: Owns the per-session spill directory W3-2 writes into. ``None`` means the
    #: spill writer must pick a process-level fallback bucket rather than let
    #: ``None`` become the literal string "None" inside a path.
    session_id: str | None = None
    #: Directory tool calls are relative to.
    cwd: str | None = None
    #: Consulted when the doom-loop breaker fires, as
    #: ``await approval_callback(name, arguments, "doom_loop")``. Three
    #: parameters, not two: the reason is first-class because "the model is
    #: looping" is a different question from "may I write this file", and a
    #: front-end must be able to word the prompt differently. W6 replaces this
    #: with ``PermissionEngine`` returning a ``Decision``.
    approval_callback: Callable[[str, dict[str, Any], str], Awaitable[bool]] | None = None

    _recent: deque[str] = field(
        default_factory=lambda: deque(maxlen=DOOM_LOOP_THRESHOLD), init=False, repr=False
    )

    def note_call(self, name: str, arguments: Any) -> bool:
        """Record a call and report whether it just completed a doom loop.

        Called at the TOP of ``dispatch_tool``, before the first ``await``.
        That ordering matters: with background agents and ``parallel_execute``
        sharing an event loop, recording after the handler returned would
        interleave calls from different logical sequences into a false
        "consecutive" run.

        Firing CLEARS the window. Without that, the deque still holds three
        identical entries after the user approves, so call four trips again,
        and five, forever -- the breaker would become its own doom loop.
        """
        signature = call_signature(name, arguments)
        self._recent.append(signature)
        if len(self._recent) < DOOM_LOOP_THRESHOLD:
            return False
        if any(entry != signature for entry in self._recent):
            return False
        self._recent.clear()
        return True

    def forget_calls(self) -> None:
        """Drop the doom-loop window (e.g. at a turn boundary)."""
        self._recent.clear()


_ACTIVE: ContextVar[DispatchContext | None] = ContextVar("djcode_dispatch_context", default=None)


def active_context() -> DispatchContext | None:
    """The context installed by the innermost enclosing ``dispatch_context``."""
    return _ACTIVE.get()


@contextmanager
def dispatch_context(ctx: DispatchContext | None):
    """Install ``ctx`` for every ``dispatch_tool`` call made inside the block."""
    token = _ACTIVE.set(ctx)
    try:
        yield ctx
    finally:
        _ACTIVE.reset(token)
