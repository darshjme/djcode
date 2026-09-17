"""Terminal rendering for the REPL — the presentation half of the de-console.

Everything in this module used to live inside ``agents/operator.py``, where a
module-level ``rich.Console`` printed tool cards directly and the thinking
splitter wrote raw ANSI to ``sys.stderr``. That made the engine unusable from
anything but a terminal: a GUI driving ``Operator`` got the output twice (once
printed to a console nobody was watching, once as data) or not at all.

Now the engine emits :class:`~djcode.core.events.CoreEvent` objects and this
module turns them into terminal output. The Textual TUI is a second consumer of
the same events; a desktop GUI is a third.

Nothing under ``djcode.core`` may import this module.
"""

from __future__ import annotations

import sys
from datetime import datetime
from typing import Any

from djcode.core.events import CoreEvent, EventType
from djcode.frontends.repl import theme
from djcode.sessions import Session

console, PALETTE = theme.make_console()

#: The REPL's accent, resolved once. Every literal gold in this module used to
#: be its own decision; there were two different ones in this file alone.
ACCENT = PALETTE.style("dj.accent")


def glyph(name: str) -> str:
    """One glyph, ASCII-safe. A cp1252 console cannot print U+23FA."""
    return PALETTE.glyph(name)

# Dimmed styling for thinking output, moved here from operator.py.
THINK_PREFIX = "\033[2m\033[3m"  # dim + italic
THINK_RESET = "\033[0m"

# Display names for every dispatchable tool. Core must not carry display
# strings, so this is the one place a tool's human-facing name is decided.
#: Every key here is a key of ``tools.TOOL_DISPATCH``, and every key of
#: ``TOOL_DISPATCH`` is here -- ``tests/test_repl_render.py`` asserts both
#: directions. The previous table listed nine tools that do not exist
#: (``agent_spawn``, ``file_multi_edit``, ``memory_read``, ``memory_write``,
#: ``screenshot``, ``session_read``, ``task``, ``think``, ``todo_write``) and
#: was missing seven that do, so a ``schedule`` call rendered as "Schedule" by
#: accident and a ``spawn_agent`` call rendered as "Spawn_Agent".
TOOL_DISPLAY: dict[str, str] = {
    "bash": "Bash",
    "file_read": "Read",
    "file_write": "Write",
    "file_edit": "Edit",
    "grep": "Grep",
    "glob": "Glob",
    "git": "Git",
    "web_fetch": "WebFetch",
    "web_search": "WebSearch",
    "task_create": "TaskCreate",
    "task_update": "TaskUpdate",
    "task_list": "TaskList",
    "notebook_read": "NotebookRead",
    "notebook_edit": "NotebookEdit",
    "parallel_execute": "Parallel",
    "spawn_agent": "Agent",
    "agent_status": "AgentStatus",
    "schedule": "Schedule",
    "skill": "Skill",
    "mcp": "MCP",
    "process": "Process",
    "browser": "Browser",
    "computer": "Computer",
    "workflow": "Workflow",
}


def format_tool_args(name: str, args: dict[str, Any]) -> str:
    """Compress a tool's arguments to the one detail worth showing inline."""
    if name == "bash":
        cmd = args.get("command", "")
        return cmd if len(cmd) < 72 else cmd[:69] + "..."
    if name in ("file_read", "file_write", "file_edit", "file_multi_edit"):
        return args.get("path", "")
    if name == "grep":
        pattern = args.get("pattern", "")
        path = args.get("path", ".")
        return f'"{pattern}" in {path}'
    if name == "glob":
        return args.get("pattern", "")
    if name == "git":
        sub = args.get("subcommand", "")
        git_args = args.get("args", "")
        return f"{sub} {git_args}".strip()
    if name == "spawn_agent":
        return str(args.get("agent") or args.get("agent_type") or args.get("role") or "")
    if name in ("notebook_read", "notebook_edit"):
        return str(args.get("notebook_path") or args.get("path") or "")
    if name in ("web_fetch",):
        return str(args.get("url", ""))[:72]
    if name in ("skill", "mcp", "process", "browser", "computer", "workflow"):
        # The six capability tools share one dispatcher and key off `action`,
        # so the action IS the interesting detail, not the first dict value.
        action = args.get("action") or args.get("name") or ""
        target = args.get("target") or args.get("server") or args.get("skill") or ""
        return f"{action} {target}".strip()[:72]
    vals = [str(v) for v in args.values() if v]
    return vals[0][:72] if vals else ""


def render_tool_call(name: str, args: dict[str, Any]) -> None:
    """Render a tool call as a clean one-liner.

    This line used to hardcode `#FFD700` -- forty lines above a module-level
    `GOLD = "#C79B7A"` in the same file. Both golds, one 286-line module. The
    accent and the bullet now come from `theme`, which is also where the ASCII
    fallback for a cp1252 console lives.
    """
    display_name = TOOL_DISPLAY.get(name, name.title())
    console.print(
        f"[{ACCENT}]{glyph('bullet')}[/] [bold white]{display_name}[/]"
        f"[dim]({format_tool_args(name, args)})[/]"
    )


#: `ToolOutcome.ok` is only a verdict when the handler or the dispatcher
#: produced it. For the seventeen tools that still report `ok_source
#: ="unverified"`, `ok=True` means "dispatch saw no failure signal" -- which is
#: not the same sentence as "the tool succeeded", and rendering it as a tick is
#: exactly the class of mistake W3-VERIFICATION.md catalogues twice.
AUTHORITATIVE_OK_SOURCES = frozenset({"handler", "raised", "dispatch"})


def verdict_of(ok: bool, details: dict[str, Any] | None) -> str:
    """``"ok"`` | ``"failed"`` | ``"blocked"`` | ``"unverified"``.

    The one function allowed to decide whether a tool card gets a tick. Nothing
    here sniffs ``content`` for an "Error:" prefix -- that sniff is the bug
    P0-8 exists to delete, and a renderer reintroducing it would be the third
    time the same regression shipped.
    """
    details = details or {}
    if details.get("refused_by") or details.get("permission") in {"hardline_block", "deny"}:
        return "blocked"
    if details.get("ok_source") not in AUTHORITATIVE_OK_SOURCES:
        return "unverified"
    return "ok" if ok else "failed"


def format_duration(ms: int) -> str:
    """`41ms` / `1.9s` / `1m 12s`, per DESIGN-CLI §9.4."""
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    minutes, seconds = divmod(int(round(ms / 1000)), 60)
    return f"{minutes}m {seconds}s"


def result_trailer(
    verdict: str,
    details: dict[str, Any] | None = None,
    *,
    duration_ms: int = 0,
    spill_path: str | None = None,
) -> str:
    """The right-hand end of a tool card: what happened, and how long it took."""
    details = details or {}
    parts: list[str] = []
    if verdict == "ok":
        parts.append(f"[dj.ok]{glyph('ok')}[/]")
    elif verdict == "failed":
        parts.append(f"[dj.err]{glyph('fail')} failed[/]")
    elif verdict == "blocked":
        reason = details.get("refused_by") or details.get("permission") or "blocked"
        parts.append(f"[dj.err]{glyph('blocked')} {reason}[/]")
    # "unverified" adds NOTHING. Silence is the honest rendering: the card shows
    # what the tool said and makes no claim about whether it worked.

    exit_code = details.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        parts.append(f"[dj.err]exit {exit_code}[/]")
    for key, label in (("hits", "hits"), ("files", "files"), ("lines", "lines")):
        value = details.get(key)
        if isinstance(value, int):
            parts.append(f"[dim]{value} {label}[/]")
    if duration_ms:
        parts.append(f"[dim]{format_duration(duration_ms)}[/]")
    if spill_path:
        parts.append(f"[dim]spill {glyph('child')} {spill_path}[/]")
    return f" [dim]{glyph('sep')}[/] ".join(parts)


def render_tool_result(
    content: str,
    details: dict[str, Any] | None = None,
    *,
    ok: bool = True,
    duration_ms: int = 0,
    spill_path: str | None = None,
) -> None:
    """Render a tool result: its diff, its body, and an honest trailer.

    The diff comes first and the text summary second, because "Edited
    <path>: replaced 1 occurrence" is the sentence the diff makes redundant.
    ``details`` carries the bounded ``FileDiff.as_dict()`` that
    ``file_edit``/``file_write`` build and that ``dispatch_tool``'s step-5
    checkpoint half builds for every other tool that changed a file -- plus,
    since W3, ``ok_source``, the spill path and the duration, none of which any
    surface has ever rendered.
    """
    if details:
        from djcode.frontends.repl.diffview import render_outcome_diff

        render_outcome_diff(console, details)

    verdict = verdict_of(ok, details)
    trailer = result_trailer(
        verdict, details, duration_ms=duration_ms, spill_path=spill_path
    )

    lines = content.strip().splitlines()
    if not lines:
        if trailer:
            console.print(f"  [dim]{glyph('child')}[/] {trailer}")
        return

    total = len(lines)
    # A failure's useful text is at the END (a traceback's last line, a
    # compiler's summary), so a failed card shows the tail rather than the head.
    if verdict in ("failed", "blocked") and total > 3:
        shown = lines[-3:]
        elided = total - 3
        prefix = "dj.err" if verdict != "blocked" else "dj.warn"
    else:
        shown = lines if total <= 3 else lines[:3]
        elided = total - len(shown)
        prefix = "dim"

    for line in shown:
        truncated = line[:120] + glyph("ellipsis") if len(line) > 120 else line
        console.print(f"  [{prefix}]  {truncated}[/]")
    if elided > 0:
        console.print(f"  [dim]  {glyph('ellipsis')} ({elided} more lines)[/]")
    if trailer:
        console.print(f"  [dim]{glyph('child')}[/] {trailer}")


def render_thinking(text: str) -> None:
    """Render a thinking chunk as dim italic text on stderr.

    stderr, not stdout, so piping the REPL's output captures the answer without
    the model's reasoning -- the behaviour operator.py had before W2, preserved
    here where it belongs.
    """
    sys.stderr.write(f"{THINK_PREFIX}{text}{THINK_RESET}")
    sys.stderr.flush()


async def render_event(event: CoreEvent) -> None:
    """Subscribe this to an :class:`~djcode.core.events.EventBus`.

    Only the event kinds a terminal shows are handled; the rest are ignored so
    a new EventType never breaks the REPL. Token events are deliberately NOT
    rendered here: ``repl.py`` already writes the streamed answer as it arrives
    from ``operator.send()``, and rendering both would double every character.
    """
    if event.event_type == EventType.THINKING:
        render_thinking(event.data.get("text", ""))
    elif event.event_type == EventType.TOOL_CALL:
        render_tool_call(event.data.get("name", ""), event.data.get("args", {}))
    elif event.event_type == EventType.TOOL_RESULT:
        render_tool_result(
            event.data.get("content", ""),
            event.data.get("details"),
            ok=bool(event.data.get("ok", True)),
            duration_ms=int(event.data.get("duration_ms", 0) or 0),
            spill_path=event.data.get("spill_path"),
        )


# ── Agent presentation ──────────────────────────────────────────────────────
# Icons and roster rendering moved out of orchestrator/engine.py in W2. The
# engine supplies data; deciding what a role looks like is a front-end concern.

#: Kept as a name for the handful of call sites below; it is now the resolved
#: palette accent rather than a second opinion about what gold is.
GOLD = ACCENT

AGENT_ICONS: dict[str, str] = {
    "orchestrator": "\U0001f3af",
    "coder": "\U0001f4bb",
    "debugger": "\U0001f50e",
    "architect": "\U0001f4d0",
    "reviewer": "✅",
    "tester": "\U0001f9ea",
    "scout": "\U0001f50d",
    "devops": "\U0001f680",
    "docs": "\U0001f4dd",
    "refactorer": "\U0001f504",
    "product_strategist": "\U0001f4ca",
    "security_compliance": "\U0001f6e1️",
    "data_scientist": "\U0001f9ec",
    "sre": "\U0001f6a8",
    "cost_optimizer": "\U0001f4b0",
    "integration": "\U0001f517",
    "ux_workflow": "\U0001f3a8",
    "legal_intelligence": "⚖️",
    "risk_engine": "⚠️",
}


def agent_icon(role_value: str) -> str:
    """The glyph for an agent role, with a neutral default."""
    return AGENT_ICONS.get(role_value, "⚡")


def render_agent_header(name: str, title: str, role_value: str) -> None:
    """Print the compact header shown when an agent starts working."""
    console.print(f"\n  [{GOLD}]{agent_icon(role_value)} {name}[/] [dim]({title})[/]")
    console.print(f"  [dim]{'---' * 17}[/]")


def render_roster(engine: Any) -> None:
    """Print the full agent roster grouped by tier."""
    from djcode.agents.registry import BLOCKING_AGENTS, AgentTier, get_agents_by_tier

    console.print(f"\n  [bold {GOLD}]Shadow Army Roster[/]\n")

    tier_labels = {
        AgentTier.CONTROL: "TIER 4 -- CONTROL",
        AgentTier.ENTERPRISE: "TIER 3 -- ENTERPRISE INTELLIGENCE",
        AgentTier.ARCHITECTURE: "TIER 2 -- ARCHITECTURE",
        AgentTier.EXECUTION: "TIER 1 -- EXECUTION",
    }

    for tier in (
        AgentTier.CONTROL,
        AgentTier.ENTERPRISE,
        AgentTier.ARCHITECTURE,
        AgentTier.EXECUTION,
    ):
        console.print(f"\n  [bold dim]{tier_labels[tier]}[/]")
        for spec in get_agents_by_tier(tier):
            mode = "[dim red]read-only[/]" if spec.read_only else "[dim green]full[/]"
            blocking = " [bold red]BLOCKING[/]" if spec.role in BLOCKING_AGENTS else ""
            console.print(
                f"  {agent_icon(spec.role.value)} [bold white]{spec.name:<16}[/] "
                f"[dim]{spec.title:<38}[/] "
                f"{mode} [dim]{len(spec.tools_allowed)} tools[/]{blocking}"
            )

    console.print("\n  [dim]Use /orchestra <task> for multi-agent execution[/]")
    console.print(
        "  [dim]Use /review, /debug, /test, /refactor, /devops, /docs for single-agent[/]\n"
    )


# ── Session list ────────────────────────────────────────────────────────────
# Moved out of sessions.py in W2: rendering a table is front-end work, and its
# rich.table import put the whole session store inside core's terminal closure.

def render_session_list(console: Any, sessions: list[Session]) -> None:
    """Render a formatted list of sessions for /history."""
    from rich.table import Table

    if not sessions:
        console.print(f"[{GOLD}]No sessions found.[/]")
        return

    table = Table(
        show_header=True,
        header_style=f"bold {GOLD}",
        border_style="dim",
        title=f"[bold {GOLD}]Session History[/]",
        title_justify="left",
    )
    table.add_column("ID", style="dim", max_width=20)
    table.add_column("Date", style="white")
    table.add_column("Model", style=f"bold {GOLD}")
    table.add_column("Messages", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Duration", justify="right", style="dim")

    for s in sessions:
        try:
            dt = datetime.fromisoformat(s.start)
            date_str = dt.strftime("%b %d %H:%M")
        except (ValueError, TypeError):
            date_str = "?"

        dur = s.duration_seconds
        if dur > 0:
            if dur < 60:
                dur_str = f"{int(dur)}s"
            elif dur < 3600:
                dur_str = f"{int(dur // 60)}m"
            else:
                dur_str = f"{int(dur // 3600)}h {int((dur % 3600) // 60)}m"
        else:
            dur_str = "active" if not s.end else "?"

        tokens = s.total_tokens
        tok_str = f"{tokens // 1000}k" if tokens >= 1000 else str(tokens)

        table.add_row(
            s.id,
            date_str,
            s.model,
            str(s.messages_count),
            tok_str,
            dur_str,
        )

    console.print()
    console.print(table)
    console.print()
    console.print("[dim]/resume <id> to resume a session  |  /history search <query>[/]")
    console.print()
