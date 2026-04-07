"""Advanced TUI enhancements for DJcode.

Provides keyboard shortcuts, mode system, interactive command picker,
progress tracking, diff display, and help overlay. Uses prompt-toolkit
for keybindings and Rich for rendering.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import threading
from typing import TYPE_CHECKING, Any, Callable

import questionary
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from prompt_toolkit import PromptSession
    from djcode.agents.operator import Operator
    from djcode.status import StatusBar

GOLD = "#FFD700"

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

    # Ctrl+T — Toggle auto-accept tools
    @kb.add("c-t")
    def _toggle_auto_accept(event: Any) -> None:
        from djcode.config import set_value, load_config

        cfg = load_config()
        new_val = not cfg.get("auto_accept", False)
        set_value("auto_accept", new_val)
        operator.auto_accept = new_val
        _mode.auto_accept = new_val
        status_bar.update(auto_accept=new_val)
        label = "ON" if new_val else "OFF"
        event.app.output.write(f"\r\033[K[auto-accept: {label}]\n")
        event.app.output.flush()

    # Ctrl+K — Kill current generation
    @kb.add("c-k")
    def _kill_generation(event: Any) -> None:
        _mode.cancel_generation()
        event.app.output.write("\r\033[K[generation cancelled]\n")
        event.app.output.flush()

    # Ctrl+P — Toggle plan/act mode
    @kb.add("c-p")
    def _toggle_plan_mode(event: Any) -> None:
        _mode.plan_mode = not _mode.plan_mode
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

COMMAND_GROUPS: dict[str, list[tuple[str, str]]] = {
    "Build": [
        ("/orchestra", "Multi-agent orchestration (auto-dispatch)"),
        ("/review", "Code review (Dharma agent)"),
        ("/debug", "Root cause analysis (Sherlock agent)"),
        ("/test", "Write tests (Agni agent)"),
        ("/refactor", "Restructure code (Shiva agent)"),
        ("/devops", "Docker/CI/CD (Vayu agent)"),
        ("/docs", "Generate docs (Saraswati agent)"),
    ],
    "Content": [
        ("/campaign", "Content campaign (12 content agents)"),
        ("/launch", "Build + Ship + Campaign (full pipeline)"),
        ("/image", "Generate image prompts (Maya)"),
        ("/video", "Cinematic video prompts (Kubera)"),
        ("/social", "Social media content (Chitragupta)"),
    ],
    "Tools": [
        ("/model", "Interactive model picker"),
        ("/provider", "Interactive provider picker"),
        ("/auth", "Configure provider + API key"),
        ("/config", "Show current config"),
        ("/set", "Set a config value"),
    ],
    "Info": [
        ("/help", "Show help"),
        ("/agents", "Show all 22 agents roster"),
        ("/stats", "Usage dashboard with activity heatmap"),
        ("/memory", "Show memory stats"),
        ("/buddy", "Show your buddy + speech bubble"),
        ("/shortcuts", "Show keyboard shortcuts"),
    ],
    "Session": [
        ("/clear", "Clear conversation history"),
        ("/save", "Save conversation to disk"),
        ("/exit", "Exit DJcode"),
    ],
}

Q_STYLE = questionary.Style([
    ("selected", "fg:#FFD700 bold"),
    ("pointer", "fg:#FFD700 bold"),
    ("highlighted", "fg:#FFD700"),
    ("question", "fg:#FFD700 bold"),
    ("answer", "fg:#FFFFFF bold"),
    ("separator", "fg:#666666"),
])


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
# Diff display
# ---------------------------------------------------------------------------

def render_diff(
    file_path: str,
    old_text: str,
    new_text: str,
    *,
    context_lines: int = 3,
) -> None:
    """Render a colored diff of old_text vs new_text for a file.

    Uses Rich Syntax with the diff lexer for proper coloring.
    """
    import difflib

    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)

    diff_lines = list(difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
        n=context_lines,
    ))

    if not diff_lines:
        console.print(f"[dim]  {file_path}: no changes[/]")
        return

    diff_text = "".join(diff_lines)

    console.print()
    console.print(
        Panel(
            Syntax(diff_text, "diff", theme="monokai", line_numbers=False),
            title=f"[bold white]{file_path}[/]",
            border_style=GOLD,
            padding=(0, 1),
        )
    )


def render_inline_diff(file_path: str, old_string: str, new_string: str) -> None:
    """Render a compact inline diff (for tool results).

    Shows removed lines in red and added lines in green.
    """
    text = Text()
    text.append(f"  {file_path}\n", style="bold white")

    for line in old_string.splitlines():
        text.append(f"  - {line}\n", style="red")
    for line in new_string.splitlines():
        text.append(f"  + {line}\n", style="green")

    console.print(text)


# ---------------------------------------------------------------------------
# Help overlay / shortcuts card
# ---------------------------------------------------------------------------

SHORTCUTS_TABLE = [
    ("Ctrl+O", "Toggle thinking verbose"),
    ("Ctrl+L", "Clear screen"),
    ("Ctrl+T", "Toggle auto-accept"),
    ("Ctrl+P", "Toggle plan/act mode"),
    ("Ctrl+R", "Rerun last command"),
    ("Ctrl+K", "Kill generation"),
    ("Escape", "Cancel current input"),
    ("/", "Interactive command picker"),
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
    "render_diff",
    "render_inline_diff",
    "show_command_picker",
    "show_shortcuts",
    "COMMAND_GROUPS",
]
