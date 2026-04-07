"""Interactive REPL for DJcode.

Uses Prompt Toolkit for input and Rich for output.
Supports slash commands, streaming responses, and tool calling.
Fixed bottom toolbar via prompt_toolkit's bottom_toolbar feature.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from typing import Any

logger = logging.getLogger(__name__)

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
from djcode.errors import classify_error, format_error, get_fallback_model
from djcode.orchestrator import Orchestrator
from djcode.agents.registry import AgentRole
from djcode.agents.content_registry import ContentRole, list_content_agents, get_content_spec
from djcode.context_file import save_context, inject_context_into_prompt
from djcode.permissions import PermissionManager
from djcode.prompt_enhancer import enhance_prompt, describe_enhancement
from djcode.stats import record_session_start, record_session_update, record_session_end, render_stats
from djcode.extensions import ExtensionManager
from djcode.recipes import RecipeManager, render_recipe_list, render_recipe_detail
from djcode.sessions import SessionDB, render_session_list
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
from djcode.tui import (
    get_mode_state,
    register_keybindings,
    show_command_picker,
    show_shortcuts,
    render_inline_diff,
    ProgressTracker,
)

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
  [cyan]/orchestra[/] <task>  Multi-agent orchestration (auto-dispatch)
  [cyan]/review[/] <code>     Code review (Dharma agent)
  [cyan]/debug[/] <issue>     Root cause analysis (Sherlock agent)
  [cyan]/test[/] <target>     Write tests (Agni agent)
  [cyan]/refactor[/] <code>   Restructure code (Shiva agent)
  [cyan]/devops[/] <task>     Docker/CI/CD (Vayu agent)
  [cyan]/docs[/] <target>     Generate docs (Saraswati agent)
  [cyan]/launch[/] <product>  Build + Ship + Campaign (full pipeline)
  [cyan]/campaign[/] <brief>  Content campaign (12 content agents)
  [cyan]/image[/] <concept>   Generate image prompts (Maya)
  [cyan]/video[/] <concept>   Cinematic video prompts (Kubera)
  [cyan]/social[/] <topic>    Social media content (Chitragupta)
  [cyan]/agents[/]            Show all 22 agents roster
  [cyan]/stats[/]             Usage dashboard with activity heatmap
  [cyan]/stats 7d[/]          Last 7 days stats
  [cyan]/stats 30d[/]         Last 30 days stats
  [cyan]/extension[/]         List MCP extensions
  [cyan]/extension add[/]     Add an MCP extension (name cmd [args])
  [cyan]/extension rm[/]      Remove an extension
  [cyan]/recipe[/]            List available recipes
  [cyan]/recipe run[/]        Run a recipe (name [params])
  [cyan]/recipe show[/]       Show recipe details
  [cyan]/history[/]           Browse past sessions
  [cyan]/history search[/]    Search past conversations
  [cyan]/resume[/]            Resume a past session by ID
  [cyan]/buddy[/]             Show your buddy + speech bubble
  [cyan]/buddy pet[/]         Pet your buddy
  [cyan]/buddy species[/]     Show all species
  [cyan]/raw[/]               Toggle raw mode (no formatting)
  [cyan]/shortcuts[/]         Show keyboard shortcuts
  [cyan]/exit[/]              Exit DJcode

[bold {GOLD}]Keyboard Shortcuts[/]

  [cyan]Ctrl+O[/]   Toggle thinking verbose
  [cyan]Ctrl+L[/]   Clear screen
  [cyan]Ctrl+T[/]   Toggle auto-accept
  [cyan]Ctrl+P[/]   Toggle plan/act mode
  [cyan]Ctrl+R[/]   Rerun last command
  [cyan]Ctrl+K[/]   Kill generation
  [cyan]  /   [/]   Interactive command picker
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
    orchestrator: Orchestrator | None = None,
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

    elif command == "/orchestra":
        if not arg:
            console.print(f"[yellow]Usage: /orchestra <task>[/]")
        else:
            async for token in orchestrator.execute(arg):
                sys.stdout.write(token)
                sys.stdout.flush()
            console.print()

    elif command == "/review":
        task = arg or "review the recent changes in this codebase"
        async for token in orchestrator.run_single_agent_streaming(AgentRole.REVIEWER, task):
            sys.stdout.write(token)
            sys.stdout.flush()
        console.print()

    elif command == "/debug":
        task = arg or "investigate recent errors in this codebase"
        async for token in orchestrator.run_single_agent_streaming(AgentRole.DEBUGGER, task):
            sys.stdout.write(token)
            sys.stdout.flush()
        console.print()

    elif command == "/test":
        task = arg or "write tests for the most recently changed files"
        async for token in orchestrator.run_single_agent_streaming(AgentRole.TESTER, task):
            sys.stdout.write(token)
            sys.stdout.flush()
        console.print()

    elif command == "/refactor":
        task = arg or "identify refactoring opportunities in this codebase"
        async for token in orchestrator.run_single_agent_streaming(AgentRole.REFACTORER, task):
            sys.stdout.write(token)
            sys.stdout.flush()
        console.print()

    elif command == "/devops":
        task = arg or "check deployment and CI/CD configuration"
        async for token in orchestrator.run_single_agent_streaming(AgentRole.DEVOPS, task):
            sys.stdout.write(token)
            sys.stdout.flush()
        console.print()

    elif command == "/docs":
        task = arg or "generate documentation for this project"
        async for token in orchestrator.run_single_agent_streaming(AgentRole.DOCS, task):
            sys.stdout.write(token)
            sys.stdout.flush()
        console.print()

    elif command == "/launch":
        if not arg:
            console.print(f"[yellow]Usage: /launch <product description>[/]")
        else:
            # Full pipeline: Build → Ship → Campaign
            console.print(f"\n  [{GOLD}]🚀 LAUNCH PIPELINE[/] [dim]build → ship → go viral[/]\n")

            console.print(f"  [{GOLD}]Phase 1: Build[/]")
            async for token in orchestrator.execute(f"build: {arg}"):
                sys.stdout.write(token)
                sys.stdout.flush()
            console.print()

            console.print(f"\n  [{GOLD}]Phase 2: Campaign[/]")
            campaign_brief = (
                f"Create a full launch campaign for: {arg}\n"
                f"Generate: blog post outline, 5 tweets, 3 LinkedIn posts, "
                f"2 image prompts, 1 video script, SEO keywords."
            )
            # Run campaign director
            spec = get_content_spec(ContentRole.CAMPAIGN_DIRECTOR)
            from djcode.orchestrator.engine import AgentRunner
            runner = AgentRunner(operator.provider, spec, orchestrator.bus, auto_accept=True)
            async for token in runner.run_streaming(campaign_brief):
                sys.stdout.write(token)
                sys.stdout.flush()
            console.print()
            console.print(f"\n  [{GOLD}]🚀 Launch complete. Product built + campaign ready.[/]\n")

    elif command == "/campaign":
        if not arg:
            console.print(f"[yellow]Usage: /campaign <brief>[/]")
        else:
            console.print(f"\n  [{GOLD}]📢 Content Campaign[/]\n")
            spec = get_content_spec(ContentRole.CAMPAIGN_DIRECTOR)
            from djcode.orchestrator.engine import AgentRunner
            runner = AgentRunner(operator.provider, spec, orchestrator.bus, auto_accept=True)
            async for token in runner.run_streaming(arg):
                sys.stdout.write(token)
                sys.stdout.flush()
            console.print()

    elif command == "/image":
        task = arg or "generate creative image prompts for a tech product"
        console.print(f"\n  [{GOLD}]🎨 Maya (Image Prompter)[/]\n")
        spec = get_content_spec(ContentRole.IMAGE_PROMPTER)
        from djcode.orchestrator.engine import AgentRunner
        runner = AgentRunner(llm, spec, orchestrator.bus, auto_accept=True)
        async for token in runner.run_streaming(task):
            sys.stdout.write(token)
            sys.stdout.flush()
        console.print()

    elif command == "/video":
        task = arg or "create a cinematic product video shot list"
        console.print(f"\n  [{GOLD}]🎬 Kubera (Video Director)[/]\n")
        spec = get_content_spec(ContentRole.VIDEO_DIRECTOR)
        from djcode.orchestrator.engine import AgentRunner
        runner = AgentRunner(llm, spec, orchestrator.bus, auto_accept=True)
        async for token in runner.run_streaming(task):
            sys.stdout.write(token)
            sys.stdout.flush()
        console.print()

    elif command == "/social":
        task = arg or "create social media content for a tech product launch"
        console.print(f"\n  [{GOLD}]📱 Chitragupta (Social Strategist)[/]\n")
        spec = get_content_spec(ContentRole.SOCIAL_STRATEGIST)
        from djcode.orchestrator.engine import AgentRunner
        runner = AgentRunner(llm, spec, orchestrator.bus, auto_accept=True)
        async for token in runner.run_streaming(task):
            sys.stdout.write(token)
            sys.stdout.flush()
        console.print()

    elif command == "/agents":
        orchestrator.render_roster()
        # Also show content agents
        console.print(f"\n  [bold {GOLD}]Content Agents[/]\n")
        for spec in list_content_agents():
            console.print(
                f"  📢 [bold white]{spec.name:<14}[/] "
                f"[dim]{spec.title:<28}[/] "
                f"{len(spec.tools_allowed)} tools  "
                f"{'[dim red]read-only[/]' if spec.read_only else '[dim green]full[/]'}  "
                f"[dim]t={spec.temperature}[/]"
            )
        console.print(f"\n  [dim]Use /campaign, /image, /video, /social for content agents[/]")
        console.print(f"  [dim]Use /launch for full build → ship → campaign pipeline[/]\n")

    elif command == "/stats":
        period = arg.strip().lower() if arg.strip() else "all"
        if period not in ("all", "7d", "30d"):
            period = "all"
        render_stats(console, period=period)

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

    elif command == "/shortcuts":
        show_shortcuts()

    # ── MCP Extensions ────────────────────────────────────────────────
    elif command == "/extension":
        ext_mgr = ExtensionManager()
        sub = arg.strip().split(maxsplit=2) if arg.strip() else []

        if not sub or sub[0] == "list":
            statuses = ext_mgr.get_status()
            if not statuses:
                console.print(f"[{GOLD}]No extensions registered.[/]")
                console.print("[dim]Add one: /extension add <name> <command> [args...][/]")
            else:
                from rich.table import Table as _T
                table = _T(show_header=True, header_style=f"bold {GOLD}", border_style="dim")
                table.add_column("Name", style="bold white")
                table.add_column("Command", style="dim")
                table.add_column("Status")
                table.add_column("Tools", justify="right")
                for s in statuses:
                    status = "[green]on[/]" if s["enabled"] else "[red]off[/]"
                    if s.get("connected"):
                        status = "[green]connected[/]"
                    if s.get("last_error"):
                        status = f"[red]error[/]"
                    table.add_row(s["name"], s["cmd"], status, str(s["tools_count"]))
                console.print()
                console.print(table)
                console.print()

        elif sub[0] == "add" and len(sub) >= 3:
            ext_name = sub[1]
            ext_cmd_parts = sub[2].split()
            ext_cmd = ext_cmd_parts[0]
            ext_args = ext_cmd_parts[1:] if len(ext_cmd_parts) > 1 else []
            ext = ext_mgr.add(ext_name, ext_cmd, ext_args)
            console.print(f"[green]Added extension:[/] {ext.name} -> {ext.cmd}")
            console.print(f"[dim]Tools will be discovered on first use. Try: /extension tools {ext_name}[/]")

        elif sub[0] in ("rm", "remove") and len(sub) >= 2:
            if ext_mgr.remove(sub[1]):
                console.print(f"[green]Removed extension:[/] {sub[1]}")
            else:
                console.print(f"[yellow]Extension not found:[/] {sub[1]}")

        elif sub[0] == "enable" and len(sub) >= 2:
            ext_mgr.enable(sub[1])
            console.print(f"[green]Enabled:[/] {sub[1]}")

        elif sub[0] == "disable" and len(sub) >= 2:
            ext_mgr.disable(sub[1])
            console.print(f"[yellow]Disabled:[/] {sub[1]}")

        elif sub[0] == "tools" and len(sub) >= 2:
            try:
                tools = await ext_mgr.refresh_tools(sub[1])
                if tools:
                    console.print(f"\n[bold {GOLD}]Tools from {sub[1]}:[/]")
                    for t in tools:
                        desc = t.get("description", "")[:60]
                        console.print(f"  [white]{t.get('name', '?')}[/] [dim]— {desc}[/]")
                    console.print()
                else:
                    console.print(f"[dim]No tools found for {sub[1]}[/]")
            except Exception as e:
                console.print(f"[red]Error connecting to {sub[1]}:[/] {e}")
            finally:
                await ext_mgr.shutdown()

        else:
            console.print("[dim]Usage: /extension [list|add|rm|enable|disable|tools] ...[/]")

    # ── Recipes ───────────────────────────────────────────────────────
    elif command == "/recipe":
        recipe_mgr = RecipeManager()
        sub = arg.strip().split(maxsplit=1) if arg.strip() else []

        if not sub or sub[0] == "list":
            render_recipe_list(console)

        elif sub[0] == "show" and len(sub) >= 2:
            try:
                recipe = recipe_mgr.load(sub[1])
                render_recipe_detail(console, recipe)
            except FileNotFoundError as e:
                console.print(f"[yellow]{e}[/]")

        elif sub[0] == "run" and len(sub) >= 2:
            # Parse: /recipe run <name> [params]
            run_parts = sub[1].split(maxsplit=1)
            recipe_name = run_parts[0]
            param_str = run_parts[1] if len(run_parts) > 1 else ""

            try:
                recipe = recipe_mgr.load(recipe_name)
                params = recipe_mgr.collect_params_from_args(recipe, param_str)

                # Check for missing required params — prompt interactively
                missing = [
                    p for p in recipe.parameters
                    if p.required and p.key not in params and not p.default
                ]
                if missing:
                    console.print(f"[bold {GOLD}]Recipe: {recipe.name}[/] — {recipe.description}")
                    console.print(f"[dim]Fill in the required parameters:[/]")
                    extra = recipe_mgr.collect_params_interactive(recipe)
                    params.update(extra)

                console.print(f"\n[bold {GOLD}]Running recipe:[/] {recipe.name}")
                console.print()

                full_response = ""
                async for token in recipe_mgr.execute(recipe, params, operator):
                    sys.stdout.write(token)
                    sys.stdout.flush()
                    full_response += token

                if full_response:
                    console.print()

            except FileNotFoundError as e:
                console.print(f"[yellow]{e}[/]")
            except ValueError as e:
                console.print(f"[red]{e}[/]")
            except Exception as e:
                console.print(f"[red]Recipe execution error:[/] {e}")

        elif sub[0] == "create":
            console.print(f"[bold {GOLD}]Create a new recipe[/]")
            try:
                name = input("  Name: ").strip()
                desc = input("  Description: ").strip()
                instructions = input("  System instructions: ").strip()
                prompt = input("  Prompt template (use {{param}} for placeholders): ").strip()
                param_keys = input("  Parameters (comma-separated keys): ").strip()

                from djcode.recipes import Recipe, RecipeParam
                params = []
                for key in param_keys.split(","):
                    key = key.strip()
                    if key:
                        param_desc = input(f"    {key} description: ").strip()
                        params.append(RecipeParam(key=key, description=param_desc))

                recipe = Recipe(
                    name=name,
                    description=desc,
                    instructions=instructions,
                    prompt=prompt,
                    parameters=params,
                    author="user",
                )
                path = recipe_mgr.save(recipe)
                console.print(f"\n[green]Recipe saved:[/] {path}")
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Cancelled.[/]")

        elif sub[0] == "delete" and len(sub) >= 2:
            if recipe_mgr.delete(sub[1]):
                console.print(f"[green]Deleted recipe:[/] {sub[1]}")
            else:
                console.print(f"[yellow]Recipe not found:[/] {sub[1]}")

        else:
            console.print("[dim]Usage: /recipe [list|show|run|create|delete] ...[/]")

    # ── Session History & Resume ──────────────────────────────────────
    elif command == "/history":
        sdb = SessionDB()
        sub = arg.strip().split(maxsplit=1) if arg.strip() else []

        if not sub:
            sessions = sdb.list_sessions(limit=20)
            render_session_list(console, sessions)

        elif sub[0] == "search" and len(sub) >= 2:
            results = sdb.search_sessions(sub[1])
            if results:
                console.print(f"\n[bold {GOLD}]Sessions matching '{sub[1]}':[/]")
                render_session_list(console, results)
            else:
                console.print(f"[dim]No sessions match '{sub[1]}'[/]")

        else:
            console.print("[dim]Usage: /history [search <query>][/]")

    elif command == "/resume":
        if not arg.strip():
            console.print("[dim]Usage: /resume <session_id>[/]")
            console.print("[dim]Use /history to find session IDs[/]")
        else:
            sdb = SessionDB()
            target_id = arg.strip()
            session = sdb.get_session(target_id)
            if not session:
                console.print(f"[yellow]Session not found:[/] {target_id}")
            else:
                messages = sdb.load_conversation(target_id)
                if not messages:
                    console.print(f"[yellow]No conversation data for session {target_id}[/]")
                else:
                    # Restore messages into operator
                    from djcode.provider import Message as _Msg
                    # Keep the current system prompt, replace the rest
                    system_msg = operator.messages[0] if operator.messages else None
                    operator.messages.clear()
                    if system_msg:
                        operator.messages.append(system_msg)

                    restored = 0
                    for m in messages:
                        role = m.get("role", "")
                        if role == "system":
                            continue  # Keep our own system prompt
                        content = m.get("content", "")
                        tc = m.get("tool_calls")
                        operator.messages.append(_Msg(
                            role=role,
                            content=content,
                            tool_calls=tc if tc else None,
                        ))
                        restored += 1

                    console.print(
                        f"[green]Resumed session {target_id}[/] "
                        f"({session.model}, {restored} messages)"
                    )
                    console.print(f"[dim]Conversation context restored. Continue where you left off.[/]")

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
    show_thinking: bool = True,
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
        show_thinking=show_thinking,
    )

    # Initialize memory
    memory = MemoryManager()

    # Initialize orchestrator
    orchestrator = Orchestrator(llm, auto_accept=effective_auto_accept)

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

    # Permission system
    permissions = PermissionManager(auto_accept=effective_auto_accept)

    # Track session (dual: legacy JSON + new SQLite)
    session_id = record_session_start(llm.config.model, llm.config.name)
    session_db = SessionDB()
    session_db.migrate_from_json()  # One-time migration, no-op if already done
    sqlite_session_id = session_db.create_session(llm.config.model, llm.config.name)
    files_touched: list[str] = []

    # Initialize extension manager
    ext_manager = ExtensionManager()

    # Print banner + permissions warning + buddy greeting
    print_banner(llm)
    permissions.show_startup_warning()
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

    # Wire up TUI keybindings (Ctrl+O, Ctrl+L, Ctrl+T, Ctrl+P, etc.)
    tui_mode = get_mode_state()
    tui_mode.auto_accept = effective_auto_accept
    tui_mode.verbose_thinking = show_thinking
    register_keybindings(session, operator, status_bar)

    while True:
        try:
            buddy.set_mood("idle")

            # Prompt: ❯ (gold) in ACT mode, ⏸ (magenta) in PLAN mode
            if tui_mode.plan_mode:
                _prompt_html = HTML("<style fg='#FF00FF'><b>\u23f8 </b></style>")
            else:
                _prompt_html = HTML("<style fg='#FFD700'><b>\u276f </b></style>")

            user_input = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: session.prompt(_prompt_html),
            )
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye.[/]")
            break

        user_input = user_input.strip()
        if not user_input:
            continue

        # Interactive command picker: bare "/" triggers fuzzy picker
        if user_input == "/":
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                picked = await asyncio.get_event_loop().run_in_executor(
                    pool, show_command_picker
                )
            if picked:
                should_continue = await handle_slash_command(
                    picked, operator, memory, status_bar, orchestrator
                )
                if not should_continue:
                    break
            continue

        # Slash commands
        if user_input.startswith("/"):
            should_continue = await handle_slash_command(
                user_input, operator, memory, status_bar, orchestrator
            )
            if not should_continue:
                break
            continue

        # Track last input for Ctrl+R rerun
        tui_mode.last_user_input = user_input
        tui_mode.reset_cancel()

        # Track in memory
        memory.add_session_message("user", user_input)

        # Enhance the prompt with context before sending
        enhanced = enhance_prompt(user_input)
        send_text = enhanced.enhanced if enhanced.was_enhanced else user_input

        # Plan mode: prepend planning instruction so the model never executes
        if tui_mode.plan_mode:
            send_text = f"{tui_mode.plan_mode_prompt_injection}\n\n{send_text}"

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

            # Live thinking indicator: ⏺ Thinking... (Xs · ↓ N tokens)
            import time as _time
            _start_time = _time.monotonic()
            _token_count = 0
            first_token = True

            # Show initial thinking indicator
            sys.stdout.write(f"\033[33m\u23fa\033[0m \033[2mThinking...\033[0m")
            sys.stdout.flush()

            async for token in operator.send(send_text):
                # Check if user hit Ctrl+K to cancel
                if tui_mode.is_cancelled:
                    sys.stdout.write("\r\033[K")
                    console.print("[yellow]Generation cancelled.[/]")
                    break

                _token_count += 1

                if first_token:
                    # Clear the thinking indicator line
                    sys.stdout.write("\r\033[K")
                    first_token = False
                else:
                    # Update thinking indicator while waiting (every 5 tokens)
                    pass

                if raw:
                    sys.stdout.write(token)
                    sys.stdout.flush()
                else:
                    sys.stdout.write(token)
                    sys.stdout.flush()

                full_response += token

                # Periodically update thinking line if we haven't started output yet
                if first_token and _token_count % 3 == 0:
                    elapsed = _time.monotonic() - _start_time
                    sys.stdout.write(f"\r\033[K\033[33m\u23fa\033[0m \033[2mThinking... ({elapsed:.1f}s \u00b7 \u2193 {_token_count} tokens)\033[0m")
                    sys.stdout.flush()

            if first_token:
                # Never got a token -- clear thinking indicator
                sys.stdout.write("\r\033[K")

            # Show response stats after completion
            if full_response and not raw:
                _elapsed = _time.monotonic() - _start_time
                _est_tokens = len(full_response) // 4
                if _est_tokens >= 1000:
                    _tok_str = f"{_est_tokens / 1000:.1f}k"
                else:
                    _tok_str = str(_est_tokens)
                console.print(f"\n  [dim]\u2193 {_tok_str} tokens \u00b7 {_elapsed:.1f}s[/]")

            if full_response:
                memory.add_session_message("assistant", full_response)
                buddy.observe(user_input, full_response, success=True)
                buddy.set_mood("success")
                buddy.tick()

                # Track usage stats (legacy JSON + SQLite)
                token_est = len(full_response) // 4
                record_session_update(session_id, tokens=token_est, messages=1)
                session_db.update_session(
                    sqlite_session_id, tokens_out=token_est, messages=1,
                )
                # Persist conversation for /resume
                session_db.save_message(sqlite_session_id, "user", user_input)
                session_db.save_message(sqlite_session_id, "assistant", full_response)

                # Tool extraction router — for models without native tool calling
                # If the model produced text but didn't use any native tool_calls,
                # scan the output for tool intents and execute them.
                if not operator.last_had_tool_calls:
                    try:
                        from djcode.tool_router import ToolExtractionRouter

                        router = ToolExtractionRouter()
                        extracted_intents = router.extract_intents(full_response)
                        if extracted_intents:
                            current_cfg_for_auto = load_config()
                            effective_auto = operator.auto_accept or current_cfg_for_auto.get("auto_accept", False)
                            tool_results = await router.extract_and_execute(
                                full_response, auto_accept=effective_auto
                            )
                            # Feed results back as context for the next turn
                            if tool_results:
                                result_context = router.format_results_for_context(tool_results)
                                if result_context:
                                    from djcode.provider import Message as _Msg
                                    operator.messages.append(
                                        _Msg(
                                            role="user",
                                            content=result_context,
                                        )
                                    )
                    except Exception as tr_err:
                        logger.debug("Tool router error: %s", tr_err)

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

            # Dim separator after each response
            if full_response and not raw:
                try:
                    _term_width = os.get_terminal_size().columns
                except OSError:
                    _term_width = 80
                console.print(f"[dim]{'─' * _term_width}[/]")

        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/]")
            buddy.set_mood("idle")
        except KeyboardInterrupt:
            raise  # Re-raise to be caught by outer handler
        except Exception as e:
            buddy.observe(user_input, "", success=False)
            buddy.set_mood("error")
            err = classify_error(e)
            console.print(f"\n{format_error(err)}")
            # Auto-fallback: suggest smaller model on OOM/timeout
            if err.fallback == "retry_with_smaller_model":
                fb = get_fallback_model(llm.config.model)
                if fb:
                    console.print(f"  [dim]Try: /model {fb}[/]")

    # Save project context on exit
    msg_count = len([m for m in operator.messages if m.role in ("user", "assistant")])
    save_context(
        model=llm.config.model,
        provider=llm.config.name,
        messages_count=msg_count,
        files_touched=files_touched,
    )
    console.print(f"  [dim]Saved djcode.md[/]")

    record_session_end(session_id)
    await llm.close()


async def run_oneshot(
    prompt: str,
    provider: str | None = None,
    model: str | None = None,
    bypass_rlhf: bool = False,
    raw: bool = False,
    show_thinking: bool = True,
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

    operator = Operator(
        llm, bypass_rlhf=bypass_rlhf, raw=raw, model=llm.config.model,
        show_thinking=show_thinking,
    )

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
