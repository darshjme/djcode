"""DJcode v4.0 Hacker Widgets -- Cyberpunk terminal components.

Custom Textual widgets for the military command center aesthetic:
  - AgentStatusBar    -- All agents with live state indicators
  - HackerHeader      -- Military HUD top bar with system telemetry
  - ProgressHUD       -- Heads-up display for current operation
  - ContextBar        -- Context window utilization meter
"""

from __future__ import annotations

import time
from typing import Any

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Use the execution registry so new roles cannot silently disappear from the HUD.
from djcode import __version__
from djcode.agents.registry import AGENT_SPECS
from djcode.tui_theme import (
    ERROR,
    GOLD,
    INFO,
    SUCCESS,
    TEXT_BASE,
    TEXT_DIM,
    TEXT_STRONG,
    THINKING,
    TIER_1_EXECUTION,
    TIER_2_ARCHITECTURE,
    TIER_3_ENTERPRISE,
    TIER_4_CONTROL,
    WARNING,
)

AGENT_ROSTER: list[tuple[str, str, int]] = [
    (spec.name, spec.title, spec.tier) for spec in AGENT_SPECS.values()
]

# Agent state -> (icon, color_key)
AGENT_STATES: dict[str, tuple[str, str]] = {
    "executing": (">>", SUCCESS),
    "researching": ("??", GOLD),
    "reviewing": ("!!", THINKING),
    "error": ("XX", ERROR),
    "idle": ("--", TEXT_DIM),
    "ready": ("..", TEXT_BASE),
    "blocked": ("!!", WARNING),
}

# Tier -> color
TIER_COLORS: dict[int, str] = {
    4: TIER_4_CONTROL,
    3: TIER_3_ENTERPRISE,
    2: TIER_2_ARCHITECTURE,
    1: TIER_1_EXECUTION,
}

# ---------------------------------------------------------------------------
# 2. AgentStatusBar -- Compact bar showing all agents with state indicators
# ---------------------------------------------------------------------------


class AgentStatusBar(Widget):
    """Horizontal bar showing all 18 agents with color-coded state indicators.

    Example: [>> Prometheus EXEC] [?? Sherlock RSRCH] [!! Kavach REVIEW] ...
    """

    DEFAULT_CSS = """
    AgentStatusBar {
        height: auto;
        min-height: 1;
        max-height: 3;
        background: #0A0A0A;
        padding: 0 1;
        overflow-y: auto;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._states: dict[str, str] = {}
        for name, _, _ in AGENT_ROSTER:
            self._states[name] = "idle"

    def compose(self) -> ComposeResult:
        yield Static(self._build_bar(), id="agent-bar-display")

    def _build_bar(self) -> str:
        """Show active work; idle roles remain available in the agent dashboard."""
        active = [(name, state) for name, state in self._states.items() if state != "idle"]
        if not active:
            return f"[{TEXT_DIM}]{len(self._states)} specialists ready · F5 agents[/]"
        return " · ".join(
            f"[{AGENT_STATES.get(state, ('', TEXT_BASE))[1]}]{name}: {state}[/]"
            for name, state in active
        )

    def set_agent_state(self, agent_name: str, state: str) -> None:
        """Update a single agent's state and refresh display."""
        if agent_name in self._states:
            self._states[agent_name] = state
            try:
                self.query_one("#agent-bar-display", Static).update(self._build_bar())
            except Exception:
                pass

    def set_all_idle(self) -> None:
        """Reset all agents to idle state."""
        for name in self._states:
            self._states[name] = "idle"
        try:
            self.query_one("#agent-bar-display", Static).update(self._build_bar())
        except Exception:
            pass

    def get_active_count(self) -> int:
        """Return count of non-idle agents."""
        return sum(1 for s in self._states.values() if s != "idle")


# ---------------------------------------------------------------------------
# 3. HackerHeader -- Military-style top bar with system telemetry
# ---------------------------------------------------------------------------


class HackerHeader(Widget):
    """Military HUD header showing system status at a glance.

    DJcode v4.0 | Model: claude-opus-4-6 | Context: 45% (450K/1M) | Agents: 3/18 | Cost: $0.42
    """

    DEFAULT_CSS = """
    HackerHeader {
        height: 2;
        background: #0A0A0A;
        border-bottom: solid #1E1E1E;
        padding: 0 1;
        content-align: center middle;
    }
    """

    model_name: reactive[str] = reactive("unknown")
    context_pct: reactive[int] = reactive(0)
    context_used: reactive[str] = reactive("0K")
    context_max: reactive[str] = reactive("1M")
    active_agents: reactive[int] = reactive(0)
    total_agents: reactive[int] = reactive(len(AGENT_ROSTER))
    session_cost: reactive[str] = reactive("$0.00")
    mode: reactive[str] = reactive("ACT")
    version: reactive[str] = reactive(__version__)

    def compose(self) -> ComposeResult:
        yield Static(self._build_header(), id="hacker-header-display")

    def _build_header(self) -> str:
        """Build the full header markup."""
        # Mode indicator
        mode_color = SUCCESS if self.mode == "ACT" else TIER_2_ARCHITECTURE
        mode_str = f"[bold {mode_color}]{self.mode}[/]"

        # Context color based on usage
        if self.context_pct < 60:
            ctx_color = SUCCESS
        elif self.context_pct < 85:
            ctx_color = WARNING
        else:
            ctx_color = ERROR

        ctx_bar = self._mini_bar(self.context_pct)

        # Agent count color
        agent_color = SUCCESS if self.active_agents > 0 else TEXT_DIM

        width = self.size.width or 120
        from rich.markup import escape

        model = self.model_name
        limit = max(12, width - 65)
        if len(model) > limit:
            model = model[: limit - 1] + "…"
        parts = [f"[bold {GOLD}]DJcode[/] [{TEXT_DIM}]v{self.version}[/]", mode_str]
        parts.append(f"[{TEXT_STRONG}]{escape(model)}[/]")
        if width >= 70:
            parts.append(f"[{ctx_color}]Context {self.context_pct}%[/]")
        if width >= 110:
            parts.append(f"[{agent_color}]Agents {self.active_agents}/{self.total_agents}[/]")

        return "  ·  ".join(parts)

    def on_resize(self) -> None:
        self._refresh()

    @staticmethod
    def _mini_bar(pct: int, width: int = 8) -> str:
        """Build a tiny progress bar: [####----]."""
        filled = max(0, min(width, int(pct / 100 * width)))
        return "[" + "#" * filled + "-" * (width - filled) + "]"

    def _refresh(self) -> None:
        try:
            self.query_one("#hacker-header-display", Static).update(self._build_header())
        except Exception:
            pass

    def watch_model_name(self, value: str) -> None:
        self._refresh()

    def watch_context_pct(self, value: int) -> None:
        self._refresh()

    def watch_active_agents(self, value: int) -> None:
        self._refresh()

    def watch_session_cost(self, value: str) -> None:
        self._refresh()

    def watch_mode(self, value: str) -> None:
        self._refresh()

    def update_context(self, used_tokens: int, max_tokens: int) -> None:
        """Update context utilization from raw token counts."""
        if max_tokens > 0:
            self.context_pct = int((used_tokens / max_tokens) * 100)
        self.context_used = self._fmt_tokens(used_tokens)
        self.context_max = self._fmt_tokens(max_tokens)
        self._refresh()

    @staticmethod
    def _fmt_tokens(count: int) -> str:
        if count >= 1_000_000:
            return f"{count / 1_000_000:.0f}M"
        if count >= 1_000:
            return f"{count / 1_000:.0f}K"
        return str(count)


# ---------------------------------------------------------------------------
# 4. ProgressHUD -- Heads-up display for current operation
# ---------------------------------------------------------------------------


class ProgressHUD(Widget):
    """Animated progress display showing operation telemetry.

    [ GENERATING ] 12.4s | 142 tok/s | 7 tool calls | 3 files changed
    """

    DEFAULT_CSS = """
    ProgressHUD {
        height: 2;
        background: #0E0E0E;
        border: solid #1E1E1E;
        padding: 0 1;
        content-align: left middle;
    }
    """

    is_active: reactive[bool] = reactive(False)
    operation: reactive[str] = reactive("IDLE")
    elapsed_sec: reactive[float] = reactive(0.0)
    tokens_per_sec: reactive[float] = reactive(0.0)
    tool_calls: reactive[int] = reactive(0)
    files_changed: reactive[int] = reactive(0)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._start_time: float = 0.0
        self._spinner_idx: int = 0
        self._timer: Timer | None = None
        self._spinner_chars = ["|", "/", "-", "\\"]

    def compose(self) -> ComposeResult:
        yield Static(self._build_display(), id="progress-hud-display")

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.25, self._tick)

    def _tick(self) -> None:
        if not self.is_active:
            return
        self._spinner_idx = (self._spinner_idx + 1) % len(self._spinner_chars)
        if self._start_time > 0:
            self.elapsed_sec = time.time() - self._start_time
        self._refresh()

    def _build_display(self) -> str:
        if not self.is_active:
            return f"  [{TEXT_DIM}][ IDLE ] Awaiting command...[/]"

        spinner = self._spinner_chars[self._spinner_idx]
        op_color = (
            SUCCESS
            if self.operation == "EXECUTING"
            else (
                THINKING
                if self.operation in ("THINKING", "GENERATING")
                else (GOLD if self.operation == "RESEARCHING" else TEXT_BASE)
            )
        )

        elapsed = f"{self.elapsed_sec:.1f}s"
        tps = f"{self.tokens_per_sec:.0f} tok/s" if self.tokens_per_sec > 0 else "--"

        parts = [
            f"  [{op_color}]{spinner} [ {self.operation} ][/]",
            f"[{TEXT_BASE}]{elapsed}[/]",
            f"[{TEXT_DIM}]|[/]",
            f"[{SUCCESS}]{tps}[/]",
            f"[{TEXT_DIM}]|[/]",
            f"[{INFO}]{self.tool_calls} tools[/]",
            f"[{TEXT_DIM}]|[/]",
            f"[{GOLD}]{self.files_changed} files[/]",
        ]
        return "  ".join(parts)

    def _refresh(self) -> None:
        try:
            self.query_one("#progress-hud-display", Static).update(self._build_display())
        except Exception:
            pass

    def start_operation(self, operation: str = "GENERATING") -> None:
        """Begin tracking a new operation."""
        self.operation = operation
        self.is_active = True
        self._start_time = time.time()
        self.elapsed_sec = 0.0
        self.tokens_per_sec = 0.0
        self.tool_calls = 0
        self.files_changed = 0
        self._refresh()

    def stop_operation(self) -> None:
        """Mark current operation as complete."""
        self.is_active = False
        self.operation = "IDLE"
        self._refresh()

    def increment_tools(self) -> None:
        self.tool_calls += 1
        self._refresh()

    def increment_files(self) -> None:
        self.files_changed += 1
        self._refresh()

    def update_tps(self, tps: float) -> None:
        self.tokens_per_sec = tps
        self._refresh()


# ---------------------------------------------------------------------------
# 6. ContextBar -- Context window utilization meter
# ---------------------------------------------------------------------------


class ContextBar(Widget):
    """Visual context window utilization bar with color thresholds.

    CONTEXT [##########----------] 45% (450K / 1M tokens)
    """

    DEFAULT_CSS = """
    ContextBar {
        height: 3;
        background: #0E0E0E;
        padding: 0 1;
        border: solid #1E1E1E;
    }
    """

    used_tokens: reactive[int] = reactive(0)
    max_tokens: reactive[int] = reactive(1_000_000)

    def compose(self) -> ComposeResult:
        yield Static(self._build_bar(), id="context-bar-display")

    def _build_bar(self) -> str:
        pct = int((self.used_tokens / self.max_tokens) * 100) if self.max_tokens > 0 else 0
        pct = min(100, max(0, pct))

        # Color thresholds
        if pct < 50:
            color = SUCCESS
        elif pct < 75:
            color = GOLD
        elif pct < 90:
            color = WARNING
        else:
            color = ERROR

        bar_width = 30
        filled = int(pct / 100 * bar_width)
        empty = bar_width - filled

        used_str = self._fmt(self.used_tokens)
        max_str = self._fmt(self.max_tokens)

        bar = f"[{color}]{'#' * filled}[/][{TEXT_DIM}]{'-' * empty}[/]"

        return (
            f"  [{GOLD}]CONTEXT[/] [{TEXT_DIM}][[/]{bar}[{TEXT_DIM}]][/]"
            f" [{color}]{pct}%[/]"
            f" [{TEXT_DIM}]({used_str} / {max_str} tokens)[/]"
        )

    def watch_used_tokens(self, value: int) -> None:
        self._refresh()

    def watch_max_tokens(self, value: int) -> None:
        self._refresh()

    def _refresh(self) -> None:
        try:
            self.query_one("#context-bar-display", Static).update(self._build_bar())
        except Exception:
            pass

    @staticmethod
    def _fmt(count: int) -> str:
        if count >= 1_000_000:
            return f"{count / 1_000_000:.1f}M"
        if count >= 1_000:
            return f"{count / 1_000:.0f}K"
        return str(count)


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "AGENT_ROSTER",
    "AGENT_STATES",
    "TIER_COLORS",
    "AgentStatusBar",
    "HackerHeader",
    "ProgressHUD",
    "ContextBar",
]
