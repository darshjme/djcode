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
from typing import Any

from rich.console import Console

from djcode.core.events import CoreEvent, EventType

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
