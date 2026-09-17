"""Diffs as data — what the agent changed, in a form any front-end can draw.

P0-3. Until this module existed a DJcode user could not see what a tool did to
a file: the approval card printed ``json.dumps(arguments)`` and the tool card
printed the first three lines of "Edited C:\\x\\y.py: replaced 1 occurrence".

Everything here is DATA. No ``rich``, no colour, no glyphs, no console --
``tests/test_headless_purity.py`` walks this module's import closure and fails
the build if any of that appears. The terminal half lives in
``djcode.frontends.repl.diffview`` and a GUI is expected to write its own.

WHERE THE TWO IMAGES COME FROM (they are not the same at both moments)
---------------------------------------------------------------------
* **Approval time** -- nothing has run, so there is no checkpoint and no post
  image. ``preview_edit`` / ``preview_write`` read the file off disk and
  *simulate* the result, reusing ``file_edit``'s own matching so the card
  cannot promise an edit the tool will then refuse.
* **Result time** -- W5's ``CheckpointStore`` has already stored the pre-image
  bytes in its blob table, keyed by ``pre_sha``, *before* the handler ran
  (``checkpoints.py::_before_file``). We read it back rather than snapshotting
  a second time; the post image is simply the file on disk.

THINGS MEASURED ON WINDOWS THAT THIS MODULE HAS TO SURVIVE
----------------------------------------------------------
* ``file_write`` writes through ``Path.write_text`` (``newline=None``), so every
  ``\\n`` becomes ``\\r\\n`` on disk, while ``file_edit`` reads and writes with
  ``newline=""``. Diffing raw bytes across those two would mark 100% of every
  file ``file_write`` touches as changed. Line endings are therefore detected
  on the raw text (``eol_before`` / ``eol_after``), then normalised away before
  the comparison. This also makes ``session``, ``uncommitted`` and ``branch``
  agree on a box with ``core.autocrlf=true``, where ``git diff`` normalises and
  the checkpoint bytes do not.
* ``str.splitlines()`` splits on ``\\x0c``, ``\\x0b``, ``\\x85`` and ``\\u2028``,
  all of which appear inside real source files. Every line number downstream of
  such a character would disagree with the user's editor and with
  ``file_edit``'s own ``content.count("\\n", 0, start) + 1``. We split on
  ``"\\n"`` only, everywhere, deliberately.
* ``difflib.SequenceMatcher``'s ``autojunk`` heuristic fires at 200 elements and
  turns a four-character change on a long line into a 64-character one
  (measured). Every matcher in this file passes ``autojunk=False``.
* ``difflib.unified_diff`` is not used: its ``@@`` header omits the length when
  it is 1, so a regex over it breaks on every single-line hunk, and it gives no
  way to tell that a hunk is exactly one deletion paired with one insertion --
  which is the condition the word-level rule turns on. Opcodes carry both for
  free.

WHAT IS NOT IN ``ToolOutcome.details``
--------------------------------------
File bodies. ``tools/__init__.py::_finish_checkpoint`` already wrote that rule
down and the reason (``details`` rides on ``tool_result_event``, which W10
serialises to JSONL). ``as_dict`` therefore emits hunks only, capped at
``MAX_DETAIL_LINES``, and never ``before_text``/``after_text`` -- those two
fields exist on the live object for an in-process renderer that wants full-file
lexing context, and they are dropped at the serialisation boundary.
"""

from __future__ import annotations

import asyncio
import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Bounds. Every one of these is a place where the honest answer is "I stopped",
# and every one of them sets ``note`` when it trips rather than truncating
# silently -- W5's house rule: state the coverage, never imply it.
# ---------------------------------------------------------------------------

#: Lines of unchanged context kept either side of a change.
DEFAULT_CONTEXT = 3

#: Below this similarity, two one-to-one lines are treated as unrelated and get
#: no word-level spans. Measured: an unrelated pair scores 0.21 and produces ten
#: scattered underlines; a genuine one-token edit scores 0.70.
WORD_LEVEL_MIN_RATIO = 0.40

#: Per side. ``SequenceMatcher`` is quadratic in the worst case; two 500k-line
#: lists would hang the REPL with no way out.
MAX_DIFF_LINES = 50_000

#: Bytes of file body a renderer should be willing to hand to a syntax lexer.
SYNTAX_MAX_BYTES = 256 * 1024

#: Bytes sniffed for a NUL before deciding a file is binary.
BINARY_SNIFF_BYTES = 8192

#: Hunk lines allowed into ``as_dict`` -- i.e. into ``ToolOutcome.details`` and
#: from there into W10's JSONL.
MAX_DETAIL_LINES = 400

#: Files a single ``/diff`` invocation will build.
MAX_DIFF_FILES = 400

#: Bytes kept from one ``git`` invocation before we stop trusting the output.
MAX_GIT_BYTES = 8 * 1024 * 1024

_TOKEN = re.compile(r"\w+|\s+|.", re.DOTALL)

#: Suffix -> pygments lexer name. Derived ONCE, here, so the approval card, the
#: tool card and ``/diff`` cannot disagree about what language a file is. A
#: lexer *name* is a string; naming one costs core no import.
_LEXERS: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".rs": "rust",
    ".go": "go",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "jsx",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".json": "json",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".md": "markdown",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".ps1": "powershell",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cc": "cpp",
    ".java": "java",
    ".kt": "kotlin",
    ".swift": "swift",
    ".rb": "ruby",
    ".php": "php",
    ".sql": "sql",
    ".xml": "xml",
    ".ini": "ini",
    ".cfg": "ini",
    ".lua": "lua",
    ".pl": "perl",
    ".vue": "vue",
    ".svelte": "html",
}


def lexer_for(path: str) -> str | None:
    """The pygments lexer name for a path, or ``None`` when we do not know."""
    name = Path(path).name.lower()
    if name in {"dockerfile", "containerfile"}:
        return "docker"
    if name in {"makefile", "gnumakefile"}:
        return "make"
    return _LEXERS.get(Path(name).suffix)


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------

LineKind = Literal["ctx", "add", "del", "meta"]


@dataclass(slots=True)
class DiffLine:
    """One rendered row.

    ``text`` carries no gutter and no trailing newline. ``old_no`` is the line
    number in the ORIGINAL file and ``new_no`` in the new one; an ``add`` has no
    ``old_no`` and a ``del`` has no ``new_no``. ``meta`` is git's
    ``\\ No newline at end of file`` and belongs to neither side.
    """

    kind: LineKind
    old_no: int | None
    new_no: int | None
    text: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "old_no": self.old_no,
            "new_no": self.new_no,
            "text": self.text,
        }


@dataclass(slots=True)
class Hunk:
    """A contiguous run of changes plus its context.

    ``word_spans`` maps an index into ``lines`` to a list of ``(start, end)``
    character offsets into that line's ``text``. It is computed HERE, not in a
    renderer: three surfaces draw these diffs and if each computed its own
    intra-line highlighting two of them would be wrong. It is also what makes
    "a re-indent produces no word-level spans" testable without importing rich.

    ``kind`` is ``"whitespace"`` when every change in the hunk is an indentation
    or trailing-space change -- ``strip()``-equal either side. A 200-line
    re-indent is 400 lines that differ in no character the eye can see, and a
    renderer that prints them all buries whatever real change shares the file.
    """

    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list[DiffLine] = field(default_factory=list)
    word_spans: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    kind: str = "change"  # "change" | "whitespace"

    def as_dict(self) -> dict[str, Any]:
        return {
            "old_start": self.old_start,
            "old_len": self.old_len,
            "new_start": self.new_start,
            "new_len": self.new_len,
            "kind": self.kind,
            "lines": [line.as_dict() for line in self.lines],
            "word_spans": {str(k): [list(s) for s in v] for k, v in self.word_spans.items()},
        }


@dataclass(slots=True)
class FileDiff:
    """Everything a surface needs to draw one file's change, and nothing else.

    The fields past ``binary`` are not in the blueprint's sketch. Each is there
    because without it a renderer has to either guess or lie:

    ``lexer``          so all three surfaces highlight the same file the same way.
    ``note``           coverage the user has to be told about (a cap was hit).
    ``unavailable``    the pre-image was never stored (oversize, or evicted by
                       the blob budget). Rendering "the whole file was added"
                       instead would be a 300 000-line lie.
    ``whitespace_only`` no character changed except indentation.
    ``newline_at_eof_changed`` a lost final newline, which otherwise renders as
                       one deletion and one insertion of a visually IDENTICAL
                       line -- indistinguishable from a re-indent in the UI.
    ``eol_before`` / ``eol_after``  CRLF vs LF, normalised out of the comparison
                       but reported, because on Windows it is a real change.
    ``before_text`` / ``after_text``  live-only. Present when the diff was built
                       in process, so a renderer can lex with full leading
                       context instead of from the top of a hunk. NEVER
                       serialised -- see the module docstring.
    """

    path: str
    hunks: list[Hunk] = field(default_factory=list)
    added: int = 0
    removed: int = 0
    binary: bool = False
    lexer: str | None = None
    note: str = ""
    unavailable: str = ""
    whitespace_only: bool = False
    newline_at_eof_changed: str = ""  # "" | "added" | "removed"
    eol_before: str = ""  # "" | "none" | "lf" | "crlf" | "cr" | "mixed"
    eol_after: str = ""
    highlight_degraded: bool = False
    size_before: int | None = None
    size_after: int | None = None
    status: str = "modified"  # modified | added | deleted
    before_text: str | None = None
    after_text: str | None = None

    @property
    def eol_changed(self) -> bool:
        return bool(
            self.eol_before
            and self.eol_after
            and self.eol_before != self.eol_after
            and "none" not in (self.eol_before, self.eol_after)
        )

    @property
    def empty(self) -> bool:
        """Nothing at all to show: no hunks, no EOL flip, no newline marker."""
        return not self.hunks and not self.newline_at_eof_changed and not self.eol_changed

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable, bounded, and without the file bodies.

        Modelled on ``RestoreReport.as_dict``. Hunks are truncated at
        ``MAX_DETAIL_LINES`` and ``note`` says so, because this is what lands in
        ``ToolOutcome.details`` and rides ``tool_result_event`` into W10's JSONL
        on every single edit.
        """
        kept: list[dict[str, Any]] = []
        budget = MAX_DETAIL_LINES
        trimmed = False
        for hunk in self.hunks:
            if budget <= 0:
                trimmed = True
                break
            if len(hunk.lines) > budget:
                trimmed = True
                kept.append(
                    Hunk(
                        old_start=hunk.old_start,
                        old_len=hunk.old_len,
                        new_start=hunk.new_start,
                        new_len=hunk.new_len,
                        lines=hunk.lines[:budget],
                        word_spans={k: v for k, v in hunk.word_spans.items() if k < budget},
                        kind=hunk.kind,
                    ).as_dict()
                )
                break
            kept.append(hunk.as_dict())
            budget -= len(hunk.lines)
        note = self.note
        if trimmed:
            extra = f"only the first {MAX_DETAIL_LINES} diff lines ride the event stream"
            note = f"{note}; {extra}" if note else extra
        return {
            "path": self.path,
            "hunks": kept,
            "added": self.added,
            "removed": self.removed,
            "binary": self.binary,
            "lexer": self.lexer,
            "note": note,
            "unavailable": self.unavailable,
            "whitespace_only": self.whitespace_only,
            "newline_at_eof_changed": self.newline_at_eof_changed,
            "eol_before": self.eol_before,
            "eol_after": self.eol_after,
            "highlight_degraded": self.highlight_degraded or trimmed,
            "size_before": self.size_before,
            "size_after": self.size_after,
            "status": self.status,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> FileDiff:
        """Rebuild from ``as_dict``. ``before_text``/``after_text`` stay ``None``.

        A renderer handed one of these has no full-file text, so it lexes each
        hunk as its own block and marks the result degraded -- honest, and
        visibly different from the in-process path.
        """
        hunks = []
        for h in data.get("hunks") or []:
            lines = [
                DiffLine(
                    kind=line.get("kind", "ctx"),
                    old_no=line.get("old_no"),
                    new_no=line.get("new_no"),
                    text=line.get("text", ""),
                )
                for line in h.get("lines") or []
            ]
            spans = {
                int(k): [(int(a), int(b)) for a, b in v]
                for k, v in (h.get("word_spans") or {}).items()
            }
            hunks.append(
                Hunk(
                    old_start=int(h.get("old_start", 0)),
                    old_len=int(h.get("old_len", 0)),
                    new_start=int(h.get("new_start", 0)),
                    new_len=int(h.get("new_len", 0)),
                    lines=lines,
                    word_spans=spans,
                    kind=h.get("kind", "change"),
                )
            )
        return FileDiff(
            path=data.get("path", ""),
            hunks=hunks,
            added=int(data.get("added", 0)),
            removed=int(data.get("removed", 0)),
            binary=bool(data.get("binary", False)),
            lexer=data.get("lexer"),
            note=data.get("note", ""),
            unavailable=data.get("unavailable", ""),
            whitespace_only=bool(data.get("whitespace_only", False)),
            newline_at_eof_changed=data.get("newline_at_eof_changed", ""),
            eol_before=data.get("eol_before", ""),
            eol_after=data.get("eol_after", ""),
            highlight_degraded=bool(data.get("highlight_degraded", False)),
            size_before=data.get("size_before"),
            size_after=data.get("size_after"),
            status=data.get("status", "modified"),
        )


@dataclass(slots=True)
class EditPreview:
    """What ``preview_edit`` found, before anything was written.

    ``verdict`` mirrors ``file_edit``'s own branches exactly, so the approval
    card never shows a confident diff for an edit the tool is about to refuse:

    ``ok``              unique byte-for-byte match; the diff is what will happen.
    ``normalised``      no byte match, but exactly one whitespace-normalised
                        region matches -- ``file_edit`` WILL write, at a
                        different span than ``old_string``, and the diff shows
                        that span.
    ``already-applied`` ``new_string`` is already there; the tool writes nothing.
    ``ambiguous``       more than one match; the tool refuses.
    ``not-found``       no match at all; the tool refuses.
    ``missing``         the file does not exist; the tool refuses.
    """

    verdict: str
    diff: FileDiff | None = None
    message: str = ""

    @property
    def will_write(self) -> bool:
        return self.verdict in {"ok", "normalised"}


# ---------------------------------------------------------------------------
# Text mechanics
# ---------------------------------------------------------------------------


def detect_eol(text: str) -> str:
    """``"lf"``/``"crlf"``/``"cr"``/``"mixed"``/``"none"``, from the RAW text."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    present = [name for name, n in (("crlf", crlf), ("lf", lf), ("cr", cr)) if n]
    if not present:
        return "none"
    return present[0] if len(present) == 1 else "mixed"


def normalise_eol(text: str) -> str:
    """Line endings out of the comparison. See the module docstring."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def split_lines(text: str) -> tuple[list[str], bool]:
    """``(lines, ends_with_newline)``, splitting on ``"\\n"`` and nothing else.

    NOT ``str.splitlines()``: that also splits on form feed, vertical tab, NEL
    and U+2028, every one of which occurs in real source and none of which ends
    a line in any editor or in ``file_edit``'s own line arithmetic.
    """
    if not text:
        return [], False
    ends = text.endswith("\n")
    lines = text.split("\n")
    if ends:
        lines.pop()
    return lines, ends


def looks_binary(data: bytes) -> bool:
    """A NUL in the first 8 KiB, or not valid UTF-8.

    Classified on BYTES, before any decode, and never with ``errors="replace"``
    -- that is how a JPEG becomes four hundred lines of U+FFFD. UTF-16 text has
    a NUL on every ASCII character and lands here too, which is correct: we
    cannot line-diff it meaningfully and pretending otherwise is mojibake.
    """
    if b"\x00" in data[:BINARY_SNIFF_BYTES]:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text)


def _offsets(tokens: list[str]) -> list[int]:
    out = [0]
    acc = 0
    for token in tokens:
        acc += len(token)
        out.append(acc)
    return out


def _word_spans(old: str, new: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]] | None:
    """Token-level spans for one deletion paired with one insertion.

    Three rules the blueprint does not state, each of which was measured to
    matter:

    1. Leading whitespace is stripped before matching (the blueprint's rule), and
       the resulting offsets are remapped with EACH LINE'S OWN indent width. One
       shared offset is the obvious bug and it only shows when the two indents
       differ -- i.e. exactly in the re-indent-plus-edit case.
    2. A similarity floor. A deletion and an unrelated insertion satisfy
       one-to-one perfectly and score 0.21, producing ten meaningless underlines.
    3. Tokens, not characters. Character-level matching underlines *inside*
       ``bool`` -> ``Decision``; token-level gives three clean whole-word spans.
    """
    old_off = len(old) - len(old.lstrip())
    new_off = len(new) - len(new.lstrip())
    a, b = old.lstrip(), new.lstrip()
    if a == b:
        return None
    if difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() < WORD_LEVEL_MIN_RATIO:
        return None
    ta, tb = _tokens(a), _tokens(b)
    pa, pb = _offsets(ta), _offsets(tb)
    matcher = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    old_spans: list[tuple[int, int]] = []
    new_spans: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:
            old_spans.append((old_off + pa[i1], old_off + pa[i2]))
        if j2 > j1:
            new_spans.append((new_off + pb[j1], new_off + pb[j2]))
    if not old_spans and not new_spans:
        return None
    return old_spans, new_spans


def _is_whitespace_change(dels: list[str], adds: list[str]) -> bool:
    """Every paired line says the same thing with different whitespace."""
    if not dels or len(dels) != len(adds):
        return False
    return all(d != a and d.strip() == a.strip() for d, a in zip(dels, adds, strict=True))


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------


def _build_hunks(
    old_lines: list[str], new_lines: list[str], context: int
) -> tuple[list[Hunk], int, int, bool]:
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    hunks: list[Hunk] = []
    added = removed = 0
    real_ops = ws_ops = 0

    for group in matcher.get_grouped_opcodes(context):
        lines: list[DiffLine] = []
        spans: dict[int, list[tuple[int, int]]] = {}
        h_real = h_ws = 0
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for k in range(i1, i2):
                    lines.append(DiffLine("ctx", k + 1, j1 + (k - i1) + 1, old_lines[k]))
                continue
            h_real += 1
            del_at: list[int] = []
            add_at: list[int] = []
            for k in range(i1, i2):
                del_at.append(len(lines))
                lines.append(DiffLine("del", k + 1, None, old_lines[k]))
                removed += 1
            for k in range(j1, j2):
                add_at.append(len(lines))
                lines.append(DiffLine("add", None, k + 1, new_lines[k]))
                added += 1
            if tag != "replace":
                continue
            if _is_whitespace_change(old_lines[i1:i2], new_lines[j1:j2]):
                h_ws += 1
                continue
            # THE ONE-TO-ONE RULE. Anything else -- 1->2, 2->1, 2->2, a pure
            # delete, a pure insert -- gets no word-level spans at all. Pairing
            # del[k] with add[k] positionally would light up unrelated lines,
            # which is the noise this rule exists to prevent.
            if len(del_at) == 1 and len(add_at) == 1:
                pair = _word_spans(old_lines[i1], new_lines[j1])
                if pair is not None:
                    old_spans, new_spans = pair
                    if old_spans:
                        spans[del_at[0]] = old_spans
                    if new_spans:
                        spans[add_at[0]] = new_spans
        i_first, i_last = group[0][1], group[-1][2]
        j_first, j_last = group[0][3], group[-1][4]
        old_len = i_last - i_first
        new_len = j_last - j_first
        hunks.append(
            Hunk(
                # 1-based, except a zero-length side, which is 0 -- the same
                # convention git prints as "@@ -0,0 +1,2 @@" for a new file.
                old_start=i_first + 1 if old_len else i_first,
                old_len=old_len,
                new_start=j_first + 1 if new_len else j_first,
                new_len=new_len,
                lines=lines,
                word_spans=spans,
                kind="whitespace" if h_real and h_ws == h_real else "change",
            )
        )
        real_ops += h_real
        ws_ops += h_ws

    whitespace_only = bool(real_ops) and ws_ops == real_ops
    return hunks, added, removed, whitespace_only


def diff_text(
    path: str,
    before: str | None,
    after: str | None,
    *,
    context: int = DEFAULT_CONTEXT,
    status: str = "",
) -> FileDiff:
    """Diff two decoded texts. ``None`` means the file did not exist.

    ``None`` and ``""`` are deliberately different: absent renders as "new file",
    empty renders as "was empty". Collapsing them loses the distinction the user
    most needs on a create.
    """
    if not status:
        if before is None:
            status = "added"
        elif after is None:
            status = "deleted"
        else:
            status = "modified"

    eol_before = detect_eol(before) if before is not None else ""
    eol_after = detect_eol(after) if after is not None else ""

    norm_before = normalise_eol(before) if before is not None else ""
    norm_after = normalise_eol(after) if after is not None else ""
    old_lines, old_ends = split_lines(norm_before)
    new_lines, new_ends = split_lines(norm_after)

    diff = FileDiff(
        path=path,
        lexer=lexer_for(path),
        eol_before=eol_before,
        eol_after=eol_after,
        status=status,
        size_before=len(before.encode("utf-8", "surrogatepass")) if before is not None else None,
        size_after=len(after.encode("utf-8", "surrogatepass")) if after is not None else None,
        before_text=norm_before if before is not None else None,
        after_text=norm_after if after is not None else None,
    )

    if len(old_lines) > MAX_DIFF_LINES or len(new_lines) > MAX_DIFF_LINES:
        diff.unavailable = (
            f"file too large to diff ({max(len(old_lines), len(new_lines))} lines; "
            f"the limit is {MAX_DIFF_LINES})"
        )
        diff.before_text = diff.after_text = None
        return diff

    hunks, added, removed, whitespace_only = _build_hunks(old_lines, new_lines, context)
    diff.hunks = hunks
    diff.added = added
    diff.removed = removed
    diff.whitespace_only = whitespace_only

    # A lost or gained final newline is a MARKER, never a line change. Because
    # split_lines drops the trailing "" the two sides compare equal, so the
    # change produces no opcodes at all -- which is exactly right, and is why
    # the fact has to be carried separately or it vanishes entirely.
    if before is not None and after is not None and old_ends != new_ends:
        diff.newline_at_eof_changed = "removed" if old_ends else "added"
        if diff.hunks:
            diff.hunks[-1].lines.append(
                DiffLine("meta", None, None, "\\ No newline at end of file")
            )

    if len(norm_before) > SYNTAX_MAX_BYTES or len(norm_after) > SYNTAX_MAX_BYTES:
        diff.highlight_degraded = True

    return diff


def diff_bytes(
    path: str,
    before: bytes | None,
    after: bytes | None,
    *,
    context: int = DEFAULT_CONTEXT,
    status: str = "",
) -> FileDiff:
    """Diff two byte images. This is the load-bearing entry point.

    Everything the checkpoint store holds is ``bytes`` -- W5 chose that
    deliberately, because a text round trip corrupts line endings and destroys
    any file that is not valid UTF-8. The decode, and the decision not to
    decode, belong here.
    """
    binary = (before is not None and looks_binary(before)) or (
        after is not None and looks_binary(after)
    )
    if binary:
        return FileDiff(
            path=path,
            hunks=[],
            added=0,
            removed=0,
            binary=True,
            size_before=len(before) if before is not None else None,
            size_after=len(after) if after is not None else None,
            status=status
            or ("added" if before is None else "deleted" if after is None else "modified"),
        )
    text_before = before.decode("utf-8") if before is not None else None
    text_after = after.decode("utf-8") if after is not None else None
    return diff_text(path, text_before, text_after, context=context, status=status)


# ---------------------------------------------------------------------------
# Approval-time previews. Nothing has run; the post image is simulated.
# ---------------------------------------------------------------------------


def preview_edit(
    path: str, old_string: str, new_string: str, *, context: int = DEFAULT_CONTEXT
) -> EditPreview:
    """What ``file_edit`` would do to this file, using ``file_edit``'s own rules.

    A naive ``content.replace(old_string, new_string, 1)`` preview is wrong three
    ways, and all three are branches ``file_edit`` already implements: more than
    one match means the tool REFUSES; zero byte matches with one
    whitespace-normalised match means the tool writes at a DIFFERENT span; and
    ``new_string`` already present means the tool writes NOTHING. Showing a
    confident diff for any of those is worse than showing none, so this calls
    into ``file_edit._normalised_span`` rather than reimplementing the match.
    """
    # Deferred: djcode.tools is inside djcode.core's import closure (via
    # provider -> capabilities -> tools), so a module-level import here would
    # close the cycle during interpreter start-up.
    from djcode.tools.file_edit import _normalised_span, _read

    target = Path(path).expanduser().resolve()
    if not target.is_file():
        return EditPreview("missing", None, f"File not found: {path}")
    try:
        content = _read(target)
    except (OSError, UnicodeDecodeError) as exc:
        return EditPreview("missing", None, f"Cannot read {path}: {exc}")

    count = content.count(old_string)
    if count == 1:
        after = content.replace(old_string, new_string, 1)
        return EditPreview("ok", diff_text(str(target), content, after, context=context))
    if count > 1:
        return EditPreview(
            "ambiguous",
            None,
            f"old_string appears {count} times; file_edit will refuse this edit.",
        )
    if new_string and new_string in content and old_string != new_string:
        return EditPreview(
            "already-applied",
            None,
            "new_string is already present; file_edit will not write.",
        )
    span = _normalised_span(content, old_string)
    if span == "ambiguous":
        return EditPreview(
            "ambiguous",
            None,
            "ignoring whitespace, old_string matches more than one place; "
            "file_edit will refuse this edit.",
        )
    if isinstance(span, tuple):
        start, end = span
        after = content[:start] + new_string + content[end:]
        line = content.count("\n", 0, start) + 1
        preview = diff_text(str(target), content, after, context=context)
        preview.note = (
            f"old_string does not match byte for byte; file_edit will edit line {line}, "
            "matched after normalising whitespace and line endings"
        )
        return EditPreview("normalised", preview)
    return EditPreview("not-found", None, "old_string is not in the file; file_edit will refuse.")


def preview_write(path: str, content: str, *, context: int = DEFAULT_CONTEXT) -> FileDiff:
    """What ``file_write`` would do. Text in, text out -- never bytes.

    ``file_write`` uses ``Path.write_text`` with ``newline=None``, so what lands
    on disk on Windows is not the string it was handed. Comparing the argument
    (LF) against the disk image (CRLF) would mark every line changed. Both sides
    here are ``str`` and ``diff_text`` normalises line endings out, so the
    preview shows the content change and reports the EOL change separately.
    """
    target = Path(path).expanduser().resolve()
    before: str | None = None
    if target.is_file():
        try:
            raw = target.read_bytes()
        except OSError:
            raw = b""
        if looks_binary(raw):
            return FileDiff(
                path=str(target),
                binary=True,
                size_before=len(raw),
                size_after=len(content.encode("utf-8", "surrogatepass")),
            )
        before = raw.decode("utf-8")
    return diff_text(str(target), before, content, context=context)


# ---------------------------------------------------------------------------
# /diff — three bases, one builder, one renderer
# ---------------------------------------------------------------------------


async def _git(args: list[str], cwd: str) -> tuple[int, bytes, bytes]:
    """One ``git`` invocation, as an exec, never through a shell.

    Not ``execute_bash`` and not ``execute_git``: ``run_process`` ``.strip()``s
    its output and prefixes ``[exit code N]`` on failure, both of which corrupt
    a diff payload, and ``git merge-base`` legitimately exits non-zero.
    ``$(git merge-base ...)`` as the blueprint writes it cannot work at all here
    -- ``create_subprocess_shell`` is ``cmd.exe`` on Windows and command
    substitution is a POSIX shell feature.

    ``core.quotepath=false`` stops git octal-escaping non-ASCII paths, which
    would otherwise put a mangled name in ``FileDiff.path``.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        "--no-pager",
        "-c",
        "core.quotepath=false",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out[:MAX_GIT_BYTES], err[:MAX_GIT_BYTES]


async def _is_repo(cwd: str) -> bool:
    try:
        code, _, _ = await _git(["rev-parse", "--git-dir"], cwd)
    except (OSError, ValueError):
        return False
    return code == 0


async def resolve_base_ref(cwd: str, explicit: str = "") -> tuple[str, str]:
    """``(ref, note)`` -- the branch point to diff against.

    A ladder, not one hardcoded ref. Measured on this box: the working branch
    has no upstream at all (``fatal: upstream branch ... not stored as a
    remote-tracking branch``), so ``@{u}`` alone would make ``/diff branch``
    useless here.
    """
    if explicit:
        return explicit, ""
    for ref in ("@{u}", "origin/HEAD", "origin/main", "origin/master", "main", "master"):
        code, out, _ = await _git(["rev-parse", "--verify", "--quiet", ref], cwd)
        if code == 0 and out.strip():
            return ref, ""
    return "", "no upstream or main/master branch found; pass a ref to /diff branch"


async def _read_disk(path: Path) -> bytes | None:
    def _read() -> bytes | None:
        try:
            if not path.is_file():
                return None
            return path.read_bytes()
        except OSError:
            return None

    return await asyncio.to_thread(_read)


async def _session_diffs(store: Any, session_id: str, context: int) -> list[FileDiff]:
    """Checkpoint pre-images vs the files on disk. No git anywhere.

    This is the base that works in a directory that is not a repository, which
    is why it is the default: SSOT's whole reason for rejecting shadow-git is
    that DJcode has to work outside a repo.
    """
    out: list[FileDiff] = []
    baseline, note = await asyncio.to_thread(store.baseline, session_id)
    for path, (pre_sha, existed, _post_sha) in sorted(baseline.items()):
        after = await _read_disk(Path(path))
        before: bytes | None = None
        if existed:
            before = await asyncio.to_thread(store.blob, pre_sha) if pre_sha else None
            if before is None:
                # Oversize at capture time, or evicted by the blob budget. W5
                # already has the user-facing sentence for this; rendering the
                # whole file as "added" instead would be a lie.
                out.append(
                    FileDiff(
                        path=path,
                        unavailable="its previous contents were never saved (size/budget)",
                        size_after=len(after) if after is not None else None,
                    )
                )
                continue
        if before is None and after is None:
            continue
        diff = diff_bytes(path, before, after, context=context)
        if diff.empty and not diff.binary:
            continue
        out.append(diff)
        if len(out) >= MAX_DIFF_FILES:
            note = (note + "; " if note else "") + f"stopped after {MAX_DIFF_FILES} files"
            break
    if note and out:
        out[0].note = (out[0].note + "; " if out[0].note else "") + note
    return out


async def _git_diffs(base_ref: str, cwd: str, context: int) -> list[FileDiff]:
    """``git`` supplies the path list and the BEFORE texts; we diff them ourselves.

    Deliberately not a parser over ``git diff``'s unified output. That output
    carries rename headers, mode changes, ``Binary files ... differ``,
    ``\\ No newline``, submodule lines, and ``@@`` headers whose length is
    omitted when it is 1 -- a second hunk algorithm with its own bug surface and
    its own tests. Asking git for the texts and running them through the same
    builder means all three bases render identically.
    """
    out: list[FileDiff] = []
    code, raw, err = await _git(["diff", "--name-status", "-z", base_ref], cwd)
    if code != 0:
        message = err.decode("utf-8", "replace").strip() or "git diff failed"
        return [FileDiff(path="", unavailable=message)]

    fields = [f for f in raw.decode("utf-8", "replace").split("\0") if f]
    entries: list[tuple[str, str]] = []
    i = 0
    while i < len(fields):
        status = fields[i][:1]
        if status in {"R", "C"}:
            # rename/copy is three fields: status, source, destination.
            if i + 2 < len(fields):
                entries.append((status, fields[i + 2]))
            i += 3
            continue
        if i + 1 < len(fields):
            entries.append((status, fields[i + 1]))
        i += 2

    root = Path(cwd)
    for status, rel in entries[:MAX_DIFF_FILES]:
        before: bytes | None = None
        if status != "A":
            code, blob, _ = await _git(["show", f"{base_ref}:{rel}"], cwd)
            before = blob if code == 0 else None
        after = None if status == "D" else await _read_disk(root / rel)
        out.append(
            diff_bytes(
                rel,
                before,
                after,
                context=context,
                status={"A": "added", "D": "deleted"}.get(status, "modified"),
            )
        )

    # `git diff` never lists untracked paths, which on an agent session is most
    # of what changed. Without this the mode silently omits every file the agent
    # created.
    code, raw, _ = await _git(["ls-files", "--others", "--exclude-standard", "-z"], cwd)
    if code == 0:
        for rel in [f for f in raw.decode("utf-8", "replace").split("\0") if f]:
            if len(out) >= MAX_DIFF_FILES:
                break
            after = await _read_disk(root / rel)
            if after is None:
                continue
            out.append(diff_bytes(rel, None, after, context=context, status="added"))
    return out


async def diff_paths(
    base: str = "session",
    store: Any = None,
    *,
    session_id: str = "",
    cwd: str | None = None,
    ref: str = "",
    context: int = DEFAULT_CONTEXT,
) -> list[FileDiff]:
    """Every changed file for one base: ``session``, ``uncommitted`` or ``branch``.

    ``async`` because two of the three bases spawn ``git``. A synchronous
    ``subprocess.run`` here would block the REPL's event loop -- the spinner,
    the token stream and Ctrl+C all stop for the duration -- which is the class
    of bug W1-7 removed from the rest of the engine.
    """
    where = cwd or str(Path.cwd())
    if base == "session":
        if store is None:
            return [FileDiff(path="", unavailable="checkpoints are not active in this session")]
        return await _session_diffs(store, session_id, context)

    if not await _is_repo(where):
        return [
            FileDiff(
                path="",
                unavailable=f"{where} is not a git repository; /diff session works anywhere",
            )
        ]

    if base == "uncommitted":
        return await _git_diffs("HEAD", where, context)

    if base == "branch":
        base_ref, note = await resolve_base_ref(where, ref)
        if not base_ref:
            return [FileDiff(path="", unavailable=note)]
        code, out, err = await _git(["merge-base", base_ref, "HEAD"], where)
        if code != 0 or not out.strip():
            message = err.decode("utf-8", "replace").strip()
            return [
                FileDiff(
                    path="",
                    unavailable=message or f"no merge base between HEAD and {base_ref}",
                )
            ]
        point = out.decode("ascii", "replace").strip()
        diffs = await _git_diffs(point, where, context)
        if diffs:
            label = f"base {base_ref} ({point[:8]})"
            diffs[0].note = (diffs[0].note + "; " if diffs[0].note else "") + label
        return diffs

    return [FileDiff(path="", unavailable=f"unknown diff base {base!r}")]
