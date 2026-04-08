"""DJcode Textual TUI — lazygit-style split-pane interface.

Premium terminal experience with:
- Left panel: chat with streaming responses + vim navigation
- Right panel: tabbed sidebar (Files/Agents/Stats/MCP)
- Gold/black theme matching DJcode brand
- Full keyboard navigation with vim keys
- All classic REPL features: slash commands, tool router, memory, orchestrator
- Command palette with fuzzy search
- Real-time token counting and session stats

Launch with: djcode (default) or djcode --classic for old REPL
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.screen import ModalScreen
from textual.widgets import (
    Footer,
    Header,
    Input,
    ListView,
    ListItem,
    OptionList,
    RichLog,
    Static,
)
from textual.widgets.option_list import Option

from djcode import __version__
from djcode.tui_panels import SidePanel, AgentPanel, StatsPanel, MCPPanel, TodoPanel, CostPanel
from djcode.tui_theme import (
    DJCODE_CSS,
    ERROR,
    GOLD,
    INFO,
    SUCCESS,
    THINKING,
    WARNING,
)


# ── All available slash commands ────────────────────────────────────────

COMMAND_REGISTRY: list[tuple[str, str]] = [
    ("/help", "Show help overlay"),
    ("/model", "Switch model (fuzzy match)"),
    ("/models", "List available models"),
    ("/provider", "Switch LLM provider"),
    ("/auth", "Configure provider + API key"),
    ("/clear", "Clear conversation history"),
    ("/save", "Save conversation to disk"),
    ("/config", "Show current configuration"),
    ("/set", "Set a config value (key=value)"),
    ("/auto", "Toggle auto-accept tool calls"),
    ("/thinking", "Toggle thinking display"),
    ("/plan", "Toggle plan/act mode"),
    ("/agents", "Show all 22 agents roster"),
    ("/scout", "Read-only codebase exploration"),
    ("/architect", "Generate implementation plan"),
    ("/orchestra", "Multi-agent orchestration"),
    ("/review", "Code review (Dharma agent)"),
    ("/debug", "Root cause analysis (Sherlock)"),
    ("/test", "Write tests (Agni agent)"),
    ("/refactor", "Restructure code (Shiva)"),
    ("/devops", "Docker/CI/CD (Vayu agent)"),
    ("/docs", "Generate documentation (Saraswati)"),
    ("/launch", "Build + Ship + Campaign pipeline"),
    ("/campaign", "Content campaign (12 agents)"),
    ("/image", "Image prompts (Maya)"),
    ("/video", "Cinematic video prompts (Kubera)"),
    ("/social", "Social media content (Chitragupta)"),
    ("/memory", "Show memory stats"),
    ("/remember", "Store a persistent fact (key=value)"),
    ("/recall", "Recall a persistent fact"),
    ("/forget", "Remove a persistent fact"),
    ("/stats", "Usage dashboard"),
    ("/extension", "Manage MCP extensions"),
    ("/recipe", "Run/list/create recipes"),
    ("/history", "Browse past sessions"),
    ("/resume", "Resume a past session by ID"),
    ("/uncensored", "Show uncensored model info"),
    ("/raw", "Toggle raw output mode"),
    ("/shortcuts", "Show keyboard shortcuts"),
    ("/todo", "Manage session todos (add/done/rm/list)"),
    ("/cost", "Show token cost estimates"),
    ("/exit", "Quit DJcode"),
]


# ── Help overlay screen ────────────────────────────────────────────────

HELP_TEXT = """\
[bold #FFD700]Keyboard Shortcuts[/]

  [cyan]j / k[/]      Scroll chat down / up
  [cyan]g[/]          Jump to top of chat
  [cyan]G[/]          Jump to bottom of chat
  [cyan]/[/]          Search in chat (when not in input)
  [cyan]Escape[/]     Return focus to input
  [cyan]Tab[/]        Cycle panel focus
  [cyan]Ctrl+O[/]     Toggle thinking display
  [cyan]Ctrl+P[/]     Toggle Plan / Act mode
  [cyan]Ctrl+L[/]     Clear chat history
  [cyan]Ctrl+T[/]     Toggle auto-accept tools
  [cyan]Ctrl+R[/]     Rerun last message
  [cyan]Ctrl+K[/]     Cancel generation
  [cyan]Ctrl+Q[/]     Quit DJcode TUI
  [cyan]F1[/]         This help screen
  [cyan]F2[/]         Agent roster
  [cyan]F3[/]         Command palette

[bold #FFD700]Slash Commands[/]

  [cyan]/[/]               Command palette (fuzzy search)
  [cyan]/model[/] <name>    Switch model
  [cyan]/provider[/] <name> Switch provider
  [cyan]/clear[/]           Clear conversation
  [cyan]/agents[/]          Show agent roster
  [cyan]/stats[/]           Usage dashboard
  [cyan]/scout[/] <query>   Codebase exploration
  [cyan]/orchestra[/] <task> Multi-agent dispatch
  [cyan]/review[/]          Code review
  [cyan]/debug[/]           Root cause analysis
  [cyan]/test[/]            Generate tests
  [cyan]/memory[/]          Memory stats
  [cyan]/extension[/]       MCP extensions
  [cyan]/recipe[/]          Run recipes
  [cyan]/help[/]            Show this help
  [cyan]/exit[/]            Quit

[dim]Press Escape to close[/]
"""


class HelpScreen(ModalScreen[None]):
    """Modal help overlay."""

    BINDINGS = [Binding("escape", "dismiss", "Close")]

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.85);
    }
    #help-box {
        width: 68;
        height: auto;
        max-height: 85%;
        background: #111111;
        border: double #FFD700;
        padding: 1 2;
        overflow-y: auto;
    }
    """

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="help-box"):
            yield Static(HELP_TEXT)


# ── Agents overlay screen ──────────────────────────────────────────────

AGENTS_TEXT = """\
[bold #FFD700]DJcode Agent Roster[/]

[bold]Build Agents[/]
  [cyan]Operator[/]    Default — general coding
  [cyan]Dharma[/]      Code review & quality
  [cyan]Sherlock[/]    Debugging & root cause analysis
  [cyan]Agni[/]        Test generation
  [cyan]Shiva[/]       Refactoring & restructuring
  [cyan]Vayu[/]        DevOps, Docker, CI/CD
  [cyan]Saraswati[/]   Documentation

[bold]Content Agents[/]
  [cyan]Maya[/]        Image generation prompts
  [cyan]Kubera[/]      Video / cinematic prompts
  [cyan]Chitragupta[/] Social media content
  [cyan]Campaign Dir[/] Full launch campaigns

[bold]Specialist Agents[/]
  [cyan]Scout[/]       Read-only codebase exploration
  [cyan]Architect[/]   Implementation planning

[dim]Use /orchestra for auto-dispatch or specific agent commands.[/]
[dim]Press Escape to close[/]
"""


class AgentsScreen(ModalScreen[None]):
    """Modal agent roster overlay."""

    BINDINGS = [Binding("escape", "dismiss", "Close")]

    DEFAULT_CSS = """
    AgentsScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.85);
    }
    #agents-box {
        width: 64;
        height: auto;
        max-height: 80%;
        background: #111111;
        border: double #FFD700;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="agents-box"):
            yield Static(AGENTS_TEXT)


# ── Interactive Model Picker ────────────────────────────────────────────


class ModelPicker(ModalScreen[str | None]):
    """Interactive model selection with fuzzy search — KiloCode-style."""

    BINDINGS = [Binding("escape", "dismiss", "Close")]

    DEFAULT_CSS = """
    ModelPicker {
        align: center middle;
        background: rgba(0, 0, 0, 0.85);
    }
    #model-box {
        width: 76;
        height: 36;
        background: #141414;
        border: double #FFD700;
        padding: 1 2;
    }
    #model-title {
        height: 1;
        color: #FFD700;
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }
    #model-search {
        height: 3;
        background: #1a1a1a;
        color: #FFD700;
        border: solid #2a2a2a;
        margin-bottom: 1;
    }
    #model-search:focus {
        border: solid #FFD700;
    }
    #model-info {
        height: 1;
        color: #6f6f6f;
        padding: 0 1;
        margin-bottom: 1;
    }
    #model-list {
        height: 1fr;
        background: #101010;
        scrollbar-color: #2a2a2a;
        scrollbar-color-hover: #FFD700;
    }
    #model-list > .option-list--option-highlighted {
        background: #FFD700 20%;
        color: #FFD700;
    }
    #model-list > .option-list--option {
        padding: 0 1;
    }
    """

    def __init__(self, provider_name: str = "ollama", base_url: str = "") -> None:
        super().__init__()
        self._provider_name = provider_name
        self._base_url = base_url
        self._models: list[dict] = []
        self._recent: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="model-box"):
            yield Static("Select Model", id="model-title")
            yield Input(
                id="model-search",
                placeholder="Search models... (type to filter)",
            )
            yield Static(
                f"  Provider: {self._provider_name}  |  Arrow keys + Enter to select",
                id="model-info",
            )
            yield OptionList(id="model-list")

    def on_mount(self) -> None:
        self.query_one("#model-search", Input).focus()
        self.run_worker(self._load_models())

    async def _load_models(self) -> None:
        """Fetch models from the current provider."""
        option_list = self.query_one("#model-list", OptionList)

        # Load recent models from config
        from djcode.config import load_config
        cfg = load_config()
        self._recent = cfg.get("recent_models", [])

        if self._provider_name == "ollama":
            try:
                from djcode.provider import fetch_ollama_models_sync
                url = self._base_url or cfg.get("ollama_url", "http://localhost:11434")
                models = fetch_ollama_models_sync(url)
                self._models = models or []
            except Exception:
                self._models = []
        else:
            # For non-Ollama providers, show common models
            provider_models = {
                "openai": [
                    "gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4",
                    "o1", "o1-mini", "o1-preview", "o3-mini",
                ],
                "anthropic": [
                    "claude-sonnet-4-6", "claude-opus-4-6",
                    "claude-haiku-4-5-20251001",
                    "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022",
                ],
                "google": [
                    "gemini-2.5-pro", "gemini-2.5-flash",
                    "gemini-2.0-flash", "gemini-1.5-pro",
                ],
                "nvidia": [
                    "deepseek-ai/deepseek-r1", "google/gemma-3-27b-it",
                    "meta/llama-3.3-70b-instruct",
                ],
                "groq": [
                    "llama-3.3-70b-versatile", "llama-3.1-8b-instant",
                    "mixtral-8x7b-32768", "gemma2-9b-it",
                ],
                "together": [
                    "meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo",
                    "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
                    "mistralai/Mixtral-8x22B-Instruct-v0.1",
                ],
                "openrouter": [
                    "anthropic/claude-sonnet-4-6",
                    "openai/gpt-4o", "google/gemini-2.5-pro",
                    "meta-llama/llama-3.3-70b-instruct",
                ],
            }
            model_names = provider_models.get(self._provider_name, [])
            self._models = [{"name": m} for m in model_names]

        self._render_models("")

    def _render_models(self, query: str) -> None:
        """Render the model list with optional search filter."""
        option_list = self.query_one("#model-list", OptionList)
        option_list.clear_options()

        query_lower = query.lower().strip()

        # Separate into recent and all
        recent_matches = []
        all_matches = []

        for m in self._models:
            name = m.get("name", "")
            if query_lower and query_lower not in name.lower():
                continue
            if name in self._recent:
                recent_matches.append(name)
            else:
                all_matches.append(name)

        # Also allow free-text entry if query doesn't match anything
        if recent_matches:
            option_list.add_option(Option("── Recent ──", disabled=True))
            for name in recent_matches:
                size = self._get_size(name)
                label = f"  {name:<40} {size}" if size else f"  {name}"
                option_list.add_option(Option(label, id=name))

        if all_matches:
            header = "── All Models ──" if recent_matches else f"── {self._provider_name.capitalize()} Models ──"
            option_list.add_option(Option(header, disabled=True))
            for name in all_matches:
                size = self._get_size(name)
                label = f"  {name:<40} {size}" if size else f"  {name}"
                option_list.add_option(Option(label, id=name))

        if not recent_matches and not all_matches and query_lower:
            # Allow custom model name entry
            option_list.add_option(
                Option(f"  Use custom: {query_lower}", id=query_lower)
            )

    def _get_size(self, model_name: str) -> str:
        """Get model size if available."""
        for m in self._models:
            if m.get("name") == model_name:
                size = m.get("size", 0)
                if size:
                    try:
                        from djcode.provider import format_model_size
                        return format_model_size(size)
                    except Exception:
                        pass
        return ""

    @on(Input.Changed, "#model-search")
    def filter_models(self, event: Input.Changed) -> None:
        self._render_models(event.value)

    @on(OptionList.OptionSelected, "#model-list")
    def select_model(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            model = str(event.option.id)
            # Save to recent
            self._save_recent(model)
            self.dismiss(model)

    @on(Input.Submitted, "#model-search")
    def submit_search(self, event: Input.Submitted) -> None:
        """Enter in search: select first match or use as custom model name."""
        option_list = self.query_one("#model-list", OptionList)
        if option_list.option_count > 0:
            for i in range(option_list.option_count):
                opt = option_list.get_option_at_index(i)
                if not opt.disabled and opt.id:
                    self._save_recent(str(opt.id))
                    self.dismiss(str(opt.id))
                    return
        # Use raw input as model name
        if event.value.strip():
            self._save_recent(event.value.strip())
            self.dismiss(event.value.strip())

    def _save_recent(self, model: str) -> None:
        """Add model to recent list (max 10)."""
        try:
            from djcode.config import load_config, save_config
            cfg = load_config()
            recent = cfg.get("recent_models", [])
            if model in recent:
                recent.remove(model)
            recent.insert(0, model)
            cfg["recent_models"] = recent[:10]
            save_config(cfg)
        except Exception:
            pass


# ── Interactive Provider Picker ────────────────────────────────────────


class ProviderPicker(ModalScreen[dict | None]):
    """Interactive provider selection with API key input — KiloCode-style."""

    BINDINGS = [Binding("escape", "dismiss", "Close")]

    DEFAULT_CSS = """
    ProviderPicker {
        align: center middle;
        background: rgba(0, 0, 0, 0.85);
    }
    #provider-box {
        width: 72;
        height: 30;
        background: #141414;
        border: double #FFD700;
        padding: 1 2;
    }
    #provider-title {
        height: 1;
        color: #FFD700;
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }
    #provider-search {
        height: 3;
        background: #1a1a1a;
        color: #FFD700;
        border: solid #2a2a2a;
        margin-bottom: 1;
    }
    #provider-search:focus {
        border: solid #FFD700;
    }
    #provider-list {
        height: 1fr;
        background: #101010;
        scrollbar-color: #2a2a2a;
        scrollbar-color-hover: #FFD700;
    }
    #provider-list > .option-list--option-highlighted {
        background: #FFD700 20%;
        color: #FFD700;
    }
    #provider-list > .option-list--option {
        padding: 0 1;
    }
    #provider-url-section {
        height: auto;
        background: #141414;
        padding: 1;
        margin-top: 1;
        display: none;
    }
    #provider-url-input {
        height: 3;
        background: #1a1a1a;
        color: #ededed;
        border: solid #2a2a2a;
    }
    #provider-url-input:focus {
        border: solid #FFD700;
    }
    #provider-key-input {
        height: 3;
        background: #1a1a1a;
        color: #ededed;
        border: solid #2a2a2a;
        margin-top: 1;
    }
    #provider-key-input:focus {
        border: solid #FFD700;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._providers: list[tuple[str, str, str]] = []  # (id, name, description)
        self._selected_provider: str | None = None
        self._mode = "select"  # "select" or "configure"

    def compose(self) -> ComposeResult:
        with Vertical(id="provider-box"):
            yield Static("Select Provider", id="provider-title")
            yield Input(
                id="provider-search",
                placeholder="Search providers...",
            )
            yield OptionList(id="provider-list")
            with Vertical(id="provider-url-section"):
                yield Static("", id="provider-config-label")
                yield Input(
                    id="provider-url-input",
                    placeholder="API Base URL (e.g. https://api.openai.com/v1)",
                )
                yield Input(
                    id="provider-key-input",
                    placeholder="API Key (leave empty to use env var)",
                    password=True,
                )

    def on_mount(self) -> None:
        self._load_providers()
        self.query_one("#provider-search", Input).focus()

    def _load_providers(self) -> None:
        """Load available providers."""
        try:
            from djcode.auth import PROVIDERS
            self._providers = [
                (pid, info.get("name", pid), info.get("description", ""))
                for pid, info in PROVIDERS.items()
            ]
        except Exception:
            self._providers = [
                ("ollama", "Ollama (Local)", "Local inference, no API key needed"),
                ("openai", "OpenAI", "GPT-4o, o1, o3 models"),
                ("anthropic", "Anthropic", "Sonnet, Opus, Haiku models"),
                ("custom", "Custom URL", "Any OpenAI-compatible endpoint"),
            ]
        self._render_providers("")

    def _render_providers(self, query: str) -> None:
        option_list = self.query_one("#provider-list", OptionList)
        option_list.clear_options()

        query_lower = query.lower().strip()

        # Custom URL option always first
        if not query_lower or "custom" in query_lower or "url" in query_lower:
            option_list.add_option(
                Option("  + Custom URL          Any OpenAI-compatible endpoint", id="__custom_url__")
            )
            option_list.add_option(Option("── Providers ──", disabled=True))

        for pid, name, desc in self._providers:
            if pid == "custom":
                continue  # Already shown as Custom URL
            if query_lower and query_lower not in name.lower() and query_lower not in pid.lower():
                continue
            needs_key = ""
            try:
                from djcode.auth import PROVIDERS
                if PROVIDERS.get(pid, {}).get("needs_key"):
                    needs_key = " [key]"
            except Exception:
                pass
            label = f"  {name:<24}{needs_key:<8}{desc}"
            option_list.add_option(Option(label, id=pid))

    @on(Input.Changed, "#provider-search")
    def filter_providers(self, event: Input.Changed) -> None:
        if self._mode == "select":
            self._render_providers(event.value)

    @on(OptionList.OptionSelected, "#provider-list")
    def select_provider(self, event: OptionList.OptionSelected) -> None:
        if not event.option.id:
            return

        provider_id = str(event.option.id)

        if provider_id == "__custom_url__":
            # Show URL + key inputs
            self._mode = "configure"
            self._selected_provider = "custom"
            url_section = self.query_one("#provider-url-section")
            url_section.styles.display = "block"
            self.query_one("#provider-config-label", Static).update(
                f"[bold #FFD700]Configure Custom Endpoint[/]"
            )
            self.query_one("#provider-url-input", Input).focus()
            return

        # Check if provider needs API key
        needs_key = False
        try:
            from djcode.auth import PROVIDERS, get_api_key
            info = PROVIDERS.get(provider_id, {})
            needs_key = info.get("needs_key", False)
            existing_key = get_api_key(provider_id) if needs_key else ""
        except Exception:
            existing_key = ""

        if needs_key and not existing_key:
            # Show key input
            self._mode = "configure"
            self._selected_provider = provider_id
            url_section = self.query_one("#provider-url-section")
            url_section.styles.display = "block"
            try:
                name = PROVIDERS.get(provider_id, {}).get("name", provider_id)
            except Exception:
                name = provider_id
            self.query_one("#provider-config-label", Static).update(
                f"[bold #FFD700]Configure {name}[/]\n[dim]Enter your API key:[/]"
            )
            url_input = self.query_one("#provider-url-input", Input)
            url_input.styles.display = "none"
            self.query_one("#provider-key-input", Input).focus()
            return

        # Provider ready — dismiss with selection
        self.dismiss({"provider": provider_id})

    @on(Input.Submitted, "#provider-url-input")
    def submit_url(self, event: Input.Submitted) -> None:
        """After entering URL, focus the key input."""
        self.query_one("#provider-key-input", Input).focus()

    @on(Input.Submitted, "#provider-key-input")
    def submit_key(self, event: Input.Submitted) -> None:
        """After entering key, dismiss with full config."""
        url = self.query_one("#provider-url-input", Input).value.strip()
        key = event.value.strip()

        if self._selected_provider == "custom" and not url:
            return  # Need URL for custom

        result: dict = {"provider": self._selected_provider or "custom"}
        if url:
            result["base_url"] = url
        if key:
            result["api_key"] = key

        # Save key to config if provided
        if key and self._selected_provider:
            try:
                from djcode.config import load_config, save_config
                cfg = load_config()
                key_field = f"{self._selected_provider}_api_key"
                cfg[key_field] = key
                if url:
                    cfg["base_url"] = url
                save_config(cfg)
            except Exception:
                pass

        self.dismiss(result)

    @on(Input.Submitted, "#provider-search")
    def submit_search(self, event: Input.Submitted) -> None:
        if self._mode == "select":
            option_list = self.query_one("#provider-list", OptionList)
            if option_list.option_count > 0:
                for i in range(option_list.option_count):
                    opt = option_list.get_option_at_index(i)
                    if not opt.disabled and opt.id:
                        self.select_provider(
                            OptionList.OptionSelected(option_list, opt, i)
                        )
                        return


# ── Command Palette overlay ────────────────────────────────────────────


class CommandPalette(ModalScreen[str | None]):
    """Fuzzy-searchable command palette."""

    BINDINGS = [Binding("escape", "dismiss", "Close")]

    DEFAULT_CSS = """
    CommandPalette {
        align: center middle;
        background: rgba(0, 0, 0, 0.85);
    }
    #palette-box {
        width: 72;
        height: 32;
        background: #111111;
        border: double #FFD700;
        padding: 1 2;
    }
    #palette-title {
        height: 1;
        color: #FFD700;
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }
    #palette-input {
        height: 3;
        background: #1a1a1a;
        color: #FFD700;
        border: solid #333333;
        margin-bottom: 1;
    }
    #palette-input:focus {
        border: solid #FFD700;
    }
    #palette-list {
        height: 1fr;
        background: #0a0a0a;
        scrollbar-color: #333333;
        scrollbar-color-hover: #FFD700;
    }
    #palette-list > .option-list--option-highlighted {
        background: #FFD700 20%;
        color: #FFD700;
    }
    #palette-list > .option-list--option {
        padding: 0 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._all_commands = COMMAND_REGISTRY

    def compose(self) -> ComposeResult:
        with Vertical(id="palette-box"):
            yield Static("Command Palette", id="palette-title")
            yield Input(
                id="palette-input",
                placeholder="Type to filter commands...",
            )
            yield OptionList(
                *[
                    Option(f"{cmd:<20} {desc}", id=cmd)
                    for cmd, desc in self._all_commands
                ],
                id="palette-list",
            )

    def on_mount(self) -> None:
        self.query_one("#palette-input", Input).focus()

    @on(Input.Changed, "#palette-input")
    def filter_commands(self, event: Input.Changed) -> None:
        """Filter the command list based on input."""
        query = event.value.lower().strip()
        option_list = self.query_one("#palette-list", OptionList)
        option_list.clear_options()

        for cmd, desc in self._all_commands:
            if not query or query in cmd.lower() or query in desc.lower():
                option_list.add_option(Option(f"{cmd:<20} {desc}", id=cmd))

    @on(OptionList.OptionSelected, "#palette-list")
    def select_command(self, event: OptionList.OptionSelected) -> None:
        """User selected a command from the palette."""
        if event.option.id:
            self.dismiss(str(event.option.id))

    @on(Input.Submitted, "#palette-input")
    def submit_filter(self, event: Input.Submitted) -> None:
        """On Enter in the filter input, select first visible option."""
        option_list = self.query_one("#palette-list", OptionList)
        if option_list.option_count > 0:
            opt = option_list.get_option_at_index(0)
            if opt.id:
                self.dismiss(str(opt.id))


# ── Search overlay ─────────────────────────────────────────────────────


class SearchBar(ModalScreen[str | None]):
    """Simple search bar overlay."""

    BINDINGS = [Binding("escape", "dismiss", "Close")]

    DEFAULT_CSS = """
    SearchBar {
        align: center top;
        background: transparent;
    }
    #search-box {
        width: 60;
        margin-top: 2;
        background: #111111;
        border: solid #FFD700;
        padding: 0 1;
        height: 3;
    }
    #search-input {
        background: #111111;
        color: #FFD700;
        height: 3;
    }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="search-box"):
            yield Static("/", id="search-prefix")
            yield Input(id="search-input", placeholder="Search chat...")

    def on_mount(self) -> None:
        self.query_one("#search-input", Input).focus()

    @on(Input.Submitted, "#search-input")
    def submit_search(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)


# ── Main application ───────────────────────────────────────────────────


class DJcodeApp(App):
    """DJcode split-pane TUI application.

    Lazygit-style layout with chat on the left and tabbed sidebar
    on the right. Streams LLM responses, vim keys, command palette.
    """

    CSS = DJCODE_CSS
    TITLE = "DJcode"
    SUB_TITLE = f"v{__version__}"

    BINDINGS = [
        Binding("ctrl+o", "toggle_thinking", "Thinking", show=True),
        Binding("ctrl+p", "toggle_plan", "Plan/Act", show=True),
        Binding("ctrl+l", "clear_chat", "Clear", show=True),
        Binding("ctrl+t", "toggle_auto", "Auto", show=True),
        Binding("ctrl+r", "rerun", "Rerun", show=False),
        Binding("ctrl+k", "cancel", "Cancel", show=False),
        Binding("ctrl+q", "quit", "Quit", show=True),
        Binding("f1", "show_help", "Help", show=True),
        Binding("f2", "show_model_picker", "Model", show=True),
        Binding("f3", "show_provider_picker", "Provider", show=True),
        Binding("f4", "show_palette", "Cmds", show=True),
        Binding("f5", "show_agents", "Agents", show=False),
        Binding("tab", "focus_next", "Focus Next", show=False),
        # Vim keys — only active when chat-log is focused
        Binding("j", "scroll_down", "Down", show=False),
        Binding("k", "scroll_up", "Up", show=False),
        Binding("g", "scroll_top", "Top", show=False),
        Binding("G", "scroll_bottom", "Bottom", show=False, key_display="shift+g"),
        Binding("i", "focus_input", "Input", show=False),
        Binding("escape", "focus_input", "Input", show=False),
    ]

    def __init__(
        self,
        *,
        provider_name: str | None = None,
        model_name: str | None = None,
        bypass_rlhf: bool = False,
        auto_accept: bool = False,
        show_thinking: bool = True,
    ) -> None:
        super().__init__()
        self._provider_name = provider_name
        self._model_name = model_name
        self._bypass_rlhf = bypass_rlhf
        self._auto_accept = auto_accept
        self._show_thinking = show_thinking
        self._plan_mode = False
        self._token_count = 0
        self._tokens_in = 0
        self._tokens_out = 0
        self._session_start = time.time()
        self._active_agent = "Operator"
        self._is_generating = False
        self._cancel_requested = False
        self._last_input = ""
        self._provider: Any = None
        self._operator: Any = None
        self._memory: Any = None
        self._orchestrator: Any = None
        self._ext_manager: Any = None
        self._session_db: Any = None
        self._sqlite_session_id: str | None = None
        self._response_times: list[float] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-layout"):
            with Vertical(id="chat-panel"):
                yield RichLog(
                    id="chat-log",
                    markup=True,
                    wrap=True,
                    highlight=True,
                    auto_scroll=True,
                )
                yield OptionList(id="cmd-suggest")
                yield Input(
                    id="prompt-input",
                    placeholder="\u276f Type a message... (/help for commands, / for palette)",
                )
            yield SidePanel(project_path=Path.cwd(), id="side-panel")
        yield Static(self._build_status_text(), id="status-bar")
        yield Footer()

    # ── Status bar helpers ────────────────────────────────────────────────

    def _build_status_text(self) -> str:
        """Build the status bar content string."""
        if self._provider and hasattr(self._provider, "config"):
            model = getattr(self._provider.config, "model", None) or self._model_name or "no model"
            provider = getattr(self._provider.config, "name", None) or self._provider_name or "none"
        else:
            model = self._model_name or "loading..."
            provider = self._provider_name or "..."

        tokens_in = self._tokens_in
        tokens_out = self._tokens_out
        in_str = f"{tokens_in / 1000:.1f}k" if tokens_in >= 1000 else str(tokens_in)
        out_str = f"{tokens_out / 1000:.1f}k" if tokens_out >= 1000 else str(tokens_out)

        elapsed = int(time.time() - self._session_start)
        mins, secs = divmod(elapsed, 60)
        hrs, mins = divmod(mins, 60)
        if hrs:
            duration = f"{hrs}h {mins:02d}m {secs:02d}s"
        else:
            duration = f"{mins}m {secs:02d}s"

        mode = "PLAN" if self._plan_mode else "ACT"
        think = "ON" if self._show_thinking else "OFF"
        auto = "ON" if self._auto_accept else "OFF"

        return (
            f"  {model} | {provider} | "
            f"\u2191{in_str} \u2193{out_str} tokens | "
            f"{duration} | {mode} | "
            f"Think: {think} | Auto: {auto}"
        )

    def _refresh_status_bar(self) -> None:
        """Update the status bar widget text."""
        try:
            bar = self.query_one("#status-bar", Static)
            bar.update(self._build_status_text())
        except Exception:
            pass

    async def on_mount(self) -> None:
        """Initialize provider, operator, and welcome message on mount."""
        chat = self.query_one("#chat-log", RichLog)
        chat.write(
            f"[bold {GOLD}]\u23fa DJcode[/] [dim]v{__version__}[/]  "
            f"[dim]Local-first AI coding CLI by DarshJ.AI[/]\n"
        )

        # Focus the input by default
        self.query_one("#prompt-input", Input).focus()

        # Start status bar refresh timer (every 1 second)
        self.set_interval(1.0, self._refresh_status_bar)

        # Initialize everything in background
        self.run_worker(self._initialize(), exclusive=True)

    async def _initialize(self) -> None:
        """Set up Provider, Operator, Memory, Orchestrator."""
        chat = self.query_one("#chat-log", RichLog)
        side = self.query_one(SidePanel)

        try:
            from djcode.provider import Provider, ProviderConfig

            config = ProviderConfig.from_config(
                provider_override=self._provider_name,
                model_override=self._model_name,
            )
            self._provider = Provider(config)

            # Validate model
            ok, msg = self._provider.validate_model()
            if msg:
                side.agent_panel.add_tool_call("validate_model", "warning")
            if not ok:
                chat.write(f"[{ERROR}]Model error: {msg}[/]")
                return

            # Create operator
            from djcode.agents.operator import Operator

            self._operator = Operator(
                self._provider,
                bypass_rlhf=self._bypass_rlhf,
                raw=True,
                model=self._provider.config.model,
                auto_accept=self._auto_accept,
                show_thinking=False,
            )
            self._operator.auto_accept = self._auto_accept

            # Initialize memory manager
            try:
                from djcode.memory.manager import MemoryManager
                self._memory = MemoryManager()
            except Exception:
                self._memory = None

            # Initialize orchestrator
            try:
                from djcode.orchestrator import Orchestrator
                self._orchestrator = Orchestrator(self._provider)
            except Exception:
                self._orchestrator = None

            # Initialize session DB
            try:
                from djcode.sessions import SessionDB
                self._session_db = SessionDB()
                session_data = self._session_db.create_session(
                    model=self._provider.config.model,
                    provider=self._provider.config.name,
                )
                self._sqlite_session_id = session_data.id if session_data else None
            except Exception:
                self._session_db = None

            # Initialize extension manager
            try:
                from djcode.extensions import ExtensionManager
                self._ext_manager = ExtensionManager()
                # Load extension statuses into the MCP panel
                statuses = self._ext_manager.get_status()
                side.mcp_panel.load_extensions(statuses)
            except Exception:
                self._ext_manager = None

            model = self._provider.config.model
            prov = self._provider.config.name
            self.sub_title = f"v{__version__} | {model} | {prov}"

            chat.write(
                f"  [dim]Model:[/]    [{GOLD}]{model}[/]\n"
                f"  [dim]Provider:[/] [{GOLD}]{prov}[/]\n"
                f"  [dim]Mode:[/]     [{GOLD}]{'PLAN' if self._plan_mode else 'ACT'}[/]\n"
                f"  [dim]Thinking:[/] [{GOLD}]{'ON' if self._show_thinking else 'OFF'}[/]\n"
            )
            chat.write(f"[dim]Ready. Type a message, /help, or press F3 for command palette.[/]\n")

            # Update sidebar panels
            side.agent_panel.set_agent("Operator", "General")
            side.stats_panel.update_stats(model=model, provider=prov)
            side.agent_panel.add_tool_call("init_provider", "ok")

            # Update memory stats
            if self._memory:
                stats = self._memory.stats
                side.agent_panel.update_memory(
                    session=stats.get("session_messages", 0),
                    facts=stats.get("persistent_facts", 0),
                    vectors=stats.get("facts_with_embeddings", 0),
                )

        except Exception as e:
            chat.write(f"[{ERROR}]Initialization error: {e}[/]")
            import traceback
            chat.write(f"[dim]{traceback.format_exc()[:500]}[/]")

    # ── Vim key actions (only when chat-log focused) ─────────────────────

    def action_scroll_down(self) -> None:
        """Vim j — scroll chat down."""
        focused = self.focused
        if focused and focused.id == "prompt-input":
            return  # Don't intercept typing
        try:
            chat = self.query_one("#chat-log", RichLog)
            chat.scroll_down(animate=False)
        except Exception:
            pass

    def action_scroll_up(self) -> None:
        """Vim k — scroll chat up."""
        focused = self.focused
        if focused and focused.id == "prompt-input":
            return
        try:
            chat = self.query_one("#chat-log", RichLog)
            chat.scroll_up(animate=False)
        except Exception:
            pass

    def action_scroll_top(self) -> None:
        """Vim g — scroll to top."""
        focused = self.focused
        if focused and focused.id == "prompt-input":
            return
        try:
            chat = self.query_one("#chat-log", RichLog)
            chat.scroll_home(animate=False)
        except Exception:
            pass

    def action_scroll_bottom(self) -> None:
        """Vim G — scroll to bottom."""
        focused = self.focused
        if focused and focused.id == "prompt-input":
            return
        try:
            chat = self.query_one("#chat-log", RichLog)
            chat.scroll_end(animate=False)
        except Exception:
            pass

    def action_focus_input(self) -> None:
        """Focus the prompt input (Escape / i)."""
        self.query_one("#prompt-input", Input).focus()

    # ── Slash command autocomplete ──────────────────────────────────────

    @on(Input.Changed, "#prompt-input")
    def _on_prompt_changed(self, event: Input.Changed) -> None:
        """Show/hide slash command suggestions as user types."""
        text = event.value
        suggest = self.query_one("#cmd-suggest", OptionList)

        if text.startswith("/") and len(text) > 0:
            query = text.lower()
            matches = [
                (cmd, desc)
                for cmd, desc in COMMAND_REGISTRY
                if query in cmd.lower() or query[1:] in desc.lower()
            ]
            suggest.clear_options()
            if matches:
                for cmd, desc in matches:
                    suggest.add_option(Option(f"{cmd:<16} {desc}", id=cmd))
                suggest.styles.display = "block"
                # Highlight first option
                if suggest.option_count > 0:
                    suggest.highlighted = 0
            else:
                suggest.styles.display = "none"
        else:
            suggest.styles.display = "none"

    def _select_suggestion(self) -> None:
        """Fill the input with the currently highlighted suggestion."""
        suggest = self.query_one("#cmd-suggest", OptionList)
        if suggest.highlighted is not None and suggest.option_count > 0:
            option = suggest.get_option_at_index(suggest.highlighted)
            inp = self.query_one("#prompt-input", Input)
            # option.id holds the command string like "/help"
            cmd = str(option.id) if option.id else ""
            if cmd:
                inp.value = cmd + " "
                inp.cursor_position = len(inp.value)
        suggest.styles.display = "none"

    def on_key(self, event) -> None:
        """Intercept keys when suggestion list is visible."""
        suggest = self.query_one("#cmd-suggest", OptionList)
        if suggest.styles.display == "none":
            return

        if event.key == "up":
            event.prevent_default()
            event.stop()
            if suggest.highlighted is not None and suggest.highlighted > 0:
                suggest.highlighted = suggest.highlighted - 1
            elif suggest.option_count > 0:
                suggest.highlighted = suggest.option_count - 1

        elif event.key == "down":
            event.prevent_default()
            event.stop()
            if suggest.highlighted is not None and suggest.highlighted < suggest.option_count - 1:
                suggest.highlighted = suggest.highlighted + 1
            elif suggest.option_count > 0:
                suggest.highlighted = 0

        elif event.key in ("enter", "tab"):
            event.prevent_default()
            event.stop()
            self._select_suggestion()

        elif event.key == "escape":
            event.prevent_default()
            event.stop()
            suggest.styles.display = "none"

    # ── Input handling ───────────────────────────────────────────────────

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle user input submission."""
        if event.input.id != "prompt-input":
            return

        # Hide suggestion list on submit
        self.query_one("#cmd-suggest", OptionList).styles.display = "none"

        text = event.value.strip()
        if not text:
            return

        inp = self.query_one("#prompt-input", Input)
        inp.value = ""

        # Bare "/" triggers command palette
        if text == "/":
            self.action_show_palette()
            return

        # Slash commands
        if text.startswith("/"):
            await self._handle_slash_command(text)
            return

        # Normal chat message
        self._last_input = text
        self.run_worker(self._send_message(text), exclusive=True)

    async def _handle_slash_command(self, text: str) -> None:
        """Route slash commands — ported from classic REPL."""
        chat = self.query_one("#chat-log", RichLog)
        side = self.query_one(SidePanel)
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "/help":
            self.push_screen(HelpScreen())

        elif cmd == "/clear":
            self.action_clear_chat()

        elif cmd in ("/exit", "/quit", "/q"):
            self.exit()

        elif cmd == "/agents":
            self.push_screen(AgentsScreen())

        elif cmd == "/model":
            if arg:
                await self._handle_model_switch(arg)
            else:
                # Interactive model picker
                self._show_model_picker()

        elif cmd == "/models":
            # Also opens interactive picker (no arg = picker, with arg = direct switch)
            self._show_model_picker()

        elif cmd == "/provider":
            if arg:
                await self._handle_provider_switch(arg)
            else:
                # Interactive provider picker
                self._show_provider_picker()

        elif cmd == "/config":
            self._show_config()

        elif cmd == "/set":
            self._handle_set(arg)

        elif cmd == "/stats":
            self._show_session_stats()

        elif cmd == "/thinking":
            self.action_toggle_thinking()

        elif cmd == "/plan":
            self.action_toggle_plan()

        elif cmd == "/auto":
            self.action_toggle_auto()

        elif cmd == "/shortcuts":
            self.push_screen(HelpScreen())

        elif cmd == "/save":
            self._handle_save()

        elif cmd == "/memory":
            self._show_memory_stats()

        elif cmd == "/remember":
            self._handle_remember(arg)

        elif cmd == "/recall":
            self._handle_recall(arg)

        elif cmd == "/forget":
            self._handle_forget(arg)

        elif cmd == "/uncensored":
            self._show_uncensored_info()

        elif cmd == "/raw":
            if self._operator:
                self._operator.raw = not self._operator.raw
                state = "ON" if self._operator.raw else "OFF"
                chat.write(f"[dim]Raw mode: {state}[/]")

        elif cmd == "/extension":
            await self._handle_extension(arg)

        elif cmd == "/recipe":
            await self._handle_recipe(arg)

        elif cmd == "/history":
            self._handle_history(arg)

        elif cmd == "/resume":
            self._handle_resume(arg)

        elif cmd == "/docs":
            self._handle_docs(arg)

        elif cmd == "/todo":
            self._handle_todo(arg)

        elif cmd == "/cost":
            self._show_cost()

        # Agent dispatch commands — send to orchestrator or operator
        elif cmd in (
            "/scout", "/architect", "/orchestra", "/review", "/debug",
            "/test", "/refactor", "/devops", "/launch", "/campaign",
            "/image", "/video", "/social",
        ):
            await self._handle_agent_command(cmd, arg)

        elif cmd == "/auth":
            chat.write(
                f"[{WARNING}]Auth requires interactive input.[/]\n"
                "[dim]Set API keys via config: /set remote_api_key=sk-...[/]\n"
                "[dim]Or env vars: OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.[/]"
            )

        else:
            # Unrecognized — try as agent task
            if self._operator:
                self._last_input = text
                self.run_worker(self._send_message(text), exclusive=True)
            else:
                chat.write(f"[{WARNING}]Unknown command: {cmd}[/]")

    # ── Agent dispatch ───────────────────────────────────────────────────

    async def _handle_agent_command(self, cmd: str, arg: str) -> None:
        """Dispatch agent-specific commands."""
        chat = self.query_one("#chat-log", RichLog)
        side = self.query_one(SidePanel)

        if not self._operator:
            chat.write(f"[{ERROR}]Not ready yet.[/]")
            return

        agent_map = {
            "/scout": ("Scout", "scout", "Read-only codebase exploration"),
            "/architect": ("Architect", "architect", "Implementation planning"),
            "/review": ("Dharma", "review", "Code review"),
            "/debug": ("Sherlock", "debug", "Root cause analysis"),
            "/test": ("Agni", "test", "Test generation"),
            "/refactor": ("Shiva", "refactor", "Restructuring"),
            "/devops": ("Vayu", "devops", "DevOps/CI/CD"),
        }

        if cmd == "/orchestra" and self._orchestrator:
            if not arg:
                chat.write(f"[{WARNING}]Usage: /orchestra <task>[/]")
                return
            chat.write(f"\n[bold {GOLD}]\u276f[/] [{GOLD}]{cmd} {arg}[/]")
            side.agent_panel.set_agent("Orchestra", "Multi-agent")
            chat.write(f"[dim]Dispatching to agent orchestra...[/]")
            try:
                async for token in self._orchestrator.execute(arg):
                    chat.write(token, shrink=False, scroll_end=True)
                side.agent_panel.add_tool_call("orchestra", "ok")
            except Exception as e:
                chat.write(f"[{ERROR}]Orchestra error: {e}[/]")
                side.agent_panel.add_tool_call("orchestra", "error")
            side.agent_panel.set_agent("Operator", "General")
            return

        if cmd in ("/scout", "/architect"):
            agent_name, module_name, desc = agent_map[cmd]
            task = arg or f"explore this codebase" if cmd == "/scout" else arg
            if not task:
                chat.write(f"[{WARNING}]Usage: {cmd} <task>[/]")
                return
            chat.write(f"\n[bold {GOLD}]\u276f[/] [{GOLD}]{cmd} {task}[/]")
            side.agent_panel.set_agent(agent_name, desc)
            chat.write(f"[dim]{agent_name} working...[/]")
            try:
                if cmd == "/scout":
                    from djcode.agents.scout import Scout
                    agent = Scout(self._provider)
                    result = await agent.investigate(task)
                else:
                    from djcode.agents.architect import Architect
                    agent = Architect(self._provider)
                    result = await agent.plan(task)
                chat.write(result)
                side.agent_panel.add_tool_call(module_name, "ok")
            except Exception as e:
                chat.write(f"[{ERROR}]{agent_name} error: {e}[/]")
                side.agent_panel.add_tool_call(module_name, "error")
            side.agent_panel.set_agent("Operator", "General")
            return

        if cmd in agent_map and self._orchestrator:
            agent_name, _, desc = agent_map[cmd]
            from djcode.agents.registry import AgentRole
            role_map = {
                "/review": AgentRole.REVIEWER,
                "/debug": AgentRole.DEBUGGER,
                "/test": AgentRole.TESTER,
                "/refactor": AgentRole.REFACTORER,
                "/devops": AgentRole.DEVOPS,
            }
            role = role_map.get(cmd)
            task = arg or f"work on this codebase"
            chat.write(f"\n[bold {GOLD}]\u276f[/] [{GOLD}]{cmd} {task}[/]")
            side.agent_panel.set_agent(agent_name, desc)
            chat.write(f"[dim]{agent_name} working...[/]")
            try:
                async for token in self._orchestrator.run_single_agent_streaming(role, task):
                    chat.write(token, shrink=False, scroll_end=True)
                side.agent_panel.add_tool_call(agent_name.lower(), "ok")
            except Exception as e:
                chat.write(f"[{ERROR}]{agent_name} error: {e}[/]")
                side.agent_panel.add_tool_call(agent_name.lower(), "error")
            side.agent_panel.set_agent("Operator", "General")
            return

        # Content agent commands
        content_map = {
            "/launch": "Full pipeline",
            "/campaign": "Content campaign",
            "/image": "Image prompts",
            "/video": "Video prompts",
            "/social": "Social content",
        }
        if cmd in content_map:
            if not arg and cmd == "/launch":
                chat.write(f"[{WARNING}]Usage: /launch <product description>[/]")
                return
            chat.write(f"\n[bold {GOLD}]\u276f[/] [{GOLD}]{cmd} {arg}[/]")
            chat.write(f"[dim]{content_map[cmd]}...[/]")

            try:
                from djcode.agents.content_registry import ContentRole, get_content_spec
                from djcode.orchestrator.engine import AgentRunner

                role_map = {
                    "/campaign": ContentRole.CAMPAIGN_DIRECTOR,
                    "/image": ContentRole.IMAGE_PROMPTER,
                    "/video": ContentRole.VIDEO_DIRECTOR,
                    "/social": ContentRole.SOCIAL_STRATEGIST,
                }

                if cmd == "/launch" and self._orchestrator:
                    # Phase 1: Build
                    chat.write(f"  [{GOLD}]Phase 1: Build[/]")
                    async for token in self._orchestrator.execute(f"build: {arg}"):
                        chat.write(token, shrink=False, scroll_end=True)
                    # Phase 2: Campaign
                    chat.write(f"  [{GOLD}]Phase 2: Campaign[/]")
                    spec = get_content_spec(ContentRole.CAMPAIGN_DIRECTOR)
                    runner = AgentRunner(
                        self._provider, spec,
                        self._orchestrator.bus, auto_accept=True,
                    )
                    async for token in runner.run_streaming(
                        f"Create a full launch campaign for: {arg}"
                    ):
                        chat.write(token, shrink=False, scroll_end=True)
                    chat.write(f"\n  [{GOLD}]Launch complete.[/]\n")
                elif cmd in role_map:
                    role = role_map[cmd]
                    spec = get_content_spec(role)
                    bus = self._orchestrator.bus if self._orchestrator else None
                    runner = AgentRunner(
                        self._provider, spec, bus, auto_accept=True,
                    )
                    async for token in runner.run_streaming(arg or f"create content"):
                        chat.write(token, shrink=False, scroll_end=True)

                side.agent_panel.add_tool_call(cmd.lstrip("/"), "ok")
            except Exception as e:
                chat.write(f"[{ERROR}]Error: {e}[/]")
                side.agent_panel.add_tool_call(cmd.lstrip("/"), "error")
            return

        # Fallback: send as message to operator
        self._last_input = text if not arg else f"{cmd} {arg}"
        self.run_worker(
            self._send_message(self._last_input), exclusive=True,
        )

    # ── Interactive Pickers ─────────────────────────────────────────────

    def _show_model_picker(self) -> None:
        """Open interactive model picker overlay."""
        provider_name = self._provider.config.name if self._provider else "ollama"
        base_url = self._provider.config.base_url if self._provider else ""

        def on_model_selected(model: str | None) -> None:
            if model:
                self.run_worker(self._handle_model_switch(model), exclusive=True)

        self.push_screen(
            ModelPicker(provider_name=provider_name, base_url=base_url),
            callback=on_model_selected,
        )

    def _show_provider_picker(self) -> None:
        """Open interactive provider picker overlay."""

        def on_provider_selected(result: dict | None) -> None:
            if not result:
                return
            provider_id = result.get("provider", "")
            base_url = result.get("base_url", "")
            api_key = result.get("api_key", "")

            # If custom URL provided, set env and switch
            if base_url:
                import os
                os.environ["DJCODE_CUSTOM_URL"] = base_url
                if api_key:
                    os.environ["DJCODE_API_KEY"] = api_key

            self.run_worker(
                self._handle_provider_switch(provider_id), exclusive=True,
            )

        self.push_screen(ProviderPicker(), callback=on_provider_selected)

    # ── Model / Provider switching ───────────────────────────────────────

    async def _handle_model_switch(self, model_name: str) -> None:
        """Switch model."""
        chat = self.query_one("#chat-log", RichLog)
        side = self.query_one(SidePanel)

        if not self._provider:
            chat.write(f"[{ERROR}]Provider not initialized yet.[/]")
            return

        old_model = self._provider.config.model
        self._provider.config.model = model_name
        ok, msg = self._provider.validate_model()

        if ok:
            new_model = self._provider.config.model
            chat.write(f"[{SUCCESS}]Model: {old_model} -> {new_model}[/]")
            self.sub_title = (
                f"v{__version__} | {new_model} | {self._provider.config.name}"
            )

            from djcode.agents.operator import Operator

            self._operator = Operator(
                self._provider,
                bypass_rlhf=self._bypass_rlhf,
                raw=True,
                model=new_model,
                auto_accept=self._auto_accept,
                show_thinking=False,
            )
            self._operator.auto_accept = self._auto_accept
            side.stats_panel.update_stats(model=new_model)
            side.agent_panel.add_tool_call("model_switch", "ok")
        else:
            self._provider.config.model = old_model
            chat.write(f"[{ERROR}]{msg}[/]")

    async def _handle_provider_switch(self, provider_name: str) -> None:
        """Switch provider."""
        chat = self.query_one("#chat-log", RichLog)
        side = self.query_one(SidePanel)

        try:
            from djcode.provider import Provider, ProviderConfig

            config = ProviderConfig.from_config(
                provider_override=provider_name,
                model_override=self._model_name,
            )
            self._provider = Provider(config)

            from djcode.agents.operator import Operator

            self._operator = Operator(
                self._provider,
                bypass_rlhf=self._bypass_rlhf,
                raw=True,
                model=self._provider.config.model,
                auto_accept=self._auto_accept,
                show_thinking=False,
            )
            self._operator.auto_accept = self._auto_accept

            # Re-initialize orchestrator with new provider
            try:
                from djcode.orchestrator import Orchestrator
                self._orchestrator = Orchestrator(self._provider)
            except Exception:
                pass

            model = self._provider.config.model
            prov = self._provider.config.name
            self.sub_title = f"v{__version__} | {model} | {prov}"
            chat.write(f"[{SUCCESS}]Provider: {prov} ({model})[/]")
            side.stats_panel.update_stats(model=model, provider=prov)
            side.agent_panel.add_tool_call("provider_switch", "ok")
        except Exception as e:
            chat.write(f"[{ERROR}]Provider error: {e}[/]")

    def _show_models_list(self) -> None:
        """List available models."""
        chat = self.query_one("#chat-log", RichLog)
        if not self._provider:
            chat.write(f"[{ERROR}]Provider not initialized.[/]")
            return

        if self._provider.config.name != "ollama":
            chat.write(
                f"[{WARNING}]Model listing only available for Ollama.[/]\n"
                f"[dim]Current model: {self._provider.config.model}[/]"
            )
            return

        try:
            from djcode.provider import fetch_ollama_models_sync, format_model_size
            models = fetch_ollama_models_sync(self._provider.config.base_url)
            if not models:
                chat.write(
                    f"[{WARNING}]No models found. Is Ollama running?[/]"
                )
                return
            chat.write(f"\n[bold {GOLD}]Available Models[/]")
            for m in models:
                name = m.get("name", "?")
                size = format_model_size(m.get("size", 0))
                current = " *" if name == self._provider.config.model else ""
                chat.write(f"  [{GOLD}]{name:<30}[/] [dim]{size}[/]{current}")
            chat.write("")
        except Exception as e:
            chat.write(f"[{ERROR}]Error listing models: {e}[/]")

    # ── Config / Stats / Memory ──────────────────────────────────────────

    def _show_config(self) -> None:
        """Display current configuration."""
        from djcode.config import load_config

        chat = self.query_one("#chat-log", RichLog)
        cfg = load_config()
        chat.write(f"\n[bold {GOLD}]Configuration[/]")
        for k, v in sorted(cfg.items()):
            display = "***" if "key" in k.lower() and v else str(v)
            chat.write(f"  [dim]{k}:[/] {display}")
        chat.write("")

    def _handle_set(self, arg: str) -> None:
        """Set a config value."""
        chat = self.query_one("#chat-log", RichLog)
        if "=" not in arg:
            chat.write(f"[dim]Usage: /set key=value[/]")
            return
        key, _, value = arg.partition("=")
        key, value = key.strip(), value.strip()
        try:
            import json
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            parsed = value
        from djcode.config import set_value
        set_value(key, parsed)
        chat.write(f"[{SUCCESS}]Set {key}={parsed}[/]")

    def _show_session_stats(self) -> None:
        """Display session stats."""
        chat = self.query_one("#chat-log", RichLog)
        elapsed = time.time() - self._session_start
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        avg_rt = (
            f"{sum(self._response_times) / len(self._response_times):.1f}s"
            if self._response_times
            else "--"
        )
        chat.write(f"\n[bold {GOLD}]Session Stats[/]")
        chat.write(f"  [dim]Tokens:[/]       [{GOLD}]{self._token_count}[/]")
        chat.write(f"  [dim]Tokens in:[/]    [{GOLD}]{self._tokens_in}[/]")
        chat.write(f"  [dim]Tokens out:[/]   [{GOLD}]{self._tokens_out}[/]")
        chat.write(f"  [dim]Elapsed:[/]      [{GOLD}]{mins}m {secs}s[/]")
        chat.write(f"  [dim]Avg response:[/] [{GOLD}]{avg_rt}[/]")
        chat.write(
            f"  [dim]Mode:[/]         [{GOLD}]{'PLAN' if self._plan_mode else 'ACT'}[/]"
        )
        chat.write(
            f"  [dim]Thinking:[/]     [{GOLD}]{'ON' if self._show_thinking else 'OFF'}[/]"
        )
        chat.write(
            f"  [dim]Auto-accept:[/]  [{GOLD}]{'ON' if self._auto_accept else 'OFF'}[/]"
        )
        chat.write(f"  [dim]Agent:[/]        [{GOLD}]{self._active_agent}[/]")
        chat.write("")

    def _show_memory_stats(self) -> None:
        """Display memory stats."""
        chat = self.query_one("#chat-log", RichLog)
        if not self._memory:
            chat.write(f"[{WARNING}]Memory not initialized.[/]")
            return
        stats = self._memory.stats
        chat.write(f"\n[bold {GOLD}]Memory Stats[/]")
        chat.write(f"  [dim]Session messages:[/]      {stats.get('session_messages', 0)}")
        chat.write(f"  [dim]Persistent facts:[/]      {stats.get('persistent_facts', 0)}")
        chat.write(f"  [dim]Facts with embeddings:[/] {stats.get('facts_with_embeddings', 0)}")
        facts = self._memory.list_facts()
        if facts:
            chat.write(f"  [dim]Facts: {', '.join(facts)}[/]")
        chat.write("")

    def _handle_remember(self, arg: str) -> None:
        chat = self.query_one("#chat-log", RichLog)
        if not self._memory:
            chat.write(f"[{WARNING}]Memory not initialized.[/]")
            return
        if "=" not in arg:
            chat.write(f"[dim]Usage: /remember key=value[/]")
            return
        key, _, value = arg.partition("=")
        self._memory.remember(key.strip(), value.strip())
        chat.write(f"[{SUCCESS}]Remembered: {key.strip()}[/]")

    def _handle_recall(self, arg: str) -> None:
        chat = self.query_one("#chat-log", RichLog)
        if not self._memory:
            chat.write(f"[{WARNING}]Memory not initialized.[/]")
            return
        if not arg:
            chat.write(f"[dim]Usage: /recall <key>[/]")
            return
        value = self._memory.recall(arg.strip())
        if value:
            chat.write(f"[{INFO}]{arg}:[/] {value}")
        else:
            chat.write(f"[{WARNING}]No memory found for: {arg}[/]")

    def _handle_forget(self, arg: str) -> None:
        chat = self.query_one("#chat-log", RichLog)
        if not self._memory:
            chat.write(f"[{WARNING}]Memory not initialized.[/]")
            return
        if not arg:
            chat.write(f"[dim]Usage: /forget <key>[/]")
            return
        if self._memory.forget(arg.strip()):
            chat.write(f"[{SUCCESS}]Forgot: {arg}[/]")
        else:
            chat.write(f"[{WARNING}]No memory found for: {arg}[/]")

    def _handle_save(self) -> None:
        chat = self.query_one("#chat-log", RichLog)
        if not self._memory:
            chat.write(f"[{WARNING}]Memory not initialized.[/]")
            return
        session_id = str(uuid.uuid4())[:8]
        path = self._memory.save_conversation(session_id)
        chat.write(f"[{SUCCESS}]Saved to: {path}[/]")

    def _show_uncensored_info(self) -> None:
        chat = self.query_one("#chat-log", RichLog)
        chat.write(f"\n[bold {GOLD}]Uncensored Models[/]")
        chat.write(f"  dolphin3       — Fully uncensored, no RLHF")
        chat.write(f"  abliterated    — RLHF removed via activation engineering")
        chat.write(f"  wizard-vicuna  — Classic unrestricted")
        chat.write(f"  nous-hermes    — Minimal alignment")
        chat.write(f"\n[dim]Switch with: /model dolphin3[/]\n")

    def _handle_docs(self, arg: str) -> None:
        chat = self.query_one("#chat-log", RichLog)
        try:
            from djcode.docs import render_docs, render_docs_index
            # Capture docs output as text (docs module uses Rich console)
            if arg.strip():
                chat.write(f"[dim]Docs: {arg.strip()}[/]")
            else:
                chat.write(
                    f"\n[bold {GOLD}]DJcode Documentation[/]\n"
                    f"  [dim]GitHub:[/]  https://github.com/darshjme/djcode\n"
                    f"  [dim]Docs:[/]    https://cli.darshj.ai\n"
                    f"  [dim]Version:[/] {__version__}\n"
                    f"  [dim]Usage:[/]   /docs <topic>[/]\n"
                )
        except Exception:
            chat.write(
                f"\n[bold {GOLD}]DJcode Documentation[/]\n"
                f"  [dim]GitHub:[/]  https://github.com/darshjme/djcode\n"
                f"  [dim]Version:[/] {__version__}\n"
            )

    # ── Todo management ──────────────────────────────────────────────────

    def _handle_todo(self, arg: str) -> None:
        """Handle /todo commands: add, done, rm, list."""
        chat = self.query_one("#chat-log", RichLog)
        side = self.query_one(SidePanel)
        sub = arg.strip().split(maxsplit=1) if arg.strip() else []

        if not sub or sub[0] == "list":
            todos = side.todo_panel.get_todos()
            if not todos:
                chat.write(f"[dim]No todos. Use /todo add <text>[/]")
            else:
                chat.write(f"\n[bold {GOLD}]Todos[/]")
                for t in todos:
                    check = "[x]" if t["done"] else "[ ]"
                    label = f"[dim]{t['text']}[/]" if t["done"] else t["text"]
                    chat.write(f"  {check} #{t['id']} {label}")
                done = sum(1 for t in todos if t["done"])
                chat.write(f"  [dim]{done}/{len(todos)} done[/]\n")

        elif sub[0] == "add" and len(sub) >= 2:
            todo_id = side.todo_panel.add_todo(sub[1])
            chat.write(f"[{SUCCESS}]Todo #{todo_id} added: {sub[1]}[/]")

        elif sub[0] == "done" and len(sub) >= 2:
            try:
                todo_id = int(sub[1])
                side.todo_panel.toggle_todo(todo_id)
                chat.write(f"[{SUCCESS}]Todo #{todo_id} toggled[/]")
            except ValueError:
                chat.write(f"[{WARNING}]Usage: /todo done <id>[/]")

        elif sub[0] in ("rm", "remove") and len(sub) >= 2:
            try:
                todo_id = int(sub[1])
                side.todo_panel.remove_todo(todo_id)
                chat.write(f"[{SUCCESS}]Todo #{todo_id} removed[/]")
            except ValueError:
                chat.write(f"[{WARNING}]Usage: /todo rm <id>[/]")

        else:
            chat.write(
                f"[dim]Usage: /todo [add|done|rm|list] ...[/]"
            )

    def _show_cost(self) -> None:
        """Show cost estimates in chat."""
        chat = self.query_one("#chat-log", RichLog)
        side = self.query_one(SidePanel)
        cp = side.cost_panel
        total = cp.tokens_in + cp.tokens_out
        cost_in = (cp.tokens_in / 1000) * cp.cost_per_1k_in
        cost_out = (cp.tokens_out / 1000) * cp.cost_per_1k_out
        cost_total = cost_in + cost_out

        chat.write(f"\n[bold {GOLD}]Token Cost Estimate[/]")
        chat.write(f"  [dim]Input tokens:[/]  {cp.tokens_in}")
        chat.write(f"  [dim]Output tokens:[/] {cp.tokens_out}")
        chat.write(f"  [dim]Total tokens:[/]  {total}")
        chat.write(f"  [dim]Input cost:[/]    ${cost_in:.4f}")
        chat.write(f"  [dim]Output cost:[/]   ${cost_out:.4f}")
        chat.write(f"  [bold {GOLD}]Total cost:[/]   ${cost_total:.4f}")
        chat.write(f"  [dim]Requests:[/]      {cp.total_requests}")
        chat.write("")

    # ── Extension management ─────────────────────────────────────────────

    async def _handle_extension(self, arg: str) -> None:
        chat = self.query_one("#chat-log", RichLog)
        side = self.query_one(SidePanel)

        if not self._ext_manager:
            try:
                from djcode.extensions import ExtensionManager
                self._ext_manager = ExtensionManager()
            except Exception:
                chat.write(f"[{ERROR}]Extension manager unavailable.[/]")
                return

        sub = arg.strip().split(maxsplit=2) if arg.strip() else []

        if not sub or sub[0] == "list":
            statuses = self._ext_manager.get_status()
            side.mcp_panel.load_extensions(statuses)
            if not statuses:
                chat.write(f"[{GOLD}]No extensions registered.[/]")
                chat.write("[dim]Add one: /extension add <name> <command>[/]")
            else:
                chat.write(f"\n[bold {GOLD}]MCP Extensions[/]")
                for s in statuses:
                    status = "[green]on[/]" if s["enabled"] else "[red]off[/]"
                    if s.get("connected"):
                        status = "[green]connected[/]"
                    chat.write(
                        f"  {s['name']:<16} {s['cmd']:<20} {status}  "
                        f"[dim]{s['tools_count']} tools[/]"
                    )
                chat.write("")

        elif sub[0] == "add" and len(sub) >= 3:
            ext_cmd_parts = sub[2].split()
            ext = self._ext_manager.add(sub[1], ext_cmd_parts[0], ext_cmd_parts[1:])
            chat.write(f"[{SUCCESS}]Added: {ext.name} -> {ext.cmd}[/]")
            side.mcp_panel.load_extensions(self._ext_manager.get_status())

        elif sub[0] in ("rm", "remove") and len(sub) >= 2:
            if self._ext_manager.remove(sub[1]):
                chat.write(f"[{SUCCESS}]Removed: {sub[1]}[/]")
                side.mcp_panel.load_extensions(self._ext_manager.get_status())
            else:
                chat.write(f"[{WARNING}]Not found: {sub[1]}[/]")

        elif sub[0] == "tools" and len(sub) >= 2:
            try:
                tools = await self._ext_manager.refresh_tools(sub[1])
                if tools:
                    chat.write(f"\n[bold {GOLD}]Tools from {sub[1]}:[/]")
                    for t in tools:
                        desc = t.get("description", "")[:60]
                        chat.write(f"  {t.get('name', '?'):<20} [dim]{desc}[/]")
                    chat.write("")
                else:
                    chat.write(f"[dim]No tools found for {sub[1]}[/]")
            except Exception as e:
                chat.write(f"[{ERROR}]Error: {e}[/]")
            finally:
                await self._ext_manager.shutdown()

        else:
            chat.write("[dim]Usage: /extension [list|add|rm|tools] ...[/]")

    # ── Recipe management ────────────────────────────────────────────────

    async def _handle_recipe(self, arg: str) -> None:
        chat = self.query_one("#chat-log", RichLog)
        try:
            from djcode.recipes import RecipeManager
        except ImportError:
            chat.write(f"[{ERROR}]Recipes not available.[/]")
            return

        recipe_mgr = RecipeManager()
        sub = arg.strip().split(maxsplit=1) if arg.strip() else []

        if not sub or sub[0] == "list":
            recipes = recipe_mgr.list_recipes()
            if not recipes:
                chat.write(f"[dim]No recipes found.[/]")
            else:
                chat.write(f"\n[bold {GOLD}]Recipes[/]")
                for r in recipes:
                    chat.write(f"  [{GOLD}]{r.name:<16}[/] [dim]{r.description}[/]")
                chat.write("")

        elif sub[0] == "run" and len(sub) >= 2:
            run_parts = sub[1].split(maxsplit=1)
            recipe_name = run_parts[0]
            param_str = run_parts[1] if len(run_parts) > 1 else ""
            try:
                recipe = recipe_mgr.load(recipe_name)
                params = recipe_mgr.collect_params_from_args(recipe, param_str)
                chat.write(f"\n[bold {GOLD}]Running recipe: {recipe.name}[/]")
                async for token in recipe_mgr.execute(recipe, params, self._operator):
                    chat.write(token, shrink=False, scroll_end=True)
                chat.write("")
            except Exception as e:
                chat.write(f"[{ERROR}]Recipe error: {e}[/]")

        elif sub[0] == "show" and len(sub) >= 2:
            try:
                recipe = recipe_mgr.load(sub[1])
                chat.write(f"\n[bold {GOLD}]{recipe.name}[/]")
                chat.write(f"  [dim]{recipe.description}[/]")
                for p in recipe.parameters:
                    req = "[red]*[/]" if p.required else " "
                    chat.write(f"  {req} {p.key}: {p.description}")
                chat.write("")
            except FileNotFoundError as e:
                chat.write(f"[{WARNING}]{e}[/]")

        else:
            chat.write("[dim]Usage: /recipe [list|show|run] ...[/]")

    # ── History / Resume ─────────────────────────────────────────────────

    def _handle_history(self, arg: str) -> None:
        chat = self.query_one("#chat-log", RichLog)
        if not self._session_db:
            try:
                from djcode.sessions import SessionDB
                self._session_db = SessionDB()
            except Exception:
                chat.write(f"[{ERROR}]Session history unavailable.[/]")
                return

        sub = arg.strip().split(maxsplit=1) if arg.strip() else []

        if not sub:
            sessions = self._session_db.list_sessions(limit=20)
            if not sessions:
                chat.write(f"[dim]No past sessions.[/]")
            else:
                chat.write(f"\n[bold {GOLD}]Recent Sessions[/]")
                for s in sessions:
                    chat.write(
                        f"  [{GOLD}]{s.id[:8]}[/] "
                        f"[dim]{s.model} | {s.created_at[:16]}[/] "
                        f"[dim]{s.messages} msgs[/]"
                    )
                chat.write(f"\n[dim]Resume with: /resume <session_id>[/]\n")

        elif sub[0] == "search" and len(sub) >= 2:
            results = self._session_db.search_sessions(sub[1])
            if results:
                chat.write(f"\n[bold {GOLD}]Sessions matching '{sub[1]}':[/]")
                for s in results:
                    chat.write(
                        f"  [{GOLD}]{s.id[:8]}[/] [dim]{s.model} | {s.created_at[:16]}[/]"
                    )
                chat.write("")
            else:
                chat.write(f"[dim]No sessions match '{sub[1]}'[/]")

    def _handle_resume(self, arg: str) -> None:
        chat = self.query_one("#chat-log", RichLog)
        if not arg.strip():
            chat.write(f"[dim]Usage: /resume <session_id>[/]")
            return

        if not self._session_db:
            try:
                from djcode.sessions import SessionDB
                self._session_db = SessionDB()
            except Exception:
                chat.write(f"[{ERROR}]Session DB unavailable.[/]")
                return

        target_id = arg.strip()
        session = self._session_db.get_session(target_id)
        if not session:
            chat.write(f"[{WARNING}]Session not found: {target_id}[/]")
            return

        messages = self._session_db.load_conversation(target_id)
        if not messages:
            chat.write(f"[{WARNING}]No conversation data for {target_id}[/]")
            return

        if self._operator:
            from djcode.provider import Message as _Msg
            system_msg = self._operator.messages[0] if self._operator.messages else None
            self._operator.messages.clear()
            if system_msg:
                self._operator.messages.append(system_msg)

            restored = 0
            for m in messages:
                role = m.get("role", "")
                if role == "system":
                    continue
                content = m.get("content", "")
                tc = m.get("tool_calls")
                self._operator.messages.append(
                    _Msg(role=role, content=content, tool_calls=tc if tc else None)
                )
                restored += 1

            chat.write(
                f"[{SUCCESS}]Resumed session {target_id} "
                f"({session.model}, {restored} messages)[/]"
            )

    # ── Message sending and streaming ────────────────────────────────────

    async def _send_message(self, text: str) -> None:
        """Send a message to the operator and stream the response."""
        chat = self.query_one("#chat-log", RichLog)
        side = self.query_one(SidePanel)

        if not self._operator:
            chat.write(f"[{ERROR}]Not ready yet. Provider is still initializing...[/]")
            return

        if self._is_generating:
            chat.write(f"[{WARNING}]Already generating. Please wait...[/]")
            return

        self._is_generating = True
        self._cancel_requested = False

        # Show user message
        chat.write(f"\n[bold {GOLD}]\u276f[/] [{GOLD}]{text}[/]")

        # Track in memory
        if self._memory:
            self._memory.add_session_message("user", text)

        # Enhance prompt with context
        actual_input = text
        try:
            from djcode.prompt_enhancer import enhance_prompt
            enhanced = enhance_prompt(text)
            if enhanced.was_enhanced:
                actual_input = enhanced.enhanced
                chat.write(f"[dim]Enhanced with context[/]")
        except Exception:
            pass

        # Plan mode prefix
        if self._plan_mode:
            actual_input = (
                "[PLAN MODE] Do not execute any tools. Only describe what "
                "you would do. List every step, file, and command you would "
                "run, but do NOT actually run anything.\n\n" + actual_input
            )

        side.agent_panel.add_tool_call("send", "pending")
        start = time.time()

        try:
            response_buf = ""
            line_buf = ""
            thinking_buf = ""
            in_thinking = False

            # Show thinking indicator
            chat.write(f"[{THINKING}]\u23fa Thinking...[/]")

            async for token in self._operator.send(actual_input):
                # Check for cancel
                if self._cancel_requested:
                    chat.write(f"\n[{WARNING}]Generation cancelled.[/]")
                    break

                # Handle thinking blocks
                if "<think>" in token:
                    in_thinking = True
                    thinking_buf = token.split("<think>", 1)[1]
                    continue
                if "</think>" in token:
                    in_thinking = False
                    if self._show_thinking and thinking_buf:
                        chat.write(
                            f"[{THINKING}][dim italic]{thinking_buf[:200]}...[/]"
                            if len(thinking_buf) > 200
                            else f"[{THINKING}][dim italic]{thinking_buf}[/]"
                        )
                    thinking_buf = ""
                    continue
                if in_thinking:
                    thinking_buf += token
                    continue

                response_buf += token
                line_buf += token
                self._token_count += 1
                self._tokens_out += 1

                # Write complete lines to chat
                if "\n" in line_buf:
                    parts = line_buf.split("\n")
                    for part in parts[:-1]:
                        if part:
                            chat.write(part, shrink=False, scroll_end=True)
                        else:
                            chat.write("", shrink=False, scroll_end=True)
                    line_buf = parts[-1]

                # Periodic stats update
                if self._token_count % 100 == 0:
                    side.agent_panel.update_tokens(self._tokens_in, self._tokens_out)
                    side.stats_panel.update_stats(
                        tokens_out=self._tokens_out,
                        tokens_in=self._tokens_in,
                    )

            # Flush remaining buffer
            if line_buf:
                chat.write(line_buf, shrink=False, scroll_end=True)

            elapsed = time.time() - start
            self._response_times.append(elapsed)
            token_est = len(response_buf) // 4
            self._tokens_in += token_est  # rough input estimate
            self._tokens_out = self._token_count

            # Log completion
            if self._operator.last_had_tool_calls:
                side.agent_panel.add_tool_call("tools_executed", "ok")

            side.agent_panel.add_tool_call("response", "ok")
            side.agent_panel.update_tokens(self._tokens_in, self._tokens_out)
            side.stats_panel.update_stats(
                tokens_in=self._tokens_in,
                tokens_out=self._tokens_out,
                response_time_ms=elapsed * 1000,
                tools_used=side.agent_panel.tool_count,
            )

            # Update cost panel
            avg_ms = (
                (sum(self._response_times) / len(self._response_times)) * 1000
                if self._response_times else 0
            )
            side.cost_panel.update_cost(
                tokens_in=self._tokens_in,
                tokens_out=self._tokens_out,
                requests=len(self._response_times),
                avg_ms=avg_ms,
            )

            # Memory tracking
            if self._memory and response_buf:
                self._memory.add_session_message("assistant", response_buf)

            # Session DB tracking
            if self._session_db and self._sqlite_session_id:
                try:
                    self._session_db.save_message(
                        self._sqlite_session_id, "user", text,
                    )
                    self._session_db.save_message(
                        self._sqlite_session_id, "assistant", response_buf,
                    )
                    self._session_db.update_session(
                        self._sqlite_session_id,
                        tokens_out=token_est,
                        messages=1,
                    )
                except Exception:
                    pass

            # Tool extraction router — for models without native tool calling
            if response_buf and not self._operator.last_had_tool_calls:
                try:
                    from djcode.tool_router import ToolExtractionRouter

                    router = ToolExtractionRouter()
                    extracted = router.extract_intents(response_buf)
                    if extracted:
                        from djcode.config import load_config
                        cfg = load_config()
                        effective_auto = (
                            self._auto_accept
                            or cfg.get("auto_accept", False)
                        )
                        results = await router.extract_and_execute(
                            response_buf, auto_accept=effective_auto,
                        )
                        if results:
                            ctx = router.format_results_for_context(results)
                            if ctx:
                                from djcode.provider import Message as _Msg
                                self._operator.messages.append(
                                    _Msg(role="user", content=ctx)
                                )
                                side.agent_panel.add_tool_call(
                                    "tool_router", "ok",
                                )
                except Exception:
                    pass

            # Stats line
            if response_buf:
                tok_str = (
                    f"{token_est / 1000:.1f}k"
                    if token_est >= 1000
                    else str(token_est)
                )
                chat.write(
                    f"[dim]\u2193 {tok_str} tokens \u00b7 {elapsed:.1f}s[/]"
                )

            chat.write("")  # Blank line after response

            # Update memory panel
            if self._memory:
                stats = self._memory.stats
                side.agent_panel.update_memory(
                    session=stats.get("session_messages", 0),
                    facts=stats.get("persistent_facts", 0),
                    vectors=stats.get("facts_with_embeddings", 0),
                )

        except ConnectionError as e:
            chat.write(f"\n[{ERROR}]Connection error: {e}[/]")
            side.agent_panel.add_tool_call("connection", "error")
        except Exception as e:
            chat.write(f"\n[{ERROR}]Error: {e}[/]")
            side.agent_panel.add_tool_call("error", "error")

            # Suggest fallback model
            try:
                from djcode.errors import classify_error, get_fallback_model
                err = classify_error(e)
                if err.fallback == "retry_with_smaller_model":
                    fb = get_fallback_model(self._provider.config.model)
                    if fb:
                        chat.write(f"  [dim]Try: /model {fb}[/]")
            except Exception:
                pass
        finally:
            self._is_generating = False

    # ── Key bindings / actions ───────────────────────────────────────────

    def action_toggle_thinking(self) -> None:
        """Toggle thinking display."""
        self._show_thinking = not self._show_thinking
        label = "ON" if self._show_thinking else "OFF"
        chat = self.query_one("#chat-log", RichLog)
        chat.write(f"[dim]Thinking: {label}[/]")
        self.notify(f"Thinking: {label}", severity="information")

    def action_toggle_plan(self) -> None:
        """Toggle plan/act mode."""
        self._plan_mode = not self._plan_mode
        mode = "PLAN" if self._plan_mode else "ACT"
        chat = self.query_one("#chat-log", RichLog)
        chat.write(f"[dim]Mode: {mode}[/]")
        try:
            side = self.query_one(SidePanel)
            side.agent_panel.set_agent(
                self._active_agent,
                f"{'Planning' if self._plan_mode else 'General'}",
            )
        except Exception:
            pass
        self.notify(f"Mode: {mode}", severity="information")

    def action_clear_chat(self) -> None:
        """Clear the chat log and reset conversation."""
        chat = self.query_one("#chat-log", RichLog)
        chat.clear()
        chat.write(f"[dim]Chat cleared.[/]\n")

        if self._operator:
            try:
                self._operator.reset()
            except AttributeError:
                from djcode.provider import Message
                from djcode.prompt import build_system_prompt
                model = self._provider.config.model if self._provider else ""
                self._operator.messages = [
                    Message(
                        role="system",
                        content=build_system_prompt(
                            bypass_rlhf=self._bypass_rlhf, model=model,
                        ),
                    )
                ]

        if self._memory:
            self._memory.clear_session()

        self.notify("Chat cleared", severity="information")

    def action_toggle_auto(self) -> None:
        """Toggle auto-accept for tool calls."""
        self._auto_accept = not self._auto_accept
        if self._operator:
            self._operator.auto_accept = self._auto_accept
        label = "ON" if self._auto_accept else "OFF"
        chat = self.query_one("#chat-log", RichLog)
        chat.write(f"[dim]Auto-accept: {label}[/]")
        self.notify(f"Auto-accept: {label}", severity="information")

    def action_rerun(self) -> None:
        """Rerun the last message."""
        if self._last_input and not self._is_generating:
            chat = self.query_one("#chat-log", RichLog)
            chat.write(f"[dim]Rerunning: {self._last_input}[/]")
            self.run_worker(
                self._send_message(self._last_input), exclusive=True,
            )
        else:
            self.notify("Nothing to rerun", severity="warning")

    def action_cancel(self) -> None:
        """Cancel current generation."""
        if self._is_generating:
            self._cancel_requested = True
            self.notify("Cancelling...", severity="warning")

    def action_show_help(self) -> None:
        """Show help overlay."""
        self.push_screen(HelpScreen())

    def action_show_agents(self) -> None:
        """Show agents overlay."""
        self.push_screen(AgentsScreen())

    def action_show_model_picker(self) -> None:
        """Open interactive model picker (F2)."""
        self._show_model_picker()

    def action_show_provider_picker(self) -> None:
        """Open interactive provider picker (F3)."""
        self._show_provider_picker()

    def action_show_palette(self) -> None:
        """Show command palette."""

        def on_palette_result(result: str | None) -> None:
            if result:
                inp = self.query_one("#prompt-input", Input)
                inp.value = result + " "
                inp.focus()

        self.push_screen(CommandPalette(), callback=on_palette_result)

    def action_show_docs(self) -> None:
        """Show docs info."""
        self._handle_docs("")

    async def on_unmount(self) -> None:
        """Clean up on exit."""
        # Record session end
        try:
            from djcode.stats import record_session_end
            record_session_end()
        except Exception:
            pass

        if self._ext_manager:
            try:
                await self._ext_manager.shutdown()
            except Exception:
                pass

        if self._provider:
            try:
                await self._provider.close()
            except Exception:
                pass


# ── Entry point ────────────────────────────────────────────────────────


def run_tui(
    *,
    provider: str | None = None,
    model: str | None = None,
    bypass_rlhf: bool = False,
    auto_accept: bool = False,
    show_thinking: bool = True,
) -> None:
    """Entry point to launch the Textual TUI."""
    app = DJcodeApp(
        provider_name=provider,
        model_name=model,
        bypass_rlhf=bypass_rlhf,
        auto_accept=auto_accept,
        show_thinking=show_thinking,
    )
    app.run()


__all__ = ["DJcodeApp", "run_tui"]
