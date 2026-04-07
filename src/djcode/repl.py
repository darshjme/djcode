"""Interactive REPL for DJcode.

Uses Prompt Toolkit for input and Rich for output.
Supports slash commands, streaming responses, and tool calling.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.formatted_text import HTML
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from djcode import __version__
from djcode.agents.operator import Operator
from djcode.config import (
    HISTORY_FILE,
    ensure_dirs,
    load_config,
    save_config,
    set_value,
)
from djcode.memory.manager import MemoryManager
from djcode.provider import (
    Provider,
    ProviderConfig,
    fetch_ollama_models_sync,
    format_model_size,
    fuzzy_match_model,
    get_ollama_model_names,
)
from djcode.status import render_status_bar

console = Console()

GOLD = "#FFD700"

ASCII_BANNER = r"""
  ██████╗      ██╗ ██████╗ ██████╗ ██████╗ ███████╗
  ██╔══██╗     ██║██╔════╝██╔═══██╗██╔══██╗██╔════╝
  ██║  ██║     ██║██║     ██║   ██║██║  ██║█████╗
  ██║  ██║██   ██║██║     ██║   ██║██║  ██║██╔══╝
  ██████╔╝╚█████╔╝╚██████╗╚██████╔╝██████╔╝███████╗
  ╚═════╝  ╚════╝  ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝
"""


def print_banner(provider: Provider) -> None:
    """Print the big DJcode ASCII splash screen."""
    cfg = load_config()
    cwd = os.getcwd()
    home = os.path.expanduser("~")
    if cwd.startswith(home):
        cwd_display = "~" + cwd[len(home):]
    else:
        cwd_display = cwd

    provider_label = provider.config.name.capitalize()
    if provider.config.name == "ollama":
        provider_detail = f"Local ({provider.config.base_url})"
    elif provider.config.name == "mlx":
        provider_detail = f"MLX ({provider.config.base_url})"
    else:
        provider_detail = provider.config.base_url or "Remote API"

    auto_accept = cfg.get("auto_accept", False)
    mode = "Interactive"
    if auto_accept:
        mode += " [auto-accept]"

    max_tokens = cfg.get("max_tokens", 8192)
    if max_tokens >= 1000:
        ctx_str = f"{max_tokens // 1000}K tokens"
    else:
        ctx_str = f"{max_tokens} tokens"

    inner = (
        f"[bold {GOLD}]{ASCII_BANNER}[/]\n"
        f"  [bold white]DarshJ.AI Code[/] [dim]v{__version__}[/]\n"
        f"  [dim]The last coding CLI you'll ever need.[/]\n"
    )

    info_lines = (
        f"\n  [bold {GOLD}]Model:[/]     [white]{provider.config.model}[/] [dim]({provider_label})[/]\n"
        f"  [bold {GOLD}]Provider:[/]  [white]{provider_detail}[/]\n"
        f"  [bold {GOLD}]Folder:[/]    [white]{cwd_display}[/]\n"
        f"  [bold {GOLD}]Context:[/]   [white]{ctx_str}[/]\n"
        f"  [bold {GOLD}]Mode:[/]      [white]{mode}[/]"
    )

    console.print()
    console.print(
        Panel(
            inner + info_lines,
            border_style=GOLD,
            padding=(1, 2),
        )
    )
    console.print()


HELP_TEXT = f"""\
[bold {GOLD}]Slash Commands[/]

  [cyan]/help[/]              Show this help
  [cyan]/model[/] <name>      Switch model (fuzzy match supported)
  [cyan]/models[/]            List available models
  [cyan]/provider[/] <p>      Switch provider (ollama, mlx, remote)
  [cyan]/provider remote[/]   Configure remote API (--url, --key)
  [cyan]/auto[/]              Toggle auto-accept tool calls
  [cyan]/memory[/]            Show memory stats
  [cyan]/remember[/] k=v      Store a persistent fact
  [cyan]/recall[/] <key>      Recall a persistent fact
  [cyan]/forget[/] <key>      Remove a persistent fact
  [cyan]/clear[/]             Clear conversation history
  [cyan]/save[/]              Save conversation to disk
  [cyan]/config[/]            Show current config
  [cyan]/set[/] k=v           Set a config value
  [cyan]/raw[/]               Toggle raw mode (no formatting)
  [cyan]/exit[/]              Exit DJcode
"""


def _handle_models_list(provider: Provider) -> None:
    """List all available models from the current provider."""
    if provider.config.name != "ollama":
        console.print(f"[yellow]Model listing only available for Ollama provider.[/]")
        console.print(f"[dim]Current model: {provider.config.model}[/]")
        return

    models = fetch_ollama_models_sync(provider.config.base_url)
    if not models:
        console.print(
            "[yellow]No models found.[/] "
            "[dim]Is Ollama running? Start with: ollama serve[/]"
        )
        return

    table = Table(
        title=f"[bold {GOLD}]Available Models[/]",
        border_style=GOLD,
        show_header=True,
        header_style=f"bold {GOLD}",
    )
    table.add_column("Model", style="white")
    table.add_column("Size", style="dim")
    table.add_column("", style="green")

    for m in models:
        name = m.get("name", "unknown")
        size = format_model_size(m.get("size", 0))
        current = "*" if name == provider.config.model else ""
        table.add_row(name, size, current)

    console.print(table)
    console.print(f"\n[dim]Switch with: /model <name>[/]")


def _handle_model_switch(arg: str, operator: Operator) -> None:
    """Handle /model <name> with fuzzy matching and validation."""
    provider = operator.provider

    if provider.config.name == "ollama":
        available = get_ollama_model_names(provider.config.base_url)

        if available:
            match = fuzzy_match_model(arg, available)
            if match:
                if match != arg:
                    console.print(f"[dim]Resolved '{arg}' -> '{match}'[/]")
                provider.config.model = match
                set_value("model", match)
                console.print(f"[green]Model switched to:[/] {match}")
            else:
                console.print(f"[red]Model '{arg}' not found.[/]")
                names = ", ".join(available[:10])
                console.print(f"[dim]Available: {names}[/]")
                console.print(f"[dim]Pull it with: ollama pull {arg}[/]")
        else:
            # Can't reach Ollama — set it anyway, will fail at chat time
            console.print(f"[yellow]Cannot verify model (Ollama unreachable).[/]")
            provider.config.model = arg
            set_value("model", arg)
            console.print(f"[green]Model set to:[/] {arg}")
    else:
        # Non-Ollama provider — just set it
        provider.config.model = arg
        set_value("model", arg)
        console.print(f"[green]Model switched to:[/] {arg}")


def _handle_provider_switch(arg: str, operator: Operator, provider_config: ProviderConfig) -> None:
    """Handle /provider command with remote API configuration."""
    parts = arg.split()
    provider_name = parts[0] if parts else ""

    if provider_name not in ("ollama", "mlx", "remote"):
        console.print(f"[red]Unknown provider:[/] {provider_name}")
        console.print("[dim]Options: ollama, mlx, remote[/]")
        return

    # Parse optional flags for remote
    if provider_name == "remote":
        url = ""
        key = ""
        i = 1
        while i < len(parts):
            if parts[i] == "--url" and i + 1 < len(parts):
                url = parts[i + 1]
                i += 2
            elif parts[i] == "--key" and i + 1 < len(parts):
                key = parts[i + 1]
                i += 2
            else:
                i += 1

        if url:
            set_value("remote_url", url)
        if key:
            set_value("remote_api_key", key)

    new_config = ProviderConfig.from_config(provider_override=provider_name)
    operator.provider = Provider(new_config)
    set_value("provider", provider_name)
    console.print(f"[green]Provider switched to:[/] {operator.provider.display_name}")


async def handle_slash_command(
    cmd: str,
    operator: Operator,
    memory: MemoryManager,
    provider_config: ProviderConfig,
) -> bool:
    """Handle a slash command. Returns True if the REPL should continue."""
    parts = cmd.strip().split(maxsplit=1)
    command = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if command == "/help":
        console.print(Panel(HELP_TEXT, title=f"[bold {GOLD}]DJcode Help[/]", border_style=GOLD))

    elif command == "/models":
        _handle_models_list(operator.provider)

    elif command == "/model":
        if not arg:
            # No arg — list models
            _handle_models_list(operator.provider)
        else:
            _handle_model_switch(arg, operator)

    elif command == "/provider":
        if not arg:
            console.print(f"[yellow]Current provider:[/] {operator.provider.config.name}")
            console.print("[dim]Usage: /provider <ollama|mlx|remote> [--url URL] [--key KEY][/]")
        else:
            _handle_provider_switch(arg, operator, provider_config)

    elif command == "/auto":
        cfg = load_config()
        new_val = not cfg.get("auto_accept", False)
        set_value("auto_accept", new_val)
        state = "ON" if new_val else "OFF"
        console.print(f"[green]Auto-accept:[/] {state}")

    elif command == "/memory":
        stats = memory.stats
        table = Table(title="Memory Stats", border_style=GOLD)
        table.add_column("Tier", style="cyan")
        table.add_column("Count", style="white")
        table.add_row("Session messages", str(stats["session_messages"]))
        table.add_row("Persistent facts", str(stats["persistent_facts"]))
        table.add_row("Facts with embeddings", str(stats["facts_with_embeddings"]))
        console.print(table)

        facts = memory.list_facts()
        if facts:
            console.print(f"\n[dim]Facts: {', '.join(facts)}[/]")

    elif command == "/remember":
        if "=" not in arg:
            console.print("[dim]Usage: /remember key=value[/]")
        else:
            key, _, value = arg.partition("=")
            memory.remember(key.strip(), value.strip())
            console.print(f"[green]Remembered:[/] {key.strip()}")

    elif command == "/recall":
        if not arg:
            console.print("[dim]Usage: /recall <key>[/]")
        else:
            value = memory.recall(arg.strip())
            if value:
                console.print(f"[cyan]{arg}:[/] {value}")
            else:
                console.print(f"[yellow]No memory found for:[/] {arg}")

    elif command == "/forget":
        if not arg:
            console.print("[dim]Usage: /forget <key>[/]")
        else:
            if memory.forget(arg.strip()):
                console.print(f"[green]Forgot:[/] {arg}")
            else:
                console.print(f"[yellow]No memory found for:[/] {arg}")

    elif command == "/clear":
        operator.reset()
        memory.clear_session()
        console.print("[green]Conversation cleared.[/]")

    elif command == "/save":
        session_id = str(uuid.uuid4())[:8]
        path = memory.save_conversation(session_id)
        console.print(f"[green]Saved to:[/] {path}")

    elif command == "/config":
        cfg = load_config()
        table = Table(title="Configuration", border_style=GOLD)
        table.add_column("Key", style="cyan")
        table.add_column("Value", style="white")
        for k, v in sorted(cfg.items()):
            # Mask API keys
            display = "***" if "key" in k.lower() and v else str(v)
            table.add_row(k, display)
        console.print(table)

    elif command == "/set":
        if "=" not in arg:
            console.print("[dim]Usage: /set key=value[/]")
        else:
            key, _, value = arg.partition("=")
            key = key.strip()
            value = value.strip()
            try:
                import json

                parsed = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                parsed = value
            set_value(key, parsed)
            console.print(f"[green]Set {key}=[/]{parsed}")

    elif command == "/raw":
        operator.raw = not operator.raw
        state = "on" if operator.raw else "off"
        console.print(f"[green]Raw mode:[/] {state}")

    elif command in ("/exit", "/quit", "/q"):
        console.print("[dim]Goodbye.[/]")
        return False

    else:
        console.print(f"[yellow]Unknown command:[/] {command}")
        console.print("[dim]Type /help for available commands[/]")

    return True


def _estimate_tokens(messages: list) -> int:
    """Rough token estimate: ~4 chars per token."""
    total_chars = sum(len(getattr(m, "content", "") or "") for m in messages)
    return total_chars // 4


async def run_repl(
    provider: str | None = None,
    model: str | None = None,
    bypass_rlhf: bool = False,
    raw: bool = False,
    auto_accept: bool = False,
) -> None:
    """Run the interactive REPL."""
    ensure_dirs()

    # Check for first-run onboarding
    from djcode.onboarding import needs_onboarding, run_onboarding

    if needs_onboarding():
        run_onboarding()

    # Apply auto_accept from CLI flag or config
    cfg = load_config()
    if auto_accept:
        set_value("auto_accept", True)

    # Initialize provider
    provider_config = ProviderConfig.from_config(
        provider_override=provider,
        model_override=model,
    )
    llm = Provider(provider_config)

    # Validate model on startup
    ok, msg = llm.validate_model()
    if not ok:
        console.print(f"[red]{msg}[/]")
        console.print("[dim]Use /model to switch or /models to list available models.[/]")
    elif msg:
        console.print(f"[dim]{msg}[/]")

    # Initialize operator
    operator = Operator(llm, bypass_rlhf=bypass_rlhf, raw=raw)

    # Initialize memory
    memory = MemoryManager()

    # Print banner
    print_banner(llm)

    # Set up prompt toolkit session
    session: PromptSession[str] = PromptSession(
        history=FileHistory(str(HISTORY_FILE)),
        auto_suggest=AutoSuggestFromHistory(),
    )

    while True:
        try:
            user_input = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: session.prompt(
                    HTML(
                        f"<style fg='#FFD700'><b>djcode</b></style>"
                        f" <ansibrightblack>></ansibrightblack> "
                    )
                ),
            )
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye.[/]")
            break

        user_input = user_input.strip()
        if not user_input:
            continue

        # Slash commands
        if user_input.startswith("/"):
            should_continue = await handle_slash_command(
                user_input, operator, memory, provider_config
            )
            if not should_continue:
                break
            continue

        # Track in memory
        memory.add_session_message("user", user_input)

        # Send to operator and stream response
        full_response = ""
        try:
            if not raw:
                console.print()  # Spacing

            async for token in operator.send(user_input):
                if raw:
                    sys.stdout.write(token)
                    sys.stdout.flush()
                else:
                    sys.stdout.write(token)
                    sys.stdout.flush()

                full_response += token

            if full_response:
                console.print()  # Newline after streaming
                memory.add_session_message("assistant", full_response)

            # Status bar after every response
            current_cfg = load_config()
            token_est = _estimate_tokens(operator.messages)
            render_status_bar(
                model=operator.provider.config.model,
                provider=operator.provider.config.name,
                token_count=token_est,
                auto_accept=current_cfg.get("auto_accept", False),
            )

        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/]")
        except ConnectionError as e:
            console.print(f"\n[red]{e}[/]")
        except Exception as e:
            console.print(f"\n[red]Error:[/] {e}")

    await llm.close()


async def run_oneshot(
    prompt: str,
    provider: str | None = None,
    model: str | None = None,
    bypass_rlhf: bool = False,
    raw: bool = False,
) -> None:
    """Run a single prompt and exit."""
    provider_config = ProviderConfig.from_config(
        provider_override=provider,
        model_override=model,
    )
    llm = Provider(provider_config)

    # Validate model
    ok, msg = llm.validate_model()
    if not ok:
        console.print(f"[red]{msg}[/]")
        return
    elif msg:
        console.print(f"[dim]{msg}[/]")

    operator = Operator(llm, bypass_rlhf=bypass_rlhf, raw=raw)

    try:
        async for token in operator.send(prompt):
            sys.stdout.write(token)
            sys.stdout.flush()
        print()  # Final newline
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/]")
    except ConnectionError as e:
        console.print(f"\n[red]{e}[/]")
    except Exception as e:
        console.print(f"[red]Error:[/] {e}")
    finally:
        await llm.close()
