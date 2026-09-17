"""W7 / P0-3: the user can see what the agent changed.

The blueprint's green gate is five items. This file pins those five and the
seventeen failure modes the two W7 audits measured on this box, because the
five alone are all satisfiable by an implementation that is wrong on Windows,
wrong on long lines, wrong on tab-indented files and wrong about what "no
change" looks like.

Every test whose name mentions a measurement was checked against real behaviour
before it was written; none of them is a guess about what difflib or rich do.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3

import pytest

from djcode.core.diff import (
    MAX_DIFF_LINES,
    WORD_LEVEL_MIN_RATIO,
    FileDiff,
    detect_eol,
    diff_bytes,
    diff_paths,
    diff_text,
    looks_binary,
    preview_edit,
    preview_write,
    split_lines,
)
from djcode.tools.file_edit import execute_file_edit


def run(coro):
    return asyncio.run(coro)


def write(path, text: str) -> None:
    """Literal bytes in both directions -- no newline translation."""
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def all_lines(diff: FileDiff):
    return [line for hunk in diff.hunks for line in hunk.lines]


def spans_of(diff: FileDiff) -> list[tuple[int, int]]:
    return [span for hunk in diff.hunks for spans in hunk.word_spans.values() for span in spans]


# ── The blueprint's five green-gate items ──────────────────────────────────


def test_line_numbers_are_the_original_files():
    """Gate 1. difflib.unified_diff cannot satisfy this -- its `@@` header omits
    the length when it is 1, so `-(\\d+),(\\d+)` fails on every single-line
    hunk. Opcodes carry both indices."""
    before = "\n".join(f"line {i}" for i in range(1, 1001)) + "\n"
    after = before.replace("line 500\n", "line 500 CHANGED\n")

    diff = diff_text("big.py", before, after, context=3)

    hunk = diff.hunks[0]
    assert hunk.old_start == 497
    assert [line.old_no for line in hunk.lines if line.kind in ("ctx", "del")] == list(
        range(497, 504)
    )
    changed = [line for line in hunk.lines if line.kind == "del"]
    assert len(changed) == 1 and changed[0].old_no == 500
    assert changed[0].new_no is None


def test_hunk_lexing_is_block_level_not_per_line():
    """Gate 2, and the claim the whole W7-4 design rests on. MEASURED: lexing
    `def not_a_def():` on its own colours `def` as a keyword (#66d9ef) even when
    it sits inside a triple-quoted string, where whole-block lexing correctly
    colours the line as a string (#e6db74).

    Asserted on STYLES, not on rendered ANSI, and the "wrong" colour is computed
    here by actually lexing the line in isolation -- so the test is independent
    of which theme is configured."""
    rich_syntax = pytest.importorskip("rich.syntax")
    from rich.console import Console

    from djcode.frontends.repl import diffview

    inside = 'def not_a_def():'
    before = f'x = """\nfiller\n{inside}\n"""\ny = 1\n'
    after = f'x = """\nCHANGED\n{inside}\n"""\ny = 1\n'
    diff = diff_text("t.py", before, after, context=3)
    hunk = diff.hunks[0]

    block = diffview._side_texts(diff, hunk, ("ctx", "add"), diff.after_text, hunk.new_start)
    target = next(t for t in block if t.plain == inside)
    got = {str(span.style.color) for span in target.spans if span.style.color}

    alone = rich_syntax.Syntax(inside, "python", theme=diffview.CODE_THEME)
    isolated = alone.highlight(inside)
    keyword = {
        str(span.style.color)
        for span in isolated.spans
        if span.style.color and isolated.plain[span.start : span.end] == "def"
    }

    assert keyword, "the isolated lexing must produce a keyword colour to compare against"
    assert not (got & keyword), (
        f"per-line lexing leaked into the block: {got} shares a colour with {keyword}"
    )
    assert Console  # imported for the isolated-lex comparison above


def test_reindent_produces_no_word_spans():
    """Gate 3. Stripping leading whitespace before matching makes the two sides
    compare equal, so there is nothing to underline."""
    before = "def f():\n    return value\n    return other\n"
    after = "def f():\n        return value\n        return other\n"

    diff = diff_text("t.py", before, after)

    assert spans_of(diff) == []


def test_added_and_deleted_files_both_render():
    """Gate 4. Pin `old_start == 0` and `old_no is None`, not just "no crash" --
    an added file is `@@ -0,0 +1,N @@` and getting that wrong shifts every line
    number by one."""
    added = diff_text("new.py", None, "a = 1\nb = 2\n")
    assert added.status == "added"
    assert added.added == 2 and added.removed == 0
    assert added.hunks[0].old_start == 0 and added.hunks[0].old_len == 0
    assert all(line.kind == "add" and line.old_no is None for line in all_lines(added))

    deleted = diff_text("gone.py", "a = 1\nb = 2\n", None)
    assert deleted.status == "deleted"
    assert deleted.removed == 2 and deleted.added == 0
    assert deleted.hunks[0].new_start == 0 and deleted.hunks[0].new_len == 0
    assert all(line.kind == "del" and line.new_no is None for line in all_lines(deleted))


def test_binary_is_reported_not_decoded():
    """Gate 5. Empty hunks and zero counts -- never a fabricated line count for
    bytes we cannot read, and never U+FFFD."""
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256)) * 4

    diff = diff_bytes("logo.png", png, png + b"\x01")

    assert diff.binary is True
    assert diff.hunks == [] and diff.added == 0 and diff.removed == 0
    assert "�" not in repr(diff)


def test_utf16_file_is_binary_not_mojibake():
    """The one that actually bites: UTF-16LE text has a NUL on every ASCII
    character, so a naive `errors="replace"` decode turns it into interleaved
    garbage instead of saying "binary"."""
    assert looks_binary("hello world\n".encode("utf-16-le")) is True


# ── Word-level highlighting: when it helps, when it is noise ────────────────


def test_word_level_survives_long_lines():
    """MEASURED: SequenceMatcher's autojunk heuristic fires at 200 elements and
    turns a 4-character change on a 215-character line into a 64-character one.
    This test fails against a straightforward implementation that leaves
    autojunk at its default."""
    pad = "x" * 150
    before = f"{pad} value = compute(alpha, beta, gamma, delta, epsilon, zeta, eta)\n"
    after = f"{pad} value = compute(alpha, beta, gamma, delta, epsilon, zeta, ETA)\n"

    diff = diff_text("long.py", before, after)
    spans = spans_of(diff)

    assert spans, "a four-character change must still produce a span"
    assert max(end - start for start, end in spans) <= 8


def test_word_level_skipped_below_similarity_floor():
    """MEASURED: an unrelated del/add pair satisfies one-to-one perfectly and
    scores 0.21, producing ten scattered underlines. The floor is not in the
    blueprint."""
    before = "    self.show_thinking = show_thinking\n"
    after = "        approval: Callable[[ToolRequest], Awaitable[Decision]] | None = None,\n"

    diff = diff_text("t.py", before, after)

    assert spans_of(diff) == []
    kinds = [line.kind for line in all_lines(diff)]
    assert kinds.count("del") == 1 and kinds.count("add") == 1


def test_word_level_only_when_one_del_pairs_one_add():
    """A 2-del/2-add replace gets NO spans anywhere, even though del[0] and
    add[0] differ by one token. Pairing positionally is the noise this rule
    exists to prevent."""
    before = "alpha = 1\nbeta = 2\n"
    after = "alpha = 9\ngamma = 3\n"

    diff = diff_text("t.py", before, after)

    assert spans_of(diff) == []


def test_word_level_spans_are_whole_tokens():
    """MEASURED: character-level matching underlines INSIDE `bool` -> `Decision`
    (`replace 'b' -> 'Decisi'`, `replace 'ol' -> 'n'`). Token-level gives clean
    whole-word spans."""
    before = "    approval_callback: Callable[[str], Awaitable[bool]] | None = None,\n"
    after = "    approval: Callable[[ToolRequest], Awaitable[Decision]] | None = None,\n"

    diff = diff_text("t.py", before, after)
    add_line = next(line for line in all_lines(diff) if line.kind == "add")
    index = next(
        i
        for hunk in diff.hunks
        for i, line in enumerate(hunk.lines)
        if line is add_line
    )
    spans = diff.hunks[0].word_spans[index]

    assert spans
    for start, end in spans:
        before_char = add_line.text[start - 1] if start else " "
        after_char = add_line.text[end] if end < len(add_line.text) else " "
        head, tail = add_line.text[start], add_line.text[end - 1]
        assert not (before_char.isalnum() and head.isalnum()), "span starts mid-word"
        assert not (after_char.isalnum() and tail.isalnum()), "span ends mid-word"


def test_similarity_floor_constant_clears_the_measured_pair():
    """0.21 out, 0.70 in. Pin the constant so a future tweak has to face them."""
    assert 0.214 < WORD_LEVEL_MIN_RATIO < 0.702


def test_word_span_offsets_survive_tabs_and_uneven_indents():
    """Offsets are remapped with EACH LINE'S OWN indent width. One shared offset
    is the obvious bug and it shows only when the two indents differ."""
    before = "\tvalue = alpha\n"
    after = "\t\tvalue = beta\n"

    diff = diff_text("t.go", before, after)
    hunk = diff.hunks[0]
    for index, spans in hunk.word_spans.items():
        text = hunk.lines[index].text
        for start, end in spans:
            assert text[start:end].strip(), f"span {start}:{end} landed on whitespace in {text!r}"
            assert "alpha" in text[start:end] or "beta" in text[start:end]


# ── Re-indent: the signal must not drown ───────────────────────────────────


def test_reindent_is_flagged_whitespace_only():
    before = "".join(f"    line{i}\n" for i in range(200))
    after = "".join(f"        line{i}\n" for i in range(200))

    diff = diff_text("t.py", before, after)

    assert diff.whitespace_only is True
    assert all(hunk.kind == "whitespace" for hunk in diff.hunks)


def test_reindent_renders_in_three_lines_not_four_hundred():
    from rich.console import Console

    from djcode.frontends.repl.diffview import render_file_diff

    before = "".join(f"    line{i}\n" for i in range(200))
    after = "".join(f"        line{i}\n" for i in range(200))
    console = Console(file=__import__("io").StringIO(), width=100, force_terminal=False)

    render_file_diff(console, diff_text("t.py", before, after))

    printed = console.file.getvalue().strip().splitlines()
    assert len(printed) <= 3, printed


def test_mixed_reindent_and_real_change_keeps_the_real_hunk():
    """THE signal-drowning gate. A genuine one-token fix buried in a formatter
    run must render in full while the 200 re-indented lines collapse."""
    from rich.console import Console

    from djcode.frontends.repl.diffview import render_file_diff

    # Ten untouched lines between the two regions, so context=3 does not group
    # them into one hunk. The collapse is per HUNK: a real change that shares a
    # hunk with a re-indent renders in full alongside it, which is the right
    # answer there -- you cannot collapse half a hunk without hiding the change.
    gap = [f"# untouched {i}" for i in range(10)]
    body_before = [f"    line{i}" for i in range(200)]
    body_after = [f"        line{i}" for i in range(200)]
    before = "\n".join(["value = alpha", *gap, *body_before]) + "\n"
    after = "\n".join(["value = beta", *gap, *body_after]) + "\n"

    diff = diff_text("t.py", before, after)
    assert diff.whitespace_only is False
    kinds = {hunk.kind for hunk in diff.hunks}
    assert kinds == {"change", "whitespace"}
    assert spans_of(diff), "the real hunk keeps its word-level span"

    console = Console(file=__import__("io").StringIO(), width=100, force_terminal=False)
    render_file_diff(console, diff)
    printed = console.file.getvalue().strip().splitlines()
    assert len(printed) < 20, len(printed)
    assert any("alpha" in line for line in printed)
    assert any("beta" in line for line in printed)


def test_trailing_whitespace_change_is_visible():
    from rich.console import Console

    from djcode.frontends.repl.diffview import render_file_diff

    diff = diff_text("t.py", "x  \n", "x\n")
    console = Console(file=__import__("io").StringIO(), width=100, force_terminal=False)
    render_file_diff(console, diff)

    assert diff.whitespace_only is True
    assert "re-indented" in console.file.getvalue() or "no content change" in (
        console.file.getvalue()
    )


# ── Line splitting and the final newline ───────────────────────────────────


def test_formfeed_does_not_split_a_line():
    """MEASURED: `'a\\x0cb'.splitlines()` is `['a', 'b']`. Form feed, vertical
    tab, NEL and U+2028 all appear in real source; every line number after one
    of them would disagree with the user's editor."""
    text = "alpha\nbeta\x0cstill beta\ngamma\n"

    lines, ends = split_lines(text)

    assert ends is True
    assert lines == ["alpha", "beta\x0cstill beta", "gamma"]
    assert len(lines) == text.count("\n")


def test_lost_trailing_newline_is_a_marker_not_a_line_change():
    """Without this, `x\\ny\\n` -> `x\\ny` renders as one deletion and one
    insertion of the VISUALLY IDENTICAL line `y` -- indistinguishable in the UI
    from a re-indent, and a different cause entirely."""
    diff = diff_text("t.py", "x\ny\n", "x\ny")

    assert diff.newline_at_eof_changed == "removed"
    assert [line for line in all_lines(diff) if line.kind in ("add", "del")] == []
    assert diff.added == 0 and diff.removed == 0


def test_no_newline_marker_rides_a_real_hunk():
    diff = diff_text("t.py", "one\ntwo\nthree\n", "one\n2\nthree")

    assert diff.newline_at_eof_changed == "removed"
    assert any(line.kind == "meta" for line in all_lines(diff))
    assert [line.text for line in all_lines(diff) if line.kind == "del"] == ["two"]
    assert [line.text for line in all_lines(diff) if line.kind == "add"] == ["2"]


# ── CRLF. This is Windows. ─────────────────────────────────────────────────


def test_crlf_to_lf_flip_produces_no_content_hunks():
    """MEASURED: `Path.write_text` translates and `open(newline="")` does not,
    so the two file tools in this repo disagree. A pure EOL flip must not light
    up 100% of every file `file_write` touches."""
    diff = diff_text("t.py", "a\r\nb\r\nc\r\n", "a\nb\nc\n")

    assert diff.hunks == []
    assert diff.added == 0 and diff.removed == 0
    assert diff.eol_before == "crlf" and diff.eol_after == "lf"
    assert diff.eol_changed is True
    assert diff.empty is False, "an EOL flip is a real change and must still render"


def test_crlf_file_edited_shows_only_the_edited_line(tmp_path):
    """Written against an ACTUAL CRLF file on disk, not a synthetic string pair.
    This is the test that catches a post-image taken from the argument rather
    than the file."""
    target = tmp_path / "crlf.py"
    write(target, "alpha\r\nbeta\r\ngamma\r\n")

    outcome = run(execute_file_edit(str(target), "beta", "BETA"))
    diff = FileDiff.from_dict(outcome.details["diff"])

    assert outcome.ok is True
    assert [line.text for line in all_lines(diff) if line.kind == "del"] == ["beta"]
    assert [line.text for line in all_lines(diff) if line.kind == "add"] == ["BETA"]
    assert diff.eol_before == "crlf" and diff.eol_after == "crlf"


def test_mixed_eol_file_does_not_crash():
    text = "a\r\nb\nc\r\n"

    diff = diff_text("t.py", text, text.replace("b", "B"))

    assert detect_eol(text) == "mixed"
    assert diff.eol_before == "mixed"
    assert len([line for line in all_lines(diff) if line.kind == "del"]) == 1


def test_file_write_on_windows_reports_the_eol_translation(tmp_path):
    """`file_write` writes LF as CRLF and W7 does not change that -- but the
    diff must not therefore claim every line changed."""
    target = tmp_path / "out.txt"
    from djcode.tools.file_write import execute_file_write

    run(execute_file_write(str(target), "one\ntwo\n"))
    outcome = run(execute_file_write(str(target), "one\nTWO\n"))
    diff = FileDiff.from_dict(outcome.details["diff"])

    assert outcome.ok is True
    assert [line.text for line in all_lines(diff) if line.kind == "del"] == ["two"]
    assert [line.text for line in all_lines(diff) if line.kind == "add"] == ["TWO"]


# ── Bounds, and telling the truth about them ───────────────────────────────


def test_missing_pre_image_renders_unavailable_not_whole_file_added():
    """A pre-image evicted by the blob budget is NOT an empty file. Reporting
    "the whole file was added" would render 300 000 green lines and be a lie."""
    from rich.console import Console

    from djcode.frontends.repl.diffview import render_file_diff

    body = "secret line\n" * 50
    diff = FileDiff(path="big.py", unavailable="its previous contents were never saved")
    console = Console(file=__import__("io").StringIO(), width=100, force_terminal=False)
    render_file_diff(console, diff)
    printed = console.file.getvalue()

    assert diff.added == 0 and diff.hunks == []
    assert "never saved" in printed
    assert "secret line" not in printed and body not in printed


def test_too_many_lines_degrades_to_unavailable_not_a_hang():
    before = "x\n" * (MAX_DIFF_LINES + 5)

    diff = diff_text("huge.py", before, before + "y\n")

    assert diff.unavailable
    assert diff.hunks == []
    assert diff.before_text is None


def test_as_dict_is_json_serialisable_and_carries_no_file_body():
    """`details` rides `tool_result_event` into W10's JSONL on every edit.
    `_finish_checkpoint`'s rule is that a file body never goes there."""
    import json

    before = "".join(f"line {i}\n" for i in range(3000))
    after = before.replace("line 5\n", "line 5!\n")
    payload = diff_text("t.py", before, after).as_dict()

    json.dumps(payload)  # must not raise
    assert "before_text" not in payload and "after_text" not in payload
    assert sum(len(h["lines"]) for h in payload["hunks"]) <= 400


def test_from_dict_round_trips_the_rendered_shape():
    diff = diff_text("t.py", "a\nb\nc\n", "a\nB\nc\n")
    rebuilt = FileDiff.from_dict(diff.as_dict())

    assert rebuilt.path == diff.path
    assert [line.as_dict() for line in all_lines(rebuilt)] == [
        line.as_dict() for line in all_lines(diff)
    ]
    assert rebuilt.hunks[0].word_spans == diff.hunks[0].word_spans
    assert rebuilt.before_text is None and rebuilt.after_text is None


# ── Renderer mechanics ─────────────────────────────────────────────────────


def test_syntax_background_never_survives_regutter():
    """MEASURED: every monokai span carries `on #272822`, which overrides the
    row's add/del tint on every character -- the tint would be invisible. And
    `style + Style(bgcolor=None)` does NOT clear a bgcolor: rich overlays only
    SET attributes, so the style has to be rebuilt."""
    from rich.console import Console

    from djcode.frontends.repl.diffview import ADD_BG, render_file_diff

    console = Console(
        file=__import__("io").StringIO(),
        width=100,
        force_terminal=True,
        color_system="truecolor",
    )
    render_file_diff(console, diff_text("t.py", "x = 1\n", "x = 2\n"))
    out = console.file.getvalue()

    def triplet(hex_colour: str) -> str:
        h = hex_colour.lstrip("#")
        return f"48;2;{int(h[0:2], 16)};{int(h[2:4], 16)};{int(h[4:6], 16)}"

    assert triplet(ADD_BG) in out, "the add tint must reach the terminal"
    assert "48;2;39;40;34" not in out, "monokai's block background leaked through"


def test_no_phantom_trailing_line():
    """MEASURED: `Syntax.highlight(code).split(allow_blank=True)` returns N+1
    entries, the last empty, whether or not the code ends with a newline."""
    from djcode.frontends.repl import diffview

    diff = diff_text("t.py", "a = 1\nb = 2\n", "a = 1\nb = 3\n")
    hunk = diff.hunks[0]
    side = diffview._side_texts(diff, hunk, ("ctx", "add"), diff.after_text, hunk.new_start)

    assert len(side) == len([line for line in hunk.lines if line.kind in ("ctx", "add")])
    assert all(text.plain for text in side)


def test_long_line_keeps_its_gutter():
    """MEASURED: without `no_wrap`/`overflow`/`crop` AT PRINT TIME the gutter
    ends up alone on line one and the wrapped body carries no gutter at all.
    Setting `text.no_wrap = True` after construction does not suppress it."""
    from rich.console import Console

    from djcode.frontends.repl.diffview import render_file_diff

    before = "y = 1\n" + "z" * 300 + "\n"
    after = "y = 2\n" + "z" * 300 + "\n"
    console = Console(file=__import__("io").StringIO(), width=40, force_terminal=False)

    render_file_diff(console, diff_text("t.py", before, after))
    printed = [line for line in console.file.getvalue().splitlines() if line.strip()]

    assert printed
    assert all(len(line) <= 40 for line in printed), printed
    body = [line for line in printed if " | " in line]
    assert len(body) == 3, body
    # Every row carries its own gutter. The failure this pins is a WRAPPED row,
    # whose continuation lines have no gutter at all and no line number.
    assert all(re.match(r"^ {2}[-+ ] +\d+ \| ", line) for line in body), body
    assert body[-1].rstrip().endswith(("…", "...")), body[-1]


def test_non_cp1252_content_does_not_crash():
    """Reproduced live during the audit: printing CJK to a redirected stdout
    raises `UnicodeEncodeError: 'charmap' codec` out of cp1252. It applies to
    FILE CONTENT, not only to glyphs -- a CJK comment, an em-dash or a smart
    quote in a JS string is enough, and a diff renderer exists to print file
    content. Not skipped off Windows: the encoding is forced."""
    import io

    from rich.console import Console

    from djcode.frontends.repl.diffview import render_file_diff

    before = '// 日本語 — "smart"\nx = 1\n'
    after = '// 日本語 — "smart"\nx = 2\n'
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", newline="")
    console = Console(file=stream, width=80, force_terminal=False)

    render_file_diff(console, diff_text("t.js", before, after))
    stream.flush()  # must not raise UnicodeEncodeError


# ── The approval preview must match what the tool actually does ────────────


@pytest.mark.parametrize(
    ("body", "old", "new", "verdict", "writes"),
    [
        ("alpha\nbeta\ngamma\n", "beta", "BETA", "ok", True),
        ("dup\ndup\n", "dup", "x", "ambiguous", False),
        ("alpha\nbeta\n", "BETA", "delta", "not-found", False),
        ("alpha\nNEW\n", "OLD", "NEW", "already-applied", False),
        ("def f():\n\treturn 1\n", "    return 1", "    return 2", "normalised", True),
    ],
)
def test_approval_preview_matches_what_the_tool_does(tmp_path, body, old, new, verdict, writes):
    """Four of `file_edit`'s branches plus the normalised one. A preview that
    only does `str.replace` shows a confident diff for an edit the tool refuses,
    and no diff for the normalised match that the tool DOES apply."""
    target = tmp_path / "t.py"
    write(target, body)

    preview = preview_edit(str(target), old, new)
    assert preview.verdict == verdict
    assert preview.will_write is writes
    predicted = preview.diff.after_text if preview.diff is not None else None

    outcome = run(execute_file_edit(str(target), old, new))

    with open(target, encoding="utf-8", newline="") as handle:
        landed = handle.read()
    if writes:
        assert outcome.ok is True
        assert predicted is not None
        assert predicted.replace("\r\n", "\n") == landed.replace("\r\n", "\n")
    else:
        assert landed == body, "the tool wrote although the preview said it would not"


def test_preview_edit_on_a_missing_file():
    preview = preview_edit("does-not-exist-anywhere.py", "a", "b")
    assert preview.verdict == "missing" and preview.diff is None


def test_preview_write_distinguishes_new_from_empty(tmp_path):
    fresh = preview_write(str(tmp_path / "new.txt"), "hello\n")
    assert fresh.status == "added" and fresh.hunks[0].old_start == 0

    empty = tmp_path / "empty.txt"
    write(empty, "")
    over = preview_write(str(empty), "hello\n")
    assert over.status == "modified"


# ── file_edit's eight return paths, and the DAF wire ───────────────────────


@pytest.mark.parametrize(
    ("body", "old", "new", "ok", "fragment"),
    [
        (None, "a", "b", False, "File not found"),
        ("alpha\nbeta\n", "beta", "BETA", True, "replaced 1 occurrence"),
        ("dup\ndup\n", "dup", "x", False, "found 2 times"),
        ("alpha\nNEW\n", "OLD", "NEW", True, "Already applied"),
        ("def f():\n\treturn 1\n", "    return 1", "    return 2", True, "normalising"),
        ("alpha\nbeta\n", "zzzz", "y", False, "not found"),
    ],
)
def test_file_edit_outcome_ok_is_correct_on_every_path(tmp_path, body, old, new, ok, fragment):
    """`ok` became AUTHORITATIVE the moment this handler started returning a
    ToolOutcome (`_build_outcome` stamps `ok_source="handler"`, and
    `workflow._result_ok` then uses the flag directly). `ToolOutcome.ok`
    defaults to True, so a forgotten flag on an error path is a VERIFIED false
    green -- the exact regression class W3-VERIFICATION.md documents.

    Note the two that read backwards and are correct: "Already applied" is a
    SUCCESS that never says "Edited", and "found 2 times" is a FAILURE that
    wrote nothing."""
    target = tmp_path / "t.py"
    if body is not None:
        write(target, body)

    # Through the real chokepoint, because `ok_source` is stamped THERE. A
    # direct call would prove the flag but not the thing that makes it
    # authoritative on the DAF wire.
    from djcode.tools import dispatch_tool

    outcome = run(
        dispatch_tool(
            "file_edit", {"path": str(target), "old_string": old, "new_string": new}
        )
    )

    assert outcome.ok is ok, str(outcome)
    assert fragment in str(outcome)
    assert outcome.details["ok_source"] == "handler"


def test_ambiguous_edit_is_a_verified_failure_on_the_daf_wire():
    """`workflow._result_ok` is what decides whether a dependent DAF node runs.
    Before W7-2 file_edit's `ok` was "unverified" and the host fell back to a
    prefix sniff; now the flag is used directly, so a wrong one skips or runs a
    whole subgraph."""
    from djcode.core.outcome import ToolOutcome
    from djcode.workflow import _result_ok

    failed = ToolOutcome(content="Error: old_string found 2 times", ok=False,
                         details={"ok_source": "handler"})
    applied = ToolOutcome(content="Already applied: x already contains new_string", ok=True,
                          details={"ok_source": "handler"})

    assert _result_ok(failed) is False
    # The one that a prefix sniff gets right only by luck and a keyword sniff
    # gets wrong: it says neither "Error" nor "Edited".
    assert _result_ok(applied) is True


# ── /diff: the three bases ─────────────────────────────────────────────────


class _Store:
    """The two accessors W7 added to W5's store, with nothing else attached."""

    def __init__(self, baseline_rows, blobs):
        self._baseline = baseline_rows
        self._blobs = blobs

    def baseline(self, session_id, *, limit=5000):
        return dict(self._baseline), ""

    def blob(self, sha):
        return self._blobs.get(sha)


def test_diff_session_uses_the_stored_pre_image(tmp_path):
    target = tmp_path / "s.py"
    write(target, "alpha\nBETA\n")
    store = _Store({str(target): ("sha-1", True, "sha-2")}, {"sha-1": b"alpha\nbeta\n"})

    diffs = run(diff_paths("session", store, session_id="sess", cwd=str(tmp_path)))

    assert len(diffs) == 1
    assert [line.text for line in all_lines(diffs[0]) if line.kind == "add"] == ["BETA"]


def test_diff_session_reports_an_evicted_pre_image(tmp_path):
    target = tmp_path / "s.py"
    write(target, "alpha\n")
    store = _Store({str(target): ("sha-gone", True, "sha-2")}, {})

    diffs = run(diff_paths("session", store, session_id="sess", cwd=str(tmp_path)))

    assert diffs[0].unavailable and diffs[0].added == 0
    assert diffs[0].hunks == []


def test_diff_session_shows_a_created_file(tmp_path):
    target = tmp_path / "made.py"
    write(target, "brand new\n")
    store = _Store({str(target): (None, False, "sha-2")}, {})

    diffs = run(diff_paths("session", store, session_id="sess", cwd=str(tmp_path)))

    assert diffs[0].status == "added"
    assert diffs[0].hunks[0].old_start == 0


def test_diff_session_needs_no_git_at_all(tmp_path, monkeypatch):
    """The differentiator, and the reason `session` is the default: SSOT's
    argument against shadow-git is that DJcode must work in a directory that is
    not a repository."""

    async def explode(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("/diff session shelled out to git")

    monkeypatch.setattr("djcode.core.diff._git", explode)
    target = tmp_path / "s.py"
    write(target, "b\n")
    store = _Store({str(target): ("sha-1", True, None)}, {"sha-1": b"a\n"})

    diffs = run(diff_paths("session", store, session_id="sess", cwd=str(tmp_path)))
    assert diffs


def test_git_bases_degrade_honestly_outside_a_repository(tmp_path):
    for base in ("uncommitted", "branch"):
        diffs = run(diff_paths(base, None, cwd=str(tmp_path)))
        assert len(diffs) == 1
        assert "not a git repository" in diffs[0].unavailable


def test_diff_session_without_a_store_says_so():
    diffs = run(diff_paths("session", None, session_id="sess"))
    assert "checkpoints are not active" in diffs[0].unavailable


def test_unknown_base_is_reported():
    diffs = run(diff_paths("sideways", None))
    assert "unknown diff base" in diffs[0].unavailable


def test_diff_uncommitted_includes_untracked_files(tmp_path):
    """`git diff HEAD` never lists untracked paths, which on an agent session is
    most of what changed. Without the `ls-files --others` pass the mode silently
    omits every file the agent created."""
    import subprocess

    cwd = str(tmp_path)

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
        )

    if git("init", "-q").returncode != 0:  # pragma: no cover - git missing
        pytest.skip("git is not available")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    write(tmp_path / "tracked.txt", "one\n")
    git("add", "tracked.txt")
    git("-c", "commit.gpgsign=false", "commit", "-qm", "init")
    write(tmp_path / "tracked.txt", "two\n")
    write(tmp_path / "created.txt", "fresh\n")

    diffs = run(diff_paths("uncommitted", None, cwd=cwd))
    paths = {d.path for d in diffs}

    assert "tracked.txt" in paths
    assert "created.txt" in paths, "an untracked file the agent created was omitted"
    created = next(d for d in diffs if d.path == "created.txt")
    assert created.status == "added"


def test_diff_branch_resolves_a_base_without_an_upstream(tmp_path):
    """MEASURED on this box: the working branch has no upstream at all, so
    `@{u}` alone makes /diff branch useless. The ladder falls through to
    main/master."""
    import subprocess

    cwd = str(tmp_path)

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
        )

    if git("init", "-q", "-b", "main").returncode != 0:  # pragma: no cover
        pytest.skip("git is not available")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    write(tmp_path / "a.txt", "one\n")
    git("add", "a.txt")
    git("-c", "commit.gpgsign=false", "commit", "-qm", "init")
    git("checkout", "-q", "-b", "work")
    write(tmp_path / "a.txt", "two\n")

    diffs = run(diff_paths("branch", None, cwd=cwd))

    assert not diffs[0].unavailable, diffs[0].unavailable
    assert any(d.path == "a.txt" for d in diffs)


# ── The two accessors W7 added to W5's store ───────────────────────────────


def test_checkpoint_store_blob_and_baseline_round_trip(tmp_path):
    from djcode.core.checkpoints import CheckpointStore
    from djcode.sessions import SessionDB

    db = SessionDB(tmp_path / "s.db")
    store = CheckpointStore(db)
    target = tmp_path / "t.txt"
    target.write_bytes(b"first\n")

    async def edit(old, new):
        pending = await store.before(
            "file_edit", {"path": str(target)}, session_id="sess", cwd=str(tmp_path)
        )
        target.write_bytes(new)
        return await store.after(pending)

    run(edit(b"first\n", b"second\n"))
    run(edit(b"second\n", b"third\n"))

    baseline, note = store.baseline("sess")
    assert note == ""
    key = next(iter(baseline))
    pre_sha, existed, post_sha = baseline[key]
    assert existed is True
    # The OLDEST checkpoint owns the content, the NEWEST owns the post sha.
    assert store.blob(pre_sha) == b"first\n"
    assert store.blob(post_sha) is None or isinstance(store.blob(post_sha), bytes)
    assert store.blob(None) is None
    assert store.blob("not-a-real-sha") is None


def test_checkpoint_diffs_land_on_the_outcome_for_a_non_file_tool(tmp_path):
    """The step-5 diff half covers every checkpointed tool, not only the two
    that build their own -- a `bash` command that rewrote three files included."""
    from djcode.core.checkpoints import CheckpointStore
    from djcode.sessions import SessionDB
    from djcode.tools import _checkpoint_diffs

    db = SessionDB(tmp_path / "s.db")
    store = CheckpointStore(db)
    target = tmp_path / "n.txt"
    target.write_bytes(b"before\n")

    async def go():
        pending = await store.before(
            "notebook_edit", {"path": str(target)}, session_id="sess", cwd=str(tmp_path)
        )
        target.write_bytes(b"after\n")
        checkpoint = await store.after(pending)
        return await _checkpoint_diffs(store, checkpoint)

    diffs = run(go())

    assert len(diffs) == 1
    rebuilt = FileDiff.from_dict(diffs[0])
    assert [line.text for line in all_lines(rebuilt) if line.kind == "del"] == ["before"]
    assert [line.text for line in all_lines(rebuilt) if line.kind == "add"] == ["after"]


def test_sqlite_blob_survives_binary_bytes(tmp_path):
    """`blob()` returns bytes, not a decoded str -- the whole reason W5 stored
    bytes in the first place."""
    from djcode.core.checkpoints import CheckpointStore
    from djcode.sessions import SessionDB

    db = SessionDB(tmp_path / "s.db")
    store = CheckpointStore(db)
    target = tmp_path / "b.bin"
    payload = bytes(range(256))
    target.write_bytes(payload)

    async def go():
        pending = await store.before(
            "file_write", {"path": str(target)}, session_id="sess", cwd=str(tmp_path)
        )
        target.write_bytes(payload + b"\x01")
        return await store.after(pending)

    checkpoint = run(go())
    assert checkpoint is not None
    got = store.blob(checkpoint.files[0].pre_sha)
    assert isinstance(got, bytes | sqlite3.Binary) and bytes(got) == payload


# ── The DAF wire, on the real engine ───────────────────────────────────────


@pytest.mark.skipif(
    __import__("shutil").which("cargo") is None
    and not __import__("os").environ.get("DJCODE_DAF_ENGINE"),
    reason="DAF engine needs a Rust toolchain; set DJCODE_DAF_ENGINE for a prebuilt one",
)
@pytest.mark.parametrize("mode", ["daf", "native"])
def test_failed_edit_skips_its_dependent_on_both_engines(tmp_path, monkeypatch, daf_runtime, mode):
    """The W3 trap in its W7 form, exercised on the REAL Rust host.

    `cargo` is installed on this box, so `WorkflowEngine.execute` takes the DAF
    branch and a test that only checked `_result_ok` in isolation would prove
    nothing about what actually runs here. `file_edit` returning a ToolOutcome
    made its `ok` authoritative on that wire (`workflow.py:_result_ok` returns
    the flag directly once `ok_source != "unverified"`), so a wrong `False`
    skips a whole subgraph and a wrong `True` runs every dependent node against
    an edit that never happened.

    Both engines are parametrised on purpose: `_execute_native` is the branch
    that runs on a box WITHOUT Rust, and a regression there is invisible to
    anyone testing on a box with it -- the asymmetry `core/outcome.py`'s module
    docstring warns about.
    """
    import djcode.workflow as workflow
    from djcode.tools import dispatch_tool
    from djcode.workflow import DEPENDENCY_SKIPPED, WorkflowEngine

    monkeypatch.setattr(workflow, "CONFIG_DIR", daf_runtime)
    target = tmp_path / "amb.py"
    write(target, "dup\ndup\n")
    ran: list[str] = []

    async def dispatch(name, args):
        ran.append(name)
        return await dispatch_tool(name, args)

    engine = WorkflowEngine(mode=mode)

    async def go():
        return await engine.execute(
            [
                {
                    "id": "edit",
                    "name": "file_edit",
                    # Ambiguous: file_edit refuses and writes nothing.
                    "arguments": {
                        "path": str(target),
                        "old_string": "dup",
                        "new_string": "x",
                    },
                    "dependencies": [],
                },
                {
                    "id": "after",
                    "name": "file_read",
                    "arguments": {"path": str(target)},
                    "dependencies": ["edit"],
                },
            ],
            dispatch,
            1,
        )

    results = run(go())

    # Prove the DAF half actually ran the Rust host rather than falling back --
    # `execute` degrades to native on DAFUnavailableError, and a silent
    # fallback would make the daf parameter a duplicate of the native one.
    assert engine.mode == mode
    if mode == "daf":
        assert any(event["event"] == "wire" for event in engine.last_events)

    assert "found 2 times" in results["edit"]
    assert results["after"] == DEPENDENCY_SKIPPED
    assert ran == ["file_edit"], "the dependent ran against an edit that never happened"
    with open(target, encoding="utf-8", newline="") as handle:
        assert handle.read() == "dup\ndup\n"


@pytest.mark.skipif(
    __import__("shutil").which("cargo") is None
    and not __import__("os").environ.get("DJCODE_DAF_ENGINE"),
    reason="DAF engine needs a Rust toolchain; set DJCODE_DAF_ENGINE for a prebuilt one",
)
@pytest.mark.parametrize("mode", ["daf", "native"])
def test_successful_edit_runs_its_dependent_on_both_engines(
    tmp_path, monkeypatch, daf_runtime, mode
):
    """The other half, and the one a too-eager `ok=False` would break silently."""
    import djcode.workflow as workflow
    from djcode.tools import dispatch_tool
    from djcode.workflow import WorkflowEngine

    monkeypatch.setattr(workflow, "CONFIG_DIR", daf_runtime)
    target = tmp_path / "good.py"
    write(target, "alpha\nbeta\n")
    ran: list[str] = []

    async def dispatch(name, args):
        ran.append(name)
        return await dispatch_tool(name, args)

    async def go():
        engine = WorkflowEngine(mode=mode)
        return await engine.execute(
            [
                {
                    "id": "edit",
                    "name": "file_edit",
                    "arguments": {
                        "path": str(target),
                        "old_string": "beta",
                        "new_string": "BETA",
                    },
                    "dependencies": [],
                },
                {
                    "id": "after",
                    "name": "file_read",
                    "arguments": {"path": str(target)},
                    "dependencies": ["edit"],
                },
            ],
            dispatch,
            1,
        )

    results = run(go())

    assert "replaced 1 occurrence" in results["edit"]
    assert ran == ["file_edit", "file_read"]
    assert "BETA" in results["after"]
