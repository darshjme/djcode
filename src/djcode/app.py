"""DJcode Textual TUI — lazygit-style split-pane interface.

Premium terminal experience with:
- Left panel: chat with streaming responses + vim navigation
- Right panel: tabbed sidebar (Files/Agents/Stats/MCP)
- Gold/black theme matching DJcode brand
- Full keyboard navigation with vim keys
- All classic REPL features: slash commands, tool router, memory, orchestrator
- Command palette with fuzzy search
- Real-time token counting and session stats

Launch with: djcode (default) or djcode --repl for the line-oriented REPL
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Input,
    OptionList,
    RichLog,
    Static,
)
from textual.widgets.option_list import Option

from djcode import __version__
from djcode.commands import command_help, commands_for, match_commands
from djcode.conversation_log import ConversationLog
from djcode.tui_hacker import AgentStatusBar, HackerHeader
from djcode.tui_panels import SidePanel
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

COMMAND_REGISTRY = [(command.name, command.description) for command in commands_for("tui")]


# ── Help overlay screen ────────────────────────────────────────────────

HELP_TEXT = """\
[bold #C79B7A]Keyboard Shortcuts[/]

  [cyan]j / k[/]      Scroll chat down / up
  [cyan]g[/]          Jump to top of chat
  [cyan]G[/]          Jump to bottom of chat
  [cyan]/ + Enter[/]  Open command palette from the prompt
  [cyan]Escape[/]     Return focus to input
  [cyan]Tab[/]        Complete a slash command or cycle focus
  [cyan]Ctrl+O[/]     Toggle thinking display
  [cyan]Ctrl+P[/]     Toggle Plan / Act mode
  [cyan]Ctrl+L[/]     Clear chat history
  [cyan]Ctrl+T[/]     Toggle auto-accept tools
  [cyan]Ctrl+R[/]     Rerun last message
  [cyan]Ctrl+K[/]     Cancel generation
  [cyan]Ctrl+Q[/]     Quit DJcode TUI
  [cyan]F1[/]         This help screen
  [cyan]F2[/]         Model picker
  [cyan]F3[/]         Provider picker
  [cyan]F4[/]         Command palette
  [cyan]F5[/]         Agent roster
  [cyan]Up / Down[/]  Prompt history (or command suggestions)

[dim]F4 commands · Ctrl+B sidebar · Ctrl+K cancel · Escape closes[/]
"""
HELP_TEXT += "\n\n" + command_help("tui")


class ToolApprovalScreen(ModalScreen[bool]):
    """Native, per-call permission prompt; Escape always denies."""

    BINDINGS = [Binding("escape", "deny", "Deny")]
    DEFAULT_CSS = """
    ToolApprovalScreen { align: center middle; background: rgba(0, 0, 0, 0.85); }
    #approval-box { width: 76; height: auto; max-height: 85%; border: round #C79B7A; background: #111111; padding: 1 2; }
    #approval-details { height: auto; max-height: 12; overflow-y: auto; }
    #approval-actions { height: 3; margin-top: 1; }
    #approval-actions Button { margin-right: 2; }
    """

    def __init__(self, name: str, arguments: dict) -> None:
        super().__init__()
        self.tool_name = name
        self.arguments = arguments

    def compose(self) -> ComposeResult:
        import json

        with Vertical(id="approval-box"):
            yield Static(f"Approve tool: {self.tool_name}", markup=False)
            with ScrollableContainer(id="approval-details"):
                yield Static(json.dumps(self.arguments, indent=2, ensure_ascii=False), markup=False)
            with Horizontal(id="approval-actions"):
                yield Button("Deny", id="deny-tool")
                yield Button("Allow once", id="allow-tool", variant="warning")

    def on_mount(self) -> None:
        self.query_one("#deny-tool", Button).focus()

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "allow-tool")

    def action_deny(self) -> None:
        self.dismiss(False)


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
        border: double #C79B7A;
        padding: 1 2;
        overflow-y: auto;
    }
    """

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="help-box"):
            yield Static(HELP_TEXT)


# ── Agents overlay screen ──────────────────────────────────────────────

AGENTS_TEXT = """\
[bold #C79B7A]DJcode Agent Roster[/]

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
        border: double #C79B7A;
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
        border: double #C79B7A;
        padding: 1 2;
    }
    #model-title {
        height: 1;
        color: #C79B7A;
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }
    #model-search {
        height: 3;
        background: #1a1a1a;
        color: #C79B7A;
        border: solid #2a2a2a;
        margin-bottom: 1;
    }
    #model-search:focus {
        border: solid #C79B7A;
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
        scrollbar-color-hover: #C79B7A;
    }
    #model-list > .option-list--option-highlighted {
        background: #C79B7A 20%;
        color: #C79B7A;
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

        from djcode.startup import probe

        if self._base_url:
            cfg["base_url"] = self._base_url
        found = await asyncio.to_thread(probe, cfg, self._provider_name)
        self._models = [{"name": name} for name in found.get("models", [])]
        if not self._models:
            self.query_one("#model-info", Static).update(
                found["message"] + " Enter an exact model ID."
            )

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
            header = (
                "── All Models ──"
                if recent_matches
                else f"── {self._provider_name.capitalize()} Models ──"
            )
            option_list.add_option(Option(header, disabled=True))
            for name in all_matches:
                size = self._get_size(name)
                label = f"  {name:<40} {size}" if size else f"  {name}"
                option_list.add_option(Option(label, id=name))

        if not recent_matches and not all_matches and query_lower:
            # Allow custom model name entry
            option_list.add_option(Option(f"  Use custom: {query_lower}", id=query_lower))

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
    """Native provider and authentication selection with cancellable device sign-in."""

    BINDINGS = [Binding("escape", "dismiss", "Close")]

    DEFAULT_CSS = """
    ProviderPicker {
        align: center middle;
        background: rgba(0, 0, 0, 0.85);
    }
    #provider-box {
        width: 90%;
        max-width: 72;
        height: 30;
        max-height: 90%;
        background: #141414;
        border: double #C79B7A;
        padding: 1 2;
    }
    #provider-title {
        height: 1;
        color: #C79B7A;
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }
    #provider-search {
        height: 3;
        background: #1a1a1a;
        color: #C79B7A;
        border: solid #2a2a2a;
        margin-bottom: 1;
    }
    #provider-search:focus {
        border: solid #C79B7A;
    }
    #provider-list {
        height: 1fr;
        background: #101010;
        scrollbar-color: #2a2a2a;
        scrollbar-color-hover: #C79B7A;
    }
    #provider-list > .option-list--option-highlighted {
        background: #C79B7A 20%;
        color: #C79B7A;
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
        border: solid #C79B7A;
    }
    #provider-key-input {
        height: 3;
        background: #1a1a1a;
        color: #ededed;
        border: solid #2a2a2a;
        margin-top: 1;
    }
    #provider-key-input:focus {
        border: solid #C79B7A;
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
                Option(
                    "  + Custom URL          Any OpenAI-compatible endpoint", id="__custom_url__"
                )
            )
            option_list.add_option(Option("── Providers ──", disabled=True))

        for pid, name, desc in self._providers:
            if pid == "custom":
                continue  # Already shown as Custom URL
            if query_lower and query_lower not in name.lower() and query_lower not in pid.lower():
                continue
            needs_key = ""
            try:
                from djcode.account_auth import has_account
                from djcode.auth import PROVIDERS
                from djcode.config import load_config

                if PROVIDERS.get(pid, {}).get("needs_key"):
                    account = load_config().get(f"{pid}_auth_method") == "account"
                    needs_key = " [account]" if account and has_account(pid) else " [auth]"
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
        if self._mode == "auth":
            self._select_auth_method(provider_id)
            return

        if provider_id == "__custom_url__":
            # Show URL + key inputs
            self._mode = "configure"
            self._selected_provider = "custom"
            url_section = self.query_one("#provider-url-section")
            url_section.styles.display = "block"
            self.query_one("#provider-config-label", Static).update(
                "[bold #C79B7A]Configure Custom Endpoint[/]"
            )
            self.query_one("#provider-url-input", Input).focus()
            return

        from djcode.auth import PROVIDERS

        if PROVIDERS.get(provider_id, {}).get("needs_key"):
            from djcode.account_auth import auth_methods, has_account
            from djcode.config import load_config

            self._selected_provider = provider_id
            self._mode = "auth"
            self.query_one("#provider-title", Static).update("Authentication method")
            self.query_one("#provider-search", Input).styles.display = "none"
            options = self.query_one("#provider-list", OptionList)
            options.clear_options()
            current = load_config().get(f"{provider_id}_auth_method", "api_key")
            for method in auth_methods(provider_id):
                label = method["label"]
                if method["id"] == "account" and has_account(provider_id):
                    label += " · use connected account"
                if method["id"] == current:
                    label += " (current)"
                if not method["available"]:
                    label += " — " + method["reason"]
                options.add_option(Option(label, id=method["id"], disabled=not method["available"]))
                if method["id"] == current and method["available"]:
                    options.highlighted = options.option_count - 1
            options.focus()
            return
        self._selected_provider = provider_id
        self._finish_auth("api_key")

    def _select_auth_method(self, method: str) -> None:
        from djcode.account_auth import auth_methods, has_account
        from djcode.auth import get_api_key

        provider = self._selected_provider
        if not provider or method not in {
            item["id"] for item in auth_methods(provider) if item["available"]
        }:
            return
        if method == "account":
            if has_account(provider):
                self._finish_auth("account")
            else:
                self._mode = "signing_in"
                self.query_one("#provider-url-section").styles.display = "block"
                self.query_one("#provider-url-input").styles.display = "none"
                self.query_one("#provider-key-input").styles.display = "none"
                self.run_worker(self._sign_in_account(), group="account-sign-in", exclusive=True)
            return
        if get_api_key(provider):
            self._finish_auth("api_key")
            return
        self._mode = "configure"
        self.query_one("#provider-url-section").styles.display = "block"
        self.query_one("#provider-config-label", Static).update("Enter API key · Escape cancels")
        self.query_one("#provider-url-input", Input).styles.display = "none"
        self.query_one("#provider-key-input", Input).focus()

    async def _sign_in_account(self) -> None:
        from rich.text import Text

        from djcode.account_auth import AccountAuthError, begin_xai_login, finish_xai_login

        label = self.query_one("#provider-config-label", Static)
        try:
            device = await begin_xai_login()
            label.update(
                Text(
                    f"Open {device.verification_url}\nCode: {device.user_code}\nWaiting… Escape cancels"
                )
            )
            await finish_xai_login(device)
            self._finish_auth("account")
        except (AccountAuthError, OSError):
            label.update("Sign-in failed or expired. Escape closes; existing setup retained.")

    def _finish_auth(self, method: str, *, key: str = "", url: str = "") -> None:
        from djcode.config import load_config, save_config

        provider = self._selected_provider or "custom"
        cfg = load_config()
        if cfg.get("provider") != provider:
            cfg["base_url"] = ""
        cfg["provider"] = provider
        cfg[f"{provider}_auth_method"] = method
        if key:
            cfg[f"{provider}_api_key"] = key
        if url:
            cfg[f"{provider}_url"] = url
        try:
            save_config(cfg)
        except OSError:
            self.query_one("#provider-url-section").styles.display = "block"
            self.query_one("#provider-config-label", Static).update(
                "Unable to save credentials. Existing setup retained."
            )
            return
        result = {"provider": provider, "auth_method": method}
        if key:
            result["api_key"] = key
        if url:
            result["base_url"] = url
        self.dismiss(result)

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

        from djcode.auth import PROVIDERS, get_api_key

        provider = self._selected_provider or "custom"
        if PROVIDERS.get(provider, {}).get("needs_key") and not key and not get_api_key(provider):
            self.query_one("#provider-config-label", Static).update(
                "Enter an API key or Escape to retain your setup."
            )
            return
        if url and not url.startswith(("http://", "https://")):
            self.query_one("#provider-config-label", Static).update(
                "Enter an HTTP(S) API endpoint."
            )
            return
        self._finish_auth("api_key", key=key, url=url)

    @on(Input.Submitted, "#provider-search")
    def submit_search(self, event: Input.Submitted) -> None:
        if self._mode == "select":
            option_list = self.query_one("#provider-list", OptionList)
            if option_list.option_count > 0:
                for i in range(option_list.option_count):
                    opt = option_list.get_option_at_index(i)
                    if not opt.disabled and opt.id:
                        self.select_provider(OptionList.OptionSelected(option_list, opt, i))
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
        border: double #C79B7A;
        padding: 1 2;
    }
    #palette-title {
        height: 1;
        color: #C79B7A;
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }
    #palette-input {
        height: 3;
        background: #1a1a1a;
        color: #C79B7A;
        border: solid #333333;
        margin-bottom: 1;
    }
    #palette-input:focus {
        border: solid #C79B7A;
    }
    #palette-list {
        height: 1fr;
        background: #0a0a0a;
        scrollbar-color: #333333;
        scrollbar-color-hover: #C79B7A;
    }
    #palette-list > .option-list--option-highlighted {
        background: #C79B7A 20%;
        color: #C79B7A;
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
                *[Option(f"{cmd:<20} {desc}", id=cmd) for cmd, desc in self._all_commands],
                id="palette-list",
            )

    def on_mount(self) -> None:
        self.query_one("#palette-input", Input).focus()

    def on_key(self, event: events.Key) -> None:
        if event.key in ("up", "down"):
            options = self.query_one("#palette-list", OptionList)
            if options.option_count:
                current = options.highlighted
                options.highlighted = max(
                    0,
                    min(
                        options.option_count - 1,
                        (current if current is not None else 0)
                        + (1 if event.key == "down" else -1),
                    ),
                )
            event.stop()
            event.prevent_default()

    @on(Input.Changed, "#palette-input")
    def filter_commands(self, event: Input.Changed) -> None:
        """Filter the command list based on input."""
        query = event.value.lower().strip()
        option_list = self.query_one("#palette-list", OptionList)
        option_list.clear_options()

        for command in match_commands(query):
            option_list.add_option(
                Option(
                    f"{command.name:<20} {command.description}",
                    id=command.name,
                )
            )
        if option_list.option_count:
            option_list.highlighted = 0

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
            opt = option_list.get_option_at_index(option_list.highlighted or 0)
            if opt.id:
                self.dismiss(str(opt.id))


# ── Main application ───────────────────────────────────────────────────


class DJcodeApp(App):
    """DJcode split-pane TUI application.

    Lazygit-style layout with chat on the left and tabbed sidebar
    on the right. Streams LLM responses, vim keys, command palette.
    """

    CSS = DJCODE_CSS
    ENABLE_COMMAND_PALETTE = False  # F4 opens DJcode commands; Ctrl+P toggles plan mode.
    TITLE = "DJcode"
    SUB_TITLE = f"v{__version__}"

    BINDINGS = [
        Binding("ctrl+o", "toggle_thinking", "Thinking", show=False),
        Binding("ctrl+p", "toggle_plan", "Plan/Act", show=True, priority=True),
        Binding("ctrl+l", "clear_chat", "Clear", show=False),
        Binding("ctrl+t", "toggle_auto", "Auto", show=False),
        Binding("ctrl+r", "rerun", "Rerun", show=False),
        Binding("ctrl+k", "cancel", "Cancel", show=True, priority=True),
        Binding("ctrl+q", "quit", "Quit", show=True),
        Binding("f1", "show_help", "Help", show=True),
        Binding("f2", "show_model_picker", "Model", show=False),
        Binding("f3", "show_provider_picker", "Provider", show=False),
        Binding("f4", "show_palette", "Cmds", show=True),
        Binding("f5", "show_agents", "Agents", show=False),
        Binding("ctrl+b", "toggle_sidebar", "Sidebar", show=False),
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
        self._generation_task: asyncio.Task | None = None
        self._approval_lock = asyncio.Lock()
        self._sidebar_override: bool | None = None
        self._last_input = ""
        self._prompt_history: list[str] = []
        self._history_index = 0
        self._draft = ""
        self._followups = []
        self._provider: Any = None
        self._operator: Any = None
        self._memory: Any = None
        self._orchestrator: Any = None
        self._ext_manager: Any = None
        self._session_db: Any = None
        self._sqlite_session_id: str | None = None
        self._response_times: list[float] = []
        self._context_mgr: Any = None

    def compose(self) -> ComposeResult:
        yield HackerHeader(id="hacker-header")
        with Horizontal(id="main-layout"):
            with Vertical(id="chat-panel"):
                yield ConversationLog(
                    id="chat-log",
                    markup=True,
                    wrap=True,
                    highlight=True,
                    auto_scroll=True,
                )
                yield OptionList(id="cmd-suggest")
                yield Static("DAF + DDAL · default engine", id="workflow-state", markup=False)
                yield AgentStatusBar(id="agent-status-bar")
                yield Input(
                    id="prompt-input",
                    placeholder="❯ Ask anything, or / for commands",
                )
            yield SidePanel(project_path=Path.cwd(), id="side-panel")
        yield Static(self._build_status_text(), id="status-bar", markup=False)
        yield Footer(compact=True)

    def on_resize(self, event: events.Resize) -> None:
        self._update_layout()

    def _update_layout(self) -> None:
        if not self.is_mounted:
            return
        visible = self._sidebar_override if self._sidebar_override is not None else False
        self.query_one("#side-panel").display = visible
        self.query_one("#chat-panel").styles.width = "65%" if visible else "100%"
        self._refresh_status_bar()

    def action_toggle_sidebar(self) -> None:
        self._sidebar_override = not self.query_one("#side-panel").display
        self._update_layout()

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

        if self.size.width < 110:
            return f" {provider} · ↑{in_str} ↓{out_str} · {mode} · Approvals: {'auto' if self._auto_accept else 'ask'} · Ctrl+B sidebar"

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
        self._update_layout()
        chat = self.query_one("#chat-log", RichLog)
        chat.write(
            f"[bold {GOLD}]\u23fa DJcode[/] [dim]v{__version__}[/]  "
            f"[dim]project by Darshankumar Joshi[/]\n"
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
            from djcode.config import CONFIG_FILE
            from djcode.provider import Provider, ProviderConfig

            if not CONFIG_FILE.exists() and not self._provider_name:
                self._show_provider_picker()
                return

            config = ProviderConfig.from_config(
                provider_override=self._provider_name,
                model_override=self._model_name,
            )
            from djcode.account_auth import has_account
            from djcode.auth import PROVIDERS

            if PROVIDERS.get(config.name, {}).get("needs_key") and not (
                has_account(config.name) if config.auth_method == "account" else config.api_key
            ):
                self._show_provider_picker()
                return
            self._provider = Provider(config)

            # Validate model
            ok, msg = await asyncio.to_thread(self._provider.validate_model)
            if msg:
                side.agent_panel.add_tool_call("validate_model", "warning")
            if not ok:
                chat.write(f"[{ERROR}]Model error: {msg}[/]")
                self._show_provider_picker()
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
                approval_callback=self._approve_tool,
            )
            self._operator.auto_accept = self._auto_accept
            self._operator.workflow.event_callback = self._workflow_event

            # Initialize memory manager
            try:
                from djcode.memory.manager import MemoryManager

                self._memory = MemoryManager()
            except Exception:
                self._memory = None

            # Initialize orchestrator (v2 ShadowOrchestrator via compat wrapper)
            try:
                from djcode.orchestrator import Orchestrator

                self._orchestrator = Orchestrator(
                    self._provider,
                    auto_accept=self._auto_accept,
                    approval_callback=self._approve_tool,
                )
            except Exception:
                self._orchestrator = None

            # Initialize context window manager
            try:
                self._context_mgr = self._operator.context_manager
            except Exception:
                self._context_mgr = None

            # Initialize session DB
            try:
                from djcode.sessions import SessionDB

                self._session_db = SessionDB()
                self._sqlite_session_id = self._session_db.create_session(
                    model=self._provider.config.model,
                    provider=self._provider.config.name,
                    cwd=str(Path.cwd()),
                )
            except Exception as error:
                self._session_db = None
                self.notify(
                    f"Session history unavailable: {type(error).__name__}", severity="warning"
                )

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

            # Update HackerHeader with model info
            try:
                hdr = self.query_one("#hacker-header", HackerHeader)
                hdr.model_name = model
                hdr.mode = "PLAN" if self._plan_mode else "ACT"
                if self._context_mgr:
                    stats = self._context_mgr.stats
                    hdr.update_context(stats.current_tokens, stats.max_context_tokens)
            except Exception:
                pass

            from rich.text import Text

            chat.write(
                Text(
                    f"{prov} · {model} · approvals {'automatic' if self._auto_accept else 'ask first'}"
                )
            )
            chat.write("[dim]Ready · F4 commands · F2 model · Ctrl+B sidebar[/]\n")

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

        if text.startswith("/") and not any(char.isspace() for char in text):
            matches = match_commands(text)
            suggest.clear_options()
            if matches:
                for command in matches:
                    suggest.add_option(
                        Option(
                            f"{command.name:<16} {command.description}",
                            id=command.name,
                        )
                    )
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
        if len(self.screen_stack) > 1 or not isinstance(self.focused, Input):
            return
        if self.focused.id != "prompt-input":
            return
        suggest = self.query_one("#cmd-suggest", OptionList)
        if suggest.styles.display == "none":
            if event.key in ("up", "down") and self._prompt_history:
                event.prevent_default()
                event.stop()
                self._navigate_history(-1 if event.key == "up" else 1)
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

        elif event.key == "tab":
            event.prevent_default()
            event.stop()
            self._select_suggestion()

        elif event.key == "escape":
            event.prevent_default()
            event.stop()
            suggest.styles.display = "none"

    def _navigate_history(self, direction: int) -> None:
        prompt = self.query_one("#prompt-input", Input)
        if self._history_index == len(self._prompt_history):
            self._draft = prompt.value
        self._history_index = max(
            0,
            min(
                len(self._prompt_history),
                self._history_index + direction,
            ),
        )
        prompt.value = (
            self._draft
            if self._history_index == len(self._prompt_history)
            else self._prompt_history[self._history_index]
        )
        prompt.cursor_position = len(prompt.value)

    def _remember_prompt(self, text: str) -> None:
        if not self._prompt_history or self._prompt_history[-1] != text:
            self._prompt_history.append(text)
            self._prompt_history = self._prompt_history[-200:]
        self._history_index = len(self._prompt_history)
        self._draft = ""

    # ── Input handling ───────────────────────────────────────────────────

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle user input submission."""
        if event.input.id != "prompt-input":
            return

        text = event.value.strip()
        if not text:
            return

        inp = self.query_one("#prompt-input", Input)
        suggest = self.query_one("#cmd-suggest", OptionList)
        # Complete partial names; Enter on an exact command executes it.
        if (
            text != "/"
            and not any(char.isspace() for char in text)
            and suggest.display
            and suggest.option_count
            and text.lower() not in {name for name, _ in COMMAND_REGISTRY}
        ):
            self._select_suggestion()
            return

        if self._is_generating and not text.startswith("/"):
            self._followups.append(text)
            inp.value = ""
            self._remember_prompt(text)
            self.notify(f"Queued follow-up {len(self._followups)} · /queue to inspect")
            return

        if self._is_generating and text.split()[0].lower() not in {
            "/cancel",
            "/help",
            "/shortcuts",
            "/exit",
            "/quit",
            "/q",
            "/queue",
            "/jobs",
        }:
            self.notify("Response running. Draft kept; Ctrl+K cancels.", severity="warning")
            return

        if not text.startswith("/") and not self._operator:
            self.notify("Still initializing. Your draft is kept.", severity="warning")
            return

        suggest.styles.display = "none"
        self._remember_prompt(text)
        inp.value = ""

        # Bare "/" triggers command palette
        if text == "/":
            self.action_show_palette()
            return

        # Slash commands
        if text.startswith("/"):
            self.run_worker(self._run_slash_command(text), group="slash-command", exclusive=False)
            return

        # Normal chat message
        self._last_input = text
        self.run_worker(self._send_message(text), group="conversation", exclusive=False)

    async def _run_slash_command(self, text: str) -> None:
        """Keep the event pump free while a specialist awaits a permission modal."""
        long_commands = {
            "/spawn",
            "/waves",
            "/orchestra",
            "/scout",
            "/architect",
            "/review",
            "/debug",
            "/test",
            "/refactor",
            "/devops",
            "/launch",
            "/campaign",
            "/image",
            "/video",
            "/social",
            "/docs",
            "/recipe",
            "/search",
        }
        tracks_task = text.split()[0].lower() in long_commands
        if tracks_task:
            if self._is_generating:
                self.notify(
                    "Already generating. Cancel or wait for completion.", severity="warning"
                )
                return
            self._is_generating = True
            self._generation_task = asyncio.current_task()
        try:
            await self._handle_slash_command(text)
        except asyncio.CancelledError:
            self.query_one("#chat-log", RichLog).write(f"[{WARNING}]Command cancelled.[/]")
            raise
        except Exception as error:
            from rich.text import Text

            self.query_one("#chat-log", RichLog).write(
                Text(f"Command failed: {error}", style=ERROR)
            )
        finally:
            if tracks_task:
                self._is_generating = False
                self._generation_task = None

    async def _handle_slash_command(self, text: str) -> None:
        """Route slash commands — ported from classic REPL."""
        chat = self.query_one("#chat-log", RichLog)
        side = self.query_one(SidePanel)
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        from djcode.commands import plan_blocks_command

        if self._plan_mode and plan_blocks_command(cmd, arg):
            chat.write(
                "[yellow]Plan mode: switch to Act with /plan before running this command.[/]"
            )
            return

        from djcode.session_commands import NAMES, handle

        if cmd in NAMES:
            from rich.text import Text

            if self._operator:
                self._operator.session_db = self._session_db
                self._operator.session_id = self._sqlite_session_id
            chat.write(Text(await handle(self._operator, cmd, arg)))
            if cmd == "/workflow" and self._operator:
                self.query_one("#workflow-state", Static).update(
                    "DAF + DDAL · default engine"
                    if self._operator.workflow.mode == "daf"
                    else "Native engine · explicitly selected"
                )
            if self._operator:
                self._sqlite_session_id = self._operator.session_id
            return
        if cmd == "/queue":
            from rich.text import Text

            if arg == "clear":
                self._followups.clear()
            chat.write(
                Text(
                    "\n".join(f"{i + 1}. {text}" for i, text in enumerate(self._followups))
                    or "No queued follow-ups"
                )
            )
            return
        if cmd == "/connect":
            self._show_provider_picker()
            return

        if cmd == "/help":
            self.push_screen(HelpScreen())

        elif cmd == "/cancel":
            self.action_cancel()

        elif cmd == "/design":
            from rich.text import Text

            from djcode.design_packs import list_packs
            from djcode.design_selection import select_pack

            if not arg.strip():
                for pack in list_packs():
                    chat.write(Text(f"{pack['id']}: {pack['title']} — {pack['summary']}"))
                chat.write("Use /design ID to select one reference, or /design off to clear it.")
            else:
                try:
                    chat.write(Text(select_pack(self._operator, arg.strip())))
                except ValueError as error:
                    chat.write(Text(str(error), style="yellow"))

        elif cmd in ("/check", "/lint"):
            from djcode.maintenance import run_checks

            chat.write("[dim]Running checks…[/]")
            try:
                result = await asyncio.to_thread(run_checks)
                from rich.text import Text

                chat.write(Text(result["summary"], style="green" if result["ok"] else "yellow"))
                for check in result.get("checks", []):
                    chat.write(Text(f"{check['name']}: {check['status']} · {check['detail']}"))
            except Exception as exc:
                chat.write(f"[red]Check failed: {type(exc).__name__}[/]")

        elif cmd == "/update":
            from djcode.updater import perform_update

            chat.write("[dim]Checking for updates…[/]")
            try:
                result = await asyncio.to_thread(perform_update, force=True)
                from rich.text import Text

                chat.write(Text(result["message"], style="green" if result["ok"] else "yellow"))
                if result.get("updated"):
                    chat.write("Restart DJcode after your current work to use the update.")
            except Exception as exc:
                chat.write(f"[red]Update failed: {type(exc).__name__}[/]")

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

        elif cmd == "/extension":
            await self._handle_extension(arg)

        elif cmd == "/recipe":
            await self._handle_recipe(arg)

        elif cmd == "/history":
            self._handle_history(arg)

        elif cmd == "/resume":
            self._handle_resume(arg)

        elif cmd == "/docs":
            from djcode.docs import DOCS_SECTIONS

            if arg and arg not in DOCS_SECTIONS and arg != "all":
                await self._handle_agent_command(cmd, arg)
            else:
                self._handle_docs(arg)

        elif cmd == "/todo":
            self._handle_todo(arg)

        elif cmd == "/cost":
            self._show_cost()

        elif cmd == "/search":
            await self._handle_search(arg)

        elif cmd == "/tasks":
            await self._handle_tasks(arg)

        elif cmd == "/spawn":
            await self._handle_spawn(arg)

        elif cmd == "/context":
            self._show_context()

        elif cmd == "/waves":
            await self._handle_waves(arg)

        # Agent dispatch commands — send to orchestrator or operator
        elif cmd in (
            "/scout",
            "/architect",
            "/orchestra",
            "/review",
            "/debug",
            "/test",
            "/refactor",
            "/devops",
            "/launch",
            "/campaign",
            "/image",
            "/video",
            "/social",
        ):
            await self._handle_agent_command(cmd, arg)

        elif cmd == "/auth":
            self._show_provider_picker()

        else:
            chat.write(f"[{WARNING}]Unknown command: {cmd}. Use /help or the command palette.[/]")

    # ── New v4.0 command handlers ────────────────────────────────────────

    async def _handle_search(self, query: str) -> None:
        """Execute a web search via the web_search tool."""
        chat = self.query_one("#chat-log", RichLog)
        if not query:
            chat.write(f"[{WARNING}]Usage: /search <query>[/]")
            return
        chat.write(f"\n[bold {GOLD}]\u276f[/] [{GOLD}]/search {query}[/]")
        chat.write("[dim]Searching...[/]")
        try:
            from djcode.tools import dispatch_tool

            result = await dispatch_tool("web_search", {"query": query})
            chat.write(result)
        except Exception as e:
            chat.write(f"[{ERROR}]Search error: {e}[/]")

    async def _handle_tasks(self, arg: str) -> None:
        """Manage session tasks: list, create, update."""
        chat = self.query_one("#chat-log", RichLog)
        from djcode.tools import dispatch_tool

        parts = arg.split(maxsplit=1) if arg else []
        sub = parts[0].lower() if parts else "list"
        rest = parts[1] if len(parts) > 1 else ""

        try:
            if sub == "list" or not arg:
                result = await dispatch_tool("task_list", {})
                chat.write(f"\n[bold {GOLD}]Session Tasks[/]\n{result}")
            elif sub == "add" and rest:
                result = await dispatch_tool("task_create", {"title": rest})
                chat.write(f"[{SUCCESS}]Task created: {rest}[/]")
            elif sub == "done" and rest:
                result = await dispatch_tool("task_update", {"task_id": rest, "status": "done"})
                chat.write(f"[{SUCCESS}]Task updated: {rest}[/]")
            else:
                chat.write(f"[{WARNING}]Usage: /tasks [list|add <title>|done <id>][/]")
        except Exception as e:
            chat.write(f"[{ERROR}]Task error: {e}[/]")

    async def _handle_spawn(self, arg: str) -> None:
        """Spawn a specialist agent via the spawn_agent tool."""
        chat = self.query_one("#chat-log", RichLog)
        if not arg:
            chat.write(f"[{WARNING}]Usage: /spawn <agent_role> <task>[/]")
            return
        parts = arg.split(maxsplit=1)
        role = parts[0]
        task = parts[1] if len(parts) > 1 else "work on this codebase"
        chat.write(f"\n[bold {GOLD}]\u276f[/] [{GOLD}]/spawn {role}[/]")
        chat.write(f"[dim]Spawning {role}...[/]")
        try:
            from djcode.tools import dispatch_tool
            from djcode.tools.agent_spawn import agent_context

            with agent_context(self._provider, self._auto_accept, self._approve_tool):
                result = await dispatch_tool("spawn_agent", {"role": role, "task": task})
            chat.write(result)
            # Update agent status bar
            try:
                bar = self.query_one("#agent-status-bar", AgentStatusBar)
                bar.set_agent_state(
                    role.capitalize(), "error" if result.startswith("Error") else "done"
                )
            except Exception:
                pass
        except Exception as e:
            chat.write(f"[{ERROR}]Spawn error: {e}[/]")

    def _show_context(self) -> None:
        """Show context window utilization stats."""
        chat = self.query_one("#chat-log", RichLog)
        if not self._context_mgr:
            chat.write(f"[{WARNING}]Context manager not initialized.[/]")
            return
        try:
            stats = self._context_mgr.stats
            chat.write(
                f"\n[bold {GOLD}]Context Window[/]\n"
                f"  [dim]Model:[/]        [{GOLD}]{stats.model}[/]\n"
                f"  [dim]Max tokens:[/]   [{GOLD}]{stats.max_context_tokens:,}[/]\n"
                f"  [dim]Current:[/]      [{GOLD}]{stats.current_tokens:,}[/]\n"
                f"  [dim]Utilization:[/]  [{GOLD}]{stats.utilization_pct:.1f}%[/]\n"
                f"  [dim]Remaining:[/]    [{GOLD}]{stats.remaining_tokens:,}[/]\n"
                f"  [dim]Messages:[/]     [{GOLD}]{stats.message_count}[/]\n"
                f"  [dim]Pinned:[/]       [{GOLD}]{stats.pinned_count}[/]\n"
                f"  [dim]Compressions:[/] [{GOLD}]{stats.compressions_performed}[/]\n"
            )
            # Update HackerHeader context display
            try:
                hdr = self.query_one("#hacker-header", HackerHeader)
                hdr.update_context(stats.current_tokens, stats.max_context_tokens)
            except Exception:
                pass
        except Exception as e:
            chat.write(f"[{ERROR}]Context error: {e}[/]")

    async def _handle_waves(self, arg: str) -> None:
        """Run multi-agent wave execution via the ShadowOrchestrator."""
        chat = self.query_one("#chat-log", RichLog)
        if not arg:
            chat.write(f"[{WARNING}]Usage: /waves <task description>[/]")
            return
        if not self._orchestrator:
            chat.write(f"[{ERROR}]Orchestrator not initialized.[/]")
            return
        chat.write(f"\n[bold {GOLD}]\u276f[/] [{GOLD}]/waves {arg}[/]")
        chat.write("[dim]Launching wave execution...[/]")
        try:
            # Access the ShadowOrchestrator's event-based execute
            shadow = self._orchestrator._shadow if hasattr(self._orchestrator, "_shadow") else None
            if shadow:
                from djcode.orchestrator.engine import ExecutionStrategy
                from djcode.orchestrator.events import EventType

                completed = False
                async for event in shadow.execute(arg, strategy_override=ExecutionStrategy.WAVE):
                    if event.event_type in (EventType.AGENT_ERROR, EventType.ORCHESTRATOR_ERROR):
                        raise RuntimeError(event.data.get("error", "Wave execution failed"))
                    if event.event_type == EventType.ORCHESTRATOR_COMPLETE:
                        completed = True
                    if event.event_type == EventType.AGENT_TOKEN:
                        token = event.data.get("token", "")
                        if token:
                            chat.write(token, shrink=True, scroll_end=True)
                    elif event.event_type == EventType.WAVE_START:
                        wave = event.data.get("wave", "?")
                        chat.write(f"\n  [{GOLD}]Wave {wave} starting...[/]")
                    elif event.event_type == EventType.WAVE_COMPLETE:
                        wave = event.data.get("wave", "?")
                        chat.write(f"\n  [{SUCCESS}]Wave {wave} complete.[/]")
                    elif event.event_type == EventType.AGENT_START:
                        name = event.data.get("agent_name", "")
                        try:
                            bar = self.query_one("#agent-status-bar", AgentStatusBar)
                            bar.set_agent_state(name, "executing")
                        except Exception:
                            pass
                    elif event.event_type == EventType.AGENT_COMPLETE:
                        name = event.data.get("agent_name", "")
                        try:
                            bar = self.query_one("#agent-status-bar", AgentStatusBar)
                            bar.set_agent_state(name, "done")
                        except Exception:
                            pass
                if not completed:
                    raise RuntimeError("Wave execution ended without completion")
                chat.write(f"\n[{SUCCESS}]Wave execution complete.[/]")
                # Reset agent bar
                try:
                    self.query_one("#agent-status-bar", AgentStatusBar).set_all_idle()
                except Exception:
                    pass
            else:
                # Fallback: use the compat wrapper's token-yielding execute
                async for token in self._orchestrator.execute(arg):
                    chat.write(token, shrink=True, scroll_end=True)
        except Exception as e:
            chat.write(f"[{ERROR}]Wave execution error: {e}[/]")

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
            "/docs": ("Saraswati", "docs", "Documentation"),
        }

        if cmd == "/orchestra" and self._orchestrator:
            if not arg:
                chat.write(f"[{WARNING}]Usage: /orchestra <task>[/]")
                return
            chat.write(f"\n[bold {GOLD}]\u276f[/] [{GOLD}]{cmd} {arg}[/]")
            side.agent_panel.set_agent("Orchestra", "Multi-agent")
            chat.write("[dim]Dispatching to agent orchestra...[/]")
            try:
                async for token in self._orchestrator.execute(arg):
                    chat.write(token, shrink=True, scroll_end=True)
                side.agent_panel.add_tool_call("orchestra", "ok")
            except Exception as e:
                chat.write(f"[{ERROR}]Orchestra error: {e}[/]")
                side.agent_panel.add_tool_call("orchestra", "error")
            side.agent_panel.set_agent("Operator", "General")
            return

        if cmd in agent_map and self._orchestrator:
            agent_name, _, desc = agent_map[cmd]
            from djcode.agents.registry import AgentRole

            role_map = {
                "/scout": AgentRole.SCOUT,
                "/architect": AgentRole.ARCHITECT,
                "/review": AgentRole.REVIEWER,
                "/debug": AgentRole.DEBUGGER,
                "/test": AgentRole.TESTER,
                "/refactor": AgentRole.REFACTORER,
                "/devops": AgentRole.DEVOPS,
                "/docs": AgentRole.DOCS,
            }
            role = role_map.get(cmd)
            task = arg or "work on this codebase"
            chat.write(f"\n[bold {GOLD}]\u276f[/] [{GOLD}]{cmd} {task}[/]")
            side.agent_panel.set_agent(agent_name, desc)
            chat.write(f"[dim]{agent_name} working...[/]")
            try:
                async for token in self._orchestrator.run_single_agent_streaming(role, task):
                    chat.write(token, shrink=True, scroll_end=True)
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
            if cmd == "/launch" and not self._orchestrator:
                chat.write(f"[{ERROR}]Launch requires an initialized orchestrator.[/]")
                return
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
                        chat.write(token, shrink=True, scroll_end=True)
                    # Phase 2: Campaign
                    chat.write(f"  [{GOLD}]Phase 2: Campaign[/]")
                    spec = get_content_spec(ContentRole.CAMPAIGN_DIRECTOR)
                    runner = AgentRunner(
                        self._provider,
                        spec,
                        self._orchestrator.bus,
                        auto_accept=self._auto_accept,
                        approval_callback=self._approve_tool,
                    )
                    async for token in runner.run_streaming(
                        f"Create a full launch campaign for: {arg}"
                    ):
                        chat.write(token, shrink=True, scroll_end=True)
                    chat.write(f"\n  [{GOLD}]Launch complete.[/]\n")
                elif cmd in role_map:
                    role = role_map[cmd]
                    spec = get_content_spec(role)
                    bus = self._orchestrator.bus if self._orchestrator else None
                    runner = AgentRunner(
                        self._provider,
                        spec,
                        bus,
                        auto_accept=self._auto_accept,
                        approval_callback=self._approve_tool,
                    )
                    async for token in runner.run_streaming(arg or "create content"):
                        chat.write(token, shrink=True, scroll_end=True)

                side.agent_panel.add_tool_call(cmd.lstrip("/"), "ok")
            except Exception as e:
                chat.write(f"[{ERROR}]Error: {e}[/]")
                side.agent_panel.add_tool_call(cmd.lstrip("/"), "error")
            return

        chat.write(f"[{ERROR}]Specialist execution unavailable. Check provider initialization.[/]")

    def _show_model_picker(self) -> None:
        """Open interactive model picker overlay."""
        if self._is_generating:
            self.notify("Cancel the response before switching models.", severity="warning")
            return
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
        if self._is_generating:
            self.notify("Cancel the response before switching providers.", severity="warning")
            return

        from djcode.connect import ConnectScreen

        def connected(result):
            if result:
                self.run_worker(self._apply_connection(result), group="connection")

        self.push_screen(ConnectScreen(), callback=connected)

    async def _apply_connection(self, config):
        from djcode.provider import Provider, ProviderConfig

        self._provider_name = config["provider"]
        self._model_name = config["model"]
        previous = self._provider
        if not self._operator:
            if previous:
                await previous.close()
            self._provider = None
            await self._initialize()
            return
        self._provider = Provider(ProviderConfig.from_config(self._provider_name, self._model_name))
        if self._operator:
            self._operator.provider = self._provider
            self._operator.context_manager.provider = self._provider
            # Keep the conversation and its session-owned tools when switching models.
            self._provider._session_runtimes = [self._operator.capabilities]
            if previous:
                previous._session_runtimes = []
            if self._orchestrator:
                self._orchestrator.provider = self._provider
                self._orchestrator._shadow.provider = self._provider
        else:
            await self._initialize()
        if previous:
            await previous.close()
        self._refresh_status_bar()
        self.notify(f"Connected to {self._provider_name} / {self._model_name}")

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
        ok, msg = await asyncio.to_thread(self._provider.validate_model)

        if ok:
            new_model = self._provider.config.model
            chat.write(f"[{SUCCESS}]Model: {old_model} -> {new_model}[/]")
            self.sub_title = f"v{__version__} | {new_model} | {self._provider.config.name}"

            if self._provider._new_provider is not None:
                await self._provider._new_provider.close()
                self._provider._new_provider = None
            from djcode.context.manager import ContextWindowManager

            self._operator.context_manager = ContextWindowManager(
                model=new_model, provider=self._provider
            )
            self._operator.context_manager.replace_messages(self._operator.messages)
            self._context_mgr = self._operator.context_manager
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
                approval_callback=self._approve_tool,
            )
            self._operator.auto_accept = self._auto_accept

            # Re-initialize orchestrator with new provider
            try:
                from djcode.orchestrator import Orchestrator

                self._orchestrator = Orchestrator(
                    self._provider,
                    auto_accept=self._auto_accept,
                    approval_callback=self._approve_tool,
                )
            except Exception:
                pass

            # Re-initialize context manager with new model
            try:
                self._context_mgr = self._operator.context_manager
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
                chat.write(f"[{WARNING}]No models found. Is Ollama running?[/]")
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
            chat.write("[dim]Usage: /set key=value[/]")
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
        display = (
            "***"
            if any(word in key.lower() for word in ("key", "token", "secret", "password"))
            else str(parsed)
        )
        chat.write(f"[{SUCCESS}]Set {key}={display}[/]")

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
        chat.write(f"  [dim]Mode:[/]         [{GOLD}]{'PLAN' if self._plan_mode else 'ACT'}[/]")
        chat.write(f"  [dim]Thinking:[/]     [{GOLD}]{'ON' if self._show_thinking else 'OFF'}[/]")
        chat.write(f"  [dim]Auto-accept:[/]  [{GOLD}]{'ON' if self._auto_accept else 'OFF'}[/]")
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
            chat.write("[dim]Usage: /remember key=value[/]")
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
            chat.write("[dim]Usage: /recall <key>[/]")
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
            chat.write("[dim]Usage: /forget <key>[/]")
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
        chat.write("  dolphin3       — Fully uncensored, no RLHF")
        chat.write("  abliterated    — RLHF removed via activation engineering")
        chat.write("  wizard-vicuna  — Classic unrestricted")
        chat.write("  nous-hermes    — Minimal alignment")
        chat.write("\n[dim]Switch with: /model dolphin3[/]\n")

    def _handle_docs(self, arg: str) -> None:
        chat = self.query_one("#chat-log", RichLog)
        try:
            # Capture docs output as text (docs module uses Rich console)
            if arg.strip():
                from rich.markdown import Markdown

                from djcode.docs import DOCS_SECTIONS

                sections = (
                    DOCS_SECTIONS.values()
                    if arg.strip() == "all"
                    else [DOCS_SECTIONS.get(arg.strip(), "Unknown documentation topic")]
                )
                for section in sections:
                    chat.write(Markdown(section))
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
                chat.write("[dim]No todos. Use /todo add <text>[/]")
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
            chat.write("[dim]Usage: /todo [add|done|rm|list] ...[/]")

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
                chat.write("[dim]No recipes found.[/]")
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
                    chat.write(token, shrink=True, scroll_end=True)
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
                chat.write("[dim]No past sessions.[/]")
            else:
                chat.write(f"\n[bold {GOLD}]Recent Sessions[/]")
                for s in sessions:
                    chat.write(
                        f"  [{GOLD}]{s.id[:8]}[/] "
                        f"[dim]{s.model} | {s.start[:16]}[/] "
                        f"[dim]{s.messages_count} msgs[/]"
                    )
                chat.write("\n[dim]Resume with: /resume <session_id>[/]\n")

        elif sub[0] == "search" and len(sub) >= 2:
            results = self._session_db.search_sessions(sub[1])
            if results:
                chat.write(f"\n[bold {GOLD}]Sessions matching '{sub[1]}':[/]")
                for s in results:
                    chat.write(f"  [{GOLD}]{s.id[:8]}[/] [dim]{s.model} | {s.start[:16]}[/]")
                chat.write("")
            else:
                chat.write(f"[dim]No sessions match '{sub[1]}'[/]")

    def _handle_resume(self, arg: str) -> None:
        chat = self.query_one("#chat-log", RichLog)
        if not arg.strip():
            chat.write("[dim]Usage: /resume <session_id>[/]")
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
                    _Msg(
                        role=role,
                        content=content,
                        tool_calls=tc or [],
                        tool_call_id=m.get("tool_call_id"),
                        name=m.get("name"),
                        images=m.get("images", []),
                    )
                )
                restored += 1

            chat.write(
                f"[{SUCCESS}]Resumed session {target_id} ({session.model}, {restored} messages)[/]"
            )

    # ── Message sending and streaming ────────────────────────────────────

    async def _approve_tool(self, name: str, arguments: dict) -> bool:
        if self._plan_mode:
            return False
        async with self._approval_lock:
            return await self._show_tool_approval(name, arguments)

    async def _show_tool_approval(self, name: str, arguments: dict) -> bool:
        decision = asyncio.get_running_loop().create_future()
        screen = ToolApprovalScreen(name, arguments)

        def finish(value: bool | None) -> None:
            if not decision.done():
                decision.set_result(bool(value))

        self.push_screen(screen, finish)
        try:
            return await decision
        finally:
            if self.screen is screen:
                self.pop_screen()

    def _workflow_event(self, event):
        from rich.text import Text

        kind = event.get("event")
        if kind == "preparing":
            label = "DAF + DDAL · preparing engine"
        elif kind == "tool":
            label = f"DAF + DDAL · {event['name']}"
            self.query_one("#chat-log", RichLog).write(Text(f"  ● {event['name']}", style=GOLD))
        elif kind == "complete":
            label = "DAF + DDAL · completed" if event.get("ok") else "DAF + DDAL · failed"
        else:
            return
        self.query_one("#workflow-state", Static).update(label)

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
        self._generation_task = asyncio.current_task()
        self._cancel_requested = False

        # Show user message
        from rich.text import Text

        chat.write(Text(f"\n❯ {text}", style=GOLD))

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
                chat.write("[dim]Enhanced with context[/]")
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

            self._operator.plan_mode = self._plan_mode
            if self._session_db and self._sqlite_session_id:
                self._operator.on_checkpoint = lambda messages: self._session_db.save_conversation(
                    self._sqlite_session_id, messages
                )
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
                            chat.write(Text(part), shrink=True, scroll_end=True)
                        else:
                            chat.write("", shrink=True, scroll_end=True)
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
                chat.write(Text(line_buf), shrink=True, scroll_end=True)

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
                if self._response_times
                else 0
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
                    self._session_db.save_conversation(
                        self._sqlite_session_id, self._operator.messages
                    )
                    self._session_db.update_session(
                        self._sqlite_session_id,
                        tokens_out=token_est,
                        messages=1,
                    )
                except Exception:
                    pass

            # Stats line
            if response_buf:
                tok_str = f"{token_est / 1000:.1f}k" if token_est >= 1000 else str(token_est)
                chat.write(f"[dim]\u2193 {tok_str} tokens \u00b7 {elapsed:.1f}s[/]")

            chat.write("")  # Blank line after response

            # Update memory panel
            if self._memory:
                stats = self._memory.stats
                side.agent_panel.update_memory(
                    session=stats.get("session_messages", 0),
                    facts=stats.get("persistent_facts", 0),
                    vectors=stats.get("facts_with_embeddings", 0),
                )

        except asyncio.CancelledError:
            chat.write(f"\n[{WARNING}]Generation cancelled.[/]")
            raise
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
            self._generation_task = None
            if self._followups and not self._cancel_requested:
                next_input = self._followups.pop(0)
                self.call_later(
                    lambda: self.run_worker(self._send_message(next_input), group="conversation")
                )

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
        if self._operator:
            self._operator.plan_mode = self._plan_mode
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
        self._refresh_status_bar()

    def action_clear_chat(self) -> None:
        """Clear the chat log and reset conversation."""
        if self._is_generating:
            self.notify("Cancel the response before clearing the conversation.", severity="warning")
            return
        chat = self.query_one("#chat-log", RichLog)
        chat.clear()
        chat.write("[dim]Chat cleared.[/]\n")

        if self._operator:
            try:
                self._operator.reset()
            except AttributeError:
                from djcode.prompt import build_system_prompt
                from djcode.provider import Message

                model = self._provider.config.model if self._provider else ""
                self._operator.messages = [
                    Message(
                        role="system",
                        content=build_system_prompt(
                            bypass_rlhf=self._bypass_rlhf,
                            model=model,
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
        if self._orchestrator:
            self._orchestrator.auto_accept = self._auto_accept
            self._orchestrator._shadow.auto_accept = self._auto_accept
        label = "ON" if self._auto_accept else "OFF"
        chat = self.query_one("#chat-log", RichLog)
        chat.write(f"[dim]Auto-accept: {label}[/]")
        self.notify(f"Auto-accept: {label}", severity="information")
        self._refresh_status_bar()

    def action_rerun(self) -> None:
        """Rerun the last message."""
        if self._last_input and not self._is_generating:
            chat = self.query_one("#chat-log", RichLog)
            chat.write(f"[dim]Rerunning: {self._last_input}[/]")
            self.run_worker(
                self._send_message(self._last_input),
                group="conversation",
                exclusive=False,
            )
        else:
            self.notify("Nothing to rerun", severity="warning")

    def action_cancel(self) -> None:
        """Cancel current generation."""
        if self._is_generating:
            self._cancel_requested = True
            if self._generation_task and not self._generation_task.done():
                self._generation_task.cancel()
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
        from djcode.tools.agent_spawn import cancel_background_agents

        await cancel_background_agents()
        if self._session_db and self._sqlite_session_id:
            try:
                if self._operator:
                    self._session_db.save_conversation(
                        self._sqlite_session_id,
                        self._operator.messages,
                    )
                self._session_db.end_session(self._sqlite_session_id)
            except Exception:
                import logging

                logging.getLogger(__name__).exception("Could not save terminal session on exit")
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
