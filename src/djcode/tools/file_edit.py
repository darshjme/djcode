"""File edit tool — surgical string replacement in files.

A failed edit used to cost a round trip and a guess: "old_string not found"
told the model nothing about *why*. W1-8 (GAP B16) gives the tool a recovery
ladder instead — it answers the question the model was about to ask.

On a miss the tool checks, in order:

1. whether the edit is already applied (a retry after a successful write);
2. whether the only difference is whitespace or line endings, in which case
   it applies the edit against the real bytes and says so;
3. what the nearest line window in the file actually is — with its line
   number and the exact bytes that differ.

On an ambiguous match it reports every occurrence with the context that tells
them apart, so the next attempt is informed rather than lucky.

W7-2 converts this tool from a plain string to a `ToolOutcome`, for two
reasons: the user could not see what the edit did, and `ok` was "unverified" on
all eight of its return paths.

**READ THIS BEFORE TOUCHING A RETURN STATEMENT.** The moment a handler returns a
`ToolOutcome`, `_build_outcome` stamps `ok_source="handler"` and
`workflow._result_ok` treats `ok` as AUTHORITATIVE on the DAF wire -- a wrong
`False` skips every dependent node, a wrong `True` runs them all on a failed
edit. `ToolOutcome.ok` also DEFAULTS to True, so an error path that forgets the
flag ships a *verified* false green. That is the exact regression class
`W3-VERIFICATION.md` documents. The eight paths and their verdicts:

    file not found                        -> False
    replaced 1 occurrence                 -> True
    old_string found N times (ambiguous)  -> False
    "Already applied: ..."                -> True   (a success that never says "Edited")
    ambiguous after normalising           -> False
    replaced after normalising            -> True
    old_string not found                  -> False
    exception                             -> False

Every one of them goes through `_ok` or `_err` below, which exist so the flag
cannot be omitted by accident.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from djcode.core.outcome import ToolOutcome

#: How many ambiguous occurrences to describe before summarising the rest.
_MAX_OCCURRENCES_SHOWN = 5

#: Lines of surrounding context shown per ambiguous occurrence.
_CONTEXT_LINES = 2

#: difflib similarity floor for "did you mean this line?".
_CLOSE_MATCH_CUTOFF = 0.6

#: Upper bound on the line windows compared during nearest-match recovery, so
#: a huge file degrades to a partial answer instead of a stalled tool call.
_MAX_WINDOWS = 20000


def _outcome(text: str, *, ok: bool, diff: object | None = None) -> ToolOutcome:
    """The only place this module builds a result. See the module docstring.

    A diff goes into ``details`` as ``FileDiff.as_dict()`` -- hunks and counts,
    bounded at ``MAX_DETAIL_LINES``, never the file body.
    ``tools/__init__.py::_finish_checkpoint`` states that rule and the reason
    (``details`` rides ``tool_result_event`` into W10's JSONL on every edit),
    and W7 keeps it: the renderer reads the hunks, not the file.
    """
    from djcode.core.outcome import ToolOutcome as _Outcome

    details: dict[str, object] = {}
    if diff is not None:
        details["diff"] = diff.as_dict()  # type: ignore[attr-defined]
        details["diff_stat"] = {
            "path": diff.path,  # type: ignore[attr-defined]
            "added": diff.added,  # type: ignore[attr-defined]
            "removed": diff.removed,  # type: ignore[attr-defined]
            "hunks": len(diff.hunks),  # type: ignore[attr-defined]
        }
    return _Outcome(content=text, ok=ok, details=details)


def _ok(text: str, diff: object | None = None) -> ToolOutcome:
    return _outcome(text, ok=True, diff=diff)


def _err(text: str) -> ToolOutcome:
    return _outcome(text, ok=False)


async def execute_file_edit(path: str, old_string: str, new_string: str) -> ToolOutcome:
    """Replace old_string with new_string in a file. The old_string must be unique."""
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return _err(f"Error: File not found: {path}")

        content = _read(p)

        count = content.count(old_string)
        if count == 1:
            after = content.replace(old_string, new_string, 1)
            _write(p, after)
            return _ok(f"Edited {p}: replaced 1 occurrence", _diff_of(p, content, after))
        if count > 1:
            return _err(_report_ambiguous(p, content, old_string, count))
        return _recover(p, content, old_string, new_string)

    except Exception as e:
        return _err(f"Error editing {path}: {e}")


def _diff_of(p: Path, before: str, after: str):
    """The change this call made, as data a front-end can draw.

    Deferred import: ``djcode.tools`` sits inside ``djcode.core``'s import
    closure (core -> provider -> capabilities -> tools), so importing
    ``djcode.core.diff`` at module level here would close the cycle at start-up.

    Both sides are ``str`` read and written with ``newline=""``, so they carry
    the file's real line endings and ``diff_text`` normalises them out of the
    comparison -- an LF file edited on Windows shows one changed line, not all
    of them.
    """
    from djcode.core.diff import diff_text

    return diff_text(str(p), before, after)


# -- I/O ---------------------------------------------------------------------


def _read(p: Path) -> str:
    """Read the file without translating line endings.

    `Path.read_text` opens in universal-newline mode, so on Windows it turns
    every CRLF into LF on the way in and `Path.write_text` turns every LF back
    into CRLF on the way out. That rewrites the line endings of any LF file
    edited on Windows, and it hides the very byte difference this tool has to
    report. Both ends of the round trip stay literal instead.
    """
    with open(p, encoding="utf-8", newline="") as handle:
        return handle.read()


def _write(p: Path, content: str) -> None:
    with open(p, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)


# -- Recovery ladder (count == 0) --------------------------------------------


def _recover(p: Path, content: str, old_string: str, new_string: str) -> ToolOutcome:
    # 1. Already applied. A model that retries after a write that did land
    #    should be told it succeeded, not sent hunting for a string that this
    #    tool itself removed. ok=True: nothing failed and nothing was written,
    #    so there is no diff either.
    if new_string and new_string in content and old_string != new_string:
        return _ok(f"Already applied: {p} already contains new_string")

    # 2. Whitespace- and line-ending-insensitive match. If exactly one region
    #    of the file says the same thing, the edit was right and the quoting
    #    was not; apply it to the real bytes.
    span = _normalised_span(content, old_string)
    if span == "ambiguous":
        return _err(
            f"Error: old_string is not in {p} byte for byte, and ignoring whitespace it "
            "matches more than one place. Quote the region exactly, including indentation."
        )
    if span is not None:
        start, end = span
        after = content[:start] + new_string + content[end:]
        _write(p, after)
        line = content.count("\n", 0, start) + 1
        # The diff is against the span that was ACTUALLY replaced, which is not
        # the span `old_string` describes -- that is the whole point of this
        # branch, and a preview built from `old_string` would point elsewhere.
        return _ok(
            f"Edited {p}: replaced 1 occurrence at line {line} "
            "(matched after normalising whitespace and line endings; "
            "old_string did not match the file byte for byte)",
            _diff_of(p, content, after),
        )

    # 3. Nearest candidate, with its line number and the bytes that differ.
    return _err(f"Error: old_string not found in {p}.\n" + _nearest_report(content, old_string))


def _normalise(text: str) -> tuple[str, list[int]]:
    """Collapse whitespace and line endings, keeping a map back to the source.

    Returns the normalised text and an index whose k-th entry is the offset in
    `text` at which the k-th normalised character starts. The list carries one
    extra entry (len(text)) so a half-open match span always maps.
    """
    out: list[str] = []
    index: list[int] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\r":
            start = i
            if i + 1 < n and text[i + 1] == "\n":
                i += 1
            i += 1
            out.append("\n")
            index.append(start)
            continue
        if ch in " \t":
            start = i
            while i < n and text[i] in " \t":
                i += 1
            # Whitespace that only pads a line ending (or the end of the text)
            # carries no meaning; dropping it lets a trailing-space difference
            # stop being an edit failure.
            if i >= n or text[i] in "\r\n":
                continue
            out.append(" ")
            index.append(start)
            continue
        out.append(ch)
        index.append(i)
        i += 1
    index.append(n)
    return "".join(out), index


def _normalised_span(content: str, old_string: str) -> tuple[int, int] | str | None:
    """Locate `old_string` in `content` ignoring whitespace runs and CRLF.

    Returns the half-open span in `content`, the string "ambiguous" when the
    normalised form appears more than once, or None when it does not appear.
    """
    needle, _ = _normalise(old_string)
    if not needle.strip():
        return None
    haystack, index = _normalise(content)
    first = haystack.find(needle)
    if first < 0:
        return None
    if haystack.find(needle, first + 1) >= 0:
        return "ambiguous"
    return index[first], index[first + len(needle)]


def _nearest_report(content: str, old_string: str) -> str:
    """Describe the closest thing in the file to what the caller asked for."""
    lines = content.splitlines()
    wanted = old_string.splitlines() or [old_string]
    height = max(1, len(wanted))
    needle = "\n".join(wanted)

    windows: list[str] = []
    starts: list[int] = []
    lower = max(1, int(len(needle) * 0.4))
    upper = max(len(needle) * 3, 80)
    for i in range(0, max(0, len(lines) - height + 1)):
        window = "\n".join(lines[i : i + height])
        if lower <= len(window) <= upper:
            windows.append(window)
            starts.append(i + 1)
        if len(windows) >= _MAX_WINDOWS:
            break

    hints = _byte_hints(content, old_string)
    if not windows:
        return "No comparable line was found in the file." + hints

    best = difflib.get_close_matches(needle, windows, n=1, cutoff=_CLOSE_MATCH_CUTOFF)
    if not best:
        return (
            "No line in the file is close to old_string; re-read the file "
            "before editing it." + hints
        )

    candidate = best[0]
    line_no = starts[windows.index(candidate)]
    ratio = difflib.SequenceMatcher(None, needle, candidate).ratio()
    report = [
        f"Nearest match at line {line_no} ({ratio:.0%} similar):",
        f"  you asked for: {_show(needle)}",
        f"  the file has:  {_show(candidate)}",
    ]
    report.extend(_differences(needle, candidate, line_no))
    return "\n".join(report) + hints


def _differences(needle: str, candidate: str, line_no: int) -> list[str]:
    """The exact bytes that differ, per line, with line and column numbers."""
    out: list[str] = []
    wanted = needle.split("\n")
    actual = candidate.split("\n")
    for offset in range(max(len(wanted), len(actual))):
        left = wanted[offset] if offset < len(wanted) else ""
        right = actual[offset] if offset < len(actual) else ""
        if left == right:
            continue
        matcher = difflib.SequenceMatcher(None, left, right, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            where = f"  line {line_no + offset}, column {j1 + 1}: "
            if tag == "insert":
                out.append(where + f"the file has {right[j1:j2]!r} where old_string has nothing")
            elif tag == "delete":
                out.append(where + f"old_string has {left[i1:i2]!r} which the file does not")
            else:
                out.append(where + f"old_string has {left[i1:i2]!r}, the file has {right[j1:j2]!r}")
        if len(out) >= 8:
            out.append("  (further differences omitted)")
            break
    return out


def _byte_hints(content: str, old_string: str) -> str:
    """Call out the two differences that are invisible in a rendered diff."""
    hints: list[str] = []
    if "\r\n" in content and "\r\n" not in old_string and "\n" in old_string:
        hints.append(
            "The file uses CRLF line endings and old_string uses LF; "
            "match the file's line endings or edit a single line."
        )
    elif "\r\n" in old_string and "\r\n" not in content:
        hints.append(
            "old_string uses CRLF line endings and the file uses LF; match the file's line endings."
        )
    if "\t" in content and "\t" not in old_string and "    " in old_string:
        hints.append("The file indents with tabs; old_string uses spaces.")
    elif "\t" in old_string and "\t" not in content:
        hints.append("old_string indents with tabs; the file uses spaces.")
    return ("\n" + "\n".join(hints)) if hints else ""


def _show(text: str, limit: int = 200) -> str:
    """Render a fragment so whitespace and line endings are visible."""
    clipped = text[:limit]
    suffix = "..." if len(text) > limit else ""
    return repr(clipped) + suffix


# -- Ambiguity report (count > 1) --------------------------------------------


def _report_ambiguous(p: Path, content: str, old_string: str, count: int) -> str:
    """List every occurrence with the context that distinguishes it."""
    lines = content.splitlines()
    report = [
        f"Error: old_string found {count} times in {p}. Provide more context to make it unique.",
    ]

    offset = content.find(old_string)
    shown = 0
    while offset >= 0 and shown < _MAX_OCCURRENCES_SHOWN:
        start_line = content.count("\n", 0, offset) + 1
        end_line = start_line + max(0, old_string.count("\n"))
        before = lines[max(0, start_line - 1 - _CONTEXT_LINES) : start_line - 1]
        after = lines[end_line : end_line + _CONTEXT_LINES]
        report.append(f"  occurrence {shown + 1} at line {start_line}:")
        for i, text in enumerate(before, start=start_line - len(before)):
            report.append(f"    {i:>6} | {text}")
        for i in range(start_line, end_line + 1):
            marker = lines[i - 1] if 0 < i <= len(lines) else ""
            report.append(f"  > {i:>6} | {marker}")
        for i, text in enumerate(after, start=end_line + 1):
            report.append(f"    {i:>6} | {text}")
        shown += 1
        offset = content.find(old_string, offset + max(1, len(old_string)))

    if count > shown:
        report.append(f"  ... and {count - shown} more occurrences")
    return "\n".join(report)
