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
from pathlib import Path

import questionary
from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from djcode import __version__
from djcode.agents.content_registry import ContentRole, get_content_spec, list_content_agents
from djcode.agents.operator import Operator
from djcode.agents.registry import AgentRole
from djcode.auth import (
    PROVIDERS,
    interactive_auth,
    interactive_provider_picker,
    is_uncensored_model,
)
from djcode.commands import SlashCompleter, command_help
from djcode.config import (
    HISTORY_FILE,
    ensure_dirs,
    load_config,
    set_value,
)
from djcode.context_file import save_context
from djcode.errors import classify_error, format_error, get_fallback_model
from djcode.extensions import ExtensionManager
from djcode.frontends.repl.render import render_session_list
from djcode.memory.manager import MemoryManager
from djcode.orchestrator import Orchestrator
from djcode.prompt_enhancer import describe_enhancement, enhance_prompt
from djcode.provider import (
    Provider,
    ProviderConfig,
    fuzzy_match_model,
    get_ollama_model_names,
)
from djcode.recipes import RecipeManager, render_recipe_detail, render_recipe_list
from djcode.repl_runtime import run_interruptible
from djcode.sessions import SessionDB
from djcode.stats import (
    record_session_end,
    record_session_start,
    record_session_update,
    render_stats,
)
from djcode.status import StatusBar
from djcode.tui import (
    get_mode_state,
    register_keybindings,
    show_command_picker,
    show_shortcuts,
)

logger = logging.getLogger(__name__)
console = Console()

GOLD = "#C79B7A"

Q_STYLE = questionary.Style(
    [
        ("selected", "fg:#C79B7A bold"),
        ("pointer", "fg:#C79B7A bold"),
        ("highlighted", "fg:#C79B7A"),
        ("question", "fg:#C79B7A bold"),
        ("answer", "fg:#FFFFFF bold"),
    ]
)


def print_banner(provider: Provider, *, auto_accept: bool = False) -> None:
    """Keep the model, workspace and approval mode visible without a splash screen."""
    cwd = str(Path.cwd())
    try:
        cwd = "~/" + str(Path.cwd().relative_to(Path.home()))
    except ValueError:
        pass
    body = Text()
    body.append(f"DJcode {__version__}", style=f"bold {GOLD}")
    body.append(f"  {provider.config.name} · {provider.config.model}\n")
    body.append(cwd + "\n", style="dim")
    body.append(f"Approvals: {'auto' if auto_accept else 'ask'}", style="yellow")
    body.append(" · /help commands · Tab complete · Ctrl+P plan", style="dim")
    console.print(Panel(body, border_style=GOLD, padding=(0, 1)))


HELP_TEXT = (
    command_help("repl")
    + "\n\n[dim]Tab completes commands · Up/Down history · /shortcuts for keys[/]"
)


def _discover_current_models(provider):
    from djcode.startup import probe

    config = load_config()
    config.update(
        provider=provider.config.name,
        model=provider.config.model,
        base_url=provider.config.base_url,
    )
    config[f"{provider.config.name}_api_key"] = provider.config.api_key
    config[f"{provider.config.name}_auth_method"] = provider.config.auth_method
    return probe(config)


def _handle_models_list(provider: Provider) -> None:
    found = _discover_current_models(provider)
    if not found["models"]:
        console.print(found["message"], markup=False)
        return
    table = Table(title="Available models", border_style=GOLD)
    table.add_column("Model")
    for item in found["models"]:
        name = item["name"]
        table.add_row(name + (" · current" if name == provider.config.model else ""))
    console.print(table)


async def _handle_model_switch_interactive(operator: Operator, status_bar: StatusBar) -> None:
    found = await asyncio.to_thread(_discover_current_models, operator.provider)
    if found["models"]:
        selected = await questionary.autocomplete(
            "Select model:",
            choices=[item["name"] for item in found["models"]],
            match_middle=True,
            ignore_case=True,
        ).ask_async()
    else:
        console.print(found["message"], markup=False)
        selected = await questionary.text("Exact model ID:").ask_async()
    if selected:
        _handle_model_switch(selected, operator, status_bar)


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
                    console.print("  [dim]\U0001f513 Uncensored mode active[/]")
            else:
                console.print(f"[red]Model '{arg}' not found.[/]")
                names = ", ".join(available[:10])
                console.print(f"[dim]Available: {names}[/]")
                console.print(f"[dim]Pull it with: ollama pull {arg}[/]")
        else:
            # Can't reach Ollama — set it anyway, will fail at chat time
            console.print("[yellow]Cannot verify model (Ollama unreachable).[/]")
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

    new_config = ProviderConfig.from_config(provider_override=provider_id)
    from djcode.account_auth import has_account

    authenticated = (
        has_account(provider_id)
        if new_config.auth_method == "account"
        else bool(new_config.api_key)
    )
    if prov_info.get("needs_key") and not authenticated:
        console.print(
            f"[yellow]No configured authentication for {prov_info['name']}.[/] [dim]Run /auth to "
            "configure.[/]"
        )
        return

    operator.provider = Provider(new_config)
    set_value("provider", provider_id)
    status_bar.update(provider=provider_id, model=new_config.model)
    console.print(f"[green]Provider switched to:[/] {prov_info.get('name', provider_id)}")


def _checkpoint_store(operator: Operator):
    """The store the chokepoint is writing into, or None when undo is inactive."""
    return getattr(getattr(operator, "dispatch_ctx", None), "checkpoints", None)


def drain_checkpoint_notices(operator: Operator) -> None:
    """Print anything the checkpoint store needs the user to know, once.

    This is what makes "bash is not covered" visible AT THE MOMENT IT MATTERS
    rather than in a docstring: the store raises a notice the first time a
    shell command runs without protection, and the REPL prints it as soon as
    the turn ends, long before the user reaches for /undo.
    """
    store = _checkpoint_store(operator)
    if store is None:
        return
    for note in store.pop_notices():
        console.print(f"[yellow]⚠ {note}[/]")


def _render_restore_plan(title: str, actions, coverage: list[str]) -> bool:
    """Show exactly what will be touched. Returns True if anything would move."""
    from rich.table import Table

    console.print(f"\n[bold {GOLD}]↺ {title}[/]")
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column(no_wrap=True)
    # Paths must never be elided: the whole point of the dry run is that the
    # user can see exactly which file is about to be overwritten or deleted.
    table.add_column(overflow="fold")
    table.add_column(overflow="fold")
    moves = 0
    for action in actions:
        if action.action == "restore":
            table.add_row("[green]restore[/]", action.path, "")
            moves += 1
        elif action.action == "delete":
            table.add_row("[red]delete[/]", action.path, f"[dim]{action.reason}[/]")
            moves += 1
        elif action.action == "skip":
            table.add_row("[yellow]skip[/]", action.path, f"[dim]{action.reason}[/]")
        elif action.action == "keep-dir":
            table.add_row("[dim]keep dir[/]", action.path, f"[dim]{action.reason}[/]")
        else:
            table.add_row("[dim]no-op[/]", action.path, f"[dim]{action.reason}[/]")
    if actions:
        console.print(table)
    skipped = sum(1 for a in actions if a.action == "skip")
    console.print(f"  [dim]{moves} change(s) · {skipped} skipped · nothing else is touched[/]")
    for note in coverage:
        console.print(f"  [dim]· {note}[/]")
    return moves > 0


def _render_restore_report(report) -> None:
    console.print(
        f"  [green]{len(report.restored)} restored[/] · "
        f"[red]{len(report.deleted)} deleted[/] · "
        f"[yellow]{len(report.skipped)} skipped[/] · "
        f"[red]{len(report.failed)} failed[/]"
    )
    for path, reason in report.failed:
        console.print(f"  [red]failed[/] {path} — {reason}", markup=False)
    for path in report.kept_dirs:
        console.print(f"  [dim]kept empty directory {path}[/]", markup=False)


def _tell_the_model(operator: Operator, report) -> None:
    """Tell the model the tree moved under it.

    Without this the model's next tool call is built on a false premise: it
    will `file_edit` with an old_string that no longer exists, fall into the
    recovery ladder, find nothing, and thrash. Same mechanism W6-4 uses for
    deny-with-comment -- a plain user message, so it survives /resume and the
    session log like any other.
    """
    if not report.restored and not report.deleted:
        return
    from djcode.provider import Message as _Msg

    lines = ["[djcode] The user rolled the working tree back with /undo."]
    for path in report.restored:
        lines.append(f"  restored  {path}")
    for path in report.deleted:
        lines.append(f"  deleted   {path}")
    for path, reason in report.skipped:
        lines.append(f"  untouched {path} ({reason})")
    lines.append("Re-read any of these files before editing them; earlier reads are stale.")
    operator.messages.append(_Msg(role="user", content="\n".join(lines)))


async def _handle_undo_family(command: str, arg: str, operator: Operator) -> None:
    """`/undo`, `/redo`, `/rewind`.

    Presentation only. Every semantic -- which checkpoints a command resolves
    to, what a restore would do, and the restore itself -- lives in
    `djcode.core.checkpoints`, which is terminal-free and which W9 does not
    move. W9 deletes these ~90 lines and re-renders them in
    frontends/repl/commands.py; it deletes no logic.
    """
    store = _checkpoint_store(operator)
    session_id = getattr(operator, "session_id", "") or ""
    if store is None:
        console.print("[yellow]Checkpoints are not active in this session.[/]")
        return

    targets = []
    title = ""
    if command == "/redo":
        target = store.pending_redo(session_id)
        if target is None:
            console.print("[dim]Nothing to redo.[/]")
            return
        targets = [target]
        title = "Redo the last undo"
    elif command == "/rewind":
        turns = store.turns(session_id, limit=20)
        if not turns:
            console.print("[dim]No checkpoints in this session yet.[/]")
            return
        choices = []
        for group in turns:
            head = group[0]
            files = sum(len(cp.files) for cp in group)
            label = head.label or head.tool_name or "(no prompt)"
            choices.append(
                questionary.Choice(
                    title=f"#{head.seq}  {head.created_at}  {files} file(s)  {label[:60]}",
                    value=min(cp.seq for cp in group),
                )
            )
        picked = await questionary.select(
            "Roll the files back to before which turn?", choices=choices, style=Q_STYLE
        ).ask_async()
        if picked is None:
            return
        targets = store.resolve_seq(session_id, int(picked))
        title = f"Rewind to before checkpoint #{picked}"
    else:
        if arg.strip().isdigit():
            seq = int(arg.strip())
            targets = store.resolve_seq(session_id, seq)
            title = f"Undo back to before checkpoint #{seq}"
        else:
            targets = store.last_turn(session_id)
            if targets:
                head = targets[0]
                shown = head.label or head.tool_name or ""
                title = f"Undo turn “{shown[:60]}” ({len(targets)} checkpoint(s))"
    if not targets:
        console.print("[dim]Nothing to undo. No tool has changed a file in this session.[/]")
        for note in store.coverage_notes():
            console.print(f"  [dim]· {note}[/]")
        return

    # A background agent inherits this session's DispatchContext and keeps
    # writing after the turn ends -- possibly while this restore runs. Rolling
    # files back under a live writer produces a tree neither of them intended.
    try:
        from djcode.tools.agent_spawn import _background_tasks

        live = [a for a, i in _background_tasks.items() if i.get("status") == "running"]
    except Exception:
        live = []
    if live:
        console.print(
            f"[yellow]⚠ {len(live)} background agent(s) are still writing "
            f"({', '.join(live[:4])}). Their changes are not in this checkpoint and "
            "they may overwrite the restore.[/]"
        )

    actions = store.plan_restore(targets)
    has_moves = _render_restore_plan(title, actions, store.coverage_notes())
    if not has_moves:
        console.print("[dim]Nothing to change.[/]")
        return
    # No flag, config key or approval mode skips this. An undo is not something
    # the model asked for, and a mis-fired one destroys work the store does not
    # hold.
    ok = await questionary.confirm("Apply?", default=False, style=Q_STYLE).ask_async()
    if not ok:
        console.print("[dim]Cancelled. Nothing was touched.[/]")
        return

    if command == "/redo":
        report = store.redo(session_id)
    else:
        report = store.restore(targets, session_id=session_id)
    _render_restore_report(report)
    _tell_the_model(operator, report)

    db = getattr(operator, "session_db", None)
    if db is not None and session_id:
        try:
            import json as _json

            db.save_message(
                session_id,
                role="system",
                content="",
                entry_type="checkpoint",
                checkpoint_blob=_json.dumps(report.as_dict()),
            )
        except Exception:
            logger.debug("could not log the restore to the session", exc_info=True)


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
        previous = operator.provider
        operator.provider = Provider(
            ProviderConfig.from_config(configured["provider"], configured["model"])
        )
        operator.provider._session_runtimes = [operator.capabilities]
        previous._session_runtimes = []
        operator.context_manager.provider = operator.provider
        await previous.close()
        status_bar.update(
            model=operator.provider.config.model, provider=operator.provider.config.name
        )
        return True

    if command == "/help":
        console.print(Panel(HELP_TEXT, title=f"[bold {GOLD}]DJcode Help[/]", border_style=GOLD))

    elif command in ("/check", "/lint"):
        from djcode.maintenance import run_checks

        result = await asyncio.to_thread(run_checks)
        console.print(result["summary"], markup=False)
        for item in result.get("checks", []):
            console.print(f"{item['name']}: {item['status']} · {item['detail']}", markup=False)

    elif command == "/update":
        from djcode.updater import perform_update

        result = await asyncio.to_thread(perform_update, force=True)
        console.print(result["message"], markup=False)
        if result.get("updated"):
            console.print("Restart DJcode after your current work to use the update.")

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
        await asyncio.to_thread(_handle_models_list, operator.provider)

    elif command == "/model":
        if not arg:
            # No arg — interactive picker
            await _handle_model_switch_interactive(operator, status_bar)
        else:
            _handle_model_switch(arg, operator, status_bar)
        if getattr(operator.provider, "_new_provider", None) is not None:
            await operator.provider._new_provider.close()
            operator.provider._new_provider = None
        if hasattr(operator, "context_manager"):
            from djcode.context.manager import ContextWindowManager

            operator.context_manager = ContextWindowManager(
                model=operator.provider.config.model, provider=operator.provider
            )
            operator.context_manager.replace_messages(operator.messages)

    elif command == "/provider":
        if not arg:
            await asyncio.to_thread(_handle_provider_switch_interactive, operator, status_bar)
        else:
            # Direct provider switch by name
            if arg in PROVIDERS:
                prov_info = PROVIDERS[arg]
                new_config = ProviderConfig.from_config(provider_override=arg)
                from djcode.account_auth import has_account

                authenticated = (
                    has_account(arg)
                    if new_config.auth_method == "account"
                    else bool(new_config.api_key)
                )
                if prov_info.get("needs_key") and not authenticated:
                    console.print(
                        f"[yellow]No configured authentication for {prov_info['name']}.[/] "
                        "[dim]Run /auth to configure.[/]"
                    )
                else:
                    operator.provider = Provider(new_config)
                    set_value("provider", arg)
                    status_bar.update(provider=arg, model=new_config.model)
                    console.print(f"[green]Provider switched to:[/] {prov_info['name']}")
            else:
                console.print(f"[red]Unknown provider:[/] {arg}")
                names = ", ".join(PROVIDERS.keys())
                console.print(f"[dim]Options: {names}[/]")

    elif command == "/auth":
        await asyncio.to_thread(interactive_auth)
        # Reload provider after auth
        cfg = load_config()
        provider_id = cfg.get("provider", "ollama")
        new_config = ProviderConfig.from_config(provider_override=provider_id)
        operator.provider = Provider(new_config)
        status_bar.update(provider=provider_id, model=new_config.model)

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
            console.print("[yellow]Usage: /scout <query>[/]")
        else:
            from djcode.agents.scout import Scout

            scout = Scout(operator.provider)
            console.print("[dim]Scout investigating...[/]")
            result = await scout.investigate(arg)
            console.print(Markdown(result))

    elif command == "/architect":
        if not arg:
            console.print("[yellow]Usage: /architect <task>[/]")
        else:
            from djcode.agents.architect import Architect

            architect = Architect(operator.provider)
            console.print("[dim]Architect planning...[/]")
            result = await architect.plan(arg)
            console.print(Markdown(result))

    elif command == "/uncensored":
        console.print(
            Panel(
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
            )
        )

    elif command == "/orchestra":
        if not arg:
            console.print("[yellow]Usage: /orchestra <task>[/]")
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
            console.print("[yellow]Usage: /launch <product description>[/]")
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

            runner = AgentRunner(
                operator.provider,
                spec,
                orchestrator.bus,
                auto_accept=operator.auto_accept,
                approval_callback=operator.approval_callback,
            )
            async for token in runner.run_streaming(campaign_brief):
                sys.stdout.write(token)
                sys.stdout.flush()
            console.print()
            console.print(f"\n  [{GOLD}]🚀 Launch complete. Product built + campaign ready.[/]\n")

    elif command == "/campaign":
        if not arg:
            console.print("[yellow]Usage: /campaign <brief>[/]")
        else:
            console.print(f"\n  [{GOLD}]📢 Content Campaign[/]\n")
            spec = get_content_spec(ContentRole.CAMPAIGN_DIRECTOR)
            from djcode.orchestrator.engine import AgentRunner

            runner = AgentRunner(
                operator.provider,
                spec,
                orchestrator.bus,
                auto_accept=operator.auto_accept,
                approval_callback=operator.approval_callback,
            )
            async for token in runner.run_streaming(arg):
                sys.stdout.write(token)
                sys.stdout.flush()
            console.print()

    elif command == "/image":
        task = arg or "generate creative image prompts for a tech product"
        console.print(f"\n  [{GOLD}]🎨 Maya (Image Prompter)[/]\n")
        spec = get_content_spec(ContentRole.IMAGE_PROMPTER)
        from djcode.orchestrator.engine import AgentRunner

        runner = AgentRunner(
            operator.provider,
            spec,
            orchestrator.bus,
            auto_accept=operator.auto_accept,
            approval_callback=operator.approval_callback,
        )
        async for token in runner.run_streaming(task):
            sys.stdout.write(token)
            sys.stdout.flush()
        console.print()

    elif command == "/video":
        task = arg or "create a cinematic product video shot list"
        console.print(f"\n  [{GOLD}]🎬 Kubera (Video Director)[/]\n")
        spec = get_content_spec(ContentRole.VIDEO_DIRECTOR)
        from djcode.orchestrator.engine import AgentRunner

        runner = AgentRunner(
            operator.provider,
            spec,
            orchestrator.bus,
            auto_accept=operator.auto_accept,
            approval_callback=operator.approval_callback,
        )
        async for token in runner.run_streaming(task):
            sys.stdout.write(token)
            sys.stdout.flush()
        console.print()

    elif command == "/social":
        task = arg or "create social media content for a tech product launch"
        console.print(f"\n  [{GOLD}]📱 Chitragupta (Social Strategist)[/]\n")
        spec = get_content_spec(ContentRole.SOCIAL_STRATEGIST)
        from djcode.orchestrator.engine import AgentRunner

        runner = AgentRunner(
            operator.provider,
            spec,
            orchestrator.bus,
            auto_accept=operator.auto_accept,
            approval_callback=operator.approval_callback,
        )
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
        console.print("\n  [dim]Use /campaign, /image, /video, /social for content agents[/]")
        console.print("  [dim]Use /launch for full build → ship → campaign pipeline[/]\n")

    elif command == "/stats":
        period = arg.strip().lower() if arg.strip() else "all"
        if period not in ("all", "7d", "30d"):
            period = "all"
        render_stats(console, period=period)

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
                table = Table(show_header=True, header_style=f"bold {GOLD}", border_style="dim")
                table.add_column("Name", style="bold white")
                table.add_column("Command", style="dim")
                table.add_column("Status")
                table.add_column("Tools", justify="right")
                for s in statuses:
                    status = "[green]on[/]" if s["enabled"] else "[red]off[/]"
                    if s.get("connected"):
                        status = "[green]connected[/]"
                    if s.get("last_error"):
                        status = "[red]error[/]"
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
            console.print(
                f"[dim]Tools will be discovered on first use. Try: /extension tools {ext_name}[/]"
            )

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
                    p
                    for p in recipe.parameters
                    if p.required and p.key not in params and not p.default
                ]
                if missing:
                    console.print(f"[bold {GOLD}]Recipe: {recipe.name}[/] — {recipe.description}")
                    console.print("[dim]Fill in the required parameters:[/]")
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

    # W5-4 (P0-1). Temporary if/elif entries per the blueprint's collision
    # matrix -- W9 moves them into the registry. Deliberately NOT blocked by
    # plan mode: plan mode stops the model changing the tree, and /undo is the
    # user un-changing it.
    elif command in ("/undo", "/redo", "/rewind"):
        await _handle_undo_family(command, arg, operator)

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
                        operator.messages.append(
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

                    operator.session_id = target_id
                    console.print(
                        f"[green]Resumed session {target_id}[/] "
                        f"({session.model}, {restored} messages)"
                    )
                    console.print(
                        "[dim]Conversation context restored. Continue where you left off.[/]"
                    )

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
        result = await run_interruptible(
            handle_slash_command(
                command,
                operator,
                memory,
                status_bar,
                orchestrator,
            )
        )
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

    # Validate model on startup. validate_model() is a blocking HTTP call; run_repl
    # is a coroutine, so it must not run on the loop (app.py already does this).
    ok, msg = await asyncio.to_thread(llm.validate_model)
    if not ok:
        console.print(f"[red]{msg}[/]")
        console.print("[dim]Use /model to switch or /models to list available models.[/]")
    elif msg:
        console.print(f"[dim]{msg}[/]")

    # Initialize operator with model-aware system prompt.
    # W2: the engine no longer prints. It emits CoreEvents on this bus and the
    # REPL's renderer turns them into terminal output — which is what lets a GUI
    # subscribe to the very same stream.
    effective_auto_accept = auto_accept or cfg.get("auto_accept", False)
    from djcode.core.events import EventBus
    from djcode.frontends.repl.render import render_event

    event_bus = EventBus()
    event_bus.subscribe(render_event)
    operator = Operator(
        llm,
        bypass_rlhf=bypass_rlhf,
        model=llm.config.model,
        auto_accept=effective_auto_accept,
        show_thinking=show_thinking,
        approval_callback=_approve_repl_tool,
        event_bus=event_bus,
    )

    # Initialize memory
    memory = MemoryManager()

    # Initialize orchestrator
    orchestrator = Orchestrator(
        llm, auto_accept=effective_auto_accept, approval_callback=_approve_repl_tool
    )

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
    operator.on_checkpoint = lambda messages: session_db.append_messages(
        operator.session_id, messages
    )
    # W5 (P0-1): hand the chokepoint somewhere to record pre-images. The store
    # reads ctx.session_id at CALL time, never caches it, so `/resume` (which
    # reassigns operator.session_id in place) keeps working.
    try:
        from djcode.core.checkpoints import store_from_config

        operator.dispatch_ctx.checkpoints = store_from_config(session_db, cwd=os.getcwd())
    except Exception:
        logger.warning("checkpoints unavailable; /undo will be inactive", exc_info=True)
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
                    _prompt_html = HTML("<style fg='#C79B7A'><b>\u276f </b></style>")

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
                    sys.stdout.write("\033[33m\u23fa\033[0m \033[2mThinking...\033[0m")
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

                        sys.stdout.write(token)
                        sys.stdout.flush()

                        full_response += token

                        # Periodically update thinking line if we haven't started output yet
                        if first_token and _token_count % 3 == 0:
                            elapsed = _time.monotonic() - _start_time
                            sys.stdout.write(
                                f"\r\033[K\033[33m\u23fa\033[0m \033[2mThinking... ({elapsed:.1f}s "
                                f"\u00b7 \u2193 {_token_count} tokens)\033[0m"
                            )
                            sys.stdout.flush()

                    if first_token:
                        # Never got a token -- clear thinking indicator
                        sys.stdout.write("\r\033[K")

                    # W5: anything the checkpoint store needs the user to know
                    # -- above all "shell commands are NOT being checkpointed"
                    # -- is printed here, at the end of the turn that raised it,
                    # not left for /undo to disclose after the damage.
                    drain_checkpoint_notices(operator)

                    # Show response stats after completion
                    if full_response:
                        _elapsed = _time.monotonic() - _start_time
                        _est_tokens = len(full_response) // 4
                        if _est_tokens >= 1000:
                            _tok_str = f"{_est_tokens / 1000:.1f}k"
                        else:
                            _tok_str = str(_est_tokens)
                        console.print(
                            f"\n  [dim]\u2193 {_tok_str} tokens \u00b7 {_elapsed:.1f}s[/]"
                        )

                    if full_response:
                        memory.add_session_message("assistant", full_response)

                        # Track usage stats (legacy JSON + SQLite)
                        token_est = len(full_response) // 4
                        record_session_update(session_id, tokens=token_est, messages=1)
                        session_db.update_session(
                            operator.session_id,
                            tokens_out=token_est,
                            messages=1,
                        )
                        # Persist conversation for /resume
                        session_db.append_messages(operator.session_id, operator.messages)

                        # Tool extraction router — for models without native tool calling
                        # Censorship detection — warn if aligned model refuses
                        from djcode.prompt import CENSORED_WARNING, detect_refusal

                        if detect_refusal(full_response) and not is_uncensored_model(
                            llm.config.model
                        ):
                            console.print(
                                Panel(
                                    CENSORED_WARNING.format(model=llm.config.model),
                                    title="[yellow]Model Censorship Detected[/]",
                                    border_style="yellow",
                                )
                            )

                    # Update status bar token count (toolbar auto-updates on next prompt)
                    token_est = _estimate_tokens(operator.messages)
                    current_cfg = load_config()
                    status_bar.update(
                        token_count=token_est,
                        auto_accept=current_cfg.get("auto_accept", False),
                    )

                    # Dim separator after each response
                    if full_response:
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
            session_db.append_messages(operator.session_id, operator.messages)

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
            console.print("  [dim]Saved djcode.md[/]")

            record_session_end(session_id)
            session_db.append_messages(operator.session_id, operator.messages)
            session_db.end_session(operator.session_id)
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
    show_thinking: bool = True,
    auto_accept: bool = False,
) -> None:
    """Run one task; preserve failures in the process exit status and always close HTTP."""
    import click

    config = ProviderConfig.from_config(provider_override=provider, model_override=model)
    llm = Provider(config)
    from djcode.sessions import SessionDB

    session_db = SessionDB()
    session_id = session_db.create_session(
        model=config.model, provider=config.name, cwd=os.getcwd()
    )
    operator = None
    try:
        ok, msg = llm.validate_model()
        if not ok:
            raise click.ClickException(msg)
        if msg:
            console.print(msg, markup=False)
        from djcode.core.events import EventBus
        from djcode.frontends.repl.render import render_event

        oneshot_bus = EventBus()
        oneshot_bus.subscribe(render_event)
        operator = Operator(
            llm,
            bypass_rlhf=bypass_rlhf,
            model=llm.config.model,
            show_thinking=show_thinking,
            auto_accept=auto_accept,
            event_bus=oneshot_bus,
        )
        operator.on_checkpoint = lambda messages: session_db.append_messages(session_id, messages)
        # W5: the one-shot path used to leave operator.session_id unset, so
        # every spill file and every checkpoint it produced was filed under the
        # process fallback bucket instead of this session. Graft it here, the
        # same way the interactive path does.
        operator.session_id = session_id
        operator.session_db = session_db
        try:
            from djcode.core.checkpoints import store_from_config

            operator.dispatch_ctx.checkpoints = store_from_config(session_db, cwd=os.getcwd())
        except Exception:
            logger.debug("checkpoints unavailable on the one-shot path", exc_info=True)
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
            session_db.append_messages(session_id, operator.messages)
        session_db.end_session(session_id)
        await llm.close()
