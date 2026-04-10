"""DJcode v4.0 Hacker Widgets -- Cyberpunk terminal components.

Custom Textual widgets for the military command center aesthetic:
  - MatrixRain        -- Animated falling green characters
  - AgentStatusBar    -- All 18 agents with live state indicators
  - HackerHeader      -- Military HUD top bar with system telemetry
  - ProgressHUD       -- Heads-up display for current operation
  - TokenBurnRate     -- Real-time ASCII sparkline of token consumption
  - AgentDashboard    -- Full-screen agent monitoring grid
  - ContextBar        -- Context window utilization meter
  - ThreatPanel       -- Blocking agent alerts (Kavach, Varuna, Mitra, Indra)
  - ArmyView          -- Bird's eye grid of all 18 agents
"""

from __future__ import annotations

import random
import time
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, ScrollableContainer, Grid
from textual.reactive import reactive
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Label, Static, Rule

from djcode.tui_theme import (
    GOLD,
    SUCCESS,
    ERROR,
    WARNING,
    THINKING,
    MATRIX_GREEN,
    TEXT_BASE,
    TEXT_DIM,
    TEXT_STRONG,
    BG_PRIMARY,
    BG_PANEL,
    TIER_4_CONTROL,
    TIER_3_ENTERPRISE,
    TIER_2_ARCHITECTURE,
    TIER_1_EXECUTION,
    THREAT_KAVACH,
    THREAT_VARUNA,
    THREAT_MITRA,
    THREAT_INDRA,
    INFO,
    BORDER,
    HUD_BORDER,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Matrix rain character sets
MATRIX_CHARS = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "@#$%&*+=<>?/"
    "\u30a0\u30a1\u30a2\u30a3\u30a4\u30a5\u30a6\u30a7\u30a8\u30a9"
    "\u30aa\u30ab\u30ac\u30ad\u30ae\u30af\u30b0\u30b1\u30b2\u30b3"
)

# The 18 PhD agent roster (name, dharmic_title, tier)
AGENT_ROSTER: list[tuple[str, str, int]] = [
    # Tier 4 - Control
    ("Vyasa", "The Narrator", 4),
    ("Brahma", "The Creator", 4),
    # Tier 3 - Enterprise
    ("Kubera", "Wealth Lord", 3),
    ("Chitragupta", "The Scribe", 3),
    ("Maya", "The Illusionist", 3),
    # Tier 2 - Architecture
    ("Vishvakarma", "Divine Architect", 2),
    ("Saraswati", "Knowledge Keeper", 2),
    ("Brihaspati", "The Strategist", 2),
    # Tier 1 - Execution
    ("Agni", "The Forger", 1),
    ("Vayu", "The Swift", 1),
    ("Shiva", "The Destroyer", 1),
    ("Dharma", "The Judge", 1),
    ("Sherlock", "The Detective", 1),
    ("Prometheus", "The Firebringer", 1),
    # Sentinel / Security
    ("Kavach", "The Shield", 1),
    ("Varuna", "The Watcher", 1),
    ("Mitra", "The Ally", 1),
    ("Indra", "The Thunder", 1),
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

# Sparkline block characters (8 levels)
SPARK_BLOCKS = " \u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"


# ---------------------------------------------------------------------------
# 1. MatrixRain -- Animated matrix rain characters in background
# ---------------------------------------------------------------------------

class MatrixRain(Widget):
    """Green falling characters -- Matrix digital rain effect.

    Renders a field of randomly updating characters that simulate
    the iconic Matrix rain. Lightweight: updates a fixed-size text
    buffer on a timer, no per-character widgets.
    """

    DEFAULT_CSS = """
    MatrixRain {
        width: 100%;
        height: 100%;
        background: #0A0A0A;
        color: #00FF41;
        overflow: hidden;
    }
    """

    is_raining: reactive[bool] = reactive(True)

    def __init__(
        self,
        width: int = 60,
        height: int = 20,
        density: float = 0.12,
        speed_ms: int = 120,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._cols = width
        self._rows = height
        self._density = density
        self._speed_ms = speed_ms
        self._grid: list[list[str]] = []
        self._bright: list[list[bool]] = []
        self._drops: list[int] = []
        self._timer: Timer | None = None
        self._init_grid()

    def _init_grid(self) -> None:
        """Initialize the character grid and drop positions."""
        self._grid = [
            [" " for _ in range(self._cols)] for _ in range(self._rows)
        ]
        self._bright = [
            [False for _ in range(self._cols)] for _ in range(self._rows)
        ]
        self._drops = [random.randint(-self._rows, 0) for _ in range(self._cols)]

    def on_mount(self) -> None:
        self._timer = self.set_interval(self._speed_ms / 1000.0, self._tick)

    def _tick(self) -> None:
        """Advance the rain by one frame."""
        if not self.is_raining:
            return

        for col in range(self._cols):
            # Reset brightness column
            for row in range(self._rows):
                self._bright[row][col] = False

            drop_y = self._drops[col]

            if drop_y >= 0:
                # Trail: place characters behind the drop head
                trail_len = random.randint(4, min(12, self._rows))
                for t in range(trail_len):
                    row = drop_y - t
                    if 0 <= row < self._rows:
                        if t == 0:
                            # Head character: bright white-green
                            self._grid[row][col] = random.choice(MATRIX_CHARS)
                            self._bright[row][col] = True
                        elif t < 3:
                            # Near head: keep bright
                            self._grid[row][col] = random.choice(MATRIX_CHARS)
                        else:
                            # Tail: occasionally mutate
                            if random.random() < 0.3:
                                self._grid[row][col] = random.choice(MATRIX_CHARS)

                # Fade out far trail
                fade_start = drop_y - trail_len
                if 0 <= fade_start < self._rows:
                    self._grid[fade_start][col] = " "

            # Advance drop
            self._drops[col] += 1
            if self._drops[col] > self._rows + 8:
                self._drops[col] = random.randint(-self._rows, -2)
                # Random chance to start new drop
                if random.random() > self._density:
                    self._drops[col] = random.randint(-self._rows * 2, -4)

        self._render_frame()

    def _render_frame(self) -> None:
        """Build Rich markup from the grid and update display."""
        lines: list[str] = []
        for row in range(self._rows):
            parts: list[str] = []
            for col in range(self._cols):
                ch = self._grid[row][col]
                if ch == " ":
                    parts.append(" ")
                elif self._bright[row][col]:
                    parts.append(f"[bold #FFFFFF]{ch}[/]")
                else:
                    parts.append(f"[#00FF41]{ch}[/]")
            lines.append("".join(parts))

        self.update("\n".join(lines))

    def render(self) -> str:
        """Initial render -- empty until first tick."""
        return ""

    def stop(self) -> None:
        """Stop the rain animation."""
        self.is_raining = False
        if self._timer:
            self._timer.stop()

    def start(self) -> None:
        """Resume the rain animation."""
        self.is_raining = True
        if self._timer:
            self._timer.resume()


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
        min-height: 2;
        max-height: 5;
        background: #0A0A0A;
        padding: 0 1;
        border: solid #1E1E1E;
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
        """Build the Rich markup string for the agent bar."""
        chips: list[str] = []
        for name, _, tier in AGENT_ROSTER:
            state = self._states.get(name, "idle")
            icon, color = AGENT_STATES.get(state, ("--", TEXT_DIM))
            tier_color = TIER_COLORS.get(tier, TEXT_DIM)

            if state == "idle":
                chip = f"[{TEXT_DIM}][{icon} {name[:6]}][/]"
            elif state == "error":
                chip = f"[bold {ERROR}][{icon} {name[:6]}][/]"
            else:
                chip = f"[{color}][{icon}[/] [{tier_color}]{name[:6]}[/][{color}]][/]"
            chips.append(chip)

        return " ".join(chips)

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
        height: 3;
        background: #0A0A0A;
        border-bottom: double #1E1E1E;
        padding: 0 1;
        content-align: center middle;
    }
    """

    model_name: reactive[str] = reactive("unknown")
    context_pct: reactive[int] = reactive(0)
    context_used: reactive[str] = reactive("0K")
    context_max: reactive[str] = reactive("1M")
    active_agents: reactive[int] = reactive(0)
    total_agents: reactive[int] = reactive(18)
    session_cost: reactive[str] = reactive("$0.00")
    mode: reactive[str] = reactive("ACT")
    version: reactive[str] = reactive("4.0")

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

        parts = [
            f"[bold {GOLD}]DJcode[/] [{TEXT_DIM}]v{self.version}[/]",
            f"[{TEXT_DIM}]|[/]",
            f"[{TEXT_BASE}]Model:[/] [{TEXT_STRONG}]{self.model_name}[/]",
            f"[{TEXT_DIM}]|[/]",
            f"[{TEXT_BASE}]Ctx:[/] [{ctx_color}]{ctx_bar} {self.context_pct}%[/] [{TEXT_DIM}]({self.context_used}/{self.context_max})[/]",
            f"[{TEXT_DIM}]|[/]",
            f"[{TEXT_BASE}]Agents:[/] [{agent_color}]{self.active_agents}/{self.total_agents}[/]",
            f"[{TEXT_DIM}]|[/]",
            f"[{TEXT_BASE}]Cost:[/] [{GOLD}]{self.session_cost}[/]",
            f"[{TEXT_DIM}]|[/]",
            mode_str,
        ]
        return "  ".join(parts)

    @staticmethod
    def _mini_bar(pct: int, width: int = 8) -> str:
        """Build a tiny progress bar: [####----]."""
        filled = max(0, min(width, int(pct / 100 * width)))
        return "[" + "#" * filled + "-" * (width - filled) + "]"

    def _refresh(self) -> None:
        try:
            self.query_one("#hacker-header-display", Static).update(
                self._build_header()
            )
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
        op_color = SUCCESS if self.operation == "EXECUTING" else (
            THINKING if self.operation in ("THINKING", "GENERATING") else (
                GOLD if self.operation == "RESEARCHING" else TEXT_BASE
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
            self.query_one("#progress-hud-display", Static).update(
                self._build_display()
            )
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
# 5. TokenBurnRate -- Real-time ASCII sparkline of token consumption
# ---------------------------------------------------------------------------

class TokenBurnRate(Widget):
    """ASCII sparkline showing token consumption rate over time.

    burn: |..........########....| 142 tok/s
    """

    DEFAULT_CSS = """
    TokenBurnRate {
        height: 1;
        background: #0A0A0A;
        color: #00FF41;
        padding: 0 1;
    }
    """

    current_rate: reactive[float] = reactive(0.0)

    def __init__(self, history_len: int = 30, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._history: list[float] = [0.0] * history_len
        self._max_len = history_len

    def compose(self) -> ComposeResult:
        yield Static(self._build_sparkline(), id="burn-rate-display")

    def _build_sparkline(self) -> str:
        """Build an ASCII sparkline from history."""
        max_val = max(self._history) if self._history else 1.0
        if max_val == 0:
            max_val = 1.0

        bars: list[str] = []
        for val in self._history:
            idx = int((val / max_val) * (len(SPARK_BLOCKS) - 1))
            idx = max(0, min(len(SPARK_BLOCKS) - 1, idx))
            bars.append(SPARK_BLOCKS[idx])

        sparkline = "".join(bars)
        rate_str = f"{self.current_rate:.0f}" if self.current_rate > 0 else "0"

        return f"  [{SUCCESS}]{sparkline}[/] [{TEXT_BASE}]{rate_str} tok/s[/]"

    def push_rate(self, rate: float) -> None:
        """Add a new rate sample and refresh."""
        self._history.append(rate)
        if len(self._history) > self._max_len:
            self._history = self._history[-self._max_len:]
        self.current_rate = rate
        try:
            self.query_one("#burn-rate-display", Static).update(
                self._build_sparkline()
            )
        except Exception:
            pass

    def reset(self) -> None:
        """Clear the history."""
        self._history = [0.0] * self._max_len
        self.current_rate = 0.0
        try:
            self.query_one("#burn-rate-display", Static).update(
                self._build_sparkline()
            )
        except Exception:
            pass


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
# 7. ThreatPanel -- Blocking agent alerts
# ---------------------------------------------------------------------------

class ThreatAlert:
    """A single threat/alert entry."""

    def __init__(
        self,
        agent: str,
        severity: str,
        message: str,
        timestamp: float | None = None,
    ) -> None:
        self.agent = agent
        self.severity = severity  # "critical", "warning", "info"
        self.message = message
        self.timestamp = timestamp or time.time()


class ThreatPanel(Widget):
    """Shows blocking agent alerts from sentinel agents.

    Monitors: Kavach (Shield), Varuna (Watcher), Mitra (Ally), Indra (Thunder)
    """

    DEFAULT_CSS = """
    ThreatPanel {
        width: 100%;
        height: 100%;
        background: #0A0A0A;
        padding: 1;
    }
    """

    alert_count: reactive[int] = reactive(0)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._alerts: list[ThreatAlert] = []
        self._sentinel_agents = {
            "Kavach": THREAT_KAVACH,
            "Varuna": THREAT_VARUNA,
            "Mitra": THREAT_MITRA,
            "Indra": THREAT_INDRA,
        }

    def compose(self) -> ComposeResult:
        yield Static(
            f"  [{ERROR}]![/] [{GOLD}]THREAT MONITOR[/] [{TEXT_DIM}]-- Sentinel Agents[/]",
            classes="hacker-section",
        )
        yield Rule()
        yield Static(self._build_sentinel_status(), id="sentinel-status")
        yield Rule()
        yield Static(
            f"  [{TEXT_DIM}]ALERTS[/] [{TEXT_BASE}](0)[/]",
            id="threat-count",
        )
        yield ScrollableContainer(id="threat-alerts-scroll")

    def _build_sentinel_status(self) -> str:
        """Show sentinel agent status line."""
        parts: list[str] = []
        for name, color in self._sentinel_agents.items():
            parts.append(f"[{color}]{name}[/]")
        return "  Sentinels: " + f" [{TEXT_DIM}]|[/] ".join(parts)

    def add_alert(self, agent: str, severity: str, message: str) -> None:
        """Add a new threat alert."""
        alert = ThreatAlert(agent, severity, message)
        self._alerts.insert(0, alert)
        if len(self._alerts) > 50:
            self._alerts = self._alerts[:50]
        self.alert_count = len(self._alerts)
        self._refresh()

    def clear_alerts(self) -> None:
        """Clear all alerts."""
        self._alerts.clear()
        self.alert_count = 0
        self._refresh()

    def _refresh(self) -> None:
        try:
            # Update count
            self.query_one("#threat-count", Static).update(
                f"  [{TEXT_DIM}]ALERTS[/] [{TEXT_BASE}]({self.alert_count})[/]"
            )

            # Rebuild alerts list
            container = self.query_one("#threat-alerts-scroll", ScrollableContainer)
            container.remove_children()

            if not self._alerts:
                container.mount(
                    Static(f"  [{TEXT_DIM}]No active threats. All clear.[/]")
                )
                return

            for alert in self._alerts[:20]:
                sev_color = {
                    "critical": ERROR,
                    "warning": WARNING,
                    "info": INFO,
                }.get(alert.severity, TEXT_BASE)

                agent_color = self._sentinel_agents.get(alert.agent, TEXT_BASE)
                sev_icon = {
                    "critical": "!!!",
                    "warning": "! !",
                    "info": " i ",
                }.get(alert.severity, " ? ")

                markup = (
                    f"  [{sev_color}][{sev_icon}][/]"
                    f" [{agent_color}]{alert.agent}[/]"
                    f" [{TEXT_BASE}]{alert.message}[/]"
                )
                container.mount(Static(markup))

        except Exception:
            pass


# ---------------------------------------------------------------------------
# 8. ArmyView -- Bird's eye grid of all 18 agents
# ---------------------------------------------------------------------------

class ArmyView(Widget):
    """Grid view showing all 18 agents in a compact visual layout.

    Each cell shows: name, state icon, and task snippet.
    3 columns x 6 rows grid.
    """

    DEFAULT_CSS = """
    ArmyView {
        width: 100%;
        height: 100%;
        background: #0A0A0A;
        padding: 1;
        overflow-y: auto;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._agent_states: dict[str, str] = {}
        self._agent_tasks: dict[str, str] = {}
        for name, _, _ in AGENT_ROSTER:
            self._agent_states[name] = "idle"
            self._agent_tasks[name] = ""

    def compose(self) -> ComposeResult:
        yield Static(
            f"  [{GOLD}]ARMY OVERVIEW[/] [{TEXT_DIM}]-- {len(AGENT_ROSTER)} operatives[/]",
            classes="hacker-section",
        )
        yield Rule()
        yield Static(self._build_grid(), id="army-grid-display")

    def _build_grid(self) -> str:
        """Build a text-based grid of all agents."""
        lines: list[str] = []

        # 3 columns layout
        cols = 3
        rows_needed = (len(AGENT_ROSTER) + cols - 1) // cols

        for row_idx in range(rows_needed):
            row_parts: list[str] = []
            for col_idx in range(cols):
                agent_idx = row_idx * cols + col_idx
                if agent_idx < len(AGENT_ROSTER):
                    name, title, tier = AGENT_ROSTER[agent_idx]
                    state = self._agent_states.get(name, "idle")
                    icon, color = AGENT_STATES.get(state, ("--", TEXT_DIM))
                    tier_color = TIER_COLORS.get(tier, TEXT_DIM)
                    task = self._agent_tasks.get(name, "")
                    task_snippet = task[:12] + ".." if len(task) > 14 else task

                    if state == "idle":
                        cell = f"[{TEXT_DIM}]{icon} {name:<12}[/]"
                    else:
                        cell = f"[{color}]{icon}[/] [{tier_color}]{name:<12}[/]"

                    if task_snippet:
                        cell += f" [{TEXT_DIM}]{task_snippet}[/]"
                    else:
                        cell += f" [{TEXT_DIM}]{'.' * 12}[/]"

                    row_parts.append(cell)
                else:
                    row_parts.append(" " * 28)

            lines.append("  " + f"  [{HUD_BORDER}]|[/]  ".join(row_parts))

            # Separator line between rows (except last)
            if row_idx < rows_needed - 1:
                lines.append(f"  [{HUD_BORDER}]{'- ' * 42}[/]")

        return "\n".join(lines)

    def set_agent_state(self, name: str, state: str, task: str = "") -> None:
        """Update an agent's state and optional task."""
        if name in self._agent_states:
            self._agent_states[name] = state
            if task:
                self._agent_tasks[name] = task
            self._refresh()

    def set_all_idle(self) -> None:
        """Reset all agents to idle."""
        for name in self._agent_states:
            self._agent_states[name] = "idle"
            self._agent_tasks[name] = ""
        self._refresh()

    def _refresh(self) -> None:
        try:
            self.query_one("#army-grid-display", Static).update(self._build_grid())
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 9. AgentDashboard -- Full-screen agent monitoring panel
# ---------------------------------------------------------------------------

class AgentCard(Vertical):
    """Individual agent card with full status display."""

    DEFAULT_CSS = """
    AgentCard {
        height: auto;
        min-height: 5;
        background: #0E0E0E;
        border: solid #1E1E1E;
        padding: 1;
        margin: 0 0 1 0;
    }

    AgentCard:hover {
        border: solid #00FF41;
    }

    AgentCard .card-name {
        color: #FFD700;
        text-style: bold;
    }

    AgentCard .card-title {
        color: #555555;
        text-style: italic;
    }

    AgentCard .card-stat {
        color: #8A8A8A;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        agent_name: str,
        dharmic_title: str,
        tier: int,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._name = agent_name
        self._title = dharmic_title
        self._tier = tier
        self._state = "idle"
        self._task = ""
        self._tokens = 0
        self._tools: list[str] = []
        self._confidence = 0.0

    def compose(self) -> ComposeResult:
        tier_color = TIER_COLORS.get(self._tier, TEXT_DIM)
        tier_label = {4: "T4:CTRL", 3: "T3:ENTR", 2: "T2:ARCH", 1: "T1:EXEC"}.get(
            self._tier, "T?"
        )

        yield Static(
            f"[{tier_color}]{tier_label}[/] [bold {GOLD}]{self._name}[/]",
            classes="card-name",
        )
        yield Static(f"[{TEXT_DIM}]{self._title}[/]", classes="card-title")
        yield Static(self._build_stats(), id=f"card-stats-{self._name}", classes="card-stat")

    def _build_stats(self) -> str:
        icon, color = AGENT_STATES.get(self._state, ("--", TEXT_DIM))
        task_str = self._task[:20] if self._task else "awaiting orders"
        tok_str = self._fmt_tokens(self._tokens)
        conf_str = f"{self._confidence:.0%}" if self._confidence > 0 else "--"

        return (
            f"  [{color}]{icon} {self._state.upper():<10}[/]"
            f" [{TEXT_DIM}]|[/] [{TEXT_BASE}]{task_str}[/]\n"
            f"  [{TEXT_DIM}]tok:[/] {tok_str}"
            f" [{TEXT_DIM}]|[/] [{TEXT_DIM}]conf:[/] {conf_str}"
        )

    def update_state(
        self,
        state: str = "",
        task: str = "",
        tokens: int = 0,
        confidence: float = 0.0,
    ) -> None:
        if state:
            self._state = state
        if task:
            self._task = task
        if tokens:
            self._tokens = tokens
        if confidence:
            self._confidence = confidence
        try:
            self.query_one(f"#card-stats-{self._name}", Static).update(
                self._build_stats()
            )
        except Exception:
            pass

    @staticmethod
    def _fmt_tokens(count: int) -> str:
        if count >= 1_000_000:
            return f"{count / 1_000_000:.1f}M"
        if count >= 1_000:
            return f"{count / 1_000:.1f}K"
        return str(count)


class AgentDashboard(Widget):
    """Full agent monitoring panel with cards for all 18 agents.

    Shows detailed status for each operative in a scrollable list
    with per-agent: state, task, tokens consumed, confidence score.
    """

    DEFAULT_CSS = """
    AgentDashboard {
        width: 100%;
        height: 100%;
        background: #0A0A0A;
        padding: 1;
        overflow-y: auto;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._cards: dict[str, AgentCard] = {}

    def compose(self) -> ComposeResult:
        yield Static(
            f"  [{GOLD}]AGENT COMMAND CENTER[/]"
            f" [{TEXT_DIM}]-- {len(AGENT_ROSTER)} PhD operatives[/]",
            classes="hacker-section",
        )
        yield Static(self._build_summary(), id="dashboard-summary")
        yield Rule()
        with ScrollableContainer(id="dashboard-cards"):
            for name, title, tier in AGENT_ROSTER:
                card = AgentCard(name, title, tier, id=f"agent-card-{name}")
                self._cards[name] = card
                yield card

    def _build_summary(self) -> str:
        """Build the top-line summary."""
        active = sum(
            1 for c in self._cards.values() if c._state not in ("idle", "ready")
        )
        return (
            f"  [{SUCCESS}]{active} ACTIVE[/]"
            f" [{TEXT_DIM}]|[/]"
            f" [{TEXT_BASE}]{len(AGENT_ROSTER) - active} standby[/]"
            f" [{TEXT_DIM}]|[/]"
            f" [{TEXT_DIM}]All systems nominal[/]"
        )

    def update_agent(
        self,
        name: str,
        state: str = "",
        task: str = "",
        tokens: int = 0,
        confidence: float = 0.0,
    ) -> None:
        """Update a specific agent's card."""
        if name in self._cards:
            self._cards[name].update_state(state, task, tokens, confidence)
            try:
                self.query_one("#dashboard-summary", Static).update(
                    self._build_summary()
                )
            except Exception:
                pass

    def reset_all(self) -> None:
        """Reset all agents to idle."""
        for card in self._cards.values():
            card.update_state(state="idle", task="")
        try:
            self.query_one("#dashboard-summary", Static).update(
                self._build_summary()
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "AGENT_ROSTER",
    "AGENT_STATES",
    "TIER_COLORS",
    "MatrixRain",
    "AgentStatusBar",
    "HackerHeader",
    "ProgressHUD",
    "TokenBurnRate",
    "ContextBar",
    "ThreatPanel",
    "ThreatAlert",
    "ArmyView",
    "AgentCard",
    "AgentDashboard",
]
