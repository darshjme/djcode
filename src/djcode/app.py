"""DJcode Textual TUI — lazygit-style split-pane interface.

Premium terminal experience with:
- Left panel: chat history with streaming responses
- Right panel: agent dashboard with tool calls, stats
- Gold/black theme matching DJcode brand
- Full keyboard navigation
- Real-time token counting and session stats

Launch with: djcode --tui
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, RichLog, Static

from djcode import __version__
from djcode.tui_theme import (
    DJCODE_CSS,
    ERROR,
    GOLD,
    SUCCESS,
    THINKING,
    WARNING,
)


# ── Help overlay screen ──────────────────────────────────────────────────

HELP_TEXT = """\
[bold #FFD700]Keyboard Shortcuts[/]

  [cyan]Ctrl+O[/]   Toggle thinking display
  [cyan]Ctrl+P[/]   Toggle Plan / Act mode
  [cyan]Ctrl+L[/]   Clear chat history
  [cyan]Ctrl+T[/]   Toggle auto-accept tools
  [cyan]Ctrl+Q[/]   Quit DJcode TUI
  [cyan]Tab[/]      Cycle panel focus
  [cyan]F1[/]       This help screen
  [cyan]F2[/]       Agent roster
  [cyan]F3[/]       Docs & info

[bold #FFD700]Slash Commands[/]

  [cyan]/model[/] <name>    Switch model
  [cyan]/provider[/] <name> Switch provider
  [cyan]/clear[/]           Clear conversation
  [cyan]/agents[/]          Show agent roster
  [cyan]/stats[/]           Usage dashboard
  [cyan]/config[/]          Show configuration
  [cyan]/help[/]            Show this help
  [cyan]/exit[/]            Quit

[dim]Press Escape to close[/]
"""


class HelpScreen(ModalScreen[None]):
    """Modal help overlay."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
    ]

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.85);
    }
    #help-box {
        width: 64;
        height: auto;
        max-height: 80%;
        background: #111111;
        border: double #FFD700;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Static(HELP_TEXT)


# ── Agents overlay screen ────────────────────────────────────────────────

AGENTS_TEXT = """\
[bold #FFD700]DJcode Agent Roster[/]

[bold]Build Agents[/]
  [cyan]Mitra[/]       Default operator — general coding
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

[bold]Specialist Agents[/]
  [cyan]Scout[/]       Read-only codebase exploration
  [cyan]Architect[/]   Implementation planning

[dim]Agents auto-dispatch via /orchestra or route manually.[/]
[dim]Press Escape to close[/]
"""


class AgentsScreen(ModalScreen[None]):
    """Modal agent roster overlay."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
    ]

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


# ── Main application ─────────────────────────────────────────────────────


class DJcodeApp(App):
    """DJcode split-pane TUI application.

    Lazygit-style layout with chat on the left and agent dashboard
    on the right. Streams LLM responses token-by-token.
    """

    CSS = DJCODE_CSS
    TITLE = "DJcode"
    SUB_TITLE = f"v{__version__}"

    BINDINGS = [
        Binding("ctrl+o", "toggle_thinking", "Thinking", show=True),
        Binding("ctrl+p", "toggle_plan", "Plan/Act", show=True),
        Binding("ctrl+l", "clear_chat", "Clear", show=True),
        Binding("ctrl+t", "toggle_auto", "Auto-accept", show=True),
        Binding("ctrl+q", "quit", "Quit", show=True),
        Binding("f1", "show_help", "Help", show=True),
        Binding("f2", "show_agents", "Agents", show=True),
        Binding("f3", "show_docs", "Docs", show=False),
        Binding("tab", "focus_next", "Focus Next", show=False),
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
        self._session_start = time.time()
        self._active_agent = "Mitra"
        self._is_generating = False
        self._provider: Any = None
        self._operator: Any = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-layout"):
            with Vertical(id="chat-panel"):
                yield RichLog(id="chat-log", markup=True, wrap=True)
                yield Input(
                    id="prompt-input",
                    placeholder="\u276f Type a message... (/help for commands)",
                )
            with Vertical(id="side-panel"):
                yield Static(
                    self._build_agent_header(), id="agent-header"
                )
                yield RichLog(id="agent-log", markup=True, wrap=True)
                yield Static(self._build_stats_bar(), id="stats-bar")
        yield Footer()

    async def on_mount(self) -> None:
        """Initialize provider, operator, and welcome message on mount."""
        chat = self.query_one("#chat-log", RichLog)
        chat.write(
            f"[bold {GOLD}]\u23fa DJcode[/] [dim]v{__version__}[/]  "
            f"[dim]Local-first AI coding CLI by DarshJ.AI[/]\n"
        )

        # Initialize provider and operator in background
        self._init_worker = self.run_worker(self._initialize(), exclusive=True)

    async def _initialize(self) -> None:
        """Set up Provider and Operator (runs in background)."""
        chat = self.query_one("#chat-log", RichLog)
        agent_log = self.query_one("#agent-log", RichLog)

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
                agent_log.write(f"[{WARNING}]{msg}[/]")
            if not ok:
                chat.write(f"[{ERROR}]Model error: {msg}[/]")
                return

            from djcode.agents.operator import Operator

            self._operator = Operator(
                self._provider,
                bypass_rlhf=self._bypass_rlhf,
                raw=True,  # We handle formatting ourselves
                model=self._provider.config.model,
                auto_accept=self._auto_accept,
                show_thinking=False,  # We render thinking in the TUI
            )
            # Override auto_accept on operator so tool gate is skipped in TUI
            self._operator.auto_accept = self._auto_accept

            model = self._provider.config.model
            prov = self._provider.config.name
            self.sub_title = f"v{__version__} | {model} | {prov}"

            chat.write(
                f"  [dim]Model:[/]    [{GOLD}]{model}[/]\n"
                f"  [dim]Provider:[/] [{GOLD}]{prov}[/]\n"
                f"  [dim]Mode:[/]     [{GOLD}]{'PLAN' if self._plan_mode else 'ACT'}[/]\n"
                f"  [dim]Thinking:[/] [{GOLD}]{'ON' if self._show_thinking else 'OFF'}[/]\n"
            )
            chat.write(f"[dim]Ready. Type a message or /help for commands.[/]\n")

            agent_log.write(f"[{SUCCESS}]\u2713 Provider initialized[/]")
            agent_log.write(f"[dim]  {prov} / {model}[/]")

            self._update_stats_bar()
            self._update_agent_header()

        except Exception as e:
            chat.write(f"[{ERROR}]Initialization error: {e}[/]")

    # ── Input handling ────────────────────────────────────────────────────

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle user input submission."""
        text = event.value.strip()
        if not text:
            return

        inp = self.query_one("#prompt-input", Input)
        inp.value = ""

        # Slash commands
        if text.startswith("/"):
            await self._handle_slash_command(text)
            return

        # Normal chat message
        await self._send_message(text)

    async def _handle_slash_command(self, text: str) -> None:
        """Route slash commands."""
        chat = self.query_one("#chat-log", RichLog)
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "/help":
            self.push_screen(HelpScreen())
        elif cmd == "/clear":
            self.action_clear_chat()
        elif cmd == "/exit" or cmd == "/quit":
            self.exit()
        elif cmd == "/agents":
            self.push_screen(AgentsScreen())
        elif cmd == "/model":
            await self._handle_model_switch(arg)
        elif cmd == "/provider":
            await self._handle_provider_switch(arg)
        elif cmd == "/config":
            self._show_config()
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
        else:
            # Try to pass unrecognized slash commands as agent tasks
            # e.g. /review, /debug, /test, /orchestra
            if self._operator:
                await self._send_message(text)
            else:
                chat.write(f"[{WARNING}]Unknown command: {cmd}[/]")

    async def _handle_model_switch(self, model_name: str) -> None:
        """Switch model."""
        chat = self.query_one("#chat-log", RichLog)
        agent_log = self.query_one("#agent-log", RichLog)

        if not model_name:
            chat.write(f"[{WARNING}]Usage: /model <name>[/]")
            return

        if not self._provider:
            chat.write(f"[{ERROR}]Provider not initialized yet.[/]")
            return

        old_model = self._provider.config.model
        self._provider.config.model = model_name
        ok, msg = self._provider.validate_model()

        if ok:
            new_model = self._provider.config.model
            chat.write(
                f"[{SUCCESS}]Model switched: {old_model} -> {new_model}[/]"
            )
            agent_log.write(f"[dim]Model: {new_model}[/]")
            self.sub_title = (
                f"v{__version__} | {new_model} | {self._provider.config.name}"
            )

            # Re-create operator with new model
            from djcode.agents.operator import Operator
            from djcode.prompt import build_system_prompt

            self._operator = Operator(
                self._provider,
                bypass_rlhf=self._bypass_rlhf,
                raw=True,
                model=new_model,
                auto_accept=self._auto_accept,
                show_thinking=False,
            )
            self._operator.auto_accept = self._auto_accept
        else:
            self._provider.config.model = old_model
            chat.write(f"[{ERROR}]{msg}[/]")

    async def _handle_provider_switch(self, provider_name: str) -> None:
        """Switch provider."""
        chat = self.query_one("#chat-log", RichLog)

        if not provider_name:
            chat.write(f"[{WARNING}]Usage: /provider <name>[/]")
            chat.write(
                "[dim]Available: ollama, openai, anthropic, nvidia, "
                "google, groq, together, openrouter, mlx, remote[/]"
            )
            return

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

            model = self._provider.config.model
            prov = self._provider.config.name
            self.sub_title = f"v{__version__} | {model} | {prov}"
            chat.write(f"[{SUCCESS}]Provider switched to {prov} ({model})[/]")
            self._update_agent_header()
        except Exception as e:
            chat.write(f"[{ERROR}]Provider error: {e}[/]")

    def _show_config(self) -> None:
        """Display current configuration in chat."""
        from djcode.config import load_config

        chat = self.query_one("#chat-log", RichLog)
        cfg = load_config()
        chat.write(f"\n[bold {GOLD}]Configuration[/]")
        for k, v in sorted(cfg.items()):
            display = "***" if "key" in k.lower() and v else str(v)
            chat.write(f"  [dim]{k}:[/] {display}")
        chat.write("")

    def _show_session_stats(self) -> None:
        """Display session stats in chat."""
        chat = self.query_one("#chat-log", RichLog)
        elapsed = time.time() - self._session_start
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        chat.write(f"\n[bold {GOLD}]Session Stats[/]")
        chat.write(f"  [dim]Tokens:[/]     [{GOLD}]{self._token_count}[/]")
        chat.write(f"  [dim]Elapsed:[/]    [{GOLD}]{mins}m {secs}s[/]")
        chat.write(
            f"  [dim]Mode:[/]       [{GOLD}]{'PLAN' if self._plan_mode else 'ACT'}[/]"
        )
        chat.write(
            f"  [dim]Thinking:[/]   [{GOLD}]{'ON' if self._show_thinking else 'OFF'}[/]"
        )
        chat.write(
            f"  [dim]Auto-accept:[/] [{GOLD}]{'ON' if self._auto_accept else 'OFF'}[/]"
        )
        chat.write(f"  [dim]Agent:[/]      [{GOLD}]{self._active_agent}[/]")
        chat.write("")

    # ── Message sending and streaming ─────────────────────────────────────

    async def _send_message(self, text: str) -> None:
        """Send a message to the operator and stream the response."""
        chat = self.query_one("#chat-log", RichLog)
        agent_log = self.query_one("#agent-log", RichLog)

        if not self._operator:
            chat.write(f"[{ERROR}]Not ready yet. Provider is still initializing...[/]")
            return

        if self._is_generating:
            chat.write(f"[{WARNING}]Already generating. Please wait...[/]")
            return

        self._is_generating = True

        # Show user message
        chat.write(f"\n[bold {GOLD}]\u276f[/] [{GOLD}]{text}[/]")

        # Inject plan mode prefix if active
        actual_input = text
        if self._plan_mode:
            actual_input = (
                "[PLAN MODE] Do not execute any tools. Only describe what "
                "you would do. List every step, file, and command you would "
                "run, but do NOT actually run anything.\n\n" + text
            )

        agent_log.write(f"\n[dim]{_short_time()}[/] [bold]Processing...[/]")
        start = time.time()

        try:
            response_buf = ""
            line_buf = ""  # Buffer tokens into complete lines

            async for token in self._operator.send(actual_input):
                # Check for thinking blocks
                if "<think>" in token or "</think>" in token:
                    if self._show_thinking:
                        agent_log.write(f"[{THINKING}]{token}[/]")
                    continue

                response_buf += token
                line_buf += token
                self._token_count += 1

                # Write complete lines (on newline) or flush periodically
                if "\n" in line_buf:
                    parts = line_buf.split("\n")
                    # Write all complete lines
                    for part in parts[:-1]:
                        if part.strip():
                            chat.write(part, shrink=False, scroll_end=True)
                    line_buf = parts[-1]  # Keep incomplete last part

                # Update stats periodically
                if self._token_count % 50 == 0:
                    self._update_stats_bar()

            # Flush remaining buffer
            if line_buf.strip():
                chat.write(line_buf, shrink=False, scroll_end=True)

            elapsed = time.time() - start

            # Log tool calls if operator had them
            if self._operator.last_had_tool_calls:
                agent_log.write(f"[{SUCCESS}]\u2713 Tools executed[/]")

            agent_log.write(
                f"[dim]{_short_time()}[/] "
                f"[{SUCCESS}]Done[/] "
                f"[dim]{elapsed:.1f}s / ~{self._token_count} tokens[/]"
            )

            chat.write("")  # Blank line after response
            self._update_stats_bar()

        except ConnectionError as e:
            chat.write(f"\n[{ERROR}]Connection error: {e}[/]")
            agent_log.write(f"[{ERROR}]Connection failed[/]")
        except Exception as e:
            chat.write(f"\n[{ERROR}]Error: {e}[/]")
            agent_log.write(f"[{ERROR}]{type(e).__name__}: {e}[/]")
        finally:
            self._is_generating = False

    # ── UI builders ───────────────────────────────────────────────────────

    def _build_agent_header(self) -> str:
        """Build the agent panel header text."""
        model = ""
        prov = ""
        if self._provider:
            model = self._provider.config.model
            prov = self._provider.config.name

        mode = "PLAN" if self._plan_mode else "ACT"
        return (
            f" \U0001f3af Active: {self._active_agent}\n"
            f" {mode} | {model} | {prov}"
        )

    def _build_stats_bar(self) -> str:
        """Build the stats bar text."""
        elapsed = time.time() - self._session_start
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        tokens = self._format_token_count(self._token_count)
        mode = "PLAN" if self._plan_mode else "ACT"
        thinking = "ON" if self._show_thinking else "OFF"
        auto = "ON" if self._auto_accept else "OFF"
        return (
            f" \U0001f4ca {tokens} tokens | "
            f"\u23f1 {mins}m{secs}s | "
            f"{mode} | "
            f"Think: {thinking} | "
            f"Auto: {auto}"
        )

    def _update_stats_bar(self) -> None:
        """Refresh the stats bar widget."""
        try:
            stats = self.query_one("#stats-bar", Static)
            stats.update(self._build_stats_bar())
        except Exception:
            pass

    def _update_agent_header(self) -> None:
        """Refresh the agent header widget."""
        try:
            header = self.query_one("#agent-header", Static)
            header.update(self._build_agent_header())
        except Exception:
            pass

    @staticmethod
    def _format_token_count(count: int) -> str:
        """Format token count for display."""
        if count >= 1_000_000:
            return f"{count / 1_000_000:.1f}m"
        if count >= 1_000:
            return f"{count / 1_000:.1f}k"
        return str(count)

    # ── Key bindings / actions ────────────────────────────────────────────

    def action_toggle_thinking(self) -> None:
        """Toggle thinking display."""
        self._show_thinking = not self._show_thinking
        label = "ON" if self._show_thinking else "OFF"
        chat = self.query_one("#chat-log", RichLog)
        chat.write(f"[dim]Thinking: {label}[/]")
        self._update_stats_bar()
        self.notify(f"Thinking: {label}", severity="information")

    def action_toggle_plan(self) -> None:
        """Toggle plan/act mode."""
        self._plan_mode = not self._plan_mode
        mode = "PLAN" if self._plan_mode else "ACT"
        chat = self.query_one("#chat-log", RichLog)
        chat.write(f"[dim]Mode: {mode}[/]")
        self._update_stats_bar()
        self._update_agent_header()
        self.notify(f"Mode: {mode}", severity="information")

    def action_clear_chat(self) -> None:
        """Clear the chat log and reset conversation."""
        chat = self.query_one("#chat-log", RichLog)
        chat.clear()
        chat.write(f"[dim]Chat cleared.[/]\n")

        # Reset operator conversation if available
        if self._operator:
            from djcode.provider import Message
            from djcode.prompt import build_system_prompt

            model = self._provider.config.model if self._provider else ""
            self._operator.messages = [
                Message(
                    role="system",
                    content=build_system_prompt(
                        bypass_rlhf=self._bypass_rlhf, model=model
                    ),
                )
            ]

        self.notify("Chat cleared", severity="information")

    def action_toggle_auto(self) -> None:
        """Toggle auto-accept for tool calls."""
        self._auto_accept = not self._auto_accept
        if self._operator:
            self._operator.auto_accept = self._auto_accept
        label = "ON" if self._auto_accept else "OFF"
        chat = self.query_one("#chat-log", RichLog)
        chat.write(f"[dim]Auto-accept: {label}[/]")
        self._update_stats_bar()
        self.notify(f"Auto-accept: {label}", severity="information")

    def action_show_help(self) -> None:
        """Show help overlay."""
        self.push_screen(HelpScreen())

    def action_show_agents(self) -> None:
        """Show agents overlay."""
        self.push_screen(AgentsScreen())

    def action_show_docs(self) -> None:
        """Show docs info in chat."""
        chat = self.query_one("#chat-log", RichLog)
        chat.write(
            f"\n[bold {GOLD}]DJcode Documentation[/]\n"
            f"  [dim]GitHub:[/]  https://github.com/darshjme/djcode\n"
            f"  [dim]Docs:[/]    https://djcode.darshj.ai\n"
            f"  [dim]Version:[/] {__version__}\n"
        )

    async def on_unmount(self) -> None:
        """Clean up on exit."""
        if self._provider:
            try:
                await self._provider.close()
            except Exception:
                pass


# ── Helpers ───────────────────────────────────────────────────────────────


def _short_time() -> str:
    """Return a short HH:MM:SS timestamp."""
    from datetime import datetime

    return datetime.now().strftime("%H:%M:%S")


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
