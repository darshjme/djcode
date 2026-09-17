"""The approval card — the moment the user decides, and the only one that matters.

What this replaces (``repl.py::_approve_repl_tool``)::

    ┌─ Approve tool ──────────────┐
    │ Tool: bash                  │
    │ {                           │
    │   "command": "pytest -q"    │
    │ }                           │
    └─────────────────────────────┘
    ? Execute this tool?  (y/N)

Two problems with that, both of which this module exists to fix.

1. **It could only say yes or no.** ``Operator._approve_tool`` has accepted a
   :class:`~djcode.core.permissions.Decision` since W6 -- including
   ``ALWAYS`` and ``SESSION``, whose persistence is already wired into
   ``grant_always``/``grant_session`` -- but the REPL returned ``bool``, so
   "don't ask me again for pytest" was unreachable from the only surface that
   ships. Every approval was a fresh decision, forever.
2. **It showed the wrong thing.** ``json.dumps(arguments)`` is the tool call,
   not its consequence. W7 fixed that for ``file_edit``/``file_write`` by
   drawing a real diff; this module keeps that and adds the two facts the other
   22 tools need: for a shell command, the executables it will actually run
   (``argv:``), and for everything, the rule ``a`` would persist -- because
   "always allow" is not a decision anyone can make safely without being shown
   its exact scope first.

The keys are single-press, per ``DESIGN-CLI.md`` §2.3: ``y`` once, ``a``
always, ``s`` this session, ``n`` no, ``e`` no with a note to the model, ``d``
show the full arguments. ``Esc`` and ``Ctrl+C`` are ``n`` -- the fail-closed
direction, and the same direction ``Decision.__bool__`` picks.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from djcode.core.permissions import (
    SHELL_COMMAND_TOOLS,
    Decision,
    DecisionAction,
    PermissionEngine,
    Rule,
    ToolRequest,
    command_of,
    hardline_message,
    hardline_reason,
    tokenise,
)

GOLD = "#C79B7A"

#: Key -> (label, help text). Order is the order they are offered.
CHOICES: tuple[tuple[str, str, str], ...] = (
    ("y", "yes", "run it once"),
    ("a", "always", "run it, and stop asking"),
    ("s", "session", "run it, and stop asking until DJcode exits"),
    ("n", "no", "do not run it"),
    ("e", "no, with a note", "tell the model what to do instead"),
    ("d", "details", "show the full arguments"),
)

_ACTIONS = {
    "y": DecisionAction.ALLOW,
    "a": DecisionAction.ALWAYS,
    "s": DecisionAction.SESSION,
    "n": DecisionAction.DENY,
    "e": DecisionAction.DENY,
}


def _display_name(tool: str) -> str:
    from djcode.frontends.repl.render import TOOL_DISPLAY

    return TOOL_DISPLAY.get(tool, tool.title())


def argv_line(command: str) -> str:
    """The executables a shell command actually runs, in order.

    A pasted one-liner can hide a second command behind a ``;``, a ``&&`` or a
    backtick, and the user is about to authorise all of them. ``tokenise``
    already extracts exactly this for the permission engine; the card shows the
    user the same list the engine judged, so an approval is never given to
    something the engine saw and the human did not.
    """
    if not command.strip():
        return ""
    try:
        return " ".join(tokenise(command).names())
    except Exception:  # pragma: no cover - the card must never raise
        return ""


def describe_rule(rule: Rule | None) -> str:
    """How an "always" would read once persisted."""
    if rule is None:
        return ""
    return f"{rule.tool}({rule.pattern})"


def render_card(
    console: Console,
    name: str,
    arguments: dict[str, Any],
    *,
    rule: Rule | None = None,
    agent_name: str = "",
    detailed: bool = False,
) -> None:
    """Draw the approval card. Preview first, then scope, then keys."""
    from djcode.repl import _render_approval_preview

    title = f"[bold {GOLD}]Approve[/]  [bold white]{_display_name(name)}[/]"
    if agent_name:
        title += f"  [dim]· via {agent_name}[/]"

    body = Text()
    command = command_of(name, arguments)
    if command:
        shown = command.strip()
        body.append(shown[:600] + ("…" if len(shown) > 600 else ""), style="white")
        names = argv_line(command)
        if names:
            body.append(f"\nargv: {names}", style="dim")
        reason = hardline_reason(command)
        if reason:
            body.append("\n\n" + hardline_message(command, reason), style="red")
    elif detailed:
        body.append(json.dumps(arguments, indent=2, default=str)[:4000], style="white")
    else:
        from djcode.frontends.repl.render import format_tool_args

        summary = format_tool_args(name, arguments)
        if summary:
            body.append(summary, style="white")

    scope = describe_rule(rule)
    if scope:
        if body.plain:
            body.append("\n\n")
        body.append(f"always  →  {scope}", style="dim")

    console.print()
    drew_preview = False
    if name in ("file_edit", "file_write"):
        try:
            drew_preview = _render_approval_preview(name, arguments)
        except Exception:  # pragma: no cover - a preview must never block approval
            drew_preview = False
    if drew_preview and not body.plain:
        console.print(body)
    elif drew_preview:
        console.print(f"  {title}")
    else:
        console.print(Panel(body, title=title, border_style=GOLD, title_align="left"))

    console.print(
        "  [dim]"
        + "  ".join(f"[{GOLD}]{key}[/] {label}" for key, label, _ in CHOICES)
        + "[/]"
    )


async def read_choice(keys: str = "yasned") -> str:
    """Block for exactly one keypress out of ``keys``. Esc / Ctrl+C mean ``n``.

    A tiny zero-height ``prompt_toolkit`` Application rather than
    ``questionary``: questionary's ``confirm`` can only carry two answers and
    its ``select`` needs an Enter after the shortcut, and an approval prompt is
    the one place in the program where a stray Enter must not be able to
    approve anything.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout, Window

    bindings = KeyBindings()

    def _bind(key: str) -> None:
        @bindings.add(key, eager=True)
        def _(event, key=key) -> None:
            event.app.exit(result=key)

    for key in keys:
        _bind(key)
        _bind(key.upper())

    @bindings.add("escape", eager=True)
    @bindings.add("c-c")
    @bindings.add("c-d")
    def _refuse(event) -> None:
        event.app.exit(result="n")

    application: Application[str] = Application(
        layout=Layout(Window(height=0)),
        key_bindings=bindings,
        erase_when_done=True,
    )
    answer = await application.run_async()
    return (answer or "n").lower()


async def read_note() -> str:
    """The note that rides along with an ``e`` denial."""
    import questionary

    answer = await questionary.text(
        "What should the model do instead?", qmark="›"
    ).ask_async()
    return (answer or "").strip()


async def ask(
    name: str,
    arguments: dict[str, Any],
    *,
    engine: PermissionEngine | None = None,
    console: Console | None = None,
    agent_name: str = "",
) -> Decision:
    """Ask the user about one tool call and return the engine's answer type.

    ``ALWAYS`` and ``SESSION`` carry the :class:`Rule` they should persist so
    ``Operator._approve_tool`` can hand it straight to ``grant_always`` /
    ``grant_session`` -- the front-end decides the scope, the engine owns the
    storage.
    """
    from djcode.frontends.repl.render import console as default_console

    console = console or default_console

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        # Audit 2 §2.3: constructing a prompt_toolkit Application against a
        # pipe raises NoConsoleScreenBufferError on Windows. Refuse the call
        # rather than crash the turn, and tell the model why so it can adapt.
        return Decision(
            DecisionAction.DENY,
            comment=(
                "This session has no interactive terminal, so the tool could not be "
                "approved. Re-run with --auto-accept for an authorised unattended task."
            ),
        )

    rule: Rule | None = None
    if engine is not None:
        try:
            rule = engine.rule_for(
                ToolRequest(tool=name, arguments=dict(arguments), cwd=None)
            )
        except Exception:  # pragma: no cover - never block approval over a label
            rule = None

    detailed = False
    while True:
        render_card(
            console, name, arguments, rule=rule, agent_name=agent_name, detailed=detailed
        )
        key = await read_choice()
        if key == "d":
            detailed = True
            continue
        if key == "e":
            note = await read_note()
            return Decision(
                DecisionAction.DENY,
                comment=note
                or "The user declined this tool call and did not say what to do instead.",
            )
        action = _ACTIONS.get(key, DecisionAction.DENY)
        if action is DecisionAction.DENY:
            return Decision(
                DecisionAction.DENY,
                comment="The user declined this tool call. Ask before trying it again.",
            )
        return Decision(action, rule=rule if action is not DecisionAction.ALLOW else None)


__all__ = [
    "CHOICES",
    "SHELL_COMMAND_TOOLS",
    "argv_line",
    "ask",
    "describe_rule",
    "read_choice",
    "render_card",
]
