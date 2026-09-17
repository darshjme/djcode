"""Advanced TUI enhancements for DJcode.

Provides keyboard shortcuts, mode system, interactive command picker,
progress tracking, diff display, and help overlay. Uses prompt-toolkit
for keybindings and Rich for rendering.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import TYPE_CHECKING, Any

import questionary
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from djcode.commands import command_groups

if TYPE_CHECKING:
    from prompt_toolkit import PromptSession

    from djcode.agents.operator import Operator
    from djcode.status import StatusBar

GOLD = "#C79B7A"

console = Console()

# ---------------------------------------------------------------------------
# Mode system
# ---------------------------------------------------------------------------


class ModeState:
    """Global mode state for the TUI."""

    def __init__(self) -> None:
        self.plan_mode: bool = False
        self.verbose_thinking: bool = True
        self.auto_accept: bool = False
        self.last_user_input: str = ""
        self._generation_cancelled: bool = False

    @property
    def mode_label(self) -> str:
        return "PLAN" if self.plan_mode else "ACT"

    @property
    def plan_mode_prompt_injection(self) -> str:
        if self.plan_mode:
            return (
                "[PLAN MODE] Do not execute any tools. Only describe what "
                "you would do. List every step, file, and command you would "
                "run, but do NOT actually run anything."
            )
        return ""

    def toggle_plan_mode(self) -> None:
        self.plan_mode = not self.plan_mode

    def toggle_thinking(self) -> None:
        self.verbose_thinking = not self.verbose_thinking

    def cancel_generation(self) -> None:
        self._generation_cancelled = True

    def reset_cancel(self) -> None:
        self._generation_cancelled = False

    @property
    def is_cancelled(self) -> bool:
        return self._generation_cancelled


_mode = ModeState()


def get_mode_state() -> ModeState:
    """Return the singleton mode state."""
    return _mode


# ---------------------------------------------------------------------------
# Keybindings
# ---------------------------------------------------------------------------


def register_keybindings(
    session: PromptSession,
    operator: Operator,
    status_bar: StatusBar,
    orchestrator=None,
) -> KeyBindings:
    """Register all DJcode keyboard shortcuts on the given PromptSession.

    Returns the KeyBindings object (also attached to the session).
    """
    kb = KeyBindings()

    # Ctrl+O — Toggle verbose/thinking mode
    @kb.add("c-o")
    def _toggle_thinking(event: Any) -> None:
        _mode.verbose_thinking = not _mode.verbose_thinking
        operator.show_thinking = _mode.verbose_thinking
        label = "ON" if _mode.verbose_thinking else "OFF"
        event.app.output.write(f"\r\033[K[thinking: {label}]\n")
        event.app.output.flush()

    # Ctrl+L — Clear screen (keep conversation)
    @kb.add("c-l")
    def _clear_screen(event: Any) -> None:
        event.app.renderer.clear()

    # Ctrl+R — Rerun last command
    @kb.add("c-r")
    def _rerun_last(event: Any) -> None:
        if _mode.last_user_input:
            buf = event.app.current_buffer
            buf.text = _mode.last_user_input
            buf.cursor_position = len(buf.text)

    # Ctrl+T — Toggle auto-accept tools, FOR THIS PROCESS ONLY.
    @kb.add("c-t")
    def _toggle_auto_accept(event: Any) -> None:
        # W6 established that `--auto-accept` must not rewrite config.json:
        # a flag that silently changes the default for every future session is
        # how a machine ends up permanently unattended with nobody having
        # decided that. Ctrl+T was still doing exactly that via
        # `set_value("auto_accept", True)` -- the same bug behind a different
        # trigger. The toggle is now session-scoped, and the permission engine
        # is moved with it so the chokepoint's own `resolve` agrees.
        new_val = not operator.auto_accept
        operator.auto_accept = new_val
        permissions = getattr(operator, "permissions", None)
        if permissions is not None:
            from djcode.core.permissions import Mode

            permissions.mode = Mode.AUTO if new_val else Mode.MANUAL
        if orchestrator is not None:
            orchestrator.auto_accept = new_val
            orchestrator._shadow.auto_accept = new_val
        _mode.auto_accept = new_val
        status_bar.update(auto_accept=new_val)
        label = "ON" if new_val else "OFF"
        event.app.output.write(f"\r\033[K[auto-accept: {label}]\n")
        event.app.output.flush()

    # Input shortcuts are active only while the prompt is accepting input.
    @kb.add("c-k")
    def _kill_generation(event: Any) -> None:
        event.app.current_buffer.delete(len(event.app.current_buffer.text_after_cursor))

    # Ctrl+P — Toggle plan/act mode
    @kb.add("c-p")
    def _toggle_plan_mode(event: Any) -> None:
        _mode.plan_mode = not _mode.plan_mode
        operator.plan_mode = _mode.plan_mode
        label = _mode.mode_label
        status_bar.update(mode=label)
        event.app.output.write(f"\r\033[K[mode: {label}]\n")
        event.app.output.flush()

    # Escape — Cancel current input
    @kb.add("escape", eager=True)
    def _cancel_input(event: Any) -> None:
        buf = event.app.current_buffer
        if buf.text:
            buf.text = ""
            buf.cursor_position = 0

    session.key_bindings = kb
    return kb


# ---------------------------------------------------------------------------
# Interactive slash command picker
# ---------------------------------------------------------------------------

COMMAND_GROUPS = command_groups("repl")

Q_STYLE = questionary.Style(
    [
        ("selected", "fg:#C79B7A bold"),
        ("pointer", "fg:#C79B7A bold"),
        ("highlighted", "fg:#C79B7A"),
        ("question", "fg:#C79B7A bold"),
        ("answer", "fg:#FFFFFF bold"),
        ("separator", "fg:#666666"),
    ]
)


def show_command_picker() -> str | None:
    """Show a fuzzy-filterable interactive slash command picker.

    Returns the selected command string (e.g. "/orchestra") or None if
    the user cancelled.
    """
    choices: list[questionary.Choice | questionary.Separator] = []
    for group, commands in COMMAND_GROUPS.items():
        choices.append(questionary.Separator(f"--- {group} ---"))
        for cmd, desc in commands:
            label = f"{cmd:<16} {desc}"
            choices.append(questionary.Choice(title=label, value=cmd))

    result = questionary.select(
        "Pick a command:",
        choices=choices,
        style=Q_STYLE,
        use_shortcuts=False,
    ).ask()

    return result


# ---------------------------------------------------------------------------
# Progress tracker
# ---------------------------------------------------------------------------

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
STALL_THRESHOLD = 30.0  # seconds


class ProgressTracker:
    """Animated progress display during long operations.

    Shows spinner, agent name, tool, elapsed time, and token count.
    Turns red after STALL_THRESHOLD seconds with no output.
    """

    def __init__(self) -> None:
        self._running = False
        self._thread: threading.Thread | None = None
        self._agent: str = ""
        self._tool: str = ""
        self._token_count: int = 0
        self._start_time: float = 0.0
        self._last_activity: float = 0.0
        self._frame_idx: int = 0

    def start(self, agent: str = "Operator") -> None:
        """Begin the progress animation."""
        self._agent = agent
        self._tool = ""
        self._token_count = 0
        self._start_time = time.time()
        self._last_activity = self._start_time
        self._frame_idx = 0
        self._running = True
        self._thread = threading.Thread(target=self._animate, daemon=True)
        self._thread.start()

    def update(
        self,
        *,
        tool: str | None = None,
        tokens: int | None = None,
        agent: str | None = None,
    ) -> None:
        """Update progress state (call from streaming loop)."""
        self._last_activity = time.time()
        if tool is not None:
            self._tool = tool
        if tokens is not None:
            self._token_count = tokens
        if agent is not None:
            self._agent = agent

    def tick_token(self) -> None:
        """Increment the token counter by one."""
        self._token_count += 1
        self._last_activity = time.time()

    def stop(self) -> None:
        """Stop the progress animation."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        # Clear the progress line
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()

    def _animate(self) -> None:
        """Background thread: render the progress line to stderr."""
        while self._running:
            elapsed = time.time() - self._start_time
            stalled = (time.time() - self._last_activity) > STALL_THRESHOLD
            frame = SPINNER_FRAMES[self._frame_idx % len(SPINNER_FRAMES)]
            self._frame_idx += 1

            parts = [f"Agent: {self._agent}"]
            if self._tool:
                parts.append(f"Tool: {self._tool}")
            parts.append(f"Time: {elapsed:.1f}s")
            if self._token_count > 0:
                parts.append(f"{self._token_count} tokens")

            info = " | ".join(parts)

            if stalled:
                line = f"\r\033[31m{frame} {info} [stalled]\033[0m"
            else:
                line = f"\r\033[33m{frame} {info}\033[0m"

            sys.stderr.write(line)
            sys.stderr.flush()
            time.sleep(0.1)


# ---------------------------------------------------------------------------
# Diff display -- moved to djcode.frontends.repl.diffview in W7
# ---------------------------------------------------------------------------
#
# `render_diff` and `render_inline_diff` lived here and were DEAD: W0's
# `9874316` removed the last import (`repl.py`), after which the only four
# references in `src/` and `tests/` were the two definitions and their two
# `__all__` entries. Neither could satisfy the W7 green gate -- `render_diff`
# lexed the unified-diff text with the `diff` lexer and printed no line numbers
# at all, and `render_inline_diff` printed every old line red then every new
# line green, which is not a diff. `BLUEPRINT-CLI.md` W7-3's "do not rewrite
# them, adapt their signatures" rests on the claim that `repl.py` imports them,
# which is false; the deviation is recorded in `diffview.py`'s module docstring.


# ---------------------------------------------------------------------------
# Help overlay / shortcuts card
# ---------------------------------------------------------------------------

#: What the keys ACTUALLY do. Every row here was checked against
#: `register_keybindings` above and against the prompt session repl.py builds;
#: `tests/test_repl_shortcuts.py` re-checks the ones it can. The card used to
#: advertise a bare-"/" command picker that W9 deleted, and described Ctrl+C as
#: cancelling "the current response" at a time when Ctrl+C on Windows quit the
#: program instead. A shortcuts card that is wrong is worse than none: it is
#: the one place a user goes to find out what is true.
SHORTCUTS_TABLE = [
    ("Ctrl+O", "Toggle thinking output"),
    ("Ctrl+L", "Clear the screen, keep the conversation"),
    ("Ctrl+T", "Toggle auto-accept for this session only"),
    ("Ctrl+P", "Toggle plan/act mode"),
    ("Ctrl+R", "Put the last prompt back in the buffer"),
    ("Ctrl+K", "Delete input after the cursor"),
    ("Ctrl+C", "Cancel the running turn, or clear the input"),
    ("Ctrl+D", "Exit at an empty prompt"),
    ("Tab", "Complete commands, subcommands, arguments and @paths"),
    ("Up / Down", "History, filtered by what is already typed"),
    ("Escape", "Clear the input"),
    ("@", "Complete a file path, anywhere in the line"),
    ("/", "Show the command list inline"),
]


def show_shortcuts() -> None:
    """Display a keybindings reference card as a Rich panel."""
    table = Table(
        show_header=False,
        box=None,
        padding=(0, 2),
        expand=False,
    )
    table.add_column("Key", style=f"bold {GOLD}", min_width=12)
    table.add_column("Action", style="white")

    for key, action in SHORTCUTS_TABLE:
        table.add_row(key, action)

    console.print()
    console.print(
        Panel(
            table,
            title=f"[bold {GOLD}]Keyboard Shortcuts[/]",
            border_style=GOLD,
            padding=(1, 2),
        )
    )
    console.print()


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "ModeState",
    "ProgressTracker",
    "get_mode_state",
    "register_keybindings",
    "show_command_picker",
    "show_shortcuts",
    "COMMAND_GROUPS",
]
