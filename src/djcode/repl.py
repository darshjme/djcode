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
from pathlib import Path

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
from djcode.commands import SlashCompleter, command_help
from djcode.repl_runtime import run_interruptible
from djcode.agents.operator import Operator
from djcode.auth import (
    PROVIDERS,
    is_uncensored_model,
)
from djcode.errors import classify_error, format_error, get_fallback_model
from djcode.orchestrator import Orchestrator
from djcode.agents.registry import AgentRole
from djcode.agents.content_registry import ContentRole, list_content_agents, get_content_spec
from djcode.context_file import save_context, inject_context_into_prompt
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

from djcode.tui_theme import GOLD

Q_STYLE = questionary.Style([
    ("selected", "fg:#7C96FF bold"),
    ("pointer", "fg:#7C96FF bold"),
    ("highlighted", "fg:#7C96FF"),
    ("question", "fg:#7C96FF bold"),
    ("answer", "fg:#FFFFFF bold"),
])


def print_banner(provider: Provider, *, auto_accept: bool = False) -> None:
    """Keep the model, workspace and approval mode visible without a splash screen."""
    cwd = str(Path.cwd())
    try:
        cwd = "~/" + str(Path.cwd().relative_to(Path.home()))
    except ValueError:
        pass
    body = Text()
    body.append(f"❯_ DJcode {__version__}", style="bold #F4F4F4")
    body.append(f"  {provider.config.name} · {provider.config.model}\n")
    body.append("Your terminal. An entire team.\n", style="bold #F4F4F4")
    body.append(cwd + "\n", style="dim")
    body.append(f"Approvals: {'auto' if auto_accept else 'ask'}", style="yellow")
    body.append(" · /help commands · Tab complete · Ctrl+P commands · Ctrl+G plan", style="dim")
    body.append("\n/features · /project · /memory · /design · /fleet", style=GOLD)
    console.print(Panel(body, border_style="#29292C", padding=(1, 2)))


HELP_TEXT = command_help("repl") + "\n\n[dim]Tab completes commands · Up/Down history · /shortcuts for keys[/]"


def _runtime_config(provider: Provider) -> dict:
    from djcode.model_selection import candidate_config
    current = provider.config
    ident = current.provider_id or current.name
    config = candidate_config(load_config(), ident, current.model)
    config["base_url"] = current.base_url
    config[f"{ident}_auth_method"] = current.auth_method
    return config


def _discover_current_models(provider, provider_id=None):
    from djcode.model_selection import model_catalog
    return model_catalog(_runtime_config(provider), provider_id)


def _handle_models_list(provider: Provider, provider_id=None) -> None:
    from djcode.model_selection import capability_summary
    found = _discover_current_models(provider, provider_id)
    if not found["models"]:
        console.print(found["message"], markup=False)
        return
    table = Table(title=f"Models · {found['provider']} · {found['source']} catalog", border_style=GOLD)
    table.add_column("Model")
    table.add_column("Provider-reported capabilities")
    for item in found["models"]:
        ident = item["id"]
        table.add_row(ident + (" · current" if ident == found.get("selected") else ""), capability_summary(item))
    console.print(table)


async def _apply_connection(config: dict, operator: Operator, status_bar: StatusBar,
                            orchestrator=None, *, persist=True) -> bool:
    from djcode.config import load_config_for_write
    candidate = None
    try:
        candidate = Provider(ProviderConfig.from_mapping(config))
        if persist:
            load_config_for_write()
            save_config(config)
    except (OSError, ValueError, TypeError) as error:
        if candidate:
            await candidate.close()
        console.print(f"Selection could not be applied: {type(error).__name__}. Current provider retained.", markup=False)
        return False
    previous = operator.provider
    operator.provider = candidate
    manager = getattr(operator, "context_manager", None)
    if manager:
        manager.reconfigure(candidate.config.model, candidate, candidate.config.context_window)
    candidate._session_runtimes = list(getattr(previous, "_session_runtimes", []))
    if not candidate._session_runtimes and getattr(operator, "capabilities", None):
        candidate._session_runtimes = [operator.capabilities]
    previous._session_runtimes = []
    if orchestrator:
        orchestrator.provider = candidate
        if getattr(orchestrator, "_shadow", None):
            orchestrator._shadow.provider = candidate
    try:
        await previous.close()
    except Exception:
        console.print("Previous provider cleanup failed; new selection is active.", markup=False)
    if getattr(operator, "session_db", None) and getattr(operator, "session_id", None):
        try:
            operator.session_db.set_session_model(operator.session_id, config["provider"], config["model"])
        except Exception:
            console.print("Model changed; session metadata could not be saved.", markup=False)
    status_bar.update(provider=config["provider"], model=config["model"], uncensored=is_uncensored_model(config["model"]))
    console.print(f"Connected to {config['provider']} / {config['model']}", markup=False)
    return True


async def _handle_model_switch(arg: str, operator: Operator, status_bar: StatusBar,
                               orchestrator=None, provider_id=None) -> bool:
    from djcode.config import load_config_for_write
    from djcode.model_selection import select_model, ModelSelectionError
    try:
        load_config_for_write()
        config = _runtime_config(operator.provider)
        selected = await asyncio.to_thread(select_model, config, arg, provider_id)
    except (ModelSelectionError, ValueError) as error:
        console.print(str(error), markup=False)
        return False
    return await _apply_connection(selected, operator, status_bar, orchestrator)


async def _handle_model_switch_interactive(operator: Operator, status_bar: StatusBar,
                                           orchestrator=None, provider_id=None) -> None:
    found = await asyncio.to_thread(_discover_current_models, operator.provider, provider_id)
    if not found["models"]:
        console.print(found["message"], markup=False)
        return
    selected = await questionary.autocomplete("Select model:", choices=[item["id"] for item in found["models"]],
                                             match_middle=True, ignore_case=True).ask_async()
    if selected:
        await _handle_model_switch(selected, operator, status_bar, orchestrator, found["provider"])


async def _handle_provider_switch_interactive(operator: Operator, status_bar: StatusBar,
                                              orchestrator=None, provider_id=None) -> None:
    """Choose a provider and a verified model as a single transaction."""
    from djcode.model_selection import provider_choices
    if provider_id is None:
        choices = provider_choices(load_config())
        provider_id = await questionary.select("Provider:", choices=[questionary.Choice(item["name"], value=item["id"]) for item in choices]).ask_async()
    if provider_id:
        await _handle_model_switch_interactive(operator, status_bar, orchestrator, provider_id)


async def handle_slash_command(
    cmd: str,
    operator: Operator,
    memory: MemoryManager,
    status_bar: StatusBar,
    orchestrator: Orchestrator | None = None,
) -> bool:
    """Handle a slash command. Returns True if the REPL should continue."""
    parts = cmd.strip().split(maxsplit=1)
    if not parts:
        return True
    command = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    from djcode.commands import plan_blocks_command
    if getattr(operator, "plan_mode", False) and plan_blocks_command(command, arg):
        console.print("[yellow]Plan mode: switch to Act with /plan before running this command.[/]")
        return True

    if orchestrator is not None and hasattr(orchestrator, "_shadow"):
        orchestrator.provider = operator.provider
        orchestrator._shadow.provider = operator.provider
        orchestrator.auto_accept = operator.auto_accept
        orchestrator._shadow.auto_accept = operator.auto_accept

    from djcode.session_commands import NAMES, handle
    if command in NAMES:
        console.print(await handle(operator, command, arg), markup=False)
        return True
    if command == "/connect":
        from djcode.startup import setup
        configured = await asyncio.to_thread(setup, load_config())
        from djcode.startup import probe
        checked = await asyncio.to_thread(probe, configured)
        if checked["status"] == "ready":
            await _apply_connection(configured, operator, status_bar, orchestrator, persist=False)
        else:
            console.print(checked["message"] + " Saved setup retained for later; current runtime unchanged.", markup=False)
        return True

    if command == "/help":
        console.print(Panel(HELP_TEXT, title=f"[bold {GOLD}]DJcode Help[/]", border_style=GOLD))

    elif command in {"/check", "/lint", "/update"}:
        from djcode.terminal_operations import run_operation
        for line in await asyncio.to_thread(run_operation, command, arg):
            console.print(line, markup=False)

    elif command == "/plan":
        operator.plan_mode = not operator.plan_mode
        mode = get_mode_state()
        mode.plan_mode = operator.plan_mode
        status_bar.update(mode=mode.mode_label)
        console.print(f"Mode: {mode.mode_label}", markup=False)

    elif command == "/thinking":
        operator.show_thinking = not operator.show_thinking
        get_mode_state().verbose_thinking = operator.show_thinking
        console.print(f"Thinking: {'ON' if operator.show_thinking else 'OFF'}", markup=False)

    elif command == "/design":
        from djcode.design_packs import list_packs
        from djcode.design_selection import select_pack
        if not arg.strip():
            for pack in list_packs():
                console.print(f"{pack['id']}: {pack['title']} · {pack['summary']}", markup=False)
            console.print("Use /design ID to select, or /design off to clear.")
        else:
            try:
                console.print(select_pack(operator, arg.strip()), markup=False)
            except ValueError as error:
                console.print(str(error), markup=False)

    elif command == "/models":
        await asyncio.to_thread(_handle_models_list, operator.provider, arg.strip() or None)

    elif command == "/model":
        if arg:
            await _handle_model_switch(arg, operator, status_bar, orchestrator)
        else:
            await _handle_model_switch_interactive(operator, status_bar, orchestrator)

    elif command == "/provider":
        await _handle_provider_switch_interactive(operator, status_bar, orchestrator, arg.strip() or None)

    elif command == "/auth":
        from djcode.terminal_operations import run_operation
        for line in await asyncio.to_thread(run_operation, command, arg):
            console.print(line, markup=False)

    elif command == "/auto":
        new_val = not operator.auto_accept
        set_value("auto_accept", new_val)
        operator.auto_accept = new_val
        get_mode_state().auto_accept = new_val
        if orchestrator is not None:
            orchestrator.auto_accept = new_val
            orchestrator._shadow.auto_accept = new_val
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

    elif command == "/build":
        if not arg.strip():
            console.print("[yellow]Usage: /build <task>[/]")
        else:
            async for token in orchestrator.run_single_agent_streaming(AgentRole.CODER, arg):
                sys.stdout.write(token)
                sys.stdout.flush()
            console.print()

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
        from djcode.docs import DOCS_SECTIONS, render_docs, render_docs_index
        topic = arg.strip().lower()
        if not topic:
            render_docs_index(console)
        elif topic in DOCS_SECTIONS or topic == "all":
            render_docs(console, topic)
        else:
            async for token in orchestrator.run_single_agent_streaming(AgentRole.DOCS, arg):
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
            runner = AgentRunner(operator.provider, spec, orchestrator.bus, auto_accept=operator.auto_accept, approval_callback=operator.approval_callback)
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
            runner = AgentRunner(operator.provider, spec, orchestrator.bus, auto_accept=operator.auto_accept, approval_callback=operator.approval_callback)
            async for token in runner.run_streaming(arg):
                sys.stdout.write(token)
                sys.stdout.flush()
            console.print()

    elif command == "/image":
        task = arg or "generate creative image prompts for a tech product"
        console.print(f"\n  [{GOLD}]🎨 Maya (Image Prompter)[/]\n")
        spec = get_content_spec(ContentRole.IMAGE_PROMPTER)
        from djcode.orchestrator.engine import AgentRunner
        runner = AgentRunner(operator.provider, spec, orchestrator.bus, auto_accept=operator.auto_accept, approval_callback=operator.approval_callback)
        async for token in runner.run_streaming(task):
            sys.stdout.write(token)
            sys.stdout.flush()
        console.print()

    elif command == "/video":
        task = arg or "create a cinematic product video shot list"
        console.print(f"\n  [{GOLD}]🎬 Kubera (Video Director)[/]\n")
        spec = get_content_spec(ContentRole.VIDEO_DIRECTOR)
        from djcode.orchestrator.engine import AgentRunner
        runner = AgentRunner(operator.provider, spec, orchestrator.bus, auto_accept=operator.auto_accept, approval_callback=operator.approval_callback)
        async for token in runner.run_streaming(task):
            sys.stdout.write(token)
            sys.stdout.flush()
        console.print()

    elif command == "/social":
        task = arg or "create social media content for a tech product launch"
        console.print(f"\n  [{GOLD}]📱 Chitragupta (Social Strategist)[/]\n")
        spec = get_content_spec(ContentRole.SOCIAL_STRATEGIST)
        from djcode.orchestrator.engine import AgentRunner
        runner = AgentRunner(operator.provider, spec, orchestrator.bus, auto_accept=operator.auto_accept, approval_callback=operator.approval_callback)
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

    elif command == "/raw":
        operator.raw = not operator.raw
        state = "on" if operator.raw else "off"
        console.print(f"[green]Raw mode:[/] {state}")

    elif command in ("/exit", "/quit", "/q"):
        console.print("[dim]Goodbye.[/]")
        return False

    elif command in {"/shortcuts", "/hotkeys"}:
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
                            tool_calls=tc or [],
                            tool_call_id=m.get("tool_call_id"), name=m.get("name"), images=m.get("images", []),
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


async def _approve_repl_tool(name: str, arguments: dict) -> bool:
    import json
    body = Text(f"Tool: {name}\n{json.dumps(arguments, indent=2)[:2000]}")
    console.print(Panel(body, title="Approve tool"))
    return bool(await questionary.confirm("Execute this tool?", default=False).ask_async())


async def _run_repl_command(command, operator, memory, status_bar, orchestrator) -> bool:
    try:
        result = await run_interruptible(handle_slash_command(
            command, operator, memory, status_bar, orchestrator,
        ))
        if result is None:
            console.print("[yellow]Command cancelled. Ready for another prompt.[/]")
        return result is not False
    except Exception as error:
        console.print(format_error(classify_error(error)))
        return True


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

    # Provider setup is handled once by the CLI startup flow.

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
        approval_callback=_approve_repl_tool,
    )

    # Initialize memory
    memory = MemoryManager()

    # Initialize orchestrator
    orchestrator = Orchestrator(llm, auto_accept=effective_auto_accept,
                                approval_callback=_approve_repl_tool)

    # Initialize status bar
    status_bar = StatusBar()
    status_bar.update(
        model=llm.config.model,
        provider=llm.config.name,
        token_count=0,
        auto_accept=effective_auto_accept,
        uncensored=is_uncensored_model(llm.config.model) or bypass_rlhf,
    )

    # Track session (dual: legacy JSON + new SQLite)
    session_id = record_session_start(llm.config.model, llm.config.name)
    session_db = SessionDB()
    session_db.migrate_from_json()  # One-time migration, no-op if already done
    operator.session_id = session_db.create_session(llm.config.model, llm.config.name)
    operator.session_db = session_db
    operator.on_checkpoint = lambda messages: session_db.save_conversation(operator.session_id, messages)
    files_touched: list[str] = []

    # Initialize extension manager
    ext_manager = ExtensionManager()

    # Compact startup summary, including the effective approval mode.
    print_banner(llm, auto_accept=effective_auto_accept)

    # Set up prompt toolkit session with FIXED bottom toolbar
    session: PromptSession[str] = PromptSession(
        history=FileHistory(str(HISTORY_FILE)),
        auto_suggest=AutoSuggestFromHistory(),
        completer=SlashCompleter(),
        complete_while_typing=True,
        bottom_toolbar=status_bar.render,
    )

    # Wire up TUI keybindings (Ctrl+O, Ctrl+L, Ctrl+T, Ctrl+P, etc.)
    tui_mode = get_mode_state()
    tui_mode.auto_accept = effective_auto_accept
    tui_mode.verbose_thinking = show_thinking
    tui_mode.plan_mode = False
    operator.plan_mode = False
    register_keybindings(session, operator, status_bar, orchestrator)

    try:
        while True:
            try:

                # Prompt: ❯ (gold) in ACT mode, ⏸ (magenta) in PLAN mode
                if tui_mode.plan_mode:
                    _prompt_html = HTML("<style fg='#FF00FF'><b>\u23f8 </b></style>")
                else:
                    _prompt_html = HTML("<style fg='#7C96FF'><b>\u276f </b></style>")

                user_input = await session.prompt_async(_prompt_html)
            except KeyboardInterrupt:
                console.print("[dim]Input cancelled. /exit or Ctrl+D to quit.[/]")
                continue
            except EOFError:
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
                    should_continue = await _run_repl_command(
                        picked, operator, memory, status_bar, orchestrator
                    )
                    if not should_continue:
                        break
                continue

            # Slash commands
            if user_input.startswith("/"):
                should_continue = await _run_repl_command(
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

            async def respond():
                full_response = ""
                try:
                    if not raw:
                        console.print()  # Spacing

                    if enhanced.was_enhanced:
                        desc = describe_enhancement(enhanced)
                        console.print(f"  [dim {GOLD}]{desc}[/]")

                    # Live thinking indicator: ⏺ Thinking... (Xs · ↓ N tokens)
                    import time as _time
                    _start_time = _time.monotonic()
                    _token_count = 0
                    first_token = True

                    # Show initial thinking indicator
                    sys.stdout.write(f"\033[33m\u23fa\033[0m \033[2mThinking...\033[0m")
                    sys.stdout.flush()

                    async for token in operator.send(send_text):
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

                        # Track usage stats (legacy JSON + SQLite)
                        token_est = len(full_response) // 4
                        record_session_update(session_id, tokens=token_est, messages=1)
                        session_db.update_session(
                            operator.session_id, tokens_out=token_est, messages=1,
                        )
                        # Persist conversation for /resume
                        session_db.save_conversation(operator.session_id, operator.messages)

                        # Tool extraction router — for models without native tool calling
                        # Censorship detection — warn if aligned model refuses
                        from djcode.prompt import CENSORED_WARNING, detect_refusal

                        if detect_refusal(full_response) and not is_uncensored_model(llm.config.model):
                            console.print(Panel(
                                CENSORED_WARNING.format(model=llm.config.model),
                                title="[yellow]Model Censorship Detected[/]",
                                border_style="yellow",
                            ))

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
                except Exception as e:
                    err = classify_error(e)
                    console.print(f"\n{format_error(err)}")
                    # Auto-fallback: suggest smaller model on OOM/timeout
                    if err.fallback == "retry_with_smaller_model":
                        fb = get_fallback_model(llm.config.model)
                        if fb:
                            console.print(f"  [dim]Try: /model {fb}[/]")
                return True
            if await run_interruptible(respond()) is None:
                sys.stdout.write("\r\033[K")
                console.print("[yellow]Response cancelled. Ready for another prompt.[/]")
            session_db.save_conversation(operator.session_id, operator.messages)

    finally:
        try:
            # Save project context on exit
            msg_count = len([m for m in operator.messages if m.role in ("user", "assistant")])
            save_context(
                model=operator.provider.config.model,
                provider=operator.provider.config.name,
                messages_count=msg_count,
                files_touched=files_touched,
            )
            console.print(f"  [dim]Saved djcode.md[/]")

            record_session_end(session_id)
            session_db.end_session(operator.session_id)
            session_db.save_conversation(operator.session_id, operator.messages)
        finally:
            await ext_manager.shutdown()
            await llm.close()
            if operator.provider is not llm:
                await operator.provider.close()


async def run_oneshot(
    prompt: str,
    provider: str | None = None,
    model: str | None = None,
    bypass_rlhf: bool = False,
    raw: bool = False,
    show_thinking: bool = True,
    auto_accept: bool = False,
) -> None:
    """Run one task; preserve failures in the process exit status and always close HTTP."""
    import click
    config = ProviderConfig.from_config(provider_override=provider, model_override=model)
    llm = Provider(config)
    from djcode.sessions import SessionDB
    session_db = SessionDB()
    session_id = session_db.create_session(model=config.model, provider=config.name, cwd=os.getcwd())
    operator = None
    try:
        ok, msg = llm.validate_model()
        if not ok:
            raise click.ClickException(msg)
        if msg:
            console.print(msg, markup=False)
        operator = Operator(llm, bypass_rlhf=bypass_rlhf, raw=raw,
                            model=llm.config.model, show_thinking=show_thinking,
                            auto_accept=auto_accept)
        operator.on_checkpoint = lambda messages: session_db.save_conversation(session_id, messages)
        async for token in operator.send(prompt):
            sys.stdout.write(token)
            sys.stdout.flush()
        print()
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise click.exceptions.Exit(130)
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        if operator:
            session_db.save_conversation(session_id, operator.messages)
        session_db.end_session(session_id)
        await llm.close()
