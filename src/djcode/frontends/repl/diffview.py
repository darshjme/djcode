"""Terminal rendering for `djcode.core.diff.FileDiff` — the picture half of P0-3.

Core decides WHAT changed. This module decides what that looks like, and it is
the only place in the REPL that knows a diff has colours.

WHY `tui.py`'s TWO RENDERERS WERE DELETED RATHER THAN ADAPTED
-------------------------------------------------------------
`BLUEPRINT-CLI.md` W7-3 says `tui.py:309 render_diff` and `:350
render_inline_diff` "are imported by `repl.py`" and instructs: "Do not rewrite
them -- adapt their signatures." Both halves of that are wrong against the code,
and the instruction rests on the false half:

* They are at 313 and 356, not 309 and 350 (`145baf7`, the W0 format pass,
  moved them).
* `repl.py` does NOT import them. W0's `9874316` removed the import. Grep of
  `src/` and `tests/` finds four hits, all inside `tui.py`: two definitions and
  two `__all__` entries. They were unreferenced dead code, so there was no
  compatibility constraint to preserve and nothing to adapt *to*.
* `render_diff` lexed the whole unified-diff text with the **diff** lexer
  (`Syntax(diff_text, "diff", ...)`), which gives red/green gutters and zero
  language highlighting, printed no line numbers at all (`line_numbers=False`),
  had no mechanism for word-level spans, and wrapped every file in a `Panel`
  against a hardcoded `theme="monokai"` and a hardcoded gold. Satisfying the
  W7 green gate ("line numbers are the ORIGINAL file's") is not reachable from
  that body. `render_inline_diff` printed every old line red then every new line
  green -- 80 lines of noise for a 40-line edit, and not a diff at all.

What survived is the shape of the `difflib` call with `keepends`/`n=context`,
which belongs in core and is there, and the empty-diff guard, which is here.
Both functions and their `__all__` entries were deleted in the same commit.

THREE MECHANICAL TRAPS, ALL MEASURED ON THIS BOX
------------------------------------------------
1. **Per-line lexing corrupts multi-line strings.** Lexing `def not_a_def():`
   alone colours `def` as a keyword even when it sits inside a triple-quoted
   string, where whole-block lexing correctly colours it as string. So each
   hunk's deleted lines are lexed as ONE block and its added lines as ONE block.
   When the caller has the full file text we lex `text[:hunk_end]` and slice,
   which also fixes the case per-hunk lexing gets wrong on its own -- a hunk
   that OPENS inside a string that began further up. When it does not (a diff
   rebuilt from `ToolOutcome.details`, which carries no bodies) we lex the hunk
   alone and say so with `· highlighting: hunk-local`. Shipping the weaker thing
   while claiming the stronger one is the failure mode `W3-VERIFICATION.md` is
   about.
2. **The code theme's background eats the diff tint.** Every monokai span
   carries `on #272822`, which overrides the row's add/del background on every
   character. Each span's style is rebuilt keeping only colour/bold/italic.
   `style + Style(bgcolor=None)` does NOT clear it: rich overlays only *set*
   attributes, and `None` means "unset", not "clear".
3. **`Syntax.highlight(code).split()` returns N+1 lines**, the last one empty,
   whether or not the code ends in a newline -- so it can never tell you about a
   trailing newline, and the phantom row has to be dropped.

Plus: `Syntax` expands tabs to 4 by default, which shifts every column and puts
word-level underlines three cells off per leading tab. `tab_size=1` keeps the
rendered string the same length as the raw one, so core's character offsets land
where core computed them.
"""

from __future__ import annotations

import sys
from typing import Any

from rich.console import Console
from rich.style import Style
from rich.text import Text

from djcode.core.diff import DiffLine, FileDiff, Hunk

# Palette. W9's theme.py takes ownership of these and of the UTF-8 reconfigure
# below; until it lands they live here so the capability ships now rather than
# waiting a wave.
ADD_BG = "#1F2A1F"
DEL_BG = "#2C1E1E"
ADD_FG = "#A2BA9A"
DEL_FG = "#C98A8A"
GOLD = "#C79B7A"
CODE_THEME = "monokai"

#: DESIGN-CLI §4.4 gives a tool card twelve lines, then a count.
CARD_LINES = 12
#: A `/diff` file gets more, but not unbounded: one 4000-line file must not
#: scroll the other nineteen off the screen.
FILE_LINES = 200

#: Above this, lexing the leading context costs more than the colour is worth.
PREFIX_BUDGET = 256 * 1024

_GLYPHS = {"dot": "⏺", "tilde": "~", "arrow": "↳", "space": "·", "tab": "→"}
_ASCII = {"dot": "*", "tilde": "~", "arrow": "->", "space": ".", "tab": ">"}


def _ensure_utf8_stdout(console: Console | None = None) -> None:
    """Stop a CJK comment inside a diff from killing the renderer on Windows.

    Reproduced live: printing `日本語` to a redirected stdout raises
    `UnicodeEncodeError: 'charmap' codec can't encode characters` out of
    cp1252. This is the crash `DESIGN-CLI.md` §0 documents for the `⏺` glyph,
    but it applies to FILE CONTENT too -- an em-dash, a smart quote or an emoji
    in a JS string is enough, and a diff renderer exists to print file content.
    W9's `theme.py` owns this call; it is here because W7 wires the renderers
    now and a crash is not an acceptable thing to defer.

    The console's OWN stream is reconfigured too, not just the process's. A
    front-end may hand us any writer, and the one thing this renderer must never
    do is raise out of the middle of a half-printed diff. A stream that cannot
    be widened to UTF-8 gets `errors="replace"` instead, so the worst case is a
    question mark rather than a traceback.
    """
    streams = [sys.stdout, sys.stderr]
    if console is not None:
        streams.append(console.file)
    for stream in streams:
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, LookupError):
            try:
                reconfigure(errors="replace")
            except Exception:  # pragma: no cover - closed or exotic stream
                pass


def glyphs(console: Console) -> dict[str, str]:
    """Unicode where the terminal can take it, ASCII where it cannot."""
    encoding = (getattr(console.file, "encoding", "") or "").lower()
    if "utf" in encoding or console.is_jupyter:
        return _GLYPHS
    return _ASCII


def human_size(n: int | None) -> str:
    if n is None:
        return "?"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


# ---------------------------------------------------------------------------
# Lexing
# ---------------------------------------------------------------------------


def _strip_backgrounds(text: Text) -> None:
    """Keep the syntax colours, drop the theme's block background.

    Rebuilt rather than overlaid: `Style(bgcolor=None)` is "unset", not "clear",
    so adding it changes nothing. Measured -- without this pass the add/del tint
    is invisible on every character under `monokai`, and DESIGN-CLI §1 rule 5
    allows a background only for diff tints and the completion menu anyway.
    """
    text.spans = [
        span._replace(
            style=Style(
                color=span.style.color,
                bold=span.style.bold,
                italic=span.style.italic,
                dim=span.style.dim,
            )
        )
        if isinstance(span.style, Style)
        else span
        for span in text.spans
    ]


def _lex_block(code: str, lexer: str | None, drop: int) -> list[Text]:
    """Lex `code` as ONE block and return its last `len - drop` lines.

    `drop` is how many leading lines were only there to give the lexer context
    (option (a), prefix-and-slice). With `drop == 0` this is plain per-hunk
    block lexing.
    """
    if not lexer:
        return [Text(line) for line in code.split("\n")][drop:]
    try:
        from rich.syntax import Syntax

        syntax = Syntax(code, lexer, theme=CODE_THEME, tab_size=1, word_wrap=False)
        rendered = syntax.highlight(code)
    except Exception:  # pragma: no cover - unknown lexer, pygments hiccup
        return [Text(line) for line in code.split("\n")][drop:]
    lines = rendered.split(allow_blank=True)
    out = list(lines)
    # Trap 3: highlight() always appends a phantom empty final line.
    if out and not out[-1].plain:
        out.pop()
    for line in out:
        line.rstrip()
        _strip_backgrounds(line)
    return out[drop:]


def _side_texts(
    diff: FileDiff, hunk: Hunk, kinds: tuple[str, ...], full_text: str | None, start: int
) -> list[Text]:
    """Styled `Text` for one side of a hunk, in hunk order."""
    body = [line.text for line in hunk.lines if line.kind in kinds]
    if not body:
        return []
    prefix_lines: list[str] = []
    if full_text is not None and start > 1 and len(full_text) <= PREFIX_BUDGET:
        prefix_lines = full_text.split("\n")[: start - 1]
    code = "\n".join([*prefix_lines, *body])
    return _lex_block(code, diff.lexer, len(prefix_lines))


def _visible_ws(text: str, marks: dict[str, str]) -> str:
    """Make an indent or a trailing run readable.

    `-    x` against `+        x` is two lines the eye cannot tell apart, which
    is the entire content of a whitespace hunk.
    """
    lead = len(text) - len(text.lstrip())
    tail_start = len(text.rstrip())
    out = list(text)
    for i in [*range(lead), *range(tail_start, len(text))]:
        if out[i] == " ":
            out[i] = marks["space"]
        elif out[i] == "\t":
            out[i] = marks["tab"]
    return "".join(out)


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def _gutter(line: DiffLine, width: int) -> Text:
    if line.kind == "meta":
        return Text(" " * (width + 6), style="dim")
    sign = {"add": "+", "del": "-", "ctx": " "}[line.kind]
    number = line.old_no if line.kind != "add" else line.new_no
    label = str(number) if number is not None else ""
    return Text(f"  {sign} {label:>{width}} | ", style="dim")


def _row(
    line: DiffLine,
    body: Text,
    spans: list[tuple[int, int]],
    width: int,
    marks: dict[str, str],
    *,
    whitespace: bool,
) -> Text:
    if whitespace and line.kind in ("add", "del"):
        body = Text(_visible_ws(line.text, marks))
    row = _gutter(line, width)
    if line.kind == "add":
        body.stylize(Style(color=ADD_FG, bgcolor=ADD_BG), 0, len(body.plain))
    elif line.kind == "del":
        body.stylize(Style(color=DEL_FG, bgcolor=DEL_BG), 0, len(body.plain))
    elif line.kind == "meta":
        body.stylize("dim italic", 0, len(body.plain))
    for start, end in spans:
        if 0 <= start < end <= len(body.plain):
            body.stylize(Style(bold=True, underline=True), start, end)
    row.append_text(body)
    return row


def _number_width(diff: FileDiff) -> int:
    biggest = 1
    for hunk in diff.hunks:
        biggest = max(biggest, hunk.old_start + hunk.old_len, hunk.new_start + hunk.new_len)
    return max(3, len(str(biggest)))


def _print(console: Console, text: Text) -> None:
    """One diff row, one terminal line, gutter intact.

    `no_wrap`/`overflow`/`crop` are passed HERE, at print time. Setting
    `text.no_wrap = True` on the object does not suppress wrapping (measured),
    and a wrapped diff row puts the gutter alone on the first line and the code
    on continuation lines with no gutter at all.
    """
    console.print(text, no_wrap=True, overflow="ellipsis", crop=True, markup=False, highlight=False)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def diff_header(diff: FileDiff, marks: dict[str, str]) -> Text:
    """`─ src/x.py · +12 −3` plus whatever honesty the diff is carrying."""
    head = Text()
    head.append(f"  {marks['dot']} ", style=GOLD)
    head.append(diff.path or "(diff)", style="bold white")
    bits: list[str] = []
    if diff.status == "added":
        bits.append("new file")
    elif diff.status == "deleted":
        bits.append("deleted")
    if diff.binary:
        bits.append("binary")
        bits.append(f"{human_size(diff.size_before)} -> {human_size(diff.size_after)}")
    elif not diff.unavailable:
        bits.append(f"+{diff.added} -{diff.removed}")
    if diff.whitespace_only:
        bits.append("whitespace only")
    if diff.eol_changed:
        bits.append(f"line endings {diff.eol_before.upper()} -> {diff.eol_after.upper()}")
    if diff.newline_at_eof_changed:
        bits.append(f"final newline {diff.newline_at_eof_changed}")
    if diff.highlight_degraded:
        bits.append("syntax highlighting reduced")
    for bit in bits:
        head.append(" · ", style="dim")
        head.append(bit, style="dim")
    return head


def render_file_diff(
    console: Console,
    diff: FileDiff,
    *,
    max_lines: int = FILE_LINES,
    header: bool = True,
) -> None:
    """Draw one file's change. The single renderer behind all three surfaces."""
    _ensure_utf8_stdout(console)
    marks = glyphs(console)
    if header:
        _print(console, diff_header(diff, marks))

    if diff.unavailable:
        # Never "the whole file was added". The pre-image was not stored.
        _print(console, Text(f"      {diff.unavailable}", style="yellow"))
        return
    if diff.binary:
        return
    if diff.note:
        _print(console, Text(f"      {diff.note}", style="dim"))
    if not diff.hunks:
        if diff.eol_changed or diff.newline_at_eof_changed:
            _print(console, Text("      no content change", style="dim"))
        else:
            _print(console, Text("      no changes", style="dim"))
        return

    if diff.whitespace_only:
        # A 200-line re-indent is 400 lines that differ in nothing the eye can
        # see. Printing them all is a card that communicates nothing.
        moved = sum(1 for h in diff.hunks for line in h.lines if line.kind == "del")
        _print(
            console,
            Text(
                f"      {marks['tilde']} {moved} line(s) re-indented · no content change",
                style="dim",
            ),
        )
        return

    width = _number_width(diff)
    shown = 0
    hidden = 0
    for hunk in diff.hunks:
        if hunk.kind == "whitespace":
            # Mixed file: collapse the formatter noise, keep the real hunk in
            # full. This is the case that matters -- a one-line fix buried in a
            # re-indent -- and collapsing is what makes it readable.
            span = f"L{hunk.old_start}-{hunk.old_start + max(0, hunk.old_len - 1)}"
            _print(
                console,
                Text(
                    f"      {marks['tilde']} {span} · indentation only · "
                    f"{sum(1 for line in hunk.lines if line.kind == 'del')} lines",
                    style="dim",
                ),
            )
            continue
        old_side = _side_texts(diff, hunk, ("ctx", "del"), diff.before_text, hunk.old_start)
        new_side = _side_texts(diff, hunk, ("ctx", "add"), diff.after_text, hunk.new_start)
        oi = ni = 0
        for index, line in enumerate(hunk.lines):
            if shown >= max_lines:
                hidden += 1
                continue
            if line.kind in ("ctx", "del"):
                body = old_side[oi] if oi < len(old_side) else Text(line.text)
                oi += 1
                if line.kind == "ctx":
                    ni += 1
            elif line.kind == "add":
                body = new_side[ni] if ni < len(new_side) else Text(line.text)
                ni += 1
            else:
                body = Text(line.text)
            _print(
                console,
                _row(
                    line,
                    body.copy(),
                    hunk.word_spans.get(index, []),
                    width,
                    marks,
                    whitespace=False,
                ),
            )
            shown += 1
    if hidden:
        _print(console, Text(f"      … +{hidden} lines", style="dim"))
    if diff.before_text is None and diff.after_text is None and diff.lexer:
        _print(console, Text("      · highlighting: hunk-local", style="dim"))


def render_diff_card(console: Console, diff: FileDiff, *, max_lines: int = CARD_LINES) -> None:
    """The compact form for a tool result. Same renderer, tighter bound."""
    render_file_diff(console, diff, max_lines=max_lines)


def render_diffs(console: Console, diffs: list[FileDiff]) -> None:
    """`/diff`'s body: every changed file, or an honest reason there are none."""
    real = [d for d in diffs if d.path or d.unavailable]
    if not real:
        console.print("[dim]No changes.[/]")
        return
    for diff in real:
        console.print()
        render_file_diff(console, diff)
    console.print()
    total_add = sum(d.added for d in real)
    total_del = sum(d.removed for d in real)
    console.print(f"[dim]  {len(real)} file(s) · +{total_add} -{total_del}[/]")


def render_outcome_diff(console: Console, details: dict[str, Any]) -> bool:
    """Draw whatever diff a `ToolOutcome.details` is carrying. True if it drew.

    `details` never holds file bodies, so the `FileDiff` rebuilt here has no
    `before_text`/`after_text` and the renderer lexes each hunk on its own.
    """
    payload = details.get("diff")
    payloads = details.get("diffs") or ([payload] if isinstance(payload, dict) else [])
    drawn = False
    for entry in payloads:
        if not isinstance(entry, dict):
            continue
        diff = FileDiff.from_dict(entry)
        if diff.empty and not diff.unavailable and not diff.binary:
            continue
        render_diff_card(console, diff)
        drawn = True
    return drawn
