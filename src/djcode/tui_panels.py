"""Textual TUI side-panel widgets for DJcode.

Provides a tabbed side panel with four views:
  - ProjectPanel  — directory tree file browser
  - AgentPanel    — live agent status dashboard
  - StatsPanel    — session statistics
  - MCPPanel      — MCP extension status
  - SidePanel     — tabbed container that hosts them all

Uses Textual 8.x API with reactive() properties and message passing.
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


# ---------------------------------------------------------------------------
# Theme constants
# ---------------------------------------------------------------------------

GOLD = "#FFD700"
BG = "#0a0a0a"
BG_HEADER = "#111111"
BORDER = "#222222"
DIM = "#666666"
TEXT = "#999999"
TEXT_BRIGHT = "#ffffff"

# File extensions -> type icons
FILE_ICONS: dict[str, str] = {
    ".py": "🐍",
    ".rs": "🦀",
    ".ts": "📘",
    ".tsx": "⚛️",
    ".js": "📒",
    ".jsx": "⚛️",
    ".json": "📋",
    ".toml": "⚙️",
    ".yaml": "⚙️",
    ".yml": "⚙️",
    ".md": "📝",
    ".txt": "📄",
    ".html": "🌐",
    ".css": "🎨",
    ".sh": "🐚",
    ".dockerfile": "🐳",
    ".sql": "🗃️",
    ".lock": "🔒",
    ".env": "🔐",
    ".gif": "🖼️",
    ".png": "🖼️",
    ".jpg": "🖼️",
    ".svg": "🖼️",
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
# 1. ProjectPanel — Directory tree file browser
# ---------------------------------------------------------------------------

class FilteredDirectoryTree(DirectoryTree):
    """DirectoryTree that filters out noise directories and adds file icons."""

    def filter_paths(self, paths: list[Path]) -> list[Path]:
        """Hide common non-essential directories and files."""
        filtered: list[Path] = []
        for p in paths:
            name = p.name
            # Skip hidden dirs from our blocklist
            if p.is_dir() and name in HIDDEN_DIRS:
                continue
            # Skip common hidden/noise files
            if name == ".DS_Store":
                continue
            filtered.append(p)
        return filtered

    def render_label(self, node: Tree.NodeData, base_style: str, style: str) -> str:  # type: ignore[override]
        """Add file-type icons and size info to labels."""
        # Use default rendering — icons are added via CSS/Rich markup
        return super().render_label(node, base_style, style)


class ProjectPanel(Vertical):
    """File browser + project info panel."""

    DEFAULT_CSS = """
    ProjectPanel {
        width: 100%;
        height: 100%;
        background: #0a0a0a;
    }

    ProjectPanel .project-header {
        height: 3;
        background: #111111;
        color: #FFD700;
        text-align: center;
        padding: 1;
        text-style: bold;
    }

    ProjectPanel .project-path {
        height: 1;
        background: #0a0a0a;
        color: #666666;
        padding: 0 1;
        text-style: italic;
    }

    ProjectPanel FilteredDirectoryTree {
        height: 1fr;
        background: #0a0a0a;
        color: #999999;
        scrollbar-color: #333333;
        scrollbar-color-hover: #FFD700;
        scrollbar-color-active: #FFD700;
        padding: 0 1;
    }

    ProjectPanel FilteredDirectoryTree:focus {
        border: tall #FFD700 30%;
    }

    ProjectPanel FilteredDirectoryTree:focus .tree--cursor {
        background: #FFD700 20%;
        color: #FFD700;
    }

    ProjectPanel FilteredDirectoryTree .tree--guides {
        color: #333333;
    }

    ProjectPanel FilteredDirectoryTree .directory-tree--folder {
        color: #FFD700;
        text-style: bold;
    }

    ProjectPanel FilteredDirectoryTree .directory-tree--file {
        color: #999999;
    }

    ProjectPanel .file-info {
        height: 2;
        background: #111111;
        color: #666666;
        padding: 0 1;
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
        yield Label(f"  {self.project_name}", classes="project-header")
        yield Label(self._truncate_path(str(self._root_path)), classes="project-path")
        yield FilteredDirectoryTree(str(self._root_path))
        yield Label("", classes="file-info", id="file-info-label")

    @on(DirectoryTree.FileSelected)
    def handle_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        """When user selects a file, post our message and update info bar."""
        path = event.path
        self.post_message(FileSelected(path))
        # Update bottom info bar
        info_label = self.query_one("#file-info-label", Label)
        try:
            size = path.stat().st_size
            ext = path.suffix.lower()
            icon = FILE_ICONS.get(ext, "📄")
            info_label.update(f" {icon} {path.name}  |  {self._format_size(size)}")
        except OSError:
            info_label.update(f" {path.name}")

    def set_path(self, new_path: str | Path) -> None:
        """Change the root directory of the tree."""
        new_path = Path(new_path)
        if new_path.is_dir():
            self._root_path = new_path
            self.project_path = str(new_path)
            self.project_name = new_path.name or "Project"
            # Rebuild the tree
            tree = self.query_one(FilteredDirectoryTree)
            tree.path = str(new_path)
            tree.reload()
            # Update header
            header = self.query("Label.project-header")
            if header:
                header.first().update(f"  {self.project_name}")
            path_label = self.query("Label.project-path")
            if path_label:
                path_label.first().update(self._truncate_path(str(new_path)))

    @staticmethod
    def _format_size(size: int) -> str:
        """Format bytes as human-readable."""
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        if size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MB"
        return f"{size / (1024 * 1024 * 1024):.1f} GB"

    @staticmethod
    def _truncate_path(path: str, max_len: int = 40) -> str:
        """Truncate long paths with ellipsis."""
        if len(path) <= max_len:
            return path
        return "..." + path[-(max_len - 3):]


# ---------------------------------------------------------------------------
# 2. AgentPanel — Live agent status dashboard
# ---------------------------------------------------------------------------

class ToolHistoryItem(Static):
    """A single tool call entry in the history."""

    DEFAULT_CSS = """
    ToolHistoryItem {
        height: 1;
        padding: 0 1;
        color: #999999;
    }
    """

    def __init__(self, tool_name: str, status: str = "ok", **kwargs: Any) -> None:
        icon = "✓" if status == "ok" else "✗" if status == "error" else "⟳"
        color = "#4ade80" if status == "ok" else "#f87171" if status == "error" else "#FFD700"
        markup = f"[{color}]{icon}[/] [{TEXT}]{tool_name}[/]"
        super().__init__(markup, **kwargs)


class AgentPanel(Vertical):
    """Live agent dashboard showing active agent, tools, progress."""

    DEFAULT_CSS = """
    AgentPanel {
        width: 100%;
        height: 100%;
        background: #0a0a0a;
        padding: 1;
    }

    AgentPanel .agent-header {
        height: 3;
        background: #111111;
        color: #FFD700;
        text-align: center;
        padding: 1;
        text-style: bold;
    }

    AgentPanel .agent-section-title {
        color: #FFD700;
        text-style: bold;
        padding: 1 0 0 0;
    }

    AgentPanel .agent-stat {
        color: #999999;
        padding: 0 1;
    }

    AgentPanel .agent-stat-value {
        color: #ffffff;
        text-style: bold;
    }

    AgentPanel .tool-history-container {
        height: auto;
        max-height: 14;
        background: #0a0a0a;
        padding: 0;
    }

    AgentPanel .memory-stats {
        color: #666666;
        padding: 1 1;
    }
    """

    active_agent: reactive[str] = reactive("Operator")
    active_role: reactive[str] = reactive("General")
    tokens_in: reactive[int] = reactive(0)
    tokens_out: reactive[int] = reactive(0)
    session_start: reactive[float] = reactive(0.0)
    tool_count: reactive[int] = reactive(0)
    memory_session: reactive[int] = reactive(0)
    memory_facts: reactive[int] = reactive(0)
    memory_vectors: reactive[int] = reactive(0)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._tool_history: list[tuple[str, str]] = []
        self._timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield Label("🎯 Agent Dashboard", classes="agent-header")
        yield Rule(style="dark")

        # Active agent
        yield Label("ACTIVE AGENT", classes="agent-section-title")
        yield Static(
            f"  🎯 [bold {GOLD}]{self.active_agent}[/] ([{TEXT}]{self.active_role}[/])",
            id="agent-active-display",
        )
        yield Rule(style="dark")

        # Token counter
        yield Label("TOKENS", classes="agent-section-title")
        yield Static("  ↑ 0  ↓ 0", id="agent-token-display", classes="agent-stat")
        yield Rule(style="dark")

        # Session timer
        yield Label("SESSION", classes="agent-section-title")
        yield Static("  0m 0s", id="agent-timer-display", classes="agent-stat")
        yield Rule(style="dark")

        # Tool history
        yield Label("TOOL HISTORY", classes="agent-section-title")
        yield ScrollableContainer(id="tool-history-scroll", classes="tool-history-container")
        yield Rule(style="dark")

        # Memory stats
        yield Label("MEMORY", classes="agent-section-title")
        yield Static(
            "  3-tier: 0 session / 0 facts / 0 vectors",
            id="agent-memory-display",
            classes="memory-stats",
        )

    def on_mount(self) -> None:
        """Start the session timer on mount."""
        if self.session_start == 0.0:
            self.session_start = time.time()
        self._timer = self.set_interval(1.0, self._update_timer)

    def _update_timer(self) -> None:
        """Update the session timer display every second."""
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
        """React to agent changes."""
        try:
            display = self.query_one("#agent-active-display", Static)
            display.update(
                f"  🎯 [bold {GOLD}]{value}[/] ([{TEXT}]{self.active_role}[/])"
            )
        except Exception:
            pass

    def watch_tokens_in(self, value: int) -> None:
        """React to token count changes."""
        self._refresh_token_display()

    def watch_tokens_out(self, value: int) -> None:
        """React to token count changes."""
        self._refresh_token_display()

    def _refresh_token_display(self) -> None:
        """Update the token counter label."""
        try:
            display = self.query_one("#agent-token-display", Static)
            display.update(
                f"  [#4ade80]↑ {self._fmt_tokens(self.tokens_in)}[/]"
                f"  [#f87171]↓ {self._fmt_tokens(self.tokens_out)}[/]"
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
        """Update the memory stats label."""
        try:
            display = self.query_one("#agent-memory-display", Static)
            display.update(
                f"  3-tier: {self.memory_session} session / "
                f"{self.memory_facts} facts / {self.memory_vectors} vectors"
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

    def set_agent(self, name: str, role: str = "General") -> None:
        """Update the active agent display."""
        self.active_agent = name
        self.active_role = role

    def update_tokens(self, tokens_in: int, tokens_out: int) -> None:
        """Update token counters (absolute values, not deltas)."""
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out

    def update_memory(self, session: int = 0, facts: int = 0, vectors: int = 0) -> None:
        """Update memory stats display."""
        self.memory_session = session
        self.memory_facts = facts
        self.memory_vectors = vectors

    @staticmethod
    def _fmt_tokens(count: int) -> str:
        """Format token count compactly."""
        if count >= 1_000_000:
            return f"{count / 1_000_000:.1f}m"
        if count >= 1_000:
            return f"{count / 1_000:.1f}k"
        return str(count)


# ---------------------------------------------------------------------------
# 3. StatsPanel — Session statistics with live updates
# ---------------------------------------------------------------------------

class StatsPanel(Vertical):
    """Session stats with live updates."""

    DEFAULT_CSS = """
    StatsPanel {
        width: 100%;
        height: 100%;
        background: #0a0a0a;
        padding: 1;
    }

    StatsPanel .stats-header {
        height: 3;
        background: #111111;
        color: #FFD700;
        text-align: center;
        padding: 1;
        text-style: bold;
    }

    StatsPanel .stats-section-title {
        color: #FFD700;
        text-style: bold;
        padding: 1 0 0 0;
    }

    StatsPanel .stats-row {
        color: #999999;
        padding: 0 1;
    }

    StatsPanel .stats-bar-container {
        height: 1;
        padding: 0 1;
    }

    StatsPanel .stats-bar-in {
        color: #4ade80;
    }

    StatsPanel .stats-bar-out {
        color: #f87171;
    }

    StatsPanel .files-list {
        height: auto;
        max-height: 10;
        background: #0a0a0a;
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
        yield Label("📊 Session Stats", classes="stats-header")
        yield Rule(style="dark")

        # Token usage with bar
        yield Label("TOKEN USAGE", classes="stats-section-title")
        yield Static("  ↑ In:  0", id="stats-tokens-in", classes="stats-row")
        yield Static(
            f"  [#4ade80]{'█' * 0}{'░' * 30}[/]",
            id="stats-bar-in",
            classes="stats-bar-container",
        )
        yield Static("  ↓ Out: 0", id="stats-tokens-out", classes="stats-row")
        yield Static(
            f"  [#f87171]{'█' * 0}{'░' * 30}[/]",
            id="stats-bar-out",
            classes="stats-bar-container",
        )
        yield Rule(style="dark")

        # Performance
        yield Label("PERFORMANCE", classes="stats-section-title")
        yield Static("  Response: --", id="stats-response-time", classes="stats-row")
        yield Static("  Tools used: 0", id="stats-tools-count", classes="stats-row")
        yield Rule(style="dark")

        # Model info
        yield Label("MODEL", classes="stats-section-title")
        yield Static("  --", id="stats-model-display", classes="stats-row")
        yield Rule(style="dark")

        # Modified files
        yield Label("MODIFIED FILES", classes="stats-section-title")
        yield ScrollableContainer(id="stats-files-scroll", classes="files-list")

    def watch_tokens_in(self, value: int) -> None:
        try:
            self.query_one("#stats-tokens-in", Static).update(
                f"  [#4ade80]↑[/] In:  [{TEXT_BRIGHT}]{self._fmt(value)}[/]"
            )
            self._update_bar("#stats-bar-in", value, "#4ade80")
        except Exception:
            pass

    def watch_tokens_out(self, value: int) -> None:
        try:
            self.query_one("#stats-tokens-out", Static).update(
                f"  [#f87171]↓[/] Out: [{TEXT_BRIGHT}]{self._fmt(value)}[/]"
            )
            self._update_bar("#stats-bar-out", value, "#f87171")
        except Exception:
            pass

    def _update_bar(self, bar_id: str, value: int, color: str) -> None:
        """Update a bar chart visual. Max width = 30 chars."""
        max_val = max(self.tokens_in, self.tokens_out, 1)
        filled = int((value / max_val) * 30) if max_val > 0 else 0
        empty = 30 - filled
        try:
            bar = self.query_one(bar_id, Static)
            bar.update(f"  [{color}]{'█' * filled}{'░' * empty}[/]")
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
        """Track a modified file in the list."""
        name = Path(file_path).name
        if name not in self._modified_files:
            self._modified_files.append(name)
            try:
                container = self.query_one("#stats-files-scroll", ScrollableContainer)
                container.mount(
                    Static(f"  [{GOLD}]•[/] [{TEXT}]{name}[/]")
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
        """Bulk update stats from the operator."""
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
            return f"{count / 1_000_000:.1f}m"
        if count >= 1_000:
            return f"{count / 1_000:.1f}k"
        return str(count)


# ---------------------------------------------------------------------------
# 4. MCPPanel — MCP extension status and management
# ---------------------------------------------------------------------------

class ExtensionRow(Horizontal):
    """A single extension entry with status dot and toggle."""

    DEFAULT_CSS = """
    ExtensionRow {
        height: 3;
        padding: 0 1;
        background: #0a0a0a;
    }

    ExtensionRow .ext-status-dot {
        width: 3;
        color: #f87171;
        padding: 0;
    }

    ExtensionRow .ext-status-dot.connected {
        color: #4ade80;
    }

    ExtensionRow .ext-name {
        width: 1fr;
        color: #999999;
        padding: 0 1;
    }

    ExtensionRow .ext-tools-count {
        width: 6;
        color: #666666;
        text-align: right;
    }

    ExtensionRow Switch {
        width: 8;
        background: #0a0a0a;
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
        dot_char = "●" if self._connected else "○"
        yield Static(dot_char, classes=dot_class)
        label = self._ext_name
        if self._description:
            label += f" — {self._description}"
        yield Label(label, classes="ext-name")
        yield Static(f"{self._tools_count}t", classes="ext-tools-count")
        yield Switch(value=self._enabled, id=f"ext-toggle-{self._ext_name}")


class MCPPanel(Vertical):
    """MCP extension status and management."""

    DEFAULT_CSS = """
    MCPPanel {
        width: 100%;
        height: 100%;
        background: #0a0a0a;
        padding: 1;
    }

    MCPPanel .mcp-header {
        height: 3;
        background: #111111;
        color: #FFD700;
        text-align: center;
        padding: 1;
        text-style: bold;
    }

    MCPPanel .mcp-section-title {
        color: #FFD700;
        text-style: bold;
        padding: 1 0 0 0;
    }

    MCPPanel .mcp-empty {
        color: #666666;
        padding: 1;
        text-align: center;
    }

    MCPPanel .ext-list {
        height: 1fr;
        background: #0a0a0a;
    }

    MCPPanel .mcp-summary {
        height: 2;
        color: #666666;
        padding: 0 1;
    }
    """

    total_extensions: reactive[int] = reactive(0)
    connected_count: reactive[int] = reactive(0)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._extension_data: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        yield Label("🔌 MCP Extensions", classes="mcp-header")
        yield Rule(style="dark")
        yield Label("EXTENSIONS", classes="mcp-section-title")
        yield ScrollableContainer(id="mcp-ext-list", classes="ext-list")
        yield Rule(style="dark")
        yield Static(
            "  0 connected / 0 registered",
            id="mcp-summary-display",
            classes="mcp-summary",
        )

    def on_mount(self) -> None:
        """Show empty state if no extensions loaded."""
        if not self._extension_data:
            self._show_empty()

    def _show_empty(self) -> None:
        """Display empty state message."""
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
        """Populate the panel from ExtensionManager.get_status() output."""
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
        """Update the summary line at the bottom."""
        try:
            display = self.query_one("#mcp-summary-display", Static)
            conn_color = "#4ade80" if self.connected_count > 0 else "#f87171"
            display.update(
                f"  [{conn_color}]{self.connected_count} connected[/] / "
                f"[{TEXT}]{self.total_extensions} registered[/]"
            )
        except Exception:
            pass

    @on(Switch.Changed)
    def handle_toggle(self, event: Switch.Changed) -> None:
        """Handle extension enable/disable toggle."""
        switch_id = event.switch.id or ""
        if switch_id.startswith("ext-toggle-"):
            ext_name = switch_id.replace("ext-toggle-", "")
            # Post a message that the main app can handle to actually
            # enable/disable the extension via ExtensionManager
            self.log.info(
                "Extension toggle: %s -> %s", ext_name, "enabled" if event.value else "disabled"
            )


# ---------------------------------------------------------------------------
# 5. SidePanel — Tabbed container for all panels
# ---------------------------------------------------------------------------

class SidePanel(Vertical):
    """Tabbed side panel with Files, Agents, Stats, MCP tabs."""

    DEFAULT_CSS = """
    SidePanel {
        width: 100%;
        height: 100%;
        background: #0a0a0a;
        border-left: tall #222222;
    }

    SidePanel TabbedContent {
        height: 100%;
        background: #0a0a0a;
    }

    SidePanel ContentSwitcher {
        height: 1fr;
        background: #0a0a0a;
    }

    SidePanel TabPane {
        padding: 0;
        background: #0a0a0a;
    }

    SidePanel Tabs {
        background: #111111;
        dock: top;
    }

    SidePanel Tab {
        background: #333333;
        color: #888888;
        padding: 0 2;
        text-style: bold;
    }

    SidePanel Tab:hover {
        background: #444444;
        color: #cccccc;
    }

    SidePanel Tab.-active {
        background: #FFD700;
        color: #000000;
        text-style: bold;
    }

    SidePanel Tab:focus {
        text-style: bold underline;
    }

    SidePanel Underline {
        color: #FFD700;
    }
    """

    def __init__(self, project_path: str | Path | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._project_path = project_path

    def compose(self) -> ComposeResult:
        with TabbedContent():
            with TabPane("Files", id="files-tab"):
                yield ProjectPanel(path=self._project_path)
            with TabPane("Agents", id="agents-tab"):
                yield AgentPanel()
            with TabPane("Stats", id="stats-tab"):
                yield StatsPanel()
            with TabPane("MCP", id="mcp-tab"):
                yield MCPPanel()

    # -- Convenience accessors for the parent app --

    @property
    def project_panel(self) -> ProjectPanel:
        """Access the ProjectPanel instance."""
        return self.query_one(ProjectPanel)

    @property
    def agent_panel(self) -> AgentPanel:
        """Access the AgentPanel instance."""
        return self.query_one(AgentPanel)

    @property
    def stats_panel(self) -> StatsPanel:
        """Access the StatsPanel instance."""
        return self.query_one(StatsPanel)

    @property
    def mcp_panel(self) -> MCPPanel:
        """Access the MCPPanel instance."""
        return self.query_one(MCPPanel)


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
    "SidePanel",
    "FilteredDirectoryTree",
]
