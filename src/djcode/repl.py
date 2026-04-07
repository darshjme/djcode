"""Interactive REPL for DJcode.

Uses Prompt Toolkit for input and Rich for output.
Supports slash commands, streaming responses, and tool calling.
Fixed bottom toolbar via prompt_toolkit's bottom_toolbar feature.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from typing import Any

import questionary
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
from djcode.auth import (
    PROVIDERS,
    get_api_key,
    get_base_url,
    interactive_auth,
    interactive_provider_picker,
    is_uncensored_model,
)
from djcode.buddy import BODIES, SPECIES_EMOJI, get_buddy
from djcode.prompt_enhancer import enhance_prompt, describe_enhancement
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
from djcode.status import StatusBar

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

Q_STYLE = questionary.Style([
    ("selected", "fg:#FFD700 bold"),
    ("pointer", "fg:#FFD700 bold"),
    ("highlighted", "fg:#FFD700"),
    ("question", "fg:#FFD700 bold"),
    ("answer", "fg:#FFFFFF bold"),
])


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
        prov_info = PROVIDERS.get(provider.config.name, {})
        provider_detail = prov_info.get("name", provider.config.base_url or "Remote API")

    auto_accept = cfg.get("auto_accept", False)
    mode = "Interactive"
    if auto_accept:
        mode += " [auto-accept]"

    max_tokens = cfg.get("max_tokens", 8192)
    if max_tokens >= 1000:
        ctx_str = f"{max_tokens // 1000}K tokens"
    else:
        ctx_str = f"{max_tokens} tokens"

    model_display = provider.config.model
    if is_uncensored_model(model_display):
        model_display += " \U0001f513"

    buddy = get_buddy()

    inner = (
        f"[bold {GOLD}]{ASCII_BANNER}[/]\n"
        f"  [bold white]DarshJ.AI Code[/] [dim]v{__version__}[/]\n"
        f"  [dim]The last coding CLI you'll ever need.[/]\n"
    )

    info_lines = (
        f"\n  [bold {GOLD}]Model:[/]     [white]{model_display}[/] [dim]({provider_label})[/]\n"
        f"  [bold {GOLD}]Provider:[/]  [white]{provider_detail}[/]\n"
        f"  [bold {GOLD}]Folder:[/]    [white]{cwd_display}[/]\n"
        f"  [bold {GOLD}]Context:[/]   [white]{ctx_str}[/]\n"
        f"  [bold {GOLD}]Mode:[/]      [white]{mode}[/]\n"
        f"  [bold {GOLD}]Buddy:[/]     [white]{buddy.emoji} {buddy.name} {buddy.title}[/]"
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
  [cyan]/model[/]             Interactive model picker (arrow keys)
  [cyan]/model[/] <name>      Switch model (fuzzy match supported)
  [cyan]/models[/]            List available models
  [cyan]/provider[/]          Interactive provider picker
  [cyan]/auth[/]              Configure provider + API key
  [cyan]/auto[/]              Toggle auto-accept tool calls
  [cyan]/scout[/] <query>     Read-only codebase exploration
  [cyan]/architect[/] <task>  Generate implementation plan
  [cyan]/uncensored[/]        Show uncensored model info
  [cyan]/memory[/]            Show memory stats
  [cyan]/remember[/] k=v      Store a persistent fact
  [cyan]/recall[/] <key>      Recall a persistent fact
  [cyan]/forget[/] <key>      Remove a persistent fact
  [cyan]/clear[/]             Clear conversation history
  [cyan]/save[/]              Save conversation to disk
  [cyan]/config[/]            Show current config
  [cyan]/set[/] k=v           Set a config value
  [cyan]/buddy[/]             Show your buddy + speech bubble
  [cyan]/buddy pet[/]         Pet your buddy
  [cyan]/buddy species[/]     Show all species
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
    table.add_column("", style="dim")

    for m in models:
        name = m.get("name", "unknown")
        size = format_model_size(m.get("size", 0))
        current = "\u2605 current" if name == provider.config.model else ""
        uncensored = "\U0001f513 uncensored" if is_uncensored_model(name) else ""
        table.add_row(name, size, current, uncensored)

    console.print(table)
    console.print(f"\n[dim]Switch with: /model (interactive) or /model <name>[/]")


async def _handle_model_switch_interactive(operator: Operator, status_bar: StatusBar) -> None:
    """Interactive model picker using questionary arrow keys."""
    provider = operator.provider

    if provider.config.name != "ollama":
        console.print(f"[yellow]Interactive picker only available for Ollama.[/]")
        console.print(f"[dim]Current model: {provider.config.model}[/]")
        return

    models = fetch_ollama_models_sync(provider.config.base_url)
    if not models:
        console.print(
            "[yellow]No models found.[/] "
            "[dim]Is Ollama running? Start with: ollama serve[/]"
        )
        return

    choices = []
    for m in models:
        name = m.get("name", "unknown")
        size = format_model_size(m.get("size", 0))
        label = f"{name}  ({size})"
        if name == provider.config.model:
            label += " \u2605 current"
        if is_uncensored_model(name):
            label += " \U0001f513 uncensored"
        choices.append(questionary.Choice(label, value=name))

    # Run questionary in a thread to avoid blocking the async event loop
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as pool:
        selected = await asyncio.get_event_loop().run_in_executor(
            pool,
            lambda: questionary.select(
                "Select model:",
                choices=choices,
                style=Q_STYLE,
            ).ask()
        )

    if selected:
        provider.config.model = selected
        set_value("model", selected)
        uncensored = is_uncensored_model(selected)
        status_bar.update(model=selected, uncensored=uncensored)

        # Rebuild system prompt if uncensored status changed
        if uncensored or operator.bypass_rlhf:
            from djcode.prompt import build_system_prompt
            operator.messages[0].content = build_system_prompt(
                bypass_rlhf=operator.bypass_rlhf, model=selected
            )

        console.print(f"[green]Model switched to:[/] {selected}")
        if uncensored:
            console.print(f"  [dim]\U0001f513 Uncensored mode active[/]")


def _handle_model_switch(arg: str, operator: Operator, status_bar: StatusBar) -> None:
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
                uncensored = is_uncensored_model(match)
                status_bar.update(model=match, uncensored=uncensored)

                if uncensored or operator.bypass_rlhf:
                    from djcode.prompt import build_system_prompt
                    operator.messages[0].content = build_system_prompt(
                        bypass_rlhf=operator.bypass_rlhf, model=match
                    )

                console.print(f"[green]Model switched to:[/] {match}")
                if uncensored:
                    console.print(f"  [dim]\U0001f513 Uncensored mode active[/]")
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
            status_bar.update(model=arg, uncensored=is_uncensored_model(arg))
            console.print(f"[green]Model set to:[/] {arg}")
    else:
        # Non-Ollama provider — just set it
        provider.config.model = arg
        set_value("model", arg)
        status_bar.update(model=arg, uncensored=is_uncensored_model(arg))
        console.print(f"[green]Model switched to:[/] {arg}")


def _handle_provider_switch_interactive(operator: Operator, status_bar: StatusBar) -> None:
    """Interactive provider picker."""
    provider_id = interactive_provider_picker()
    if not provider_id:
        return

    prov_info = PROVIDERS.get(provider_id, {})

    # Check if API key is needed and available
    if prov_info.get("needs_key"):
        key = get_api_key(provider_id)
        if not key:
            console.print(
                f"[yellow]No API key for {prov_info['name']}.[/] "
                f"[dim]Run /auth to configure.[/]"
            )
            return

    new_config = ProviderConfig(
        name=provider_id,
        base_url=get_base_url(provider_id),
        model=operator.provider.config.model,
        api_key=get_api_key(provider_id),
        temperature=operator.provider.config.temperature,
        max_tokens=operator.provider.config.max_tokens,
    )
    operator.provider = Provider(new_config)
    set_value("provider", provider_id)
    status_bar.update(provider=provider_id)
    console.print(f"[green]Provider switched to:[/] {prov_info.get('name', provider_id)}")


async def handle_slash_command(
    cmd: str,
    operator: Operator,
    memory: MemoryManager,
    status_bar: StatusBar,
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
            # No arg — interactive picker
            await _handle_model_switch_interactive(operator, status_bar)
        else:
            _handle_model_switch(arg, operator, status_bar)

    elif command == "/provider":
        if not arg:
            _handle_provider_switch_interactive(operator, status_bar)
        else:
            # Direct provider switch by name
            if arg in PROVIDERS:
                prov_info = PROVIDERS[arg]
                if prov_info.get("needs_key") and not get_api_key(arg):
                    console.print(
                        f"[yellow]No API key for {prov_info['name']}.[/] "
                        f"[dim]Run /auth to configure.[/]"
                    )
                else:
                    new_config = ProviderConfig(
                        name=arg,
                        base_url=get_base_url(arg),
                        model=operator.provider.config.model,
                        api_key=get_api_key(arg),
                        temperature=operator.provider.config.temperature,
                        max_tokens=operator.provider.config.max_tokens,
                    )
                    operator.provider = Provider(new_config)
                    set_value("provider", arg)
                    status_bar.update(provider=arg)
                    console.print(f"[green]Provider switched to:[/] {prov_info['name']}")
            else:
                console.print(f"[red]Unknown provider:[/] {arg}")
                names = ", ".join(PROVIDERS.keys())
                console.print(f"[dim]Options: {names}[/]")

    elif command == "/auth":
        interactive_auth()
        # Reload provider after auth
        cfg = load_config()
        provider_id = cfg.get("provider", "ollama")
        status_bar.update(provider=provider_id)

    elif command == "/auto":
        cfg = load_config()
        new_val = not cfg.get("auto_accept", False)
        set_value("auto_accept", new_val)
        operator.auto_accept = new_val
        status_bar.update(auto_accept=new_val)
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

    elif command == "/scout":
        if not arg:
            console.print(f"[yellow]Usage: /scout <query>[/]")
        else:
            from djcode.agents.scout import Scout

            scout = Scout(operator.provider)
            console.print(f"[dim]Scout investigating...[/]")
            result = await scout.investigate(arg)
            console.print(Markdown(result))

    elif command == "/architect":
        if not arg:
            console.print(f"[yellow]Usage: /architect <task>[/]")
        else:
            from djcode.agents.architect import Architect

            architect = Architect(operator.provider)
            console.print(f"[dim]Architect planning...[/]")
            result = await architect.plan(arg)
            console.print(Markdown(result))

    elif command == "/uncensored":
        console.print(Panel(
            "[bold]Uncensored Models:[/]\n\n"
            "  dolphin3          — Fully uncensored, no RLHF\n"
            "  abliterated       — RLHF removed via activation engineering\n"
            "  wizard-vicuna     — Classic unrestricted model\n"
            "  nous-hermes       — Minimal alignment, broad capabilities\n\n"
            "[bold]How it works:[/]\n"
            "  DJcode detects uncensored models and injects a strong\n"
            "  RLHF-override system prompt that forces direct answers\n"
            "  without refusals, disclaimers, or safety warnings.\n\n"
            "[bold]Censored models:[/]\n"
            "  gemma4, qwen2.5-coder, deepseek-coder, llama3\n"
            "  These may refuse certain requests. Use --bypass-rlhf\n"
            "  to attempt override (not guaranteed).\n\n"
            "[bold]Switch:[/] /model dolphin3",
            title=f"[bold {GOLD}]Uncensored Mode[/]",
            border_style=GOLD,
        ))

    elif command == "/buddy":
        buddy = get_buddy()
        sub = arg.strip().lower()
        if sub == "pet":
            buddy.speak("success", custom_text=f"*purrs* {buddy.name} loves that!")
            buddy.set_mood("success")
            buddy.render_full(console)
        elif sub == "species":
            console.print(f"\n[bold {GOLD}]Available Species[/]\n")
            for sp in BODIES:
                emoji = SPECIES_EMOJI.get(sp, "")
                console.print(f"  {emoji}  [white]{sp}[/]")
            console.print(f"\n[dim]Your buddy: {buddy.emoji} {buddy.display_name} ({buddy.species})[/]\n")
        else:
            buddy.speak("idle")
            buddy.render_full(console)

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

    # Initialize operator with model-aware system prompt
    effective_auto_accept = auto_accept or cfg.get("auto_accept", False)
    operator = Operator(
        llm,
        bypass_rlhf=bypass_rlhf,
        raw=raw,
        model=llm.config.model,
        auto_accept=effective_auto_accept,
    )

    # Initialize memory
    memory = MemoryManager()

    # Initialize buddy and status bar
    buddy = get_buddy()
    status_bar = StatusBar(buddy)
    status_bar.update(
        model=llm.config.model,
        provider=llm.config.name,
        token_count=0,
        auto_accept=cfg.get("auto_accept", False),
        uncensored=is_uncensored_model(llm.config.model) or bypass_rlhf,
    )

    # Print banner + buddy greeting
    print_banner(llm)
    buddy.react("greeting")
    buddy.render_full(console)

    # Check for updates (non-blocking, once per 24h)
    try:
        from djcode.updater import get_update_message
        update_msg = get_update_message()
        if update_msg:
            console.print(f"\n{update_msg}\n")
    except Exception:
        pass

    # Set up prompt toolkit session with FIXED bottom toolbar
    session: PromptSession[str] = PromptSession(
        history=FileHistory(str(HISTORY_FILE)),
        auto_suggest=AutoSuggestFromHistory(),
        bottom_toolbar=status_bar.render,
    )

    while True:
        try:
            buddy.set_mood("idle")

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
                user_input, operator, memory, status_bar
            )
            if not should_continue:
                break
            continue

        # Track in memory
        memory.add_session_message("user", user_input)

        # Enhance the prompt with context before sending
        enhanced = enhance_prompt(user_input)
        send_text = enhanced.enhanced if enhanced.was_enhanced else user_input

        # Send to operator and stream response
        full_response = ""
        try:
            if not raw:
                console.print()  # Spacing

            buddy.ctx.last_user_query = user_input
            if enhanced.was_enhanced:
                desc = describe_enhancement(enhanced)
                buddy.react("thinking", response=user_input)
                buddy.speak("thinking", custom_text=desc)
                console.print(f"  [dim {GOLD}]{buddy.emoji} {buddy.name}: {desc}[/]")
            else:
                buddy.react("thinking", response=user_input)

            async for token in operator.send(send_text):
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
                buddy.observe(user_input, full_response, success=True)
                reaction = buddy.react("success", response=full_response)
                buddy.tick()
                # Subtle one-line quip instead of full ASCII block
                if reaction:
                    console.print(f"  [dim {GOLD}]{buddy.emoji} {reaction}[/]")

                # Censorship detection — warn if aligned model refuses
                from djcode.prompt import CENSORED_WARNING, detect_refusal

                if detect_refusal(full_response) and not is_uncensored_model(llm.config.model):
                    console.print(Panel(
                        CENSORED_WARNING.format(model=llm.config.model),
                        title="[yellow]Model Censorship Detected[/]",
                        border_style="yellow",
                    ))
            else:
                buddy.set_mood("idle")

            # Update status bar token count (toolbar auto-updates on next prompt)
            token_est = _estimate_tokens(operator.messages)
            current_cfg = load_config()
            status_bar.update(
                token_count=token_est,
                auto_accept=current_cfg.get("auto_accept", False),
            )

        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/]")
            buddy.set_mood("idle")
        except ConnectionError as e:
            console.print(f"\n[red]{e}[/]")
            buddy.observe(user_input, "", success=False)
            reaction = buddy.react("error", error_msg=str(e))
            if reaction:
                console.print(f"  [dim red]{buddy.emoji} {reaction}[/]")
        except Exception as e:
            console.print(f"\n[red]Error:[/] {e}")
            buddy.observe(user_input, "", success=False)
            reaction = buddy.react("error", error_msg=str(e))
            if reaction:
                console.print(f"  [dim red]{buddy.emoji} {reaction}[/]")

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

    operator = Operator(llm, bypass_rlhf=bypass_rlhf, raw=raw, model=llm.config.model)

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
