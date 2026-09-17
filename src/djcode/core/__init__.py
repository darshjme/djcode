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
``CoreSession``/``SessionOptions`` (W8), ``FileDiff``/``Hunk`` (W7) and
``OnboardingFlow`` (W8). Those modules do not exist yet.
``PermissionEngine``/``Decision`` were on that list until W6 and are exported
below; note the module is pure data and logic, with no Rich anywhere -- the old
top-level ``permissions.py`` instantiated a ``Console()`` at import time, which
is exactly why it could never have lived in core.
``Checkpoint``/``CheckpointStore``/``RestoreReport`` were on that
list until W5 and are exported below.

SSOT 2.3 hangs the undo API off ``CoreSession.checkpoints()`` / ``.restore(id)``
-- but ``CoreSession`` is W8 and does not exist, so W5 cannot put it there. The
methods live on ``CheckpointStore`` (``checkpoints(session_id)``,
``restore(...)``, ``restore_ids(...)``) and W8 re-exposes them as the thin
``CoreSession`` facade over a store it already holds. Exporting names that
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
from djcode.core.checkpoints import (
    Checkpoint,
    CheckpointStore,
    FileChange,
    RestoreReport,
)
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
from djcode.core.permissions import (
    Decision,
    DecisionAction,
    Level,
    Mode,
    Narrowing,
    PermissionEngine,
    Resolution,
    Rule,
    ToolRequest,
    Verdict,
)
from djcode.errors import classify_error
from djcode.provider import TOOL_DEFINITIONS, Message, Provider, ProviderConfig
from djcode.sessions import Session, SessionDB

__all__ = [
    "TOOL_DEFINITIONS",
    "Decision",
    "DecisionAction",
    "Level",
    "Mode",
    "Narrowing",
    "PermissionEngine",
    "Resolution",
    "Rule",
    "ToolRequest",
    "Verdict",
    "AgentRole",
    "AgentSpec",
    "ContextStats",
    "ContextWindowManager",
    "Checkpoint",
    "CheckpointStore",
    "CoreEvent",
    "DispatchContext",
    "EventBus",
    "EventType",
    "FileChange",
    "HookBus",
    "HookEvent",
    "HookResult",
    "InjectedContext",
    "Message",
    "OrchestratorEvent",
    "Provider",
    "ProviderConfig",
    "RestoreReport",
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
