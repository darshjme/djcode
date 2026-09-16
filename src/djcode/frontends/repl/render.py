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

from rich.console import Console

from djcode.core.events import CoreEvent, EventType
from djcode.sessions import Session

console = Console()

# Dimmed styling for thinking output, moved here from operator.py.
THINK_PREFIX = "\033[2m\033[3m"  # dim + italic
THINK_RESET = "\033[0m"

# Display names for every dispatchable tool. Core must not carry display
# strings, so this is the one place a tool's human-facing name is decided.
TOOL_DISPLAY: dict[str, str] = {
    "bash": "Bash",
    "file_read": "Read",
    "file_write": "Write",
    "file_edit": "Edit",
    "file_multi_edit": "MultiEdit",
    "grep": "Grep",
    "glob": "Glob",
    "git": "Git",
    "web_fetch": "WebFetch",
    "web_search": "WebSearch",
    "todo_write": "Todo",
    "notebook_edit": "Notebook",
    "agent_spawn": "Agent",
    "task": "Task",
    "skill": "Skill",
    "workflow": "Workflow",
    "memory_read": "MemoryRead",
    "memory_write": "MemoryWrite",
    "session_read": "SessionRead",
    "computer": "Computer",
    "screenshot": "Screenshot",
    "browser": "Browser",
    "mcp": "MCP",
    "think": "Think",
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
    vals = [str(v) for v in args.values() if v]
    return vals[0][:72] if vals else ""


def render_tool_call(name: str, args: dict[str, Any]) -> None:
    """Render a tool call as a clean one-liner."""
    display_name = TOOL_DISPLAY.get(name, name.title())
    console.print(
        f"[#FFD700]⏺[/] [bold white]{display_name}[/][dim]({format_tool_args(name, args)})[/]"
    )


def render_tool_result(content: str) -> None:
    """Render a tool result as an indented dim summary."""
    lines = content.strip().splitlines()
    if not lines:
        return

    total = len(lines)
    first = lines[0].strip().lower()
    if first.startswith("error") or first.startswith("traceback"):
        console.print(f"  [dim]↳[/] [red]Error: {lines[0][:120]}[/]")
        return

    shown = lines if total <= 3 else lines[:3]
    for line in shown:
        truncated = line[:120] + "..." if len(line) > 120 else line
        console.print(f"  [dim]  {truncated}[/]")
    if total > 3:
        console.print(f"  [dim]  ... ({total - 3} more lines)[/]")


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
        render_tool_result(event.data.get("content", ""))


# ── Agent presentation ──────────────────────────────────────────────────────
# Icons and roster rendering moved out of orchestrator/engine.py in W2. The
# engine supplies data; deciding what a role looks like is a front-end concern.

GOLD = "#C79B7A"

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
