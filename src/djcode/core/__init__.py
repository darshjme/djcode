"""djcode.core — the headless engine contract.

This package is the ENTIRE dependency surface a front-end needs. The REPL, the
Textual TUI, the headless JSONL stream and a future desktop GUI are all peers:
each one consumes this package and renders it however it likes.

The rule that makes that true, enforced by ``tests/test_headless_purity.py``:

    No module under ``djcode.core`` -- or anywhere in its import closure -- may
    import ``rich``, ``questionary``, ``prompt_toolkit``, ``textual`` or
    ``click``, or call ``print()`` / ``input()`` / ``sys.stdout.write()`` /
    ``sys.stderr.write()``.

``click`` is on that list deliberately. A GUI cannot do anything meaningful with
a CLI framework's exceptions, so errors leave core as ``djcode.errors`` types.

If a front-end needs something that is not exported here, the answer is to export
it here -- never to reach into ``djcode.repl`` or ``djcode.app``.

WHAT IS NOT HERE YET
--------------------
``SSOT.md`` section 2.3 specifies a larger contract than this file exports:
``CoreSession``/``SessionOptions`` (W8), ``PermissionEngine``/``Decision`` (W6),
``Checkpoint``/``CheckpointStore`` (W5) and ``FileDiff``/``Hunk`` (W7),
``OnboardingFlow`` (W8). None of those modules exist yet. Exporting names that
do not exist would make ``import djcode.core`` raise, so each is added to this
file by the wave that builds it -- not before. ``HookBus`` and
``DispatchContext`` were on that list until W3 built them; they are exported
below because they now exist.

The already-headless re-exports below were each verified to exist at the path
given. Note ``ContextStats`` and ``InjectedContext`` live in ``context.manager``,
NOT in ``context.models`` as the blueprint's section 2.3 sketch claims; the
sketch is wrong and reality wins.
"""

from __future__ import annotations

from djcode.agents.registry import AgentRole, AgentSpec
from djcode.config import load_config, save_config, set_value
from djcode.context.manager import ContextStats, ContextWindowManager, InjectedContext
from djcode.core.dispatch import DispatchContext, dispatch_context
from djcode.core.events import (
    CoreEvent,
    EventBus,
    EventType,
    OrchestratorEvent,
    checkpoint_event,
    complete_event,
    diff_event,
    error_event,
    thinking_event,
    token_event,
    tool_call_event,
    tool_result_event,
)
from djcode.core.hooks import HookBus, HookEvent, HookResult
from djcode.core.outcome import ToolOutcome
from djcode.errors import classify_error
from djcode.provider import TOOL_DEFINITIONS, Message, Provider, ProviderConfig
from djcode.sessions import Session, SessionDB

__all__ = [
    "TOOL_DEFINITIONS",
    "AgentRole",
    "AgentSpec",
    "ContextStats",
    "ContextWindowManager",
    "CoreEvent",
    "DispatchContext",
    "EventBus",
    "EventType",
    "HookBus",
    "HookEvent",
    "HookResult",
    "InjectedContext",
    "Message",
    "OrchestratorEvent",
    "Provider",
    "ProviderConfig",
    "Session",
    "SessionDB",
    "ToolOutcome",
    "checkpoint_event",
    "classify_error",
    "complete_event",
    "diff_event",
    "dispatch_context",
    "error_event",
    "load_config",
    "save_config",
    "set_value",
    "thinking_event",
    "token_event",
    "tool_call_event",
    "tool_result_event",
]
