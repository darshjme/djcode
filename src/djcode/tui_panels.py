"""Textual TUI side-panel widgets for DJcode v4.0 -- Hacker Command Center.

Provides a tabbed side panel with eight views:
  - ProjectPanel  -- directory tree file browser
  - AgentPanel    -- live agent status dashboard with RA integration
  - StatsPanel    -- session statistics
  - MCPPanel      -- MCP extension status
  - TodoPanel     -- per-session todo list
  - CostPanel     -- token usage with cost estimates
  - ArmyTabPanel  -- bird's eye view of all 18 agents
  - IntelPanel    -- context utilization + threat monitor
  - SidePanel     -- tabbed container that hosts them all

Uses Textual 8.x API with reactive() properties and message passing.
Cyberpunk hacker aesthetic: neon green, electric gold, deep black.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal, ScrollableContainer
from textual.message import Message
from textual.reactive import reactive
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import (
    DirectoryTree,
    Label,
    Static,
    TabbedContent,
    TabPane,
    ProgressBar,
    Rule,
    Switch,
    Tree,
)

from djcode.tui_theme import (
    GOLD,
    BG_PRIMARY,
    BG_PANEL,
    BG_HEADER,
    BG_INPUT,
    BORDER,
    BORDER_SUBTLE,
    TEXT_STRONG,
    TEXT_BASE,
    TEXT_DIM,
    SUCCESS,
    ERROR,
    WARNING,
    INFO,
    THINKING,
    TIER_4_CONTROL,
    TIER_3_ENTERPRISE,
    TIER_2_ARCHITECTURE,
    TIER_1_EXECUTION,
    MATRIX_GREEN,
    HUD_BORDER,
)


# ---------------------------------------------------------------------------
# Theme constants (shorthand for inline Rich markup)
# ---------------------------------------------------------------------------

BG = BG_PRIMARY
DIM = TEXT_DIM
TEXT = TEXT_BASE
TEXT_BRIGHT = TEXT_STRONG

# File extensions -> type icons
FILE_ICONS: dict[str, str] = {
    ".py": "PY",
    ".rs": "RS",
    ".ts": "TS",
    ".tsx": "TX",
    ".js": "JS",
    ".jsx": "JX",
    ".json": "{}",
    ".toml": "TM",
    ".yaml": "YM",
    ".yml": "YM",
    ".md": "MD",
    ".txt": "TX",
    ".html": "HT",
    ".css": "CS",
    ".sh": "SH",
    ".dockerfile": "DK",
    ".sql": "SQ",
    ".lock": "LK",
    ".env": "EV",
    ".gif": "IM",
    ".png": "IM",
    ".jpg": "IM",
    ".svg": "SV",
}

# Directories to hide in the file tree
HIDDEN_DIRS: set[str] = {
    "node_modules",
    ".git",
    "__pycache__",
    ".next",
    "dist",
    ".venv",
    "venv",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".tox",
    "target",
    ".eggs",
    "*.egg-info",
    ".DS_Store",
}


# ---------------------------------------------------------------------------
# Messages (for inter-panel communication)
# ---------------------------------------------------------------------------

class FileSelected(Message):
    """Posted when the user selects a file in the directory tree."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path


class AgentUpdated(Message):
    """Posted when the active agent or tool state changes."""

    def __init__(
        self,
        agent_name: str = "",
        agent_role: str = "",
        tool_name: str = "",
        tool_status: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        super().__init__()
        self.agent_name = agent_name
        self.agent_role = agent_role
        self.tool_name = tool_name
        self.tool_status = tool_status
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out


# ---------------------------------------------------------------------------
# 1. ProjectPanel -- Directory tree file browser (hacker themed)
# ---------------------------------------------------------------------------

class FilteredDirectoryTree(DirectoryTree):
    """DirectoryTree that filters out noise directories and adds file icons."""

    def filter_paths(self, paths: list[Path]) -> list[Path]:
        """Hide common non-essential directories and files."""
        filtered: list[Path] = []
        for p in paths:
            name = p.name
            if p.is_dir() and name in HIDDEN_DIRS:
                continue
            if name == ".DS_Store":
                continue
            filtered.append(p)
        return filtered

    def render_label(self, node: Tree.NodeData, base_style: str, style: str) -> str:  # type: ignore[override]
        return super().render_label(node, base_style, style)


class ProjectPanel(Vertical):
    """File browser + project info panel -- hacker command center style."""

    DEFAULT_CSS = """
    ProjectPanel {
        width: 100%;
        height: 1fr;
        background: #0A0A0A;
    }

    ProjectPanel .project-header {
        height: 3;
        background: #0A0A0A;
        color: #FFD700;
        text-align: center;
        padding: 1;
        text-style: bold;
        border-bottom: double #1E1E1E;
    }

    ProjectPanel .project-path {
        height: 1;
        background: #0A0A0A;
        color: #555555;
        padding: 0 1;
        text-style: italic;
    }

    ProjectPanel FilteredDirectoryTree {
        height: 1fr;
        background: #0A0A0A;
        color: #8A8A8A;
        scrollbar-color: #1E1E1E;
        scrollbar-color-hover: #00FF41;
        scrollbar-color-active: #FFD700;
        padding: 0 1;
    }

    ProjectPanel FilteredDirectoryTree:focus {
        border: tall #00FF41 30%;
    }

    ProjectPanel FilteredDirectoryTree:focus .tree--cursor {
        background: #00FF41 15%;
        color: #00FF41;
    }

    ProjectPanel FilteredDirectoryTree .tree--guides {
        color: #1E1E1E;
    }

    ProjectPanel FilteredDirectoryTree .directory-tree--folder {
        color: #FFD700;
        text-style: bold;
    }

    ProjectPanel FilteredDirectoryTree .directory-tree--file {
        color: #8A8A8A;
    }

    ProjectPanel .file-info {
        height: 2;
        background: #0E0E0E;
        color: #555555;
        padding: 0 1;
        border-top: solid #1E1E1E;
    }
    """

    project_name: reactive[str] = reactive("Project")
    project_path: reactive[str] = reactive("")

    def __init__(self, path: str | Path | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._root_path = Path(path) if path else Path.cwd()
        self.project_path = str(self._root_path)
        self.project_name = self._root_path.name or "Project"

    def compose(self) -> ComposeResult:
        yield Label(f"  [{GOLD}]FILES[/] // {self.project_name}", classes="project-header")
        yield Label(self._truncate_path(str(self._root_path)), classes="project-path")
        yield FilteredDirectoryTree(str(self._root_path))
        yield Label("", classes="file-info", id="file-info-label")

    @on(DirectoryTree.FileSelected)
    def handle_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        path = event.path
        self.post_message(FileSelected(path))
        info_label = self.query_one("#file-info-label", Label)
        try:
            size = path.stat().st_size
            ext = path.suffix.lower()
            icon = FILE_ICONS.get(ext, ">>")
            info_label.update(f" [{SUCCESS}]{icon}[/] [{TEXT}]{path.name}[/]  [{DIM}]|[/]  [{TEXT}]{self._format_size(size)}[/]")
        except OSError:
            info_label.update(f" [{TEXT}]{path.name}[/]")

    def set_path(self, new_path: str | Path) -> None:
        new_path = Path(new_path)
        if new_path.is_dir():
            self._root_path = new_path
            self.project_path = str(new_path)
            self.project_name = new_path.name or "Project"
            tree = self.query_one(FilteredDirectoryTree)
            tree.path = str(new_path)
            tree.reload()
            header = self.query("Label.project-header")
            if header:
                header.first().update(f"  [{GOLD}]FILES[/] // {self.project_name}")
            path_label = self.query("Label.project-path")
            if path_label:
                path_label.first().update(self._truncate_path(str(new_path)))

    @staticmethod
    def _format_size(size: int) -> str:
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        if size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MB"
        return f"{size / (1024 * 1024 * 1024):.1f} GB"

    @staticmethod
    def _truncate_path(path: str, max_len: int = 40) -> str:
        if len(path) <= max_len:
            return path
        return "..." + path[-(max_len - 3):]


# ---------------------------------------------------------------------------
# 2. AgentPanel -- Live agent status with RA integration (hacker themed)
# ---------------------------------------------------------------------------

class ToolHistoryItem(Static):
    """A single tool call entry in the history."""

    DEFAULT_CSS = """
    ToolHistoryItem {
        height: 1;
        padding: 0 1;
        color: #8A8A8A;
    }
    """

    def __init__(self, tool_name: str, status: str = "ok", **kwargs: Any) -> None:
        icon = ">>" if status == "ok" else "XX" if status == "error" else ".."
        color = SUCCESS if status == "ok" else ERROR if status == "error" else GOLD
        markup = f"[{color}]{icon}[/] [{TEXT}]{tool_name}[/]"
        super().__init__(markup, **kwargs)


class AgentPanel(Vertical):
    """Live agent dashboard with RA status integration.

    Shows: active agent, tier, RA status, tools, tokens, memory.
    """

    DEFAULT_CSS = """
    AgentPanel {
        width: 100%;
        height: 100%;
        background: #0A0A0A;
        padding: 1;
    }

    AgentPanel .agent-header {
        height: 3;
        background: #0A0A0A;
        color: #FFD700;
        text-align: center;
        padding: 1;
        text-style: bold;
        border-bottom: double #1E1E1E;
    }

    AgentPanel .agent-section-title {
        color: #00FF41;
        text-style: bold;
        padding: 1 0 0 0;
    }

    AgentPanel .agent-stat {
        color: #8A8A8A;
        padding: 0 1;
    }

    AgentPanel .agent-stat-value {
        color: #E8E8E8;
        text-style: bold;
    }

    AgentPanel .tool-history-container {
        height: auto;
        max-height: 14;
        background: #0A0A0A;
        padding: 0;
    }

    AgentPanel .memory-stats {
        color: #555555;
        padding: 1 1;
    }
    """

    active_agent: reactive[str] = reactive("Operator")
    active_role: reactive[str] = reactive("General")
    active_tier: reactive[int] = reactive(1)
    ra_status: reactive[str] = reactive("standalone")
    tokens_in: reactive[int] = reactive(0)
    tokens_out: reactive[int] = reactive(0)
    session_start: reactive[float] = reactive(0.0)
    tool_count: reactive[int] = reactive(0)
    memory_session: reactive[int] = reactive(0)
    memory_facts: reactive[int] = reactive(0)
    memory_vectors: reactive[int] = reactive(0)
    confidence: reactive[float] = reactive(0.0)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._tool_history: list[tuple[str, str]] = []
        self._timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield Label(f"  [{GOLD}]AGENT[/] // OPERATIVE STATUS", classes="agent-header")
        yield Rule()

        # Active agent with tier badge
        yield Label("ACTIVE OPERATIVE", classes="agent-section-title")
        yield Static(
            self._build_agent_display(),
            id="agent-active-display",
        )
        yield Rule()

        # RA (Retrieval Augmented) Status
        yield Label("RA STATUS", classes="agent-section-title")
        yield Static(
            f"  [{DIM}]Mode:[/] [{TEXT}]standalone[/]  [{DIM}]Confidence:[/] [{TEXT}]--[/]",
            id="agent-ra-display",
            classes="agent-stat",
        )
        yield Rule()

        # Token counter
        yield Label("TOKENS", classes="agent-section-title")
        yield Static("  [{SUCCESS}]>> 0[/]  [{ERROR}]<< 0[/]", id="agent-token-display", classes="agent-stat")
        yield Rule()

        # Session timer
        yield Label("SESSION", classes="agent-section-title")
        yield Static("  0m 0s", id="agent-timer-display", classes="agent-stat")
        yield Rule()

        # Tool history
        yield Label("TOOL HISTORY", classes="agent-section-title")
        yield ScrollableContainer(id="tool-history-scroll", classes="tool-history-container")
        yield Rule()

        # Memory stats
        yield Label("MEMORY", classes="agent-section-title")
        yield Static(
            f"  [{DIM}]3-tier:[/] [{TEXT}]0 session / 0 facts / 0 vectors[/]",
            id="agent-memory-display",
            classes="memory-stats",
        )

    def _build_agent_display(self) -> str:
        """Build agent display with tier badge."""
        from djcode.tui_theme import (
            TIER_4_CONTROL, TIER_3_ENTERPRISE,
            TIER_2_ARCHITECTURE, TIER_1_EXECUTION,
        )
        tier_colors = {
            4: TIER_4_CONTROL, 3: TIER_3_ENTERPRISE,
            2: TIER_2_ARCHITECTURE, 1: TIER_1_EXECUTION,
        }
        tier_labels = {4: "T4:CTRL", 3: "T3:ENTR", 2: "T2:ARCH", 1: "T1:EXEC"}
        tier_color = tier_colors.get(self.active_tier, TEXT_DIM)
        tier_label = tier_labels.get(self.active_tier, "T?")

        return (
            f"  [{tier_color}][{tier_label}][/]"
            f" [bold {GOLD}]{self.active_agent}[/]"
            f" [{TEXT}]({self.active_role})[/]"
        )

    def on_mount(self) -> None:
        if self.session_start == 0.0:
            self.session_start = time.time()
        self._timer = self.set_interval(1.0, self._update_timer)

    def _update_timer(self) -> None:
        elapsed = time.time() - self.session_start if self.session_start > 0 else 0
        minutes = int(elapsed // 60)
        seconds = int(elapsed % 60)
        hours = int(minutes // 60)
        minutes = minutes % 60

        if hours > 0:
            time_str = f"  {hours}h {minutes}m {seconds}s"
        else:
            time_str = f"  {minutes}m {seconds}s"

        try:
            self.query_one("#agent-timer-display", Static).update(time_str)
        except Exception:
            pass

    def watch_active_agent(self, value: str) -> None:
        try:
            self.query_one("#agent-active-display", Static).update(
                self._build_agent_display()
            )
        except Exception:
            pass

    def watch_active_tier(self, value: int) -> None:
        try:
            self.query_one("#agent-active-display", Static).update(
                self._build_agent_display()
            )
        except Exception:
            pass

    def watch_ra_status(self, value: str) -> None:
        self._refresh_ra_display()

    def watch_confidence(self, value: float) -> None:
        self._refresh_ra_display()

    def _refresh_ra_display(self) -> None:
        try:
            ra_color = SUCCESS if self.ra_status == "augmented" else (
                THINKING if self.ra_status == "retrieving" else TEXT_DIM
            )
            conf_str = f"{self.confidence:.0%}" if self.confidence > 0 else "--"
            conf_color = SUCCESS if self.confidence > 0.8 else (
                WARNING if self.confidence > 0.5 else ERROR if self.confidence > 0 else TEXT_DIM
            )
            self.query_one("#agent-ra-display", Static).update(
                f"  [{DIM}]Mode:[/] [{ra_color}]{self.ra_status}[/]"
                f"  [{DIM}]Confidence:[/] [{conf_color}]{conf_str}[/]"
            )
        except Exception:
            pass

    def watch_tokens_in(self, value: int) -> None:
        self._refresh_token_display()

    def watch_tokens_out(self, value: int) -> None:
        self._refresh_token_display()

    def _refresh_token_display(self) -> None:
        try:
            display = self.query_one("#agent-token-display", Static)
            display.update(
                f"  [{SUCCESS}]>> {self._fmt_tokens(self.tokens_in)}[/]"
                f"  [{ERROR}]<< {self._fmt_tokens(self.tokens_out)}[/]"
            )
        except Exception:
            pass

    def watch_memory_session(self, value: int) -> None:
        self._refresh_memory_display()

    def watch_memory_facts(self, value: int) -> None:
        self._refresh_memory_display()

    def watch_memory_vectors(self, value: int) -> None:
        self._refresh_memory_display()

    def _refresh_memory_display(self) -> None:
        try:
            display = self.query_one("#agent-memory-display", Static)
            display.update(
                f"  [{DIM}]3-tier:[/] [{TEXT}]{self.memory_session} session /"
                f" {self.memory_facts} facts / {self.memory_vectors} vectors[/]"
            )
        except Exception:
            pass

    def add_tool_call(self, tool_name: str, status: str = "ok") -> None:
        """Record a tool call in the history (keeps last 10)."""
        self._tool_history.append((tool_name, status))
        if len(self._tool_history) > 10:
            self._tool_history = self._tool_history[-10:]
        self.tool_count += 1

        try:
            container = self.query_one("#tool-history-scroll", ScrollableContainer)
            container.remove_children()
            for name, st in self._tool_history:
                container.mount(ToolHistoryItem(name, st))
            container.scroll_end(animate=False)
        except Exception:
            pass

    def set_agent(self, name: str, role: str = "General", tier: int = 1) -> None:
        """Update the active agent display."""
        self.active_agent = name
        self.active_role = role
        self.active_tier = tier

    def update_tokens(self, tokens_in: int, tokens_out: int) -> None:
        """Update token counters (absolute values, not deltas)."""
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out

    def update_memory(self, session: int = 0, facts: int = 0, vectors: int = 0) -> None:
        self.memory_session = session
        self.memory_facts = facts
        self.memory_vectors = vectors

    def update_ra(self, status: str = "standalone", confidence: float = 0.0) -> None:
        """Update RA (retrieval augmented) status."""
        self.ra_status = status
        self.confidence = confidence

    @staticmethod
    def _fmt_tokens(count: int) -> str:
        if count >= 1_000_000:
            return f"{count / 1_000_000:.1f}M"
        if count >= 1_000:
            return f"{count / 1_000:.1f}K"
        return str(count)


# ---------------------------------------------------------------------------
# 3. StatsPanel -- Session statistics with live updates (hacker themed)
# ---------------------------------------------------------------------------

class StatsPanel(Vertical):
    """Session stats with live updates -- cyberpunk HUD style."""

    DEFAULT_CSS = """
    StatsPanel {
        width: 100%;
        height: 100%;
        background: #0A0A0A;
        padding: 1;
    }

    StatsPanel .stats-header {
        height: 3;
        background: #0A0A0A;
        color: #FFD700;
        text-align: center;
        padding: 1;
        text-style: bold;
        border-bottom: double #1E1E1E;
    }

    StatsPanel .stats-section-title {
        color: #00FF41;
        text-style: bold;
        padding: 1 0 0 0;
    }

    StatsPanel .stats-row {
        color: #8A8A8A;
        padding: 0 1;
    }

    StatsPanel .stats-bar-container {
        height: 1;
        padding: 0 1;
    }

    StatsPanel .stats-bar-in {
        color: #00FF41;
    }

    StatsPanel .stats-bar-out {
        color: #FF1744;
    }

    StatsPanel .files-list {
        height: auto;
        max-height: 10;
        background: #0A0A0A;
        padding: 0 1;
    }
    """

    tokens_in: reactive[int] = reactive(0)
    tokens_out: reactive[int] = reactive(0)
    response_time_ms: reactive[float] = reactive(0.0)
    tools_used: reactive[int] = reactive(0)
    current_model: reactive[str] = reactive("unknown")
    current_provider: reactive[str] = reactive("unknown")

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._modified_files: list[str] = []

    def compose(self) -> ComposeResult:
        yield Label(f"  [{GOLD}]STATS[/] // SESSION TELEMETRY", classes="stats-header")
        yield Rule()

        yield Label("TOKEN USAGE", classes="stats-section-title")
        yield Static(f"  [{SUCCESS}]>>[/] In:  0", id="stats-tokens-in", classes="stats-row")
        yield Static(
            f"  [{SUCCESS}]{'#' * 0}{'-' * 30}[/]",
            id="stats-bar-in",
            classes="stats-bar-container",
        )
        yield Static(f"  [{ERROR}]<<[/] Out: 0", id="stats-tokens-out", classes="stats-row")
        yield Static(
            f"  [{ERROR}]{'#' * 0}{'-' * 30}[/]",
            id="stats-bar-out",
            classes="stats-bar-container",
        )
        yield Rule()

        yield Label("PERFORMANCE", classes="stats-section-title")
        yield Static("  Response: --", id="stats-response-time", classes="stats-row")
        yield Static("  Tools used: 0", id="stats-tools-count", classes="stats-row")
        yield Rule()

        yield Label("MODEL", classes="stats-section-title")
        yield Static("  --", id="stats-model-display", classes="stats-row")
        yield Rule()

        yield Label("MODIFIED FILES", classes="stats-section-title")
        yield ScrollableContainer(id="stats-files-scroll", classes="files-list")

    def watch_tokens_in(self, value: int) -> None:
        try:
            self.query_one("#stats-tokens-in", Static).update(
                f"  [{SUCCESS}]>>[/] In:  [{TEXT_BRIGHT}]{self._fmt(value)}[/]"
            )
            self._update_bar("#stats-bar-in", value, SUCCESS)
        except Exception:
            pass

    def watch_tokens_out(self, value: int) -> None:
        try:
            self.query_one("#stats-tokens-out", Static).update(
                f"  [{ERROR}]<<[/] Out: [{TEXT_BRIGHT}]{self._fmt(value)}[/]"
            )
            self._update_bar("#stats-bar-out", value, ERROR)
        except Exception:
            pass

    def _update_bar(self, bar_id: str, value: int, color: str) -> None:
        max_val = max(self.tokens_in, self.tokens_out, 1)
        filled = int((value / max_val) * 30) if max_val > 0 else 0
        empty = 30 - filled
        try:
            bar = self.query_one(bar_id, Static)
            bar.update(f"  [{color}]{'#' * filled}{'-' * empty}[/]")
        except Exception:
            pass

    def watch_response_time_ms(self, value: float) -> None:
        try:
            if value < 1000:
                display = f"{value:.0f}ms"
            else:
                display = f"{value / 1000:.1f}s"
            self.query_one("#stats-response-time", Static).update(
                f"  Response: [{TEXT_BRIGHT}]{display}[/]"
            )
        except Exception:
            pass

    def watch_tools_used(self, value: int) -> None:
        try:
            self.query_one("#stats-tools-count", Static).update(
                f"  Tools used: [{TEXT_BRIGHT}]{value}[/]"
            )
        except Exception:
            pass

    def watch_current_model(self, value: str) -> None:
        self._refresh_model_display()

    def watch_current_provider(self, value: str) -> None:
        self._refresh_model_display()

    def _refresh_model_display(self) -> None:
        try:
            self.query_one("#stats-model-display", Static).update(
                f"  [{TEXT_BRIGHT}]{self.current_model}[/] via [{GOLD}]{self.current_provider}[/]"
            )
        except Exception:
            pass

    def add_modified_file(self, file_path: str) -> None:
        name = Path(file_path).name
        if name not in self._modified_files:
            self._modified_files.append(name)
            try:
                container = self.query_one("#stats-files-scroll", ScrollableContainer)
                container.mount(
                    Static(f"  [{SUCCESS}]>>[/] [{TEXT}]{name}[/]")
                )
            except Exception:
                pass

    def update_stats(
        self,
        *,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        response_time_ms: float | None = None,
        tools_used: int | None = None,
        model: str | None = None,
        provider: str | None = None,
    ) -> None:
        if tokens_in is not None:
            self.tokens_in = tokens_in
        if tokens_out is not None:
            self.tokens_out = tokens_out
        if response_time_ms is not None:
            self.response_time_ms = response_time_ms
        if tools_used is not None:
            self.tools_used = tools_used
        if model is not None:
            self.current_model = model
        if provider is not None:
            self.current_provider = provider

    @staticmethod
    def _fmt(count: int) -> str:
        if count >= 1_000_000:
            return f"{count / 1_000_000:.1f}M"
        if count >= 1_000:
            return f"{count / 1_000:.1f}K"
        return str(count)


# ---------------------------------------------------------------------------
# 4. MCPPanel -- MCP extension status and management (hacker themed)
# ---------------------------------------------------------------------------

class ExtensionRow(Horizontal):
    """A single extension entry with status dot and toggle."""

    DEFAULT_CSS = """
    ExtensionRow {
        height: 3;
        padding: 0 1;
        background: #0A0A0A;
    }

    ExtensionRow .ext-status-dot {
        width: 3;
        color: #FF1744;
        padding: 0;
    }

    ExtensionRow .ext-status-dot.connected {
        color: #00FF41;
    }

    ExtensionRow .ext-name {
        width: 1fr;
        color: #8A8A8A;
        padding: 0 1;
    }

    ExtensionRow .ext-tools-count {
        width: 6;
        color: #555555;
        text-align: right;
    }

    ExtensionRow Switch {
        width: 8;
        background: #0A0A0A;
    }
    """

    def __init__(
        self,
        ext_name: str,
        connected: bool = False,
        enabled: bool = True,
        tools_count: int = 0,
        description: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._ext_name = ext_name
        self._connected = connected
        self._enabled = enabled
        self._tools_count = tools_count
        self._description = description

    def compose(self) -> ComposeResult:
        dot_class = "ext-status-dot connected" if self._connected else "ext-status-dot"
        dot_char = ">>" if self._connected else "XX"
        yield Static(dot_char, classes=dot_class)
        label = self._ext_name
        if self._description:
            label += f" -- {self._description}"
        yield Label(label, classes="ext-name")
        yield Static(f"{self._tools_count}t", classes="ext-tools-count")
        yield Switch(value=self._enabled, id=f"ext-toggle-{self._ext_name}")


class MCPPanel(Vertical):
    """MCP extension status and management -- hacker themed."""

    DEFAULT_CSS = """
    MCPPanel {
        width: 100%;
        height: 100%;
        background: #0A0A0A;
        padding: 1;
    }

    MCPPanel .mcp-header {
        height: 3;
        background: #0A0A0A;
        color: #FFD700;
        text-align: center;
        padding: 1;
        text-style: bold;
        border-bottom: double #1E1E1E;
    }

    MCPPanel .mcp-section-title {
        color: #00FF41;
        text-style: bold;
        padding: 1 0 0 0;
    }

    MCPPanel .mcp-empty {
        color: #555555;
        padding: 1;
        text-align: center;
    }

    MCPPanel .ext-list {
        height: 1fr;
        background: #0A0A0A;
    }

    MCPPanel .mcp-summary {
        height: 2;
        color: #555555;
        padding: 0 1;
    }
    """

    total_extensions: reactive[int] = reactive(0)
    connected_count: reactive[int] = reactive(0)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._extension_data: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        yield Label(f"  [{GOLD}]MCP[/] // EXTENSIONS", classes="mcp-header")
        yield Rule()
        yield Label("EXTENSIONS", classes="mcp-section-title")
        yield ScrollableContainer(id="mcp-ext-list", classes="ext-list")
        yield Rule()
        yield Static(
            f"  [{ERROR}]0 connected[/] / [{TEXT}]0 registered[/]",
            id="mcp-summary-display",
            classes="mcp-summary",
        )

    def on_mount(self) -> None:
        if not self._extension_data:
            self._show_empty()

    def _show_empty(self) -> None:
        try:
            container = self.query_one("#mcp-ext-list", ScrollableContainer)
            container.remove_children()
            container.mount(
                Static(
                    f"[{DIM}]No extensions registered.\n"
                    f"Use /ext add <name> <cmd> to add one.[/]",
                    classes="mcp-empty",
                )
            )
        except Exception:
            pass

    def load_extensions(self, statuses: list[dict[str, Any]]) -> None:
        self._extension_data = statuses
        self.total_extensions = len(statuses)
        self.connected_count = sum(1 for s in statuses if s.get("connected"))

        try:
            container = self.query_one("#mcp-ext-list", ScrollableContainer)
            container.remove_children()

            if not statuses:
                self._show_empty()
                return

            for status in statuses:
                row = ExtensionRow(
                    ext_name=status.get("name", "unknown"),
                    connected=status.get("connected", False),
                    enabled=status.get("enabled", True),
                    tools_count=status.get("tools_count", 0),
                    description=status.get("description", ""),
                )
                container.mount(row)

        except Exception:
            pass

        self._refresh_summary()

    def _refresh_summary(self) -> None:
        try:
            display = self.query_one("#mcp-summary-display", Static)
            conn_color = SUCCESS if self.connected_count > 0 else ERROR
            display.update(
                f"  [{conn_color}]{self.connected_count} connected[/] / "
                f"[{TEXT}]{self.total_extensions} registered[/]"
            )
        except Exception:
            pass

    @on(Switch.Changed)
    def handle_toggle(self, event: Switch.Changed) -> None:
        switch_id = event.switch.id or ""
        if switch_id.startswith("ext-toggle-"):
            ext_name = switch_id.replace("ext-toggle-", "")
            self.log.info(
                "Extension toggle: %s -> %s", ext_name, "enabled" if event.value else "disabled"
            )


# ---------------------------------------------------------------------------
# 5. TodoPanel -- Per-session todo list (hacker themed)
# ---------------------------------------------------------------------------

class TodoItem(Horizontal):
    """A single todo entry."""

    DEFAULT_CSS = """
    TodoItem {
        height: 1;
        padding: 0 1;
        color: #8A8A8A;
    }
    TodoItem .todo-check {
        width: 3;
        color: #555555;
    }
    TodoItem .todo-check.done {
        color: #00FF41;
    }
    TodoItem .todo-label {
        width: 1fr;
        color: #8A8A8A;
    }
    TodoItem .todo-label.done {
        color: #555555;
        text-style: strike;
    }
    """

    def __init__(self, text: str, done: bool = False, todo_id: int = 0, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._text = text
        self._done = done
        self._todo_id = todo_id

    def compose(self) -> ComposeResult:
        check_cls = "todo-check done" if self._done else "todo-check"
        label_cls = "todo-label done" if self._done else "todo-label"
        check_char = "[x]" if self._done else "[ ]"
        yield Static(check_char, classes=check_cls)
        yield Label(self._text, classes=label_cls)


class TodoPanel(Vertical):
    """Per-session todo list panel -- hacker styled."""

    DEFAULT_CSS = """
    TodoPanel {
        width: 100%;
        height: 100%;
        background: #0A0A0A;
        padding: 1;
    }
    TodoPanel .todo-header {
        height: 3;
        background: #0A0A0A;
        color: #FFD700;
        text-align: center;
        padding: 1;
        text-style: bold;
        border-bottom: double #1E1E1E;
    }
    TodoPanel .todo-count {
        height: 1;
        color: #555555;
        padding: 0 1;
    }
    TodoPanel .todo-list {
        height: 1fr;
        background: #0A0A0A;
    }
    TodoPanel .todo-empty {
        color: #555555;
        padding: 1;
        text-align: center;
    }
    """

    total: reactive[int] = reactive(0)
    done: reactive[int] = reactive(0)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._todos: list[dict[str, Any]] = []
        self._next_id = 1

    def compose(self) -> ComposeResult:
        yield Label(f"  [{GOLD}]TODO[/] // MISSION OBJECTIVES", classes="todo-header")
        yield Static("  0 / 0 done", id="todo-count-display", classes="todo-count")
        yield ScrollableContainer(id="todo-list-scroll", classes="todo-list")

    def on_mount(self) -> None:
        self._show_empty()

    def _show_empty(self) -> None:
        try:
            container = self.query_one("#todo-list-scroll", ScrollableContainer)
            container.remove_children()
            container.mount(
                Static(f"[{DIM}]No objectives. Use /todo add <text>[/]", classes="todo-empty")
            )
        except Exception:
            pass

    def add_todo(self, text: str) -> int:
        todo_id = self._next_id
        self._next_id += 1
        self._todos.append({"id": todo_id, "text": text, "done": False})
        self.total = len(self._todos)
        self._refresh_list()
        return todo_id

    def toggle_todo(self, todo_id: int) -> None:
        for t in self._todos:
            if t["id"] == todo_id:
                t["done"] = not t["done"]
                break
        self.done = sum(1 for t in self._todos if t["done"])
        self._refresh_list()

    def remove_todo(self, todo_id: int) -> None:
        self._todos = [t for t in self._todos if t["id"] != todo_id]
        self.total = len(self._todos)
        self.done = sum(1 for t in self._todos if t["done"])
        self._refresh_list()

    def _refresh_list(self) -> None:
        try:
            container = self.query_one("#todo-list-scroll", ScrollableContainer)
            container.remove_children()
            if not self._todos:
                self._show_empty()
                return
            for t in self._todos:
                container.mount(TodoItem(t["text"], t["done"], t["id"]))
            self.query_one("#todo-count-display", Static).update(
                f"  [{SUCCESS}]{self.done}[/] / [{TEXT}]{self.total}[/] done"
            )
        except Exception:
            pass

    def get_todos(self) -> list[dict[str, Any]]:
        return list(self._todos)


# ---------------------------------------------------------------------------
# 6. CostPanel -- Token usage and cost estimates (hacker themed)
# ---------------------------------------------------------------------------

class CostPanel(Vertical):
    """Token usage with cost estimates -- cyberpunk accounting."""

    DEFAULT_CSS = """
    CostPanel {
        width: 100%;
        height: 100%;
        background: #0A0A0A;
        padding: 1;
    }
    CostPanel .cost-header {
        height: 3;
        background: #0A0A0A;
        color: #FFD700;
        text-align: center;
        padding: 1;
        text-style: bold;
        border-bottom: double #1E1E1E;
    }
    CostPanel .cost-section-title {
        color: #00FF41;
        text-style: bold;
        padding: 1 0 0 0;
    }
    CostPanel .cost-row {
        color: #8A8A8A;
        padding: 0 1;
    }
    CostPanel .cost-total {
        color: #E8E8E8;
        text-style: bold;
        padding: 1 1;
    }
    """

    tokens_in: reactive[int] = reactive(0)
    tokens_out: reactive[int] = reactive(0)
    cost_per_1k_in: reactive[float] = reactive(0.0)
    cost_per_1k_out: reactive[float] = reactive(0.0)
    total_requests: reactive[int] = reactive(0)
    avg_response_ms: reactive[float] = reactive(0.0)
    session_start: reactive[float] = reactive(0.0)

    def compose(self) -> ComposeResult:
        yield Label(f"  [{GOLD}]COST[/] // RESOURCE EXPENDITURE", classes="cost-header")
        yield Rule()

        yield Label("TOKENS", classes="cost-section-title")
        yield Static("  Input:  0", id="cost-tokens-in", classes="cost-row")
        yield Static("  Output: 0", id="cost-tokens-out", classes="cost-row")
        yield Static("  Total:  0", id="cost-tokens-total", classes="cost-row")
        yield Rule()

        yield Label("ESTIMATED COST", classes="cost-section-title")
        yield Static("  Input:  $0.00", id="cost-dollars-in", classes="cost-row")
        yield Static("  Output: $0.00", id="cost-dollars-out", classes="cost-row")
        yield Static(f"  [{GOLD}]Total:  $0.00[/]", id="cost-dollars-total", classes="cost-total")
        yield Rule()

        yield Label("PERFORMANCE", classes="cost-section-title")
        yield Static("  Requests: 0", id="cost-requests", classes="cost-row")
        yield Static("  Avg time: --", id="cost-avg-time", classes="cost-row")
        yield Static("  Session:  0m 0s", id="cost-session-time", classes="cost-row")

    def on_mount(self) -> None:
        if self.session_start == 0.0:
            self.session_start = time.time()
        self.set_interval(5.0, self._update_session_time)

    def _update_session_time(self) -> None:
        elapsed = time.time() - self.session_start if self.session_start > 0 else 0
        minutes = int(elapsed // 60)
        seconds = int(elapsed % 60)
        hours = int(minutes // 60)
        minutes = minutes % 60
        if hours > 0:
            t = f"  Session:  {hours}h {minutes}m {seconds}s"
        else:
            t = f"  Session:  {minutes}m {seconds}s"
        try:
            self.query_one("#cost-session-time", Static).update(t)
        except Exception:
            pass

    def watch_tokens_in(self, value: int) -> None:
        self._refresh_display()

    def watch_tokens_out(self, value: int) -> None:
        self._refresh_display()

    def watch_total_requests(self, value: int) -> None:
        try:
            self.query_one("#cost-requests", Static).update(f"  Requests: {value}")
        except Exception:
            pass

    def watch_avg_response_ms(self, value: float) -> None:
        try:
            if value < 1000:
                display = f"{value:.0f}ms"
            else:
                display = f"{value / 1000:.1f}s"
            self.query_one("#cost-avg-time", Static).update(f"  Avg time: {display}")
        except Exception:
            pass

    def _refresh_display(self) -> None:
        total = self.tokens_in + self.tokens_out
        cost_in = (self.tokens_in / 1000) * self.cost_per_1k_in
        cost_out = (self.tokens_out / 1000) * self.cost_per_1k_out
        cost_total = cost_in + cost_out
        try:
            self.query_one("#cost-tokens-in", Static).update(
                f"  Input:  {self._fmt(self.tokens_in)}"
            )
            self.query_one("#cost-tokens-out", Static).update(
                f"  Output: {self._fmt(self.tokens_out)}"
            )
            self.query_one("#cost-tokens-total", Static).update(
                f"  Total:  {self._fmt(total)}"
            )
            self.query_one("#cost-dollars-in", Static).update(
                f"  Input:  ${cost_in:.4f}"
            )
            self.query_one("#cost-dollars-out", Static).update(
                f"  Output: ${cost_out:.4f}"
            )
            self.query_one("#cost-dollars-total", Static).update(
                f"  [{GOLD}]Total:  ${cost_total:.4f}[/]"
            )
        except Exception:
            pass

    def update_cost(
        self,
        *,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cost_per_1k_in: float | None = None,
        cost_per_1k_out: float | None = None,
        requests: int | None = None,
        avg_ms: float | None = None,
    ) -> None:
        if tokens_in is not None:
            self.tokens_in = tokens_in
        if tokens_out is not None:
            self.tokens_out = tokens_out
        if cost_per_1k_in is not None:
            self.cost_per_1k_in = cost_per_1k_in
        if cost_per_1k_out is not None:
            self.cost_per_1k_out = cost_per_1k_out
        if requests is not None:
            self.total_requests = requests
        if avg_ms is not None:
            self.avg_response_ms = avg_ms

    @staticmethod
    def _fmt(count: int) -> str:
        if count >= 1_000_000:
            return f"{count / 1_000_000:.1f}M"
        if count >= 1_000:
            return f"{count / 1_000:.1f}K"
        return str(count)


# ---------------------------------------------------------------------------
# 7. ArmyTabPanel -- Bird's eye view of all 18 agents (NEW)
# ---------------------------------------------------------------------------

class ArmyTabPanel(Vertical):
    """Compact army overview panel for the sidebar tab.

    Shows all 18 agents in a grid with state indicators and tier badges.
    Wraps the ArmyView widget from tui_hacker with panel chrome.
    """

    DEFAULT_CSS = """
    ArmyTabPanel {
        width: 100%;
        height: 100%;
        background: #0A0A0A;
        padding: 1;
    }

    ArmyTabPanel .army-header {
        height: 3;
        background: #0A0A0A;
        color: #FFD700;
        text-align: center;
        padding: 1;
        text-style: bold;
        border-bottom: double #1E1E1E;
    }

    ArmyTabPanel .army-summary {
        height: 1;
        color: #555555;
        padding: 0 1;
    }

    ArmyTabPanel .army-grid-area {
        height: 1fr;
        background: #0A0A0A;
        overflow-y: auto;
        padding: 0 1;
    }
    """

    active_count: reactive[int] = reactive(0)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._agent_states: dict[str, str] = {}
        self._agent_tasks: dict[str, str] = {}
        # Import roster from hacker module
        from djcode.tui_hacker import AGENT_ROSTER as ROSTER, AGENT_STATES as STATES, TIER_COLORS
        self._roster = ROSTER
        self._state_map = STATES
        self._tier_colors = TIER_COLORS
        for name, _, _ in self._roster:
            self._agent_states[name] = "idle"
            self._agent_tasks[name] = ""

    def compose(self) -> ComposeResult:
        yield Label(f"  [{GOLD}]ARMY[/] // OPERATIVE GRID", classes="army-header")
        yield Static(
            f"  [{DIM}]{len(self._roster)} operatives[/] [{TEXT}]| 0 active[/]",
            id="army-summary-display",
            classes="army-summary",
        )
        yield Rule()
        yield ScrollableContainer(
            Static(self._build_grid(), id="army-grid-text"),
            id="army-grid-scroll",
            classes="army-grid-area",
        )

    def _build_grid(self) -> str:
        """Build text-based agent grid: 2 columns for sidebar width."""
        lines: list[str] = []
        cols = 2
        rows_needed = (len(self._roster) + cols - 1) // cols

        for row_idx in range(rows_needed):
            row_parts: list[str] = []
            for col_idx in range(cols):
                idx = row_idx * cols + col_idx
                if idx < len(self._roster):
                    name, title, tier = self._roster[idx]
                    state = self._agent_states.get(name, "idle")
                    icon, color = self._state_map.get(state, ("--", TEXT_DIM))
                    tier_color = self._tier_colors.get(tier, TEXT_DIM)
                    task = self._agent_tasks.get(name, "")
                    task_snip = task[:10] + ".." if len(task) > 12 else (task or ".." * 5)

                    if state == "idle":
                        cell = f"[{DIM}]{icon} {name:<10}[/] [{DIM}]{task_snip}[/]"
                    else:
                        cell = f"[{color}]{icon}[/] [{tier_color}]{name:<10}[/] [{TEXT}]{task_snip}[/]"
                    row_parts.append(cell)
                else:
                    row_parts.append(" " * 24)

            lines.append("  " + f" [{HUD_BORDER}]|[/] ".join(row_parts))
            if row_idx < rows_needed - 1:
                lines.append(f"  [{HUD_BORDER}]{'-' * 52}[/]")

        return "\n".join(lines)

    def set_agent_state(self, name: str, state: str, task: str = "") -> None:
        if name in self._agent_states:
            self._agent_states[name] = state
            if task:
                self._agent_tasks[name] = task
            self.active_count = sum(
                1 for s in self._agent_states.values() if s not in ("idle", "ready")
            )
            self._refresh()

    def _refresh(self) -> None:
        try:
            self.query_one("#army-summary-display", Static).update(
                f"  [{DIM}]{len(self._roster)} operatives[/]"
                f" [{TEXT}]| [{SUCCESS}]{self.active_count}[/] active[/]"
            )
            self.query_one("#army-grid-text", Static).update(self._build_grid())
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 8. IntelPanel -- Context utilization + Threat monitor (NEW)
# ---------------------------------------------------------------------------

class IntelPanel(Vertical):
    """Intelligence panel: context utilization and threat alerts.

    Combines:
    - Context window utilization bar (how much of the LLM context is used)
    - Threat monitor from sentinel agents (Kavach, Varuna, Mitra, Indra)
    """

    DEFAULT_CSS = """
    IntelPanel {
        width: 100%;
        height: 100%;
        background: #0A0A0A;
        padding: 1;
    }

    IntelPanel .intel-header {
        height: 3;
        background: #0A0A0A;
        color: #FFD700;
        text-align: center;
        padding: 1;
        text-style: bold;
        border-bottom: double #1E1E1E;
    }

    IntelPanel .intel-section {
        color: #00FF41;
        text-style: bold;
        padding: 1 0 0 0;
    }

    IntelPanel .context-display {
        padding: 0 1;
        height: 3;
    }

    IntelPanel .threat-area {
        height: 1fr;
        background: #0A0A0A;
        overflow-y: auto;
    }
    """

    context_used: reactive[int] = reactive(0)
    context_max: reactive[int] = reactive(1_000_000)
    alert_count: reactive[int] = reactive(0)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._alerts: list[dict[str, str]] = []
        self._sentinel_colors = {
            "Kavach": "#FF1744",
            "Varuna": "#FF6D00",
            "Mitra": "#FF8C00",
            "Indra": "#D50000",
        }

    def compose(self) -> ComposeResult:
        yield Label(f"  [{GOLD}]INTEL[/] // SYSTEM INTELLIGENCE", classes="intel-header")
        yield Rule()

        # Context section
        yield Label("CONTEXT WINDOW", classes="intel-section")
        yield Static(self._build_context_bar(), id="intel-context-bar", classes="context-display")
        yield Rule()

        # Sentinel status
        yield Label("SENTINEL STATUS", classes="intel-section")
        yield Static(self._build_sentinel_line(), id="intel-sentinel-line")
        yield Rule()

        # Threat alerts
        yield Label("THREAT ALERTS", classes="intel-section")
        yield Static(
            f"  [{DIM}]({self.alert_count} alerts)[/]",
            id="intel-alert-count",
        )
        yield ScrollableContainer(id="intel-threat-scroll", classes="threat-area")

    def _build_context_bar(self) -> str:
        pct = int((self.context_used / self.context_max) * 100) if self.context_max > 0 else 0
        pct = min(100, max(0, pct))

        if pct < 50:
            color = SUCCESS
        elif pct < 75:
            color = GOLD
        elif pct < 90:
            color = WARNING
        else:
            color = ERROR

        bar_width = 28
        filled = int(pct / 100 * bar_width)
        empty = bar_width - filled

        used_str = self._fmt(self.context_used)
        max_str = self._fmt(self.context_max)

        return (
            f"  [{color}]{'#' * filled}[/][{DIM}]{'-' * empty}[/]"
            f" [{color}]{pct}%[/]\n"
            f"  [{DIM}]{used_str} / {max_str} tokens[/]"
        )

    def _build_sentinel_line(self) -> str:
        parts: list[str] = []
        for name, color in self._sentinel_colors.items():
            parts.append(f"[{color}]{name}[/]")
        return "  " + f" [{DIM}]|[/] ".join(parts) + f" [{DIM}]-- monitoring[/]"

    def update_context(self, used: int, maximum: int) -> None:
        self.context_used = used
        self.context_max = maximum
        try:
            self.query_one("#intel-context-bar", Static).update(
                self._build_context_bar()
            )
        except Exception:
            pass

    def add_threat(self, agent: str, severity: str, message: str) -> None:
        """Add a threat alert from a sentinel agent."""
        self._alerts.insert(0, {
            "agent": agent,
            "severity": severity,
            "message": message,
            "time": time.strftime("%H:%M:%S"),
        })
        if len(self._alerts) > 50:
            self._alerts = self._alerts[:50]
        self.alert_count = len(self._alerts)
        self._refresh_threats()

    def clear_threats(self) -> None:
        self._alerts.clear()
        self.alert_count = 0
        self._refresh_threats()

    def _refresh_threats(self) -> None:
        try:
            self.query_one("#intel-alert-count", Static).update(
                f"  [{DIM}]({self.alert_count} alerts)[/]"
            )

            container = self.query_one("#intel-threat-scroll", ScrollableContainer)
            container.remove_children()

            if not self._alerts:
                container.mount(
                    Static(f"  [{DIM}]All clear. No active threats.[/]")
                )
                return

            for alert in self._alerts[:20]:
                sev_color = {
                    "critical": ERROR,
                    "warning": WARNING,
                    "info": INFO,
                }.get(alert["severity"], TEXT)

                agent_color = self._sentinel_colors.get(alert["agent"], TEXT)
                sev_icon = {
                    "critical": "!!!",
                    "warning": "! !",
                    "info": " i ",
                }.get(alert["severity"], " ? ")

                container.mount(Static(
                    f"  [{sev_color}][{sev_icon}][/]"
                    f" [{DIM}]{alert['time']}[/]"
                    f" [{agent_color}]{alert['agent']}[/]"
                    f" [{TEXT}]{alert['message']}[/]"
                ))

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
# 9. SidePanel -- Tabbed container for all panels (8 tabs now)
# ---------------------------------------------------------------------------

class SidePanel(Vertical):
    """Tabbed side panel: Files, Agent, Stats, MCP, Todo, Cost, Army, Intel."""

    DEFAULT_CSS = """
    SidePanel {
        width: 100%;
        height: 100%;
        background: #0A0A0A;
        border-left: double #1E1E1E;
    }

    SidePanel TabbedContent {
        height: 100%;
        background: #0A0A0A;
    }

    SidePanel ContentSwitcher {
        height: 1fr;
        background: #0A0A0A;
    }

    SidePanel TabPane {
        padding: 0;
        height: 1fr;
        background: #0A0A0A;
    }

    SidePanel Tabs {
        background: #0A0A0A;
        dock: top;
        border-bottom: solid #1E1E1E;
    }

    SidePanel Tab {
        background: #111111;
        color: #555555;
        padding: 0 2;
        text-style: bold;
    }

    SidePanel Tab:hover {
        background: #1A1A1A;
        color: #00FF41;
    }

    SidePanel Tab.-active {
        background: #00FF41;
        color: #0A0A0A;
        text-style: bold;
    }

    SidePanel Tab:focus {
        text-style: bold underline;
    }

    SidePanel Underline {
        color: #00FF41;
    }
    """

    def __init__(self, project_path: str | Path | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._project_path = project_path

    def compose(self) -> ComposeResult:
        with TabbedContent():
            with TabPane("Files", id="files-tab"):
                yield ProjectPanel(path=self._project_path)
            with TabPane("Agent", id="agents-tab"):
                yield AgentPanel()
            with TabPane("Stats", id="stats-tab"):
                yield StatsPanel()
            with TabPane("MCP", id="mcp-tab"):
                yield MCPPanel()
            with TabPane("Todo", id="todos-tab"):
                yield TodoPanel()
            with TabPane("Cost", id="cost-tab"):
                yield CostPanel()
            with TabPane("Army", id="army-tab"):
                yield ArmyTabPanel()
            with TabPane("Intel", id="intel-tab"):
                yield IntelPanel()

    # -- Convenience accessors for the parent app --

    @property
    def project_panel(self) -> ProjectPanel:
        return self.query_one(ProjectPanel)

    @property
    def agent_panel(self) -> AgentPanel:
        return self.query_one(AgentPanel)

    @property
    def stats_panel(self) -> StatsPanel:
        return self.query_one(StatsPanel)

    @property
    def mcp_panel(self) -> MCPPanel:
        return self.query_one(MCPPanel)

    @property
    def todo_panel(self) -> TodoPanel:
        return self.query_one(TodoPanel)

    @property
    def cost_panel(self) -> CostPanel:
        return self.query_one(CostPanel)

    @property
    def army_panel(self) -> ArmyTabPanel:
        return self.query_one(ArmyTabPanel)

    @property
    def intel_panel(self) -> IntelPanel:
        return self.query_one(IntelPanel)


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "FileSelected",
    "AgentUpdated",
    "ProjectPanel",
    "AgentPanel",
    "StatsPanel",
    "MCPPanel",
    "TodoPanel",
    "CostPanel",
    "ArmyTabPanel",
    "IntelPanel",
    "SidePanel",
    "FilteredDirectoryTree",
]
