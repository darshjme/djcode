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
from djcode.provider import Provider, ProviderConfig

console = Console()


def print_banner(provider: Provider) -> None:
    """Print the DJcode startup banner."""
    console.print()
    console.print(
        Panel(
            f"[bold white]DJcode[/] [dim]v{__version__}[/]  "
            f"[dim]|[/]  [cyan]{provider.display_name}[/]  "
            f"[dim]|[/]  [dim]{os.getcwd()}[/]\n"
            f"[dim]Local-first AI coding CLI by DarshJ.AI[/]  "
            f"[dim]|[/]  [dim]Type /help for commands[/]",
            border_style="bright_blue",
            padding=(0, 2),
        )
    )
    console.print()


HELP_TEXT = """\
[bold]Slash Commands[/]

  [cyan]/help[/]          Show this help
  [cyan]/model[/] <name>  Switch model (e.g. /model llama3.2:latest)
  [cyan]/provider[/] <p>  Switch provider (ollama, mlx, remote)
  [cyan]/memory[/]        Show memory stats
  [cyan]/remember[/] k=v  Store a persistent fact
  [cyan]/recall[/] <key>  Recall a persistent fact
  [cyan]/forget[/] <key>  Remove a persistent fact
  [cyan]/clear[/]         Clear conversation history
  [cyan]/save[/]          Save conversation to disk
  [cyan]/config[/]        Show current config
  [cyan]/set[/] k=v       Set a config value
  [cyan]/raw[/]           Toggle raw mode (no formatting)
  [cyan]/exit[/]          Exit DJcode
"""


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
        console.print(Panel(HELP_TEXT, title="[bold]DJcode Help[/]", border_style="blue"))

    elif command == "/model":
        if not arg:
            console.print(f"[yellow]Current model:[/] {operator.provider.config.model}")
            console.print("[dim]Usage: /model <name>[/]")
        else:
            operator.provider.config.model = arg
            set_value("model", arg)
            console.print(f"[green]Model switched to:[/] {arg}")

    elif command == "/provider":
        if not arg:
            console.print(f"[yellow]Current provider:[/] {operator.provider.config.name}")
            console.print("[dim]Usage: /provider <ollama|mlx|remote>[/]")
        else:
            if arg not in ("ollama", "mlx", "remote"):
                console.print(f"[red]Unknown provider:[/] {arg}")
                console.print("[dim]Options: ollama, mlx, remote[/]")
            else:
                # Rebuild provider
                new_config = ProviderConfig.from_config(provider_override=arg)
                operator.provider = Provider(new_config)
                set_value("provider", arg)
                console.print(f"[green]Provider switched to:[/] {operator.provider.display_name}")

    elif command == "/memory":
        stats = memory.stats
        table = Table(title="Memory Stats", border_style="blue")
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
        table = Table(title="Configuration", border_style="blue")
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
            # Try to parse as JSON for bools/numbers
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


async def run_repl(
    provider: str | None = None,
    model: str | None = None,
    bypass_rlhf: bool = False,
    raw: bool = False,
) -> None:
    """Run the interactive REPL."""
    ensure_dirs()

    # Initialize provider
    provider_config = ProviderConfig.from_config(
        provider_override=provider,
        model_override=model,
    )
    llm = Provider(provider_config)

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
                    HTML("<ansicyan><b>djcode</b></ansicyan> <ansibrightblack>></ansibrightblack> ")
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

        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/]")
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
    operator = Operator(llm, bypass_rlhf=bypass_rlhf, raw=raw)

    try:
        async for token in operator.send(prompt):
            sys.stdout.write(token)
            sys.stdout.flush()
        print()  # Final newline
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/]")
    except Exception as e:
        console.print(f"[red]Error:[/] {e}")
    finally:
        await llm.close()
