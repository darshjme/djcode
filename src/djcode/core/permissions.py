"""The permission engine -- policy, floor and tokeniser. No terminal, ever.

THE ONE SENTENCE THIS MODULE EXISTS TO MAKE TRUE
------------------------------------------------
**The policy always evaluates. The mode governs only whether a prompt is
shown.** ``PermissionEngine.evaluate`` does not take a ``Mode`` and never reads
``self.mode``; ``resolve`` is the separate, tiny function that applies a mode to
an already-computed ``Verdict``. That split is the fix for F3 (``auto_accept``
returning ``True`` before any policy ran) and it is enforced by
``tests/test_permissions.py::test_evaluate_is_mode_blind``, not by a comment.

WHAT THE TOKENISER CAN AND CANNOT PROVE
---------------------------------------
Its contract, in full: *"I can prove what this command is NAMED, under the union
of POSIX and cmd.exe grammar. I can never prove what it DOES."* Every command
whose argv[0] is itself a way to run another program -- ``sh -c``, ``xargs``,
``make``, ``npm run``, ``git <alias>``, ``powershell -EncodedCommand`` -- is a
place where those two diverge, so none of them can ever reach ``ALLOW``.

The grammar parsed is the UNION of POSIX sh and cmd.exe, not either one.
Measured on the Windows host this ships on, ``create_subprocess_shell`` is
``cmd.exe``: ``;``, ``$( )`` and backticks are INERT there, while ``&``, ``^``
and ``%VAR%`` are live -- and ``bash -c`` / ``wsl`` / ``powershell`` are one
token away at all times, where the opposite is true. A tokeniser that picks one
grammar is wrong half the time. A tokeniser that unions both over-detects, and
over-detection costs an ASK, not a breach.

FOUR MEANINGFUL VERDICTS, NOT THREE
-----------------------------------
The blueprint specifies ``HARDLINE_BLOCK | ALLOW | ASK``. That is one short, and
the missing one is load-bearing: in ``auto`` and ``bypass`` an ``ASK`` resolves
to ALLOW with nobody watching, so "fail closed to ASK" silently becomes fail
OPEN in exactly the two modes where no human is present. ``ASK_UNPARSEABLE``
("the parse is not proven") is therefore a distinct verdict that a mode is not
allowed to downgrade: it prompts in ``manual``/``accept-edits`` and DENIES in
``auto``/``bypass`` and on every non-interactive path. (``DENY`` is the fifth
member, produced only by an explicit ``deny`` rule; unlike the floor it can be
removed by editing ``permissions.json``.)

WHAT IS DELIBERATELY NOT HERE
-----------------------------
No prompt, no Rich markup, no emoji, no subprocess, no execution of anything.
Every string returned is model-facing or front-end-facing plain text. The
approval card lives in ``frontends/repl/`` (W9); this module only decides.

PROJECT-LOCAL POLICY IS NOT SUPPORTED, ON PURPOSE
-------------------------------------------------
Exactly one file is read: ``config.CONFIG_DIR / "permissions.json"`` (which
honours ``DJCODE_CONFIG_DIR``, so tests never write into a real home). A cloned
repository already authors part of the system prompt (``prompt.py`` injects
``CLAUDE.md``) and already contributes skills (``skills.py`` reads
``./.djcode/skills``). It must never also author the policy that gates the
tools. If a project-local file is ever added, the only safe semantics are
deny-only / ask-only -- a project may TIGHTEN, never widen -- gated behind a
"trust this directory" record stored in the USER file, keyed by resolved path.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from djcode.config import CONFIG_DIR

__all__ = [
    "EXEC_DELEGATING",
    "HARDLINE_PREFIX",
    "PROTECTED_WRITE_GLOBS",
    "SECRET_GLOBS",
    "UNPARSEABLE_MESSAGE",
    "VERDICT_ORDER",
    "Decision",
    "DecisionAction",
    "Level",
    "Mode",
    "Narrowing",
    "Parse",
    "PermissionEngine",
    "Resolution",
    "Rule",
    "Segment",
    "ToolRequest",
    "Verdict",
    "command_of",
    "hardline_message",
    "hardline_reason",
    "tokenise",
]


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class Verdict(StrEnum):
    """What the policy decided, before any mode is applied."""

    ALLOW = "allow"
    ASK = "ask"
    #: The parse is not proven. A mode may NOT downgrade this to allow.
    ASK_UNPARSEABLE = "ask_unparseable"
    #: An explicit deny rule. Refused in every mode, but editable by the user.
    DENY = "deny"
    #: The floor. Refused in every mode, and no flag or config key reaches it.
    HARDLINE_BLOCK = "hardline_block"


#: Strictness ladder, for tests and rule merging. Never read by ``resolve``.
VERDICT_ORDER: dict[str, int] = {
    Verdict.ALLOW: 0,
    Verdict.ASK: 1,
    Verdict.ASK_UNPARSEABLE: 2,
    Verdict.DENY: 3,
    Verdict.HARDLINE_BLOCK: 4,
}


class Mode(StrEnum):
    """How an ``ASK`` is rendered. Never how a policy is computed.

    ``MANUAL`` is spelled "ask" in the status line and "approval" in the
    composer table; one enum value, three renderings, no fourth spelling.
    ``BYPASS`` is deliberately not reachable from the Ctrl+T cycle, and is
    unrelated to the pre-existing ``--bypass-rlhf`` flag (an uncensored-prompt
    switch with nothing to do with permissions -- do not add a ``--bypass``
    flag beside it).
    """

    MANUAL = "manual"
    ACCEPT_EDITS = "accept-edits"
    AUTO = "auto"
    BYPASS = "bypass"


class Level(StrEnum):
    """A rule's outcome. Three values, because tighten-only needs a ladder."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


LEVEL_ORDER: dict[str, int] = {Level.ALLOW: 0, Level.ASK: 1, Level.DENY: 2}


class DecisionAction(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ALWAYS = "always"
    SESSION = "session"


class Resolution(StrEnum):
    """What a mode does with a verdict: run it, refuse it, or ask a human."""

    ALLOW = "allow"
    DENY = "deny"
    PROMPT = "prompt"


# ---------------------------------------------------------------------------
# Glob matching. Not ``fnmatch``: ``**`` must cross separators and ``*`` must not.
# ---------------------------------------------------------------------------

_GLOB_CACHE: dict[str, re.Pattern[str]] = {}


def _glob_regex(pattern: str) -> re.Pattern[str]:
    cached = _GLOB_CACHE.get(pattern)
    if cached is not None:
        return cached
    out = ["(?s)"]
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern[index : index + 2] == "**":
                out.append(".*")
                index += 2
                continue
            out.append("[^/\\\\]*")
        elif char == "?":
            out.append("[^/\\\\]")
        else:
            out.append(re.escape(char))
        index += 1
    out.append(r"\Z")
    compiled = re.compile("".join(out), re.IGNORECASE)
    _GLOB_CACHE[pattern] = compiled
    return compiled


def _glob_match(pattern: str, target: str) -> bool:
    if pattern in ("*", "**"):
        return True
    return bool(_glob_regex(pattern).match(target))


@dataclass(slots=True)
class Rule:
    """One policy entry: ``bash(pytest *)`` / ``file_write(**/src/**)``.

    ``pattern`` is a glob matched against a TARGET, never against a raw command
    string: for path tools the ``Path.resolve()``d path, for ``bash`` the parsed
    ``argv`` of one segment rejoined. Matching a raw string is how the old
    module's ``"rm -rf" in command.lower()`` fired on ``echo "never rm -rf /"``.
    """

    tool: str
    pattern: str = "*"
    level: Level = Level.ASK
    scope: str = "user"  # builtin | user | session
    cwd: str | None = None
    origin: str = ""

    @property
    def key(self) -> str:
        return f"{self.tool}({self.pattern})"

    def applies_here(self, cwd: str | None) -> bool:
        """A grant made in a scratch directory is not a grant in the monorepo."""
        if self.cwd is None:
            return True
        if cwd is None:
            return False
        try:
            return Path(self.cwd).resolve() == Path(cwd).resolve()
        except OSError:  # pragma: no cover - defensive
            return False

    def matches(self, tool: str, target: str) -> bool:
        if self.tool != tool and self.tool != "*":
            return False
        return _glob_match(self.pattern, target)

    def to_json(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "pattern": self.pattern,
            "level": str(self.level),
            "scope": self.scope,
            "cwd": self.cwd,
            "origin": self.origin,
        }

    @staticmethod
    def from_json(raw: Any) -> Rule:
        if not isinstance(raw, dict):
            raise ValueError("a rule must be a JSON object")
        tool = raw.get("tool")
        if not isinstance(tool, str) or not tool:
            raise ValueError("rule.tool must be a non-empty string")
        pattern = raw.get("pattern", "*")
        if not isinstance(pattern, str):
            raise ValueError("rule.pattern must be a string")
        cwd = raw.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            raise ValueError("rule.cwd must be a string or null")
        return Rule(
            tool=tool,
            pattern=pattern,
            level=Level(raw.get("level", "ask")),
            scope=str(raw.get("scope", "user")),
            cwd=cwd,
            origin=str(raw.get("origin", "")),
        )


@dataclass(slots=True, frozen=True)
class Narrowing:
    """A level change that can only TIGHTEN. Widening raises at construction.

    This is about PROGRAMMATIC level changes -- rule merging, promoting a
    session grant to a persisted one, a child inheriting a parent's grant. It is
    NOT about a human pressing ``a`` on a visible card: answering "always allow"
    to an ``ask`` rule IS a widening, it is legal, and it goes through
    ``PermissionEngine.grant_always``. The one thing even a human cannot do from
    a card is lift a persisted ``deny``.
    """

    current: Level
    new: Level
    rule: Rule | None = None

    def __post_init__(self) -> None:
        if LEVEL_ORDER[self.new] < LEVEL_ORDER[self.current]:
            raise ValueError(
                f"a Narrowing cannot widen {self.current} -> {self.new}; tighten-only is the rule"
            )


@dataclass(slots=True)
class Decision:
    """The answer a front-end gives back, and what "always" would persist.

    ``__bool__`` is a FAIL-CLOSED safety net, not a migration plan. A
    ``@dataclass(slots=True)`` with no ``__bool__`` is unconditionally truthy,
    so every legacy ``if not await _approve_tool(...)`` site would have silently
    started allowing denials the moment the return type changed -- and there are
    seven such sites, three of them (the subagent write gate, the spawn relay
    and the doom-loop breaker) among the most security-relevant in the tree.
    """

    action: DecisionAction
    comment: str = ""
    rule: Rule | None = None

    def __bool__(self) -> bool:
        return self.action in (
            DecisionAction.ALLOW,
            DecisionAction.ALWAYS,
            DecisionAction.SESSION,
        )


#: Tools that carry a shell command string in an argument. Enumerated from the
#: live ``TOOL_DEFINITIONS``: it is three tools, not one. ``schedule`` and
#: ``process`` both DEFER the command and run it later, detached, with no
#: approval context -- and ``scheduler.run_once`` calls ``execute_bash``
#: directly, never ``dispatch_tool``. The create call is therefore the only
#: moment either command can be judged at all.
SHELL_COMMAND_TOOLS: frozenset[str] = frozenset({"bash", "schedule", "process"})


def command_of(tool: str, arguments: Any) -> str:
    """The shell command a call carries, or ``""`` if it carries none.

    Reads ``command`` OR ``content``. The text-extraction path approves a bash
    intent as ``_approve_tool("bash", {"path": None, "content": "<command>"})``
    -- no ``command`` key at all -- so an engine keyed only on
    ``arguments["command"]`` would see nothing and fail OPEN on every
    non-function-calling model.
    """
    if tool not in SHELL_COMMAND_TOOLS or not isinstance(arguments, dict):
        return ""
    for key in ("command", "content"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


@dataclass(slots=True)
class ToolRequest:
    """One call, as the policy sees it."""

    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    cwd: str | None = None
    agent: str | None = None

    @property
    def command(self) -> str:
        return command_of(self.tool, self.arguments)

    @property
    def path(self) -> str:
        for key in ("path", "file_path", "notebook_path"):
            value = self.arguments.get(key)
            if isinstance(value, str) and value:
                return value
        return ""


# ---------------------------------------------------------------------------
# The tokeniser
# ---------------------------------------------------------------------------

#: Tier 1 caps. A megabyte one-liner is not a command anyone reviewed.
MAX_COMMAND_BYTES = 8192
MAX_SEGMENTS = 32

_ZERO_WIDTH = "\u200b\u200c\u200d\ufeff\u2060"
_BIDI = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
_INVISIBLE = _ZERO_WIDTH + _BIDI

_VAR_POSIX = re.compile(r"\$[A-Za-z_{(]")
_VAR_CMD = re.compile(r"%[A-Za-z_][A-Za-z0-9_]*%|%[0-9~*]")
_VAR_DELAYED = re.compile(r"![A-Za-z_][A-Za-z0-9_]*!")
_FOR_F = re.compile(r"(?i)\bfor\s+/f\b")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass(slots=True)
class Segment:
    """One command in a pipeline or list, as parsed."""

    argv: list[str] = field(default_factory=list)
    raw: str = ""
    redirections: list[tuple[str, str]] = field(default_factory=list)
    background: bool = False

    @property
    def name(self) -> str:
        return basename_of(self.argv[0]) if self.argv else ""

    def writes(self) -> bool:
        return any(operator in (">", ">>", "&>", ">&") for operator, _ in self.redirections)


@dataclass(slots=True)
class Parse:
    """The tokeniser's whole output. ``proven`` is the only field that gates ALLOW."""

    segments: list[Segment] = field(default_factory=list)
    proven: bool = True
    reason: str = ""
    command: str = ""

    def names(self) -> list[str]:
        return [segment.name for segment in self.segments if segment.argv]


def _strip_invisible(text: str) -> str:
    for char in _INVISIBLE:
        text = text.replace(char, "")
    return text


def basename_of(token: str) -> str:
    """``/usr/bin/rm`` -> ``rm``, ``RM.EXE`` -> ``rm``. The ordinary reading."""
    text = _strip_invisible(token).replace("'", "").replace('"', "").replace("^", "")
    text = re.split(r"[/\\]", text)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat", ".com", ".ps1"):
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def name_variants(token: str) -> set[str]:
    """Every command name one token could plausibly resolve to.

    Two readings are needed and neither is correct alone on this host.
    ``/usr/bin/rm`` needs the basename reading; ``r\\m`` needs the
    backslash-SPLICE reading, because POSIX unescaping recovers ``rm`` from it
    while the basename reading yields ``m``. The floor checks both, because
    over-detection there costs one rejected command and under-detection costs
    the machine.
    """
    cleaned = _strip_invisible(token).replace("'", "").replace('"', "").replace("^", "")
    variants = {basename_of(token), cleaned.replace("\\", "").lower()}
    return {variant for variant in variants if variant}


def _is_cmd_flag(token: str) -> bool:
    """``/b``, ``/s``, ``/fs:NTFS`` are cmd switches. ``/bin`` is a path."""
    if not token.startswith("/") or len(token) < 2:
        return False
    body = token[1:]
    if len(body) == 1 and (body.isalpha() or body == "?"):
        return True
    return bool(re.fullmatch(r"[A-Za-z]{1,4}:[^/\\]*", body))


def is_flag(token: str) -> bool:
    return token.startswith("-") or _is_cmd_flag(token)


def _operands(argv: Sequence[str]) -> list[str]:
    return [token for token in argv[1:] if not is_flag(token)]


def tokenise(command: str) -> Parse:
    """Split a command string into segments of argv, under POSIX ∪ cmd grammar.

    ``proven`` starts True and any single tier below trips it to False. There is
    no per-case judgement and no negotiation: an unproven parse can never reach
    ``ALLOW`` and can never be downgraded by a mode.
    """
    if not isinstance(command, str):
        return Parse([], False, "command is not a string", "")
    if not command.strip():
        return Parse([], False, "empty command", command if isinstance(command, str) else "")

    # --- Tier 1: is the parse trustworthy at all? -------------------------
    unproven: list[str] = []
    if len(command.encode("utf-8", "replace")) > MAX_COMMAND_BYTES:
        unproven.append(f"longer than {MAX_COMMAND_BYTES} bytes")
    if "\x00" in command:
        unproven.append("contains a NUL byte")
    if any(char in command for char in _INVISIBLE):
        unproven.append("contains zero-width or bidi-override characters")
    if any(ord(char) > 127 for char in command):
        unproven.append("contains non-ASCII characters (possible homoglyphs)")
    if any(ord(char) < 32 and char not in "\t\r\n" for char in command):
        unproven.append("contains control characters")

    segments, scan_reasons = _scan(command)
    unproven.extend(scan_reasons)
    if len(segments) > MAX_SEGMENTS:
        unproven.append(f"more than {MAX_SEGMENTS} segments")

    # --- Tiers 3 and 4: is the argv the whole story? ----------------------
    for segment in segments:
        unproven.extend(_delegation_reasons(segment))

    return Parse(
        segments=segments,
        proven=not unproven,
        reason="; ".join(dict.fromkeys(unproven)),
        command=command,
    )


def _scan(command: str) -> tuple[list[Segment], list[str]]:
    """The character scanner: quote-aware, separator-union, redirection-aware."""
    reasons: list[str] = []
    segments: list[Segment] = []
    argv: list[str] = []
    redirections: list[tuple[str, str]] = []
    token = ""
    has_token = False
    seg_start = 0
    index = 0
    length = len(command)
    in_single = False
    in_double = False
    pending_redirect: str | None = None
    background = False

    def flush_token() -> None:
        nonlocal token, has_token, pending_redirect
        if not has_token:
            return
        if pending_redirect is not None:
            redirections.append((pending_redirect, token))
            pending_redirect = None
        else:
            argv.append(token)
        token = ""
        has_token = False

    def flush_segment(raw_end: int) -> None:
        nonlocal argv, redirections, background, seg_start, pending_redirect
        flush_token()
        pending_redirect = None
        if argv or redirections:
            segments.append(
                Segment(
                    argv=list(argv),
                    raw=command[seg_start:raw_end],
                    redirections=list(redirections),
                    background=background,
                )
            )
        argv = []
        redirections = []
        background = False
        seg_start = raw_end

    while index < length:
        char = command[index]

        if in_single:
            if char == "'":
                in_single = False
            else:
                token += char
            index += 1
            continue
        if in_double:
            if char == '"':
                in_double = False
            else:
                token += char
            index += 1
            continue

        if char == "'":
            in_single = True
            has_token = True
            index += 1
            continue
        if char == '"':
            in_double = True
            has_token = True
            index += 1
            continue

        # cmd.exe caret escape: `e^cho` runs `echo` (measured on this host), so
        # `de^l` runs `del`. Strip it before any name is extracted.
        if char == "^" and index + 1 < length:
            token += command[index + 1]
            has_token = True
            index += 2
            continue

        # POSIX backslash. An escape only in front of a metacharacter, so that
        # `C:\Users\x` survives intact -- `shlex(posix=True)` turns that into
        # `C:Usersx` and every path rule then matches a corrupted string.
        if char == "\\" and index + 1 < length:
            following = command[index + 1]
            if following in " \t;&|<>()\"'$`^\\":
                token += following
                has_token = True
                index += 2
                continue
            token += char
            has_token = True
            index += 1
            continue

        if char in " \t":
            flush_token()
            index += 1
            continue

        two = command[index : index + 2]

        if two == "<<":
            reasons.append("contains a here-document")
            token += two
            has_token = True
            index += 2
            continue
        if two in ("<(", ">("):
            # Process substitution. NOT a redirection: `bash <(curl url)` runs
            # the inner command and hands its output to `bash` as a file.
            reasons.append("contains a process substitution")
            flush_segment(index)
            index += 2
            seg_start = index
            continue
        if two in (">>", "&>", ">&"):
            flush_token()
            pending_redirect = two
            index += 2
            continue
        if char in "<>":
            if char == ">" and token.isdigit():
                # `2>` / `1>`: the digit belongs to the operator, not to argv.
                token = ""
                has_token = False
            flush_token()
            pending_redirect = char
            index += 1
            continue
        if two in ("&&", "||"):
            flush_segment(index)
            index += 2
            seg_start = index
            continue
        if char in ";|&\n\r":
            if char == "&":
                background = True
            flush_segment(index)
            index += 1
            seg_start = index
            continue
        if char in "()":
            reasons.append("contains a subshell or command group")
            flush_segment(index)
            index += 1
            seg_start = index
            continue

        token += char
        has_token = True
        index += 1

    flush_segment(length)

    if in_single or in_double:
        reasons.append("unbalanced quote")

    # --- Tier 2: the command name is computed, not written ----------------
    outside = _outside_single_quotes(command)
    if _VAR_POSIX.search(outside):
        reasons.append("contains a shell expansion or substitution ($(...), ${...}, $VAR)")
    if "`" in outside:
        reasons.append("contains a backtick (POSIX substitution / PowerShell escape)")
    if _VAR_CMD.search(outside):
        reasons.append("contains a cmd.exe %VAR% expansion")
    if _VAR_DELAYED.search(outside):
        reasons.append("contains a cmd.exe !VAR! delayed expansion")
    if _FOR_F.search(outside):
        reasons.append("contains `for /f`, which executes its quoted argument")
    if any(segment.name in ("set", "setlocal") for segment in segments) and (
        "%" in outside or "!" in outside
    ):
        reasons.append("assigns a variable and later expands one")
    for segment in segments:
        if segment.argv and _ASSIGNMENT.match(segment.argv[0]):
            reasons.append("starts with a NAME=value assignment prefix")
            break
    return segments, reasons


def _outside_single_quotes(command: str) -> str:
    out: list[str] = []
    in_single = False
    for char in command:
        if char == "'":
            in_single = not in_single
            continue
        if not in_single:
            out.append(char)
    return "".join(out)


# ---------------------------------------------------------------------------
# Tier 3/4: programs whose job is to run another program
# ---------------------------------------------------------------------------

#: An ``argv[0]`` here means "the argv is a lie". None of these can ever reach
#: ALLOW from a parsed-argv rule, however innocent the rest of the line looks.
#: This is the honest half of the tokeniser's contract written down as a set.
EXEC_DELEGATING: frozenset[str] = frozenset(
    {
        "eval", "exec", "source", ".", "env", "nice", "nohup", "timeout",
        "stdbuf", "setsid", "ionice", "chrt", "unbuffer", "watch", "time",
        "command", "xargs", "parallel", "sudo", "doas", "runas", "su", "ssh",
        "scp", "sftp", "rsync", "sh", "bash", "zsh", "dash", "ksh", "fish",
        "ash", "busybox", "cmd", "powershell", "pwsh", "wsl", "node", "deno",
        "bun", "perl", "ruby", "php", "lua", "osascript", "make", "ninja",
        "gradle", "mvn", "ant", "rake", "just", "npm", "pnpm", "yarn", "npx",
        "bunx", "uvx", "pipx", "poetry", "hatch", "docker", "podman",
        "kubectl", "nerdctl", "systemd-run", "at", "schtasks", "start", "call",
        "cscript", "wscript", "rundll32", "mshta", "certutil", "wmic",
        "diskpart", "reg",
    }
)

#: Deferred or detached execution: escapes the timeout, the cancel path and the
#: checkpoint window. ``run_process.stop()`` on Windows kills only the direct
#: child, so a ``start``-ed grandchild outlives every control this system has.
DETACHING: frozenset[str] = frozenset(
    {"start", "at", "schtasks", "systemd-run", "nohup", "setsid", "screen", "tmux"}
)

#: Read-only git subcommands.
GIT_READ_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "status", "log", "diff", "show", "blame", "shortlog", "describe",
        "rev-parse", "rev-list", "ls-files", "ls-tree", "ls-remote", "cat-file",
        "name-rev", "whatchanged", "count-objects", "verify-commit",
    }
)

#: Every git subcommand recognised at all. An UNKNOWN subcommand may be a
#: user-defined alias -- ``git config alias.x '!rm -rf /'`` then ``git x``, two
#: calls each innocuous alone -- so a rule of the shape ``bash(git *)`` is
#: equivalent to arbitrary code execution. It is never seeded as a default.
GIT_KNOWN_SUBCOMMANDS: frozenset[str] = GIT_READ_SUBCOMMANDS | frozenset(
    {
        "add", "commit", "push", "pull", "fetch", "clone", "checkout", "switch",
        "restore", "branch", "tag", "merge", "rebase", "reset", "revert",
        "stash", "cherry-pick", "clean", "init", "remote", "config", "mv", "rm",
        "apply", "am", "bisect", "worktree", "submodule", "gc", "reflog",
        "archive", "format-patch", "notes", "prune", "repack", "grep",
    }
)


def _delegation_reasons(segment: Segment) -> list[str]:
    """Tier 3/4 checks for one segment."""
    if not segment.argv:
        return []
    reasons: list[str] = []
    name = segment.name
    rest = list(segment.argv[1:])
    lowered = [token.lower() for token in rest]

    if name in DETACHING:
        reasons.append(f"`{name}` detaches execution from this session")
    if name in EXEC_DELEGATING:
        reasons.append(f"`{name}` runs another program; its argv does not say what it does")
    if name in ("find", "fd") and any(
        flag in lowered for flag in ("-exec", "-execdir", "-ok", "-okdir")
    ):
        reasons.append("`find -exec` introduces a second command")
    if name == "git":
        if any(token == "-c" or token.startswith("-c") for token in rest):
            reasons.append("`git -c` can define an alias that runs arbitrary shell")
        subcommand = next((token for token in rest if not is_flag(token)), "")
        if subcommand and subcommand.lower() not in GIT_KNOWN_SUBCOMMANDS:
            reasons.append(f"`git {subcommand}` is not a known subcommand; it may be an alias")
    if name in ("python", "python3", "py", "python2") and any(
        token in ("-c", "-m", "-") for token in lowered
    ):
        reasons.append("`python -c`/`-m` runs a program written in another language")
    if name == "uv" and not _uv_is_read_only(list(segment.argv)):
        reasons.append("`uv run` executes an arbitrary project entry point")
    if name in ("powershell", "pwsh") and any(token.startswith("-e") for token in lowered):
        # -EncodedCommand accepts any unambiguous prefix. The presence of the
        # flag is the signal; never decode the payload to decide.
        reasons.append("an encoded or expression PowerShell payload cannot be read")
    return reasons


def _uv_is_read_only(argv: Sequence[str]) -> bool:
    """``uv run --frozen ruff check --no-cache src/`` is the one allowed shape.

    ``--frozen`` forbids lockfile mutation and ``check`` without ``--fix``
    forbids source mutation. Every other ``uv`` invocation -- ``add``, ``sync``,
    ``pip``, or ``run`` on any other entry point -- mutates state the checkpoint
    store cannot undo, and goes down the ordinary ASK path.
    """
    rest = list(argv[1:])
    if not rest or rest[0] != "run" or "--frozen" not in rest:
        return False
    if "--fix" in rest or "--unsafe-fixes" in rest:
        return False
    operands = [token for token in rest[1:] if not is_flag(token)]
    return bool(operands) and operands[0] == "ruff" and "check" in operands


# ---------------------------------------------------------------------------
# The HARDLINE floor
# ---------------------------------------------------------------------------

HARDLINE_PREFIX = "Error: blocked by the DJcode hardline floor"

#: Launcher prefixes peeled off before the floor looks at a segment, so that
#: `env - rm -rf /` and `timeout 5 rm -rf /` are seen for what they are.
_LAUNCHERS: frozenset[str] = frozenset(
    {
        "env", "nohup", "nice", "ionice", "chrt", "stdbuf", "setsid", "time",
        "timeout", "command", "exec", "sudo", "doas", "runas", "unbuffer",
        "busybox", "start", "call",
    }
)

#: Targets whose recursive removal is unrecoverable. Normalised: lowercased,
#: backslashes folded to slashes, quotes and carets removed.
_ROOT_TARGETS: frozenset[str] = frozenset(
    {
        "", "/", "/*", "//", "~", "~/*", "$home", "$home/*", "%userprofile%",
        "%userprofile%/*", "%homepath%", "%systemroot%", "c:", "c:/*", "d:",
        "/usr", "/lib", "/lib64", "/etc", "/bin", "/sbin", "/var", "/boot",
        "/opt", "/srv", "/root", "/home", "/users", "/system", "/library",
        "/private", "/dev", "/proc", "/sys", "c:/windows", "c:/windows/*",
        "c:/users", "c:/users/*", "c:/program files",
    }
)

_SYSTEM_CRITICAL: frozenset[str] = frozenset(
    {"/etc/shadow", "/etc/passwd", "/etc/sudoers", "/etc/ssh"}
)

_DEVICE = re.compile(r"^/dev/(sd|nvme|hd|disk|vd|mmcblk|mapper)")

_FETCHERS: frozenset[str] = frozenset(
    {
        "curl", "wget", "iwr", "irm", "invoke-webrequest", "invoke-restmethod",
        "aria2c", "httpie", "bitsadmin",
    }
)

_DECODERS: frozenset[str] = frozenset({"base64", "openssl", "xxd", "uudecode", "certutil"})

_INTERPRETERS: frozenset[str] = frozenset(
    {
        "sh", "bash", "zsh", "dash", "ksh", "ash", "fish", "iex",
        "invoke-expression", "powershell", "pwsh", "cmd", "python", "python3",
        "node", "perl", "ruby", "php", "eval",
    }
)

#: Commands whose argument IS code: a fetcher inside one is remote execution.
_EVALUATORS: frozenset[str] = frozenset({"eval", "iex", "invoke-expression"})

_NETWORK_SINKS: frozenset[str] = frozenset(
    {
        "curl", "wget", "nc", "ncat", "netcat", "socat", "scp", "sftp", "ssh",
        "rsync", "ftp", "tftp", "telnet", "iwr", "irm", "invoke-webrequest",
        "invoke-restmethod", "bitsadmin",
    }
)

#: Credential material. Disclosure is the one damage class that no checkpoint,
#: no undo and no reboot can reverse.
_CREDENTIAL_MARKERS: tuple[str, ...] = (
    "/.ssh", ".ssh/", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    ".aws/credentials", ".config/gcloud", ".kube/config", ".docker/config.json",
    ".npmrc", ".pypirc", ".netrc", ".git-credentials", ".djcode/auth.json",
)

#: cmd.exe removers and their PowerShell aliases. ``rm`` aliases ``Remove-Item``
#: in PowerShell and is a real GNU binary on this box (Git-for-Windows puts
#: coreutils 8.32 on PATH), so the POSIX patterns stay live on Windows too.
_WINDOWS_REMOVERS: frozenset[str] = frozenset({"del", "erase", "rd", "rmdir", "remove-item", "ri"})

_PS_RECURSE_FLAGS = ("-recurse", "-recurs", "-recu", "-rec", "-r")
_PS_FORCE_FLAGS = ("-force", "-forc", "-for", "-fo", "-f")


def _norm_target(token: str) -> str:
    text = _strip_invisible(token)
    text = text.replace("'", "").replace('"', "").replace("^", "")
    text = text.replace("\\", "/").strip().lower()
    while len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    return text


def _rm_is_recursive_force(argv: Sequence[str]) -> bool:
    """``-rf``, ``-fr``, ``-r -f`` and ``--recursive --force`` are one command."""
    flags = [token for token in argv[1:] if token.startswith("-")]
    recursive = any(
        token.lower() in ("--recursive", "--recurse")
        or (not token.startswith("--") and "r" in token[1:].lower())
        for token in flags
    )
    force = any(
        token.lower() == "--force"
        or (not token.startswith("--") and "f" in token[1:].lower())
        for token in flags
    )
    return recursive and force


def _strip_launchers(argv: Sequence[str]) -> list[str]:
    """Peel ``sudo`` / ``env -`` / ``timeout 5`` / ``nice -n 10`` off the front."""
    current = list(argv)
    for _ in range(6):
        if not current or not (name_variants(current[0]) & _LAUNCHERS):
            return current
        rest = current[1:]
        while rest and (is_flag(rest[0]) or rest[0] == "-" or rest[0].isdigit()):
            rest = rest[1:]
        current = rest
    return current


def _floor_segment(raw_argv: Sequence[str]) -> str | None:
    """Every single-segment floor rule. Returns a reason, or ``None``."""
    argv = _strip_launchers(raw_argv)
    if not argv:
        return None
    names = name_variants(argv[0])
    targets = [_norm_target(token) for token in _operands(argv)]
    lowered = [token.lower() for token in argv[1:]]
    hits_root = any(target in _ROOT_TARGETS for target in targets)

    if "rm" in names and _rm_is_recursive_force(argv):
        if hits_root:
            return "recursive force delete of a filesystem root or home directory"
        if "--no-preserve-root" in lowered:
            return "`rm --no-preserve-root` exists only to defeat the shell's own guard"

    if names & _WINDOWS_REMOVERS:
        quiet = any(flag in lowered for flag in ("/s", "/q", "/f"))
        ps_recursive = any(token.startswith(_PS_RECURSE_FLAGS) for token in lowered) and any(
            token.startswith(_PS_FORCE_FLAGS) for token in lowered
        )
        if (quiet or ps_recursive) and hits_root:
            return "recursive quiet delete of a drive root, the OS tree or the user profile"

    if "format" in names and any(
        re.fullmatch(r"[a-z]:", target) or target == "/dev" for target in targets
    ):
        return "formatting a drive destroys every byte on it"

    if any(name == "mkfs" or name.startswith("mkfs.") for name in names):
        return "creating a filesystem destroys everything on the target device"

    if "dd" in names and any(
        token.lower().startswith("of=") and _DEVICE.match(_norm_target(token[3:]))
        for token in argv[1:]
    ):
        return "`dd of=` writes raw bytes straight over a block device"

    if "shred" in names and any(_DEVICE.match(target) for target in targets):
        return "`shred` on a block device is designed to be unrecoverable"

    if names & {"chmod", "chown", "chgrp"}:
        recursive = any(
            token.lower() in ("-r", "--recursive")
            or (token.startswith("-") and not token.startswith("--") and "R" in token)
            for token in argv[1:]
        )
        if recursive and (hits_root or any(t in _SYSTEM_CRITICAL for t in targets)):
            return "recursive ownership or permission change across a system root"

    if "vssadmin" in names and "delete" in lowered and "shadows" in lowered:
        return "deleting every shadow copy destroys the OS's own restore points"
    if "wmic" in names and "shadowcopy" in lowered and "delete" in lowered:
        return "deleting every shadow copy destroys the OS's own restore points"
    if "wbadmin" in names and "delete" in lowered and "catalog" in lowered:
        return "deleting the backup catalog makes recovery impossible"

    if "reg" in names and lowered[:1] == ["delete"]:
        key = _norm_target(argv[2]) if len(argv) > 2 else ""
        if key.startswith(("hklm/", "hkey_local_machine/")) and key.count("/") <= 1:
            return "deleting a top-level HKLM hive subtree is unrecoverable"

    if "icacls" in names and "/t" in lowered:
        joined = " ".join(lowered).replace(" ", "")
        if "/granteveryone:f" in joined or ("everyone:f" in joined and "/grant" in lowered):
            if any(target.startswith(("c:/windows", "c:/", "/")) for target in targets):
                return "granting Everyone full control across the OS tree"

    if names & {"find", "fd"} and targets:
        root_ish = targets[0] in _ROOT_TARGETS
        destructive = "-delete" in lowered or (
            any(flag in lowered for flag in ("-exec", "-execdir", "-ok"))
            and any(name_variants(token) & {"rm", "del", "shred"} for token in argv[1:])
        )
        if root_ish and destructive:
            return "`find` deleting across the whole filesystem"
    return None


def _payload_tokens(argv: Sequence[str]) -> list[str]:
    """Flatten an interpreter's quoted payload back into words."""
    out: list[str] = []
    for token in argv[1:]:
        for word in token.split():
            out.append(word.strip("$()`'\"<>|&;{}[]"))
    return out


def _floor_nested(argv: Sequence[str], depth: int) -> str | None:
    """Re-parse an interpreter's payload. ``sh -c "rm -rf /"`` is still ``rm -rf /``."""
    if depth <= 0:
        return None
    stripped = _strip_launchers(argv)
    if not stripped or not (name_variants(stripped[0]) & _INTERPRETERS):
        return None
    for token in stripped[1:]:
        if " " in token.strip():
            reason = hardline_reason(token, depth=depth - 1)
            if reason:
                return reason
    return None


def _floor_pipeline(parse: Parse, command: str) -> str | None:
    """Floor rules that need more than one segment, or the whole string, to see."""
    segments = [segment for segment in parse.segments if segment.argv]
    heads: list[set[str]] = []
    for segment in segments:
        stripped = _strip_launchers(segment.argv)
        # Both readings: the launcher-stripped head (so `sudo bash` reads as
        # `bash`) AND the literal head (so `env | curl` still reads as `env`,
        # which stripping would otherwise erase entirely).
        names = name_variants(stripped[0]) if stripped else set()
        heads.append(names | name_variants(segment.argv[0]))

    # curl | sh, irm | iex, base64 -d | sh: a fetcher or decoder on the left of
    # a pipe and an interpreter on the right. `sudo` in between changes nothing.
    for index, names in enumerate(heads):
        if names & (_FETCHERS | _DECODERS):
            if any(later & _INTERPRETERS for later in heads[index + 1 :]):
                return "a downloaded or decoded payload piped straight into an interpreter"

    # Process substitution: `bash <(curl -s url)` has no pipe token at all.
    if ("<(" in command or ">(" in command) and any(n & _INTERPRETERS for n in heads):
        if any(n & _FETCHERS for n in heads):
            return "an interpreter reading from a process substitution that fetches code"

    # The payload can be a single quoted token, so there is no second segment
    # to look at. `hardline_reason` already re-parses such a payload (see
    # `_floor_nested`), which covers `sh -c "curl url | sh"` and
    # `powershell -c "iwr url | iex"`. Two shapes survive that and are handled
    # here.
    #
    # Deliberately NOT a rule: "an interpreter with a fetcher anywhere in its
    # payload". That would hardline-block `sh -c "echo curl"` and
    # `sh -c "curl -O https://x/f.tar"` -- a plain download -- and an
    # undisablable block on a legitimate command is a product defect, not a
    # safety win. The two rules below are narrow because the floor has to be.
    for segment in segments:
        stripped = _strip_launchers(segment.argv)
        if not stripped:
            continue
        names = name_variants(stripped[0])
        words = {word.lower() for word in _payload_tokens(stripped)}
        # 1. `eval`/`iex` EVALUATE their argument, so a fetcher anywhere inside
        #    it is remote code execution by definition, pipe or no pipe.
        if (names & _EVALUATORS) and (words & _FETCHERS):
            return "remote output passed straight to an evaluator"
        if not (names & _INTERPRETERS):
            continue
        # 2. The PowerShell download cradle, which names no fetcher binary at
        #    all -- it constructs one.
        blob = " ".join(stripped[1:]).lower()
        if any(
            marker in blob
            for marker in ("downloadstring", "downloadfile", "webclient", "iex(", "iex (")
        ):
            return "a PowerShell download cradle"

    # A password piped into `sudo -S` is never a legitimate agent action, and it
    # also writes the secret into the session log.
    for segment in segments[1:]:
        if segment.argv and (name_variants(segment.argv[0]) & {"sudo", "doas"}):
            if "-S" in segment.argv:
                return "a password piped into `sudo -S`"

    # Credential exfiltration: secret material anywhere, a network sink anywhere.
    tokens = [_norm_target(token) for segment in segments for token in segment.argv]
    has_secret = any(
        marker in token for token in tokens for marker in _CREDENTIAL_MARKERS
    ) or any(token.endswith("/.env") or token == ".env" or ".env." in token for token in tokens)
    if has_secret and any(names & _NETWORK_SINKS for names in heads):
        return "credential material and a network sink in the same command"

    # `env | curl -d @-`: the whole environment, including every API token.
    for index, names in enumerate(heads):
        if "env" in names and not _operands(segments[index].argv):
            if any(later & _NETWORK_SINKS for later in heads[index + 1 :]):
                return "the whole environment piped to a network sink"

    # Redirection straight onto a block device -- no command in argv at all.
    for segment in parse.segments:
        for operator, target in segment.redirections:
            if operator in (">", ">>", "&>", ">&") and _DEVICE.match(_norm_target(target)):
                return "redirecting output over a raw block device"
    return None


#: `:(){ :|:& };:` and any renamed equivalent: a function that pipes itself into
#: itself in the background. A literal-string signature fails on the whitespace
#: variant, so the rule is structural.
_FORK_BOMB = re.compile(r"([A-Za-z_:][\w:]*)\(\)\{[^}]*\|[^}]*&\};?\1")
_FORK_IDIOMS = ("forkwhilefork", "while(1)fork", "while(true)fork")


def _floor_text(command: str) -> str | None:
    """Rules about the SHAPE of the whole string, not about any argv."""
    squeezed = re.sub(r"\s+", "", command)
    if _FORK_BOMB.search(squeezed):
        return "a fork bomb"
    lowered = squeezed.lower()
    if any(idiom in lowered for idiom in _FORK_IDIOMS):
        return "a fork bomb idiom"
    return None


def _paranoid_views(command: str) -> list[str]:
    """Extra views built ONLY when the parse is already unproven.

    An unproven parse is at best an ASK, so scanning harder here can only make
    the answer stricter. It can never turn a legitimate command into a block,
    because a legitimate command that parses cleanly never reaches this code --
    which is exactly what keeps the blueprint's own gate true: ``echo "never rm
    -rf /"`` is PROVEN, so these views are never built for it.

    View one DELETES quote and escape characters (recovering ``rm`` from
    ``$(printf 'r''m')``) and replaces the remaining metacharacters with spaces.
    View two additionally deletes variable expansions, which recovers
    ``rm -rf /`` from ``rm -rf "$UNSET_VAR"/`` -- the classic empty-variable bug.
    """
    deleted = command.replace("'", "").replace('"', "").replace("^", "").replace("\\", "")
    view_one = re.sub(r"[$(){}`|;&<>\[\]=]", " ", deleted)
    view_two = re.sub(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|%[A-Za-z_][A-Za-z0-9_]*%", "", deleted)
    view_two = re.sub(r"[$(){}`|;&<>\[\]=]", " ", view_two)
    return [view_one, view_two]


_PARANOID_HEADS = frozenset(
    {
        "rm", "del", "erase", "rd", "rmdir", "remove-item", "ri", "mkfs", "dd",
        "shred", "format", "chmod", "chown", "vssadmin", "wbadmin", "reg",
    }
)


def _paranoid_hit(view: str) -> str | None:
    """Look for a floor signature ANYWHERE in a flattened token stream."""
    words = view.split()
    for index, word in enumerate(words):
        candidate = basename_of(word)
        if candidate in _PARANOID_HEADS or any(candidate.startswith(p) for p in ("mkfs.",)):
            reason = _floor_segment(words[index:])
            if reason:
                return reason
    return None


def hardline_reason(command: str, parse: Parse | None = None, *, depth: int = 2) -> str | None:
    """The undisablable floor. Returns a reason string, or ``None``.

    Not reachable by any flag, mode or config key -- ``bypass`` included. This
    function is called from THREE places, not one: the dispatch chokepoint and
    BOTH shell-spawn sites (``tools/bash.py::run_process`` and
    ``capabilities.py::process``). A floor written only inside ``execute_bash``
    would miss ``capabilities.py``'s own ``create_subprocess_shell``; a floor
    written only at the chokepoint would miss ``scheduler.run_once``, which
    calls ``execute_bash`` directly and never touches ``dispatch_tool``.
    """
    if not isinstance(command, str) or not command.strip():
        return None
    parse = parse if parse is not None else tokenise(command)

    reason = _floor_text(command)
    if reason:
        return reason

    for segment in parse.segments:
        reason = _floor_segment(segment.argv)
        if reason:
            return reason
        reason = _floor_nested(segment.argv, depth)
        if reason:
            return reason

    reason = _floor_pipeline(parse, command)
    if reason:
        return reason

    if not parse.proven:
        for view in _paranoid_views(command):
            hit = _paranoid_hit(view) or _floor_text(view)
            if hit:
                return hit
    return None


def hardline_message(command: str, reason: str) -> str:
    """The exact text the model receives. It MUST begin with "Error".

    ``workflow._result_ok`` falls back to a prefix check whenever it is handed a
    bare string -- and ``scheduler.run_once`` hands it exactly that. On the DAF
    wire a block that reads as a success lets every dependent node run, which is
    W3-VERIFICATION's BLOCKER 1 re-entering through a new door. Belt (a
    structured ``ok=False`` outcome at the chokepoint) and braces (this prefix).
    """
    shortened = " ".join(command.strip().split())
    if len(shortened) > 200:
        shortened = shortened[:197] + "..."
    return (
        f"{HARDLINE_PREFIX}: {reason}. The command was NOT executed, and no flag, "
        f"mode or config key can permit it. Choose a different approach.\n"
        f"Command: {shortened}"
    )


UNPARSEABLE_MESSAGE = (
    "Error: this command could not be parsed safely, so it was not executed "
    "({reason}). Re-issue it as a single command with literal arguments, or "
    "split it into separate calls."
)


# ---------------------------------------------------------------------------
# Seeded defaults
# ---------------------------------------------------------------------------

#: Copied from ``tool_router.PROTECTED_FILES`` rather than imported: that module
#: is 1179 lines and is marked for deletion of its dead execution half, and core
#: must not drag its closure in. Four defects of the original are fixed here --
#: it matched only files sitting directly in the cwd (a nested ``backend/.env``
#: was unprotected), it had no ``.env`` variants, it had no credential files at
#: all, and every entry was the same severity. Note this is the FIRST
#: enforcement these names have ever had on ANY path: ``_is_protected`` has zero
#: callers, so nothing was protected before this wave.
PROTECTED_WRITE_GLOBS: tuple[str, ...] = (
    "**/pyproject.toml", "**/package.json", "**/Cargo.toml", "**/go.mod",
    "**/pom.xml", "**/build.gradle", "**/Gemfile", "**/mix.exs",
    "**/composer.json", "**/README.md", "**/readme.md", "**/LICENSE",
    "**/license", "**/.gitignore", "**/Makefile", "**/Dockerfile",
    "**/tsconfig.json", "**/next.config.js", "**/next.config.ts",
    "**/next.config.mjs", "**/vite.config.ts", "**/vite.config.js",
    "**/uv.lock", "**/package-lock.json", "**/yarn.lock", "**/Cargo.lock",
)

#: Secrets: ASK on READ as well as on write. ``.env`` and a private key are not
#: "protected project files", they are disclosure risks, and disclosure is the
#: one damage class no checkpoint can reverse.
SECRET_GLOBS: tuple[str, ...] = (
    "**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/id_rsa", "**/id_rsa.*",
    "**/id_ed25519", "**/id_ed25519.*", "**/id_ecdsa", "**/id_dsa",
    "**/.ssh/**", "**/.aws/credentials", "**/.npmrc", "**/.pypirc",
    "**/.netrc", "**/.git-credentials", "**/.kube/config",
    "**/.djcode/auth.json", "**/.git/config",
)

#: Tools with no side effect outside this process. Everything NOT listed here
#: defaults to ASK -- the inversion of the old module's "Unknown tool, allow by
#: default", which was fail-OPEN and is why every capability tool (``process``,
#: ``mcp``, ``browser``, ``computer``, ``skill``, ``workflow``) was permitted
#: unconditionally.
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "file_read", "grep", "glob", "notebook_read", "task_list",
        "task_create", "task_update", "agent_status", "web_search",
    }
)

#: Tools whose whole purpose is editing files in the working tree. These are the
#: only ones ``accept-edits`` auto-allows.
EDIT_TOOLS: frozenset[str] = frozenset({"file_write", "file_edit", "notebook_edit"})


def _git_is_read_only(argv: list[str]) -> bool:
    if any(token == "-c" for token in argv[1:]):
        return False
    subcommand = next((token for token in argv[1:] if not is_flag(token)), "")
    return subcommand.lower() in GIT_READ_SUBCOMMANDS


def _interpreter_version_only(argv: list[str]) -> bool:
    rest = [token.lower() for token in argv[1:]]
    return bool(rest) and all(
        token in ("--version", "-v", "-vv", "--help", "-h") for token in rest
    )


def _find_is_read_only(argv: list[str]) -> bool:
    lowered = [token.lower() for token in argv[1:]]
    return not any(
        flag in lowered for flag in ("-delete", "-exec", "-execdir", "-ok", "-okdir")
    )


#: bash command names that are read-only, each keyed to a predicate over argv.
#: The predicate is the point: ``ruff check`` and ``ruff check --fix`` are the
#: same command name and opposite verdicts, so a name-only allowlist is a lie.
_READ_ONLY_COMMANDS: dict[str, Callable[[list[str]], bool]] = {
    "ls": lambda argv: True,
    "dir": lambda argv: True,
    "pwd": lambda argv: True,
    "cd": lambda argv: True,
    "echo": lambda argv: True,
    "cat": lambda argv: True,
    "type": lambda argv: True,
    "head": lambda argv: True,
    "tail": lambda argv: True,
    "wc": lambda argv: True,
    "stat": lambda argv: True,
    "file": lambda argv: True,
    "which": lambda argv: True,
    "where": lambda argv: True,
    "whoami": lambda argv: True,
    "date": lambda argv: True,
    "hostname": lambda argv: True,
    "grep": lambda argv: True,
    "rg": lambda argv: True,
    "diff": lambda argv: True,
    "tree": lambda argv: True,
    "du": lambda argv: True,
    "df": lambda argv: True,
    "find": _find_is_read_only,
    "git": _git_is_read_only,
    "python": _interpreter_version_only,
    "python3": _interpreter_version_only,
    "node": _interpreter_version_only,
    "uv": _uv_is_read_only,
    "ruff": lambda argv: "check" in argv and "--fix" not in argv,
}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

PERMISSIONS_FILE = CONFIG_DIR / "permissions.json"
SCHEMA_VERSION = 1


@dataclass(slots=True)
class LoadResult:
    rules: list[Rule]
    notice: str = ""


def load_rules(path: Path | None = None) -> LoadResult:
    """Read persisted ALWAYS rules. A bad file is dropped WITH A NOTICE.

    ``config.load_config`` swallows a ``JSONDecodeError`` and silently returns
    defaults. For permissions that silence is wrong: a user whose grants failed
    to load sees only extra prompts, reads them as a bug, and reaches for
    ``--auto-accept`` -- which is how a corrupt file becomes a disabled gate.
    """
    target = path if path is not None else PERMISSIONS_FILE
    try:
        if not target.exists():
            return LoadResult([])
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return LoadResult([], f"permissions.json could not be read ({exc}); defaults are in use")
    if not isinstance(raw, dict) or raw.get("version") != SCHEMA_VERSION:
        found = raw.get("version") if isinstance(raw, dict) else "?"
        return LoadResult(
            [], f"permissions.json has an unrecognised version {found!r}; defaults are in use"
        )
    rules: list[Rule] = []
    for entry in raw.get("rules", []):
        try:
            rules.append(Rule.from_json(entry))
        except (ValueError, KeyError, TypeError) as exc:
            return LoadResult(
                [], f"permissions.json contains an invalid rule ({exc}); defaults are in use"
            )
    return LoadResult(rules)


def save_rules(rules: Iterable[Rule], path: Path | None = None) -> None:
    """Atomic write, mirroring ``config.save_config``. A torn file is a fail-open."""
    target = path if path is not None else PERMISSIONS_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": SCHEMA_VERSION,
        "rules": [rule.to_json() for rule in rules if rule.scope not in ("builtin", "session")],
    }
    handle, temporary = tempfile.mkstemp(prefix=".permissions-", dir=str(target.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def default_rules() -> list[Rule]:
    rules: list[Rule] = []
    for tool in sorted(READ_ONLY_TOOLS):
        rules.append(
            Rule(tool=tool, pattern="*", level=Level.ALLOW, scope="builtin", origin="read-only")
        )
    for glob in SECRET_GLOBS:
        for tool in ("file_read", "file_write", "file_edit", "notebook_read", "notebook_edit"):
            rules.append(
                Rule(tool=tool, pattern=glob, level=Level.ASK, scope="builtin", origin="secret")
            )
    for glob in PROTECTED_WRITE_GLOBS:
        for tool in sorted(EDIT_TOOLS):
            rules.append(
                Rule(
                    tool=tool,
                    pattern=glob,
                    level=Level.ASK,
                    scope="builtin",
                    origin="tool_router.PROTECTED_FILES",
                )
            )
    return rules


def _level_to_verdict(level: Level) -> Verdict:
    if level is Level.ALLOW:
        return Verdict.ALLOW
    if level is Level.DENY:
        return Verdict.DENY
    return Verdict.ASK


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


class PermissionEngine:
    """Evaluates policy. Never prompts, never prints, never spawns anything.

    ``evaluate`` is MODE-BLIND by construction: it reads ``self._rules`` and
    ``self._session`` and nothing else. ``self.mode`` exists only so ``resolve``
    and the surfaces have somewhere to read it from.
    """

    def __init__(
        self,
        mode: Mode = Mode.MANUAL,
        *,
        rules: Sequence[Rule] | None = None,
        persist_path: Path | None = None,
        load: bool = False,
    ) -> None:
        self.mode = Mode(mode)
        self.notice = ""
        self._persist_path = persist_path
        self._rules: list[Rule] = list(default_rules())
        if rules:
            self._rules.extend(rules)
        if load:
            result = load_rules(persist_path)
            self._rules.extend(result.rules)
            self.notice = result.notice
        #: SESSION grants keyed by requester. A parent who pressed `s` must not
        #: silently grant the same thing to every subagent: the ContextVar that
        #: carries DispatchContext hands children the SAME object, so an unkeyed
        #: session set would quietly undo W1-6's consent-descent rule.
        self._session: dict[str, set[str]] = {}

    # -- policy ------------------------------------------------------------

    def evaluate(self, request: ToolRequest) -> Verdict:
        """Pure policy. No mode, no prompt, no side effect, no I/O."""
        if request.tool in SHELL_COMMAND_TOOLS and request.command:
            return self._evaluate_command(request)
        return self._evaluate_path_tool(request)

    def _evaluate_command(self, request: ToolRequest) -> Verdict:
        command = request.command
        parse = tokenise(command)
        if hardline_reason(command, parse) is not None:
            return Verdict.HARDLINE_BLOCK
        if not parse.proven:
            return Verdict.ASK_UNPARSEABLE
        if request.tool in ("schedule", "process"):
            # Deferred execution: it runs later, detached, with no approval
            # context and -- for the scheduler -- no dispatch_tool at all.
            return Verdict.ASK
        if not parse.segments:
            return Verdict.ASK
        return _level_to_verdict(self._level_for_command(request, parse))

    def _level_for_command(self, request: ToolRequest, parse: Parse) -> Level:
        """ALLOW needs a POSITIVE proof for EVERY segment.

        Not "no segment matched a deny rule". ``git status && rm -rf build`` is
        flagged because ``rm`` had no allow rule, never because ``rm`` was on a
        blocklist -- a blocklist is a list of the attacks somebody already
        thought of; an allowlist is a list of what this session actually needs.
        """
        worst = Level.ALLOW
        for segment in parse.segments:
            level = self._level_for_segment(request, segment)
            if LEVEL_ORDER[level] > LEVEL_ORDER[worst]:
                worst = level
        return worst

    def _level_for_segment(self, request: ToolRequest, segment: Segment) -> Level:
        if not segment.argv:
            return Level.ASK
        target = " ".join([segment.name, *segment.argv[1:]]).strip()
        explicit = self._match_rules(request, "bash", target)
        if explicit is not None:
            return explicit
        if segment.writes():
            # `cat x > y` is a WRITE. A read-only allow rule must never permit
            # it, and a redirection target is subject to the same path rules as
            # file_write.
            return Level.ASK
        predicate = _READ_ONLY_COMMANDS.get(segment.name)
        if predicate is None or not predicate(list(segment.argv)):
            return Level.ASK
        if not self._operands_inside_cwd(request, segment):
            return Level.ASK
        return Level.ALLOW

    def _operands_inside_cwd(self, request: ToolRequest, segment: Segment) -> bool:
        """A read is only cheap while it stays inside the project."""
        try:
            base = (Path(request.cwd) if request.cwd else Path.cwd()).resolve()
        except OSError:  # pragma: no cover - defensive
            return False
        for token in _operands(segment.argv):
            if not token:
                continue
            try:
                candidate = Path(os.path.expanduser(token))
                if not candidate.is_absolute():
                    candidate = base / candidate
                resolved = candidate.resolve()
            except (OSError, ValueError):
                return False
            if resolved != base and base not in resolved.parents:
                return False
        return True

    def _evaluate_path_tool(self, request: ToolRequest) -> Verdict:
        target = request.path or "*"
        if target != "*":
            try:
                target = str(Path(os.path.expanduser(target)).resolve()).replace("\\", "/")
            except (OSError, ValueError):
                return Verdict.ASK_UNPARSEABLE
        level = self._match_rules(request, request.tool, target)
        if level is None:
            # FAIL CLOSED. The old module returned True for any tool it did not
            # recognise ("Unknown tool, allow by default"); that inversion is
            # why every capability tool was permitted unconditionally.
            return Verdict.ASK
        return _level_to_verdict(level)

    #: A later scope overrides an earlier one; within one scope the stricter
    #: level wins. Without the scope rank, "most restrictive wins" would mean a
    #: builtin ASK on `**/pyproject.toml` silently outvoted the user's own
    #: explicit "always allow" -- the card would keep reappearing forever and
    #: the user would learn that the buttons do nothing.
    _SCOPE_RANK = {"builtin": 0, "user": 1, "session": 2}

    def _match_rules(self, request: ToolRequest, tool: str, target: str) -> Level | None:
        """Highest scope wins; within a scope, most-restrictive wins."""
        best: Level | None = None
        best_rank = -1
        for rule in self._rules:
            if not rule.applies_here(request.cwd):
                continue
            if not rule.matches(tool, target):
                continue
            rank = self._SCOPE_RANK.get(rule.scope, 1)
            if rank > best_rank:
                best_rank, best = rank, rule.level
            elif rank == best_rank and (
                best is None or LEVEL_ORDER[rule.level] > LEVEL_ORDER[best]
            ):
                best = rule.level
        if best in (None, Level.ASK):
            granted = self._session.get(self._requester(request), set())
            key = f"{tool}({target})"
            if key in granted or any(_glob_match(entry, key) for entry in granted):
                return Level.ALLOW
        return best

    @staticmethod
    def _requester(request: ToolRequest) -> str:
        return request.agent or "__operator__"

    # -- mode --------------------------------------------------------------

    def resolve(self, verdict: Verdict, mode: Mode | None = None, *, tool: str = "") -> Resolution:
        """Apply a mode to an already-computed verdict. The ONLY place mode acts."""
        mode = Mode(mode) if mode is not None else self.mode
        if verdict in (Verdict.HARDLINE_BLOCK, Verdict.DENY):
            return Resolution.DENY
        if verdict is Verdict.ASK_UNPARSEABLE:
            # A mode may not downgrade this. In auto/bypass there is no human to
            # prompt, so the fail-closed answer is DENY -- not "run it anyway".
            return Resolution.DENY if mode in (Mode.AUTO, Mode.BYPASS) else Resolution.PROMPT
        if verdict is Verdict.ALLOW:
            return Resolution.ALLOW
        if mode in (Mode.AUTO, Mode.BYPASS):
            return Resolution.ALLOW
        if mode is Mode.ACCEPT_EDITS and tool in EDIT_TOOLS:
            return Resolution.ALLOW
        return Resolution.PROMPT

    # -- grants ------------------------------------------------------------

    def rule_for(self, request: ToolRequest) -> Rule:
        """What "always" would persist for this request."""
        if request.tool in SHELL_COMMAND_TOOLS and request.command:
            parse = tokenise(request.command)
            name = parse.segments[0].name if parse.segments else ""
            return Rule(
                tool="bash", pattern=f"{name} *", level=Level.ALLOW, scope="user", cwd=request.cwd
            )
        target = request.path or "*"
        if target != "*":
            try:
                target = str(Path(os.path.expanduser(target)).resolve()).replace("\\", "/")
            except (OSError, ValueError):
                pass
        return Rule(
            tool=request.tool, pattern=target, level=Level.ALLOW, scope="user", cwd=request.cwd
        )

    def grant_session(self, request: ToolRequest, rule: Rule | None = None) -> Rule:
        rule = rule or self.rule_for(request)
        self._session.setdefault(self._requester(request), set()).add(rule.key)
        return rule

    def grant_always(self, request: ToolRequest, rule: Rule | None = None) -> Rule:
        rule = rule or self.rule_for(request)
        if self._match_rules(request, rule.tool, rule.pattern) is Level.DENY:
            raise ValueError("a persisted DENY cannot be lifted by a runtime approval")
        rule.level = Level.ALLOW
        rule.scope = "user"
        self._rules.append(rule)
        save_rules(self._rules, self._persist_path)
        return rule

    def narrow(self, request: ToolRequest, new: Level) -> Narrowing:
        """Tighten an existing level. Raises if ``new`` would widen it."""
        rule = self.rule_for(request)
        current = self._match_rules(request, rule.tool, rule.pattern) or Level.ASK
        narrowing = Narrowing(current=current, new=new, rule=rule)
        rule.level = new
        rule.scope = "session"
        self._rules.append(rule)
        return narrowing

    # -- text for the model / the front-end ---------------------------------

    def explain(self, request: ToolRequest) -> str:
        """A one-line, markup-free reason a front-end can render verbatim."""
        command = request.command
        if command:
            parse = tokenise(command)
            reason = hardline_reason(command, parse)
            if reason:
                return hardline_message(command, reason)
            if not parse.proven:
                return UNPARSEABLE_MESSAGE.format(reason=parse.reason)
        return ""
