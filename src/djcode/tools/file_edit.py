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

The tool returns plain strings; W3 moves the whole tool surface to
`ToolOutcome` in one pass.
"""

from __future__ import annotations

import difflib
from pathlib import Path

#: How many ambiguous occurrences to describe before summarising the rest.
_MAX_OCCURRENCES_SHOWN = 5

#: Lines of surrounding context shown per ambiguous occurrence.
_CONTEXT_LINES = 2

#: difflib similarity floor for "did you mean this line?".
_CLOSE_MATCH_CUTOFF = 0.6

#: Upper bound on the line windows compared during nearest-match recovery, so
#: a huge file degrades to a partial answer instead of a stalled tool call.
_MAX_WINDOWS = 20000


async def execute_file_edit(path: str, old_string: str, new_string: str) -> str:
    """Replace old_string with new_string in a file. The old_string must be unique."""
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return f"Error: File not found: {path}"

        content = _read(p)

        count = content.count(old_string)
        if count == 1:
            _write(p, content.replace(old_string, new_string, 1))
            return f"Edited {p}: replaced 1 occurrence"
        if count > 1:
            return _report_ambiguous(p, content, old_string, count)
        return _recover(p, content, old_string, new_string)

    except Exception as e:
        return f"Error editing {path}: {e}"


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


def _recover(p: Path, content: str, old_string: str, new_string: str) -> str:
    # 1. Already applied. A model that retries after a write that did land
    #    should be told it succeeded, not sent hunting for a string that this
    #    tool itself removed.
    if new_string and new_string in content and old_string != new_string:
        return f"Already applied: {p} already contains new_string"

    # 2. Whitespace- and line-ending-insensitive match. If exactly one region
    #    of the file says the same thing, the edit was right and the quoting
    #    was not; apply it to the real bytes.
    span = _normalised_span(content, old_string)
    if span == "ambiguous":
        return (
            f"Error: old_string is not in {p} byte for byte, and ignoring whitespace it "
            "matches more than one place. Quote the region exactly, including indentation."
        )
    if span is not None:
        start, end = span
        _write(p, content[:start] + new_string + content[end:])
        line = content.count("\n", 0, start) + 1
        return (
            f"Edited {p}: replaced 1 occurrence at line {line} "
            "(matched after normalising whitespace and line endings; "
            "old_string did not match the file byte for byte)"
        )

    # 3. Nearest candidate, with its line number and the bytes that differ.
    return f"Error: old_string not found in {p}.\n" + _nearest_report(content, old_string)


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
