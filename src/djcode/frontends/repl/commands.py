"""The commands that existed only in the Textual TUI, ported to the REPL.

`SSOT.md` §3.1 lists nine commands the registry marks `repl=False`. The TUI is
in maintenance and the REPL is where people work, so "it exists in the TUI" has
meant "it does not exist" for most of DJcode's users. Seven of the nine are
ported here. The other two are deliberate omissions:

* **`/queue`** -- superseded by W8's steer/queue on `CoreSession`. Porting the
  TUI's version would ship a second, unrelated queue.
* **`/cancel`** -- Ctrl+C does it, and as of W9 it actually works on Windows.
  A command you have to finish typing is a poor cancel button.

Two of the seven are NOT ports:

* **`/cost`** is rebuilt, per blueprint W9-7's explicit instruction not to port
  `CostPanel`. That panel multiplied a per-1k rate by token counts the TUI had
  guessed with `len(text) // 4`, and presented the product as dollars. It now
  reads `Operator.session_usage` -- the provider's own accounting, collected
  since W9 -- and says so, or prints nothing but an explanation when the
  provider reported none. A cost figure derived from a guess is worse than no
  cost figure.
* **`/todo`** is an alias of `/tasks`. The TUI's `/todo` was backed by a
  sidebar widget whose state lived in that widget and was never persisted
  anywhere; there is no sidebar in the REPL and nothing to port. Rather than
  invent a second task store, `/todo` presents the real task tools with
  checkboxes. Stated here because a silent alias would be a surprise.

This module deliberately does NOT contain the registry dispatcher that
blueprint W9-7 also assigns to this filename. Converting the 708-line if/elif
chain and adding seven new commands in one pass makes a broken port and a
broken conversion indistinguishable; the conversion is left for its own change,
with `tests/test_repl_commands.py` already standing under it.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.table import Table

from djcode.frontends.repl import theme

__all__ = [
    "PORTED",
    "handle_context",
    "handle_cost",
    "handle_search",
    "handle_spawn",
    "handle_tasks",
    "handle_waves",
]


def _console() -> Console:
    from djcode.frontends.repl.render import console

    return console


def _accent() -> str:
    from djcode.frontends.repl.render import ACCENT

    return ACCENT


def _glyph(name: str) -> str:
    from djcode.frontends.repl.render import glyph

    return glyph(name)


# ── /context ────────────────────────────────────────────────────────────────


async def handle_context(operator: Any, arg: str = "") -> None:
    """What is filling the context window, and how full it is.

    The TUI printed eight numbers. This adds the bill of materials -- which
    SOURCE the tokens came from -- because "83% full" is not actionable and
    "62% of it is one injected file" is.
    """
    console = _console()
    accent = _accent()
    try:
        stats = operator.context_manager.stats
    except Exception as error:  # pragma: no cover - defensive
        console.print(f"[dj.err]Context unavailable: {error}[/]")
        return

    fraction = max(0.0, min(1.0, (stats.utilization_pct or 0) / 100.0))
    bar = theme.meter(fraction, width=24)
    console.print(f"\n[bold {accent}]Context window[/]  [dim]{stats.model}[/]")
    console.print(
        f"  {bar} [bold]{stats.utilization_pct:.1f}%[/]"
        f"  [dim]{stats.current_tokens:,} / {stats.max_context_tokens:,}"
        f" {_glyph('sep')} {stats.remaining_tokens:,} left[/]"
    )
    console.print(
        f"  [dim]{stats.message_count} messages"
        f" {_glyph('sep')} {stats.pinned_count} pinned"
        f" {_glyph('sep')} {stats.compressions_performed} compactions[/]"
    )

    items = _bill_of_materials(operator)
    if items:
        table = Table(box=None, pad_edge=False, show_header=True, header_style="dim")
        table.add_column("source", style=accent)
        table.add_column("tokens", justify="right")
        table.add_column("items", justify="right", style="dim")
        for source, tokens, count in items:
            table.add_row(source, f"{tokens:,}", str(count))
        console.print()
        console.print(table)
    console.print()


def _bill_of_materials(operator: Any) -> list[tuple[str, int, int]]:
    """`(source, tokens, count)`, largest first.

    Deliberately NOT matching `DESIGN-CLI.md` §7's mock, which lists "tool
    schemas (22)" as a line item. Tool schemas are not messages and
    `ContextWindowManager` does not count them, so a row for them would be a
    number this function invented. The mock is wrong against the code; the
    honest table is smaller.
    """
    manager = getattr(operator, "context_manager", None)
    if manager is None:
        return []
    totals: dict[str, list[int]] = {}
    for injected in getattr(manager, "_injected", []) or []:
        if getattr(injected, "is_expired", False):
            continue
        bucket = totals.setdefault(f"injected: {injected.source}", [0, 0])
        bucket[0] += int(getattr(injected, "tokens", 0) or 0)
        bucket[1] += 1
    for message in getattr(operator, "messages", []) or []:
        role = getattr(message, "role", "?")
        bucket = totals.setdefault(f"message: {role}", [0, 0])
        bucket[0] += len(getattr(message, "content", "") or "") // 4
        bucket[1] += 1
    rows = [(source, tokens, count) for source, (tokens, count) in totals.items()]
    rows.sort(key=lambda row: row[1], reverse=True)
    return rows


# ── /cost ───────────────────────────────────────────────────────────────────


async def handle_cost(operator: Any, arg: str = "") -> None:
    """What this session actually cost, or an honest refusal to guess."""
    console = _console()
    accent = _accent()
    usage = getattr(operator, "session_usage", None)
    model = getattr(getattr(operator, "provider", None), "config", None)
    model_name = getattr(model, "model", "") or ""

    if usage is None or not usage.received:
        console.print(
            f"\n[bold {accent}]Cost[/]\n"
            "  [dim]This provider reported no token accounting for this session, so\n"
            "  there is nothing to price. Local runtimes (Ollama, LM Studio, MLX)\n"
            "  do not bill and do not report; hosted providers do both.[/]\n"
        )
        return

    total = usage.usage
    cost = usage.cost(model_name)
    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_column(style="dim")
    table.add_column(justify="right")
    table.add_row("input", f"{total.input_tokens:,}")
    table.add_row("output", f"{total.output_tokens:,}")
    if total.cache_read_tokens:
        table.add_row("cache read", f"{total.cache_read_tokens:,}")
    if total.cache_creation_tokens:
        table.add_row("cache write", f"{total.cache_creation_tokens:,}")
    if total.thinking_tokens:
        table.add_row("thinking", f"{total.thinking_tokens:,}")
    table.add_row("requests", f"{usage.requests:,}")
    console.print(f"\n[bold {accent}]Cost[/]  [dim]{model_name}[/]")
    console.print(table)
    if cost:
        priced = "provider" if total.total_cost else "local price table"
        console.print(f"  [bold {accent}]${cost:.4f}[/] [dim]({priced})[/]\n")
    else:
        console.print(
            f"  [dim]No price is known for {model_name!r}, so the tokens above are\n"
            "  the whole story.[/]\n"
        )


# ── /search ─────────────────────────────────────────────────────────────────


async def handle_search(operator: Any, arg: str = "") -> None:
    console = _console()
    if not arg.strip():
        console.print("[dj.warn]Usage: /search <query>[/]")
        return
    from djcode.tools import dispatch_tool

    console.print(f"\n[{_accent()}]{_glyph('bullet')} WebSearch[/][dim]({arg})[/]")
    try:
        outcome = await dispatch_tool("web_search", {"query": arg})
    except Exception as error:
        console.print(f"[dj.err]Search failed: {error}[/]")
        return
    _render_outcome(outcome)


# ── /tasks and /todo ────────────────────────────────────────────────────────


async def handle_tasks(operator: Any, arg: str = "", *, checkboxes: bool = False) -> None:
    console = _console()
    from djcode.tools import dispatch_tool

    parts = arg.split(maxsplit=1) if arg.strip() else []
    sub = parts[0].lower() if parts else "list"
    rest = parts[1] if len(parts) > 1 else ""
    label = "Todos" if checkboxes else "Session tasks"

    try:
        if sub in ("list", "") or not arg.strip():
            outcome = await dispatch_tool("task_list", {})
            console.print(f"\n[bold {_accent()}]{label}[/]")
            _render_outcome(outcome)
        elif sub == "add" and rest:
            outcome = await dispatch_tool("task_create", {"title": rest})
            _render_outcome(outcome)
        elif sub == "done" and rest:
            outcome = await dispatch_tool(
                "task_update", {"task_id": rest, "status": "done"}
            )
            _render_outcome(outcome)
        else:
            verb = "/todo" if checkboxes else "/tasks"
            console.print(f"[dj.warn]Usage: {verb} [list|add <title>|done <id>][/]")
    except Exception as error:
        console.print(f"[dj.err]Task error: {error}[/]")


async def handle_todo(operator: Any, arg: str = "") -> None:
    """An alias of `/tasks`. See the module docstring for why."""
    await handle_tasks(operator, arg, checkboxes=True)


# ── /spawn ──────────────────────────────────────────────────────────────────


async def handle_spawn(operator: Any, arg: str = "") -> None:
    console = _console()
    if not arg.strip():
        console.print("[dj.warn]Usage: /spawn <agent_role> <task>[/]")
        return
    parts = arg.split(maxsplit=1)
    role = parts[0]
    task = parts[1] if len(parts) > 1 else "work on this codebase"

    from djcode.tools import dispatch_tool
    from djcode.tools.agent_spawn import agent_context

    console.print(f"\n[{_accent()}]{_glyph('bullet')} Agent[/][dim]({role})[/]")
    try:
        # The child inherits the parent's approval channel, NOT its consent
        # (B10, closed in W1-6): every tool the child wants is still presented,
        # labelled with the agent that asked for it.
        with agent_context(
            operator.provider, operator.auto_accept, operator.approval_callback
        ):
            outcome = await dispatch_tool("spawn_agent", {"role": role, "task": task})
    except Exception as error:
        console.print(f"[dj.err]Spawn failed: {error}[/]")
        return
    _render_outcome(outcome)


# ── /waves ──────────────────────────────────────────────────────────────────


async def handle_waves(operator: Any, arg: str = "", *, orchestrator: Any = None) -> None:
    console = _console()
    if not arg.strip():
        console.print("[dj.warn]Usage: /waves <task description>[/]")
        return
    shadow = getattr(orchestrator, "_shadow", None)
    if shadow is None:
        console.print("[dj.err]The orchestrator is not available in this session.[/]")
        return

    from djcode.orchestrator.engine import ExecutionStrategy
    from djcode.orchestrator.events import EventType

    console.print(f"\n[{_accent()}]{_glyph('bullet')} Waves[/][dim]({arg})[/]")
    completed = False
    try:
        async for event in shadow.execute(arg, strategy_override=ExecutionStrategy.WAVE):
            kind = event.event_type
            data = event.data or {}
            if kind in (EventType.AGENT_ERROR, EventType.ORCHESTRATOR_ERROR):
                console.print(f"  [dj.err]{data.get('error', 'wave execution failed')}[/]")
                return
            if kind == EventType.ORCHESTRATOR_COMPLETE:
                completed = True
            elif kind == EventType.WAVE_START:
                console.print(f"  [{_accent()}]wave {data.get('wave', '?')}[/]")
            elif kind == EventType.WAVE_COMPLETE:
                console.print(f"  [dj.ok]{_glyph('ok')} wave {data.get('wave', '?')}[/]")
            elif kind == EventType.AGENT_START:
                console.print(
                    f"    [dim]{_glyph('running')} {data.get('agent_name', '')}[/]"
                )
            elif kind == EventType.AGENT_COMPLETE:
                console.print(
                    f"    [dj.ok]{_glyph('ok')}[/] [dim]{data.get('agent_name', '')}[/]"
                )
    except Exception as error:
        console.print(f"[dj.err]Wave execution failed: {error}[/]")
        return
    if not completed:
        console.print("  [dj.warn]The run ended without reporting completion.[/]")


# ── shared ──────────────────────────────────────────────────────────────────


def _render_outcome(outcome: Any) -> None:
    """Print a tool result through the card renderer, honouring `ok_source`."""
    from djcode.frontends.repl.render import render_tool_result

    render_tool_result(
        str(outcome),
        dict(getattr(outcome, "details", None) or {}),
        ok=bool(getattr(outcome, "ok", True)),
        duration_ms=int(getattr(outcome, "duration_ms", 0) or 0),
        spill_path=getattr(outcome, "spill_path", None),
    )


#: Command name -> handler. `repl.py` dispatches through this so adding a
#: command here is the only edit required.
PORTED = {
    "/context": handle_context,
    "/cost": handle_cost,
    "/search": handle_search,
    "/tasks": handle_tasks,
    "/todo": handle_todo,
    "/spawn": handle_spawn,
    "/waves": handle_waves,
}
