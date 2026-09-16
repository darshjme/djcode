"""DJcode tool system -- local-first file, shell, and git operations.

``dispatch_tool`` below is THE chokepoint. All 24 dispatchable names pass
through it, so it is the only place in the codebase where a hook, a permission
check, a checkpoint, an output bound or a loop breaker has to be written once to
cover every tool. Blueprint section 2.4 fixes the order of those steps; the
numbered comments in the function body are that order, and steps whose owning
wave has not landed yet are present as explicitly-marked no-ops rather than
absent -- building them as five separate passes would mean five rewrites of the
same forty lines.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Mapping
from functools import partial
from typing import Any

from djcode.capabilities import dispatch_capability
from djcode.core.dispatch import DOOM_LOOP_THRESHOLD, DispatchContext, active_context
from djcode.core.hooks import HookEvent
from djcode.core.outcome import ToolOutcome
from djcode.scheduler import schedule_tool
from djcode.tools.agent_spawn import execute_agent_status, execute_spawn_agent
from djcode.tools.bash import execute_bash
from djcode.tools.file_edit import execute_file_edit
from djcode.tools.file_read import execute_file_read
from djcode.tools.file_write import execute_file_write
from djcode.tools.git import execute_git
from djcode.tools.glob import execute_glob
from djcode.tools.grep import execute_grep
from djcode.tools.notebook import execute_notebook_edit, execute_notebook_read
from djcode.tools.parallel_exec import execute_parallel
from djcode.tools.task_tracker import execute_task_create, execute_task_list, execute_task_update
from djcode.tools.web_fetch import execute_web_fetch
from djcode.tools.web_search import execute_web_search

# Central dispatch table
TOOL_DISPATCH: dict[str, Any] = {
    "bash": execute_bash,
    "file_read": execute_file_read,
    "file_write": execute_file_write,
    "file_edit": execute_file_edit,
    "grep": execute_grep,
    "glob": execute_glob,
    "git": execute_git,
    "web_fetch": execute_web_fetch,
    "web_search": execute_web_search,
    "task_create": execute_task_create,
    "task_update": execute_task_update,
    "task_list": execute_task_list,
    "notebook_read": execute_notebook_read,
    "notebook_edit": execute_notebook_edit,
    "parallel_execute": execute_parallel,
    "spawn_agent": execute_spawn_agent,
    "agent_status": execute_agent_status,
    "schedule": schedule_tool,
}

# The six capability tools share one dispatcher, keyed by the tool name.
for _name in ("skill", "mcp", "process", "browser", "computer", "workflow"):
    TOOL_DISPATCH[_name] = partial(dispatch_capability, _name)


# ---------------------------------------------------------------------------
# How honest is ``ToolOutcome.ok``?  Read this before trusting it.
# ---------------------------------------------------------------------------
#
# W3-1 says ``ok`` is computed from the handler, never sniffed out of the
# content text. That rule is right and it is enforced below -- there is no
# ``content.lower().startswith("error")`` anywhere in this file. But it exposes
# an uncomfortable truth about the handlers, measured tool by tool:
#
#   SEVEN of the 24 tools raise on failure. For those, returning normally is a
#   VERIFIED success and ``ok=True`` means what it says.
#
#   SEVENTEEN of the 24 signal failure ONLY by returning a string, and three of
#   those do not even use the word "Error" when they fail:
#       tools/grep.py    -> "Search timed out after 30s"
#       tools/git.py     -> "Warning: '<cmd>' is a destructive operation..."  (a refusal)
#       tools/bash.py    -> "[exit code 1]\n..."
#   while two lie the other way -- ``web_search``'s "Error: No results from
#   Brave Search" is an empty result used as internal control flow, and
#   ``file_edit``'s "Already applied: ..." is a SUCCESS that never says "Edited".
#
# So for those seventeen the dispatcher has no failure signal at all, and there
# are only two honest options: guess, or say so. This file says so.
# ``details["ok_source"]`` carries which one you got:
#
#   "handler"     the handler's own verdict -- either it raises on failure and
#                 did not raise, or it returned a ``ToolOutcome`` itself.
#   "unverified"  ``ok=True`` means "dispatch observed no failure", NOT "the
#                 tool succeeded". Seventeen tools are here today.
#   "raised"      the handler raised; ``ok=False`` is a real verdict.
#   "dispatch"    the chokepoint itself refused (unknown tool, hook veto,
#                 doom loop); ``ok=False`` is a real verdict.
#
# An unverified ``True`` is honest. A sniffed ``True``/``False`` is a guess
# wearing a structured field's clothes, and it is exactly the bug P0-8 exists to
# delete. Callers that need better than "unverified" in the interim keep their
# own prefix sniff and remove it in the wave that fixes each handler -- the same
# policy ``core/events.py::tool_result_event`` already documents. Every such
# interim sniff left in the tree is marked ``W3 interim``.
#
# The fix is per-handler and it is not W3's: each of the seventeen has to return
# a ``ToolOutcome`` (or raise) instead of an "Error: ..." string. W7-2 already
# schedules the first two (``file_edit``/``file_write`` must return pre/post in
# ``details``). ``UNVERIFIED_FAILURE_TOOLS`` below is the counter for that work
# and ``tests/test_dispatch_chokepoint.py`` asserts it only ever shrinks.

#: Tools whose handler RAISES on failure, so a normal return is a verified
#: success. Six capability tools plus ``schedule``; each was read to confirm it,
#: not assumed. (``workflow`` is a partial exception: it raises for its own
#: failures, but its happy path JSON-encodes child node results, so ``ok=True``
#: there means "the graph ran", not "every node succeeded".)
STRUCTURED_FAILURE_TOOLS = frozenset(
    {"browser", "computer", "mcp", "process", "schedule", "skill", "workflow"}
)

#: The seventeen tools that still report failure as text. This set must only
#: ever get smaller.
UNVERIFIED_FAILURE_TOOLS = frozenset(TOOL_DISPATCH) - STRUCTURED_FAILURE_TOOLS


def _is_bind_error(handler: Any, arguments: Mapping[str, Any]) -> bool:
    """Did ``TypeError`` come from calling the handler wrong, or from inside it?

    Worth distinguishing: "the model invented an argument name" is a different
    repair from "the tool broke", and the second kind hides behind the first.
    (``app.py:1730`` calls ``task_create`` with ``title=`` when the handler takes
    ``subject=``; the old swallow-into-an-f-string dispatcher has been hiding
    that broken TUI command.) Only reached on the error path, so the
    ``signature`` lookup costs nothing normally. An unintrospectable handler is
    reported as an ordinary handler failure.
    """
    try:
        inspect.signature(handler).bind(**arguments)
    except TypeError:
        return True
    except Exception:
        return False
    return False


def _build_outcome(
    name: str,
    raw: Any,
    *,
    duration_ms: int,
    extra_details: Mapping[str, Any],
) -> ToolOutcome:
    """Step 8: turn whatever the handler returned into one ``ToolOutcome``.

    A handler may already return a ``ToolOutcome`` -- nothing does today, and
    that is the seam W7-2 uses to put a file's pre/post image into ``details``
    without touching this function.
    """
    if isinstance(raw, ToolOutcome):
        outcome = raw
        outcome.details.setdefault("ok_source", "handler")
    else:
        outcome = ToolOutcome(
            content=str(raw),
            ok=True,
            details={
                "ok_source": "handler" if name in STRUCTURED_FAILURE_TOOLS else "unverified"
            },
        )
    outcome.details.setdefault("tool", name)
    for key, value in extra_details.items():
        outcome.details.setdefault(key, value)
    outcome.duration_ms = outcome.duration_ms or duration_ms
    return outcome


async def dispatch_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    ctx: DispatchContext | None = None,
) -> ToolOutcome:
    """Execute one tool, under whatever enforcement ``ctx`` supplies.

    ``ctx`` is optional and every field inside it is optional, so every existing
    caller -- ``ra.py``, ``app.py``, ``capabilities.py``, ``parallel_exec.py``
    and the two test patches -- keeps working with no edit to the call. A
    context can also be installed for a whole block with
    ``djcode.core.dispatch.dispatch_context``, which is how the production path
    (``Operator`` -> ``WorkflowEngine.one`` -> this function, called
    positionally) gets one without a signature change.

    Returns a ``ToolOutcome``, never a bare ``str``. ``ToolOutcome.__str__``
    returns ``content``, so ``str(await dispatch_tool(...))`` is byte-identical
    to the old return value -- but anything that calls ``.startswith`` or slices
    the result now needs ``str(...)`` around it first. See ``ok_source`` above
    before trusting ``ok``.

    ``asyncio.CancelledError`` propagates. It is not an ``Exception`` subclass,
    so the ``except Exception`` clause below never caught it and the old code
    was already correct here; the explicit re-raise is there so that the next
    person who widens that clause to ``BaseException`` has to delete a comment
    saying why they must not. ``Operator.send``'s cancellation bookkeeping
    (``operator.py:186-209``, which synthesises a ``role="tool"`` message for
    every unanswered ``tool_call`` id) only runs if the cancel reaches it; swallow
    it here and Ctrl+C silently becomes "tool failed, carry on to the next
    billed round-trip".
    """
    started = time.monotonic()
    if ctx is None:
        ctx = active_context()
    handler = TOOL_DISPATCH.get(name)

    def elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    def refused(text: str, *, by: str, extra: Mapping[str, Any]) -> ToolOutcome:
        details: dict[str, Any] = {"tool": name, "ok_source": "dispatch", "refused_by": by}
        details.update(extra)
        return ToolOutcome(content=text, ok=False, details=details, duration_ms=elapsed_ms())

    hook_details: dict[str, Any] = {}
    payload: dict[str, Any] = {
        "name": name,
        "arguments": arguments,
        "session_id": getattr(ctx, "session_id", None),
        "cwd": getattr(ctx, "cwd", None),
    }

    # 1. PreToolUse hook (P1-1 seam, shipped in W3-4). A veto here means the
    #    tool never runs, so no side effect, no checkpoint, no diagnostics.
    if ctx is not None and ctx.hooks is not None:
        verdict = await ctx.hooks.fire(HookEvent.PRE_TOOL_USE, payload)
        hook_details.update(verdict.details)
        if not verdict.allow:
            return refused(
                verdict.reason or f"Error: '{name}' was blocked by a PreToolUse hook.",
                by="hook",
                extra=hook_details,
            )

    # 2. Permission evaluation (P0-2). W6 builds the engine; what exists today
    #    is the half of the rule that must never wait for it -- an unknown tool
    #    FAILS CLOSED. Do not move this below the handler call: "unknown" is the
    #    one verdict this layer can reach on its own.
    if handler is None:
        return refused(f"Error: Unknown tool '{name}'", by="unknown_tool", extra=hook_details)
    if not isinstance(arguments, Mapping):
        return refused(
            f"Error executing {name}: arguments must be a JSON object, "
            f"got {type(arguments).__name__}",
            by="bad_arguments",
            extra=hook_details,
        )
    # W6 STUB: ctx.permissions.evaluate(ToolRequest(...)) -> HARDLINE_BLOCK | ALLOW | ASK.
    # The policy must always evaluate and the mode must govern only whether a
    # prompt is shown (blueprint W6-1). Until then this is deliberately empty
    # rather than a permissive placeholder that a later wave might forget.

    # 2b. Doom-loop breaker (P1-9, W3-5). Recorded before the first await so
    #     that concurrent children cannot forge a false "consecutive" run.
    if ctx is not None and ctx.note_call(name, arguments):
        approved = False
        if ctx.approval_callback is not None:
            approved = bool(await ctx.approval_callback(name, dict(arguments), "doom_loop"))
        if not approved:
            return refused(
                f"Error: '{name}' was called {DOOM_LOOP_THRESHOLD} times in a row with "
                f"byte-identical arguments and was not executed again. Nothing about the "
                f"result has changed; change the approach instead of repeating the call.",
                by="doom_loop",
                extra={**hook_details, "doom_loop": True},
            )

    # 3. Checkpoint pre-image capture (P0-1). W5 STUB -- reads the target's
    #    pre-image for file_write/file_edit/notebook_edit and takes the bash
    #    mtime+size ledger, via ctx.checkpoints. Nothing to capture yet.

    # 4. The handler. Everything above this line can refuse; nothing below it
    #    can, because from here on the side effect has happened.
    try:
        raw = await handler(**arguments)
    except asyncio.CancelledError:
        # MUST propagate -- see the docstring. Redundant against `except
        # Exception` (CancelledError is a BaseException on 3.12) and kept
        # deliberately so the invariant is stated, not inferred.
        raise
    except TypeError as exc:
        phase = "bind" if _is_bind_error(handler, arguments) else "handler"
        return ToolOutcome(
            content=f"Error executing {name}: {exc}",
            ok=False,
            details={
                **hook_details,
                "tool": name,
                "ok_source": "raised",
                "exception": "TypeError",
                "phase": phase,
            },
            duration_ms=elapsed_ms(),
        )
    except Exception as exc:
        return ToolOutcome(
            content=f"Error executing {name}: {exc}",
            ok=False,
            details={
                **hook_details,
                "tool": name,
                "ok_source": "raised",
                "exception": type(exc).__name__,
            },
            duration_ms=elapsed_ms(),
        )

    # 5. Checkpoint post-image + diff construction (P0-1/P0-3). W5/W7 STUB.
    # 6. Post-edit diagnostics (P1-3). W11 STUB.
    # 7. Bounded output + spill file (P1-4). W3-2 -- the next stage of THIS
    #    wave -- replaces this comment with head-2000 + tail-2000 truncation and
    #    a spill file under CONFIG_DIR/tool-output/<session>/, setting
    #    outcome.spill_path and putting the absolute path in content too. It
    #    also deletes the five ad-hoc truncations in bash/grep/parallel_exec/
    #    notebook/agent_spawn, because this is where that policy lives now.
    #    Nothing else in this function moves when that lands.

    # 8. ToolOutcome construction (P0-8).
    outcome = _build_outcome(name, raw, duration_ms=elapsed_ms(), extra_details=hook_details)

    # 9. PostToolUse hook. Advisory only: the side effect already happened, so a
    #    veto here cannot undo it and is recorded rather than enforced.
    if ctx is not None and ctx.hooks is not None:
        post = await ctx.hooks.fire(HookEvent.POST_TOOL_USE, {**payload, "outcome": outcome})
        outcome.details.update(post.details)
        if not post.allow:
            outcome.details["post_hook_reason"] = post.reason
    return outcome
