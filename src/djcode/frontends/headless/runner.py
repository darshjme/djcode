"""``djcode --json`` -- one turn, machine-readable, no terminal assumptions.

P0-9 / W10-1. The third front-end. The REPL draws for a human, the Textual TUI
draws for a human in a full-screen frame, and this one draws for a program:
**JSONL on stderr, the final assistant message alone on stdout.**

WHY THE SPLIT IS THE POINT
--------------------------
``repl.run_oneshot`` writes every streamed token to stdout as it arrives, so::

    djcode "summarise this repo" > notes.md

produces a file containing the thinking, the tool chatter and the answer,
interleaved, with no way to separate them afterwards. Here stdout receives
``COMPLETE.data["response"]`` once, at the end, and nothing else -- so the same
redirect yields exactly the answer, and ``2>events.jsonl`` yields the full trace
beside it.

APPROVAL, WITH NOBODY THERE
---------------------------
There is no human on this surface, so there is nobody to ask. Two wrong answers
were available and both were rejected:

* *Imply ``--auto-accept``.* That is B4 rebuilt -- ``--json`` is an output format,
  and an output format must never widen what the agent is allowed to do.
* *Let ``Operator`` raise.* Without an approval callback and without a tty,
  ``Operator._approve_tool`` raises ``PermissionError``, which kills the turn on
  its first gated tool and produces a truncated stream with no ``COMPLETE``.

So this front-end supplies a callback that **denies, with a reason the model
reads**. The permission engine still runs first, so an allowlisted or read-only
tool proceeds untouched; only a call that would have opened a prompt is refused.
The refusal is visible on the wire as a ``permission_request`` /
``permission_decided`` pair, which is what lets a CI step report *which* tool it
needs to be trusted with. Pass ``--auto-accept`` and the engine's AUTO mode
resolves the same calls without ever reaching the callback -- the HARDLINE floor
W6 installed still applies, exactly as it does in the REPL.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, TextIO

from djcode.core.events import EventBus, EventType
from djcode.core.permissions import Decision, DecisionAction, ToolRequest
from djcode.core.session import CoreSession, SessionOptions
from djcode.errors import classify_error
from djcode.frontends.headless.serialise import EventWriter
from djcode.provider import Provider, ProviderConfig

logger = logging.getLogger(__name__)

#: What the model is told when a tool needed a human and there was none.
NO_HUMAN = (
    "Tool execution needs approval and --json runs unattended, so this call was "
    "refused. Continue without it, or the operator can re-run with --auto-accept."
)


def _denier(auto_accept: bool):
    """The approval callback for a surface with no human attached.

    ``None`` when ``--auto-accept`` is set: the engine resolves those calls in
    AUTO mode and never reaches a callback, and passing one anyway would put a
    deny in front of calls the operator explicitly authorised.
    """
    if auto_accept:
        return None

    async def decide(request: ToolRequest) -> Decision:
        return Decision(DecisionAction.DENY, comment=NO_HUMAN)

    return decide


async def run_headless(
    prompt: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    bypass_rlhf: bool = False,
    show_thinking: bool = True,
    auto_accept: bool = False,
    cwd: str | Path | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run one turn, stream JSONL to ``stderr``, print the answer to ``stdout``.

    Returns a process exit code: ``0`` if the turn completed, ``1`` if it failed,
    ``130`` if it was interrupted. Nothing here raises a ``click`` exception --
    a front-end owns its own exit convention, and ``cli.py`` translates this int.
    """
    out: TextIO = stdout if stdout is not None else sys.stdout
    err: TextIO = stderr if stderr is not None else sys.stderr
    writer = EventWriter(err)

    config = ProviderConfig.from_config(provider_override=provider, model_override=model)
    llm = Provider(config)
    options = SessionOptions(
        model=config.model,
        bypass_rlhf=bypass_rlhf,
        auto_accept=auto_accept,
        show_thinking=show_thinking,
        cwd=Path(cwd) if cwd is not None else Path(os.getcwd()),
    )
    session: CoreSession | None = None
    answer = ""
    code = 0
    try:
        ok, message = llm.validate_model()
        if not ok:
            writer.write(_error(message or "the configured model is not usable"))
            await llm.close()
            return 1
        if message:
            # Not an error and not the answer: a note, on the event stream where
            # a machine can ignore it. run_oneshot printed it to the console.
            writer.write(_note(message))
        session = CoreSession(
            {},
            provider=llm,
            event_bus=EventBus(),
            approval=_denier(auto_accept),
            options=options,
        )
        async for event in session.send(prompt):
            writer.write(event)
            if event.event_type is EventType.COMPLETE:
                answer = str(event.data.get("response", "") or "")
    except (KeyboardInterrupt, SystemExit):
        writer.write(_error("interrupted", kind="Interrupted"))
        code = 130
    except Exception as error:  # noqa: BLE001 - reported as an ERROR event, then as an exit code
        classified = classify_error(error)
        writer.write(
            _error(
                str(error) or type(error).__name__,
                kind=str(getattr(classified, "category", "") or type(error).__name__),
                recoverable=bool(getattr(classified, "recoverable", False)),
            )
        )
        code = 1
    finally:
        if session is not None:
            try:
                await session.close()
            except Exception:  # pragma: no cover - close must not mask the turn
                logger.debug("closing the headless session failed", exc_info=True)
        else:
            try:
                await llm.close()
            except Exception:  # pragma: no cover
                logger.debug("closing the provider failed", exc_info=True)

    if answer:
        # The whole reason this surface exists: stdout is the answer, alone.
        out.write(answer if answer.endswith("\n") else answer + "\n")
        flush = getattr(out, "flush", None)
        if callable(flush):
            flush()
    return code


def _error(text: str, *, kind: str = "", recoverable: bool = False) -> Any:
    from djcode.core.events import error_event

    return error_event(text, kind=kind, recoverable=recoverable)


def _note(text: str) -> Any:
    """A provider notice, carried as a recoverable ERROR rather than invented.

    Adding a NOTE event type to ``core.events`` for one string would be a core
    change made by a front-end, which is the direction this architecture forbids.
    ``recoverable=True`` with ``kind="notice"`` says exactly what it is and
    costs no new type.
    """
    from djcode.core.events import error_event

    return error_event(text, kind="notice", recoverable=True)


__all__ = ["NO_HUMAN", "run_headless"]
