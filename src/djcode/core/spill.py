r"""P1-4 -- ONE output policy for all 24 tools, applied ONCE, at the chokepoint.

Before W3-2 five tools each invented their own bound, their own units and their
own sentence, and a sixth cut silently::

    tools/bash.py:14,58     50_000 bytes  -> "\n... (output truncated)"
    tools/git.py:60         30_000 bytes  -> same suffix, a third of bash's bound
    tools/grep.py:49-53     200 LINES     -> "... (200 matches shown, more truncated)"
    tools/parallel_exec.py  10_000 chars  -> "... (output truncated at 10000 chars)"
    tools/notebook.py       5_000 chars   -> "..." per output + "  ... (output truncated)"
    tools/web_fetch.py:22   10_000 chars  -> NOTHING. The model could not tell.

Six limits, five messages, four units, and in every one of them the discarded
text was gone for good: the model was told "there was more" and given no way to
read it. That is what this module replaces. ``dispatch_tool`` calls
``apply_output_policy`` once, on the way out, so the policy is identical for
``bash`` and for ``notebook_read`` and cannot drift again.

THE POLICY
----------
Output at or under ``SPILL_THRESHOLD`` is passed through untouched -- most tool
calls never reach this code at all. Above it:

* ``content`` becomes head ``HEAD_CHARS`` + tail ``TAIL_CHARS``. Both ends,
  never just the head: a build log's verdict is in the last ten lines and a
  traceback's cause is in the first.
* the FULL text is written to ``<CONFIG_DIR>/tool-output/<bucket>/<uuid4>.txt``.
* the ABSOLUTE path appears in ``outcome.spill_path`` AND inside ``content``.

Both, deliberately, and each for a different reader:

``spill_path`` is for the front-end, which must not have to parse English out of
``content`` to find it (``core/outcome.py`` forbids reconstructing structure by
sniffing content, and ``tool_result_event`` already ships the field).

``content`` is for the MODEL, which has no other channel -- ``ToolOutcome.__str__``
returns ``content`` alone and ``details`` is documented as never shown to it. If
the path were only in ``spill_path`` the model could never ``file_read`` the
elided text, and "truncated, and you cannot get it back" is the exact failure
P1-4 exists to end.

The path appears twice in ``content`` -- once in the elision marker between head
and tail, once as the last line. That is not an accident. ``parallel_execute``
and the ``workflow`` capability re-enter ``dispatch_tool``, so a large batch is
bounded once per child and again for the aggregate; the aggregate keeps its own
head and tail, so a marker sitting only in the middle is the one thing a nested
bound is guaranteed to drop. The trailing copy survives it.

ORDERING
--------
This runs AFTER the handler and after ``ToolOutcome`` construction, even though
blueprint section 2.4 numbers "bounded output" (7) before "ToolOutcome
construction" (8). A handler may return its own ``ToolOutcome`` -- that is the
seam W7-2 uses to attach a diff -- so bounding the raw handler value would miss
exactly the outcomes W7 produces. One call site, applied to the constructed
outcome, covers both shapes.

WINDOWS AND ``mode=0o700`` -- MEASURED, NOT ASSUMED
---------------------------------------------------
The blueprint says "dir mode 0o700 where the OS honours it". On this platform it
does not, and the honest thing is to say so rather than claim a permission bit
that does nothing::

    >>> d.mkdir(parents=True, exist_ok=True, mode=0o700)
    >>> oct(stat.S_IMODE(d.stat().st_mode))
    '0o777'                      # os.name == 'nt'; the mode argument is ignored

The directory inherits its parent's ACL instead, and on a real box that ACL is
not necessarily private -- ``icacls C:\Users\<user>\.djcode`` on the machine this
was written on carries an explicit ``CodexSandboxUsers:(OI)(CI)(RX)`` entry that
every spill file underneath inherits. Spill files hold raw tool output: a
``git diff``, a build log, a ``cat`` of a config file. So the ``mode`` argument
is passed because it is free and correct on POSIX, and it is NOT a security
property here. Hardening it on Windows needs ``icacls``/``pywin32`` and is not
W3's job.

``newline=""`` on the write is load-bearing on this platform too: without it
Python's text layer rewrites every ``\n`` as ``\r\n`` (measured), so the file
would not be a byte-exact copy of what the tool produced. ``encoding="utf-8"``
is mandatory for the same class of reason -- ``open()`` still defaults to the
locale codepage (cp1252 here), and ``run_process`` decodes with
``errors="replace"``, so its U+FFFD would raise ``UnicodeEncodeError`` from
inside the chokepoint's own output path.

PURITY
------
``djcode.config`` is already inside ``djcode.core``'s hard import closure, and
everything else here is stdlib, so ``tests/test_headless_purity.py`` stays green.
``config.CONFIG_DIR`` is resolved at CALL time (``config.CONFIG_DIR``, never
``from djcode.config import CONFIG_DIR``) because the module-level-constant
pattern used elsewhere in this codebase cannot be monkeypatched -- see
``tests/conftest.py``, which has to set an environment variable before any
djcode import and then rebind ``workflow.CONFIG_DIR`` by hand to work around it.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from djcode import config
from djcode.core.outcome import ToolOutcome

#: Characters kept from the start and the end of an oversized output.
HEAD_CHARS = 2000
TAIL_CHARS = 2000

#: Below head+tail+this, nothing is spilled. Without the slack a 4_001-character
#: output would be "bounded" into something LONGER than the original, and the
#: marker would announce that one single character had been elided.
MARKER_SLACK = 500

#: The only size any tool's output is measured against.
SPILL_THRESHOLD = HEAD_CHARS + TAIL_CHARS + MARKER_SLACK

_UNSAFE_BUCKET = re.compile(r"[^A-Za-z0-9._-]")

#: Where spills land when no session owns the call. ``DispatchContext.session_id``
#: is genuinely absent on every headless path and in every test: ``Operator``
#: never sets ``self.session_id`` in ``__init__`` -- ``repl.py``, ``app.py`` and
#: ``session_commands.py`` graft it on afterwards. A real bucket beats letting
#: ``None`` become the literal directory name "None".
_PROCESS_BUCKET = f"proc-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def session_bucket(session_id: str | None) -> str:
    """Directory name for ``session_id``, safe to join onto a path.

    Session ids are ``s_<uuid4 hex>`` (``sessions.py:225``) and need no cleaning,
    but this value reaches the filesystem, so a caller-supplied id is sanitised
    rather than trusted: anything outside ``[A-Za-z0-9._-]`` is replaced, the
    result is length-capped, and a name made only of dots (``..`` escapes a
    directory) falls back to the process bucket.
    """
    if not session_id:
        return _PROCESS_BUCKET
    cleaned = _UNSAFE_BUCKET.sub("_", str(session_id))[:64]
    if not cleaned.strip("."):
        return _PROCESS_BUCKET
    return cleaned


def spill_dir(session_id: str | None = None) -> Path:
    """The directory this session's spill files belong in. Not created here."""
    return config.CONFIG_DIR / "tool-output" / session_bucket(session_id)


def write_spill(text: str, *, session_id: str | None = None) -> Path:
    """Write ``text`` verbatim and return the absolute path it landed at."""
    directory = spill_dir(session_id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = (directory / f"{uuid.uuid4().hex}.txt").resolve()
    path.write_text(text, encoding="utf-8", errors="replace", newline="")
    return path


def bound_output(
    text: str, *, session_id: str | None = None
) -> tuple[str, str | None, dict[str, object]]:
    """Apply the policy to one string.

    Returns ``(content, spill_path, details)``. ``spill_path`` is ``None`` both
    when nothing was elided and when the spill could not be written -- in the
    second case ``details["spill_error"]`` says why and ``content`` still states
    plainly that the text is gone. Failing to write a spill file must never turn
    a successful tool call into a failed one: the side effect already happened,
    and this is the one step that runs after it.
    """
    total = len(text)
    if total <= SPILL_THRESHOLD:
        return text, None, {}

    details: dict[str, object] = {"output_chars": total}
    path: Path | None = None
    try:
        path = write_spill(text, session_id=session_id)
    except (OSError, ValueError) as exc:  # disk full, denied, unwritable CONFIG_DIR
        details["spill_error"] = f"{type(exc).__name__}: {exc}"

    where = str(path) if path is not None else "(spill file could not be written)"
    elided = total - HEAD_CHARS - TAIL_CHARS
    details["output_elided"] = elided
    content = (
        f"{text[:HEAD_CHARS]}\n\n"
        f"... [djcode] {elided} of {total} characters elided; full output: {where}\n\n"
        f"{text[-TAIL_CHARS:]}\n\n"
        f"[djcode] full output: {where}"
    )
    return content, (str(path) if path is not None else None), details


def apply_output_policy(outcome: ToolOutcome, *, session_id: str | None = None) -> ToolOutcome:
    """Bound ``outcome`` in place. The chokepoint's single call into this module."""
    if not isinstance(outcome.content, str):
        outcome.content = str(outcome.content)
    content, path, details = bound_output(outcome.content, session_id=session_id)
    if not details:
        return outcome
    outcome.content = content
    if path is not None:
        # Never clobber a spill a handler set for itself (the W7-2 seam).
        outcome.spill_path = path
    outcome.details.update(details)
    return outcome
