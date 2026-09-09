"""DJcode CLI — the main entry point.

Usage:
    djcode                         Interactive TUI
    djcode "write a function"     One-shot mode
    djcode --provider mlx          Use MLX backend
    djcode --provider https://…    Custom OpenAI-compatible endpoint
    djcode -u https://…            Shorthand for custom URL provider
    djcode --model gemma4          Specific model
    djcode --bypass-rlhf           Unrestricted mode
    djcode --version               Show version
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import click
from rich.console import Console

from djcode import __version__

console = Console()


def redact_config(value, name=""):
    if any(word in name.lower() for word in ("key", "token", "secret", "password")):
        return "***" if value else value
    if isinstance(value, dict):
        return {key: redact_config(item, key) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_config(item) for item in value]
    return value


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("prompt", required=False, default=None)
@click.option(
    "--provider",
    "-p",
    default=None,
    help="LLM provider name or OpenAI-compatible URL (default: ollama)",
)
@click.option(
    "--url",
    "-u",
    default=None,
    help="Custom OpenAI-compatible API base URL (shorthand for --provider <url>)",
)
@click.option(
    "--model",
    "-m",
    default=None,
    help="Model name (default: gemma4)",
)
@click.option(
    "--bypass-rlhf",
    is_flag=True,
    default=False,
    help="Enable unrestricted mode",
)
@click.option(
    "--auto-accept",
    is_flag=True,
    default=False,
    help="Auto-accept all tool calls without confirmation",
)
@click.option(
    "--thinking/--no-thinking",
    default=True,
    help="Show model thinking process (verbose reasoning)",
)
@click.option(
    "--config",
    "show_config",
    is_flag=True,
    default=False,
    help="Show current configuration",
)
@click.option(
    "--army",
    is_flag=True,
    default=False,
    help="Launch with army panel visible (18-agent overview)",
)
@click.option(
    "--wave",
    type=str,
    default=None,
    help="Run a task with wave execution strategy then exit",
)
@click.option("--repl", "use_repl", is_flag=True, help="Use the line-oriented REPL instead of the full-screen TUI.")
@click.option("--design-packs", is_flag=True, help="List seven bundled original design references (offline).")
@click.option("--design-pack", type=str, help="Print a design reference, or add it to the supplied prompt.")
@click.option("--design-export", type=click.Path(path_type=Path), help="Export the selected design reference and original SVG to a new directory.")
@click.option("--revision", is_flag=True, help="Show the installed version and managed build revision without network access.")
@click.option("--setup", is_flag=True, help="Choose provider, supported authentication method and model.")
@click.option("--check", "check_install", is_flag=True, help="Check installation syntax, registries and fatal lint.")
@click.option("--lint", is_flag=True, help="Run the installation quality checks (alias for --check).")
@click.option("--update", is_flag=True, help="Install the latest CI-validated canonical build into a managed install.")
@click.option("--rollback", is_flag=True, help="Restore the previous managed build and switch updates to manual.")
@click.option("--update-mode", type=click.Choice(["auto", "manual", "disabled"]), help="Set automatic, manual or disabled updates.")
@click.option("--no-update", is_flag=True, help="Skip update checks for this invocation.")
@click.version_option(version=__version__, prog_name="djcode")
def main(
    prompt: str | None,
    provider: str | None,
    url: str | None,
    model: str | None,
    bypass_rlhf: bool,
    auto_accept: bool,
    thinking: bool,
    show_config: bool,
    army: bool,
    wave: str | None,
    use_repl: bool,
    setup: bool,
    revision: bool,
    design_packs: bool,
    design_pack: str | None,
    design_export,
    check_install: bool,
    lint: bool,
    update: bool,
    rollback: bool,
    update_mode: str | None,
    no_update: bool,
) -> None:
    """DJcode — Local-first AI coding CLI by DarshJ.AI

    Run without arguments for the interactive TUI, or pass a prompt for one-shot mode.
    """
    if design_packs:
        from djcode.design_packs import list_packs
        for pack in list_packs():
            console.print(f"{pack['id']}: {pack['title']} · {pack['summary']}", markup=False)
        console.print("Use --design-pack ID to read, or --design-pack ID 'your task' to apply. No design-service account required.", markup=False)
        return
    if design_export and not design_pack:
        raise click.UsageError("--design-export requires --design-pack ID")
    if design_pack:
        from djcode.design_packs import get_pack, get_example, get_license
        try:
            reference = get_pack(design_pack)
            if design_export:
                example = get_example(design_pack)
                license_text = get_license()
                design_export.mkdir(parents=True, exist_ok=False)
                (design_export / f"{design_pack}.md").write_text(reference)
                (design_export / f"{design_pack}.svg").write_text(example)
                (design_export / "LICENSE").write_text(license_text)
                console.print(f"Exported original design reference to {design_export.resolve()}", markup=False)
                return
        except (ValueError, OSError) as error:
            raise click.ClickException(str(error)) from error
        if not prompt:
            console.print(reference, markup=False, highlight=False)
            return
        prompt = f"{prompt}\n\nOptional original design reference (adapt to this task):\n{reference}"
    if revision:
        from djcode.managed_update import installation
        managed = installation()
        if managed:
            _, receipt = managed
            console.print(f"DJcode {__version__} · build {receipt.get('commit', 'unknown')}", markup=False)
        else:
            console.print(f"DJcode {__version__} · unmanaged installation", markup=False)
        return
    # --url / -u takes precedence; --provider with an http value also works
    if url:
        provider = url
    if no_update:
        os.environ["DJCODE_NO_UPDATE_CHECK"] = "1"
    if check_install or lint:
        from djcode.maintenance import run_checks
        checked = run_checks()
        for item in checked["checks"]:
            console.print(f"{item['name']}: {item['status']} · {item['detail']}", markup=False)
        if not checked["ok"]:
            raise click.ClickException(checked["summary"])
        return
    if update_mode:
        from djcode.config import set_value
        set_value("update_mode", update_mode)
        console.print(f"Update mode: {update_mode}", markup=False)
        return
    if update or rollback:
        from djcode.managed_update import perform_update, rollback as restore_previous
        result = restore_previous() if rollback else perform_update(force=True)
        console.print(result["message"], markup=False)
        if not result["ok"]:
            raise click.ClickException("The requested maintenance operation did not complete.")
        return
    from djcode.auth import PROVIDERS
    from djcode.config import load_config
    known = set(PROVIDERS) | {"remote"} | set(load_config().get("custom_providers", {}))
    if provider and not provider.startswith(("https://", "http://")) and provider not in known:
        raise click.BadParameter(f"Unknown provider: {provider}", param_hint="--provider")

    if show_config:
        from djcode.config import load_config
        from rich.table import Table

        cfg = load_config()
        table = Table(title="DJcode Configuration", border_style="blue")
        table.add_column("Key", style="cyan")
        table.add_column("Value", style="white")
        for k, v in sorted(cfg.items()):
            display = str(redact_config(v, k))
            table.add_row(k, display)
        console.print(table)
        return

    try:
        from djcode.startup import prepare
        if not setup and not os.environ.get("DJCODE_UPDATE_REEXEC"):
            from djcode.updater import perform_update
            status_console = Console(stderr=True)
            with status_console.status("Checking validated DJcode updates…", spinner="dots"):
                changed = perform_update(force=False)
            if changed["status"] not in {"disabled", "manual_required", "manual"}:
                status_console.print(changed["message"], markup=False)
            if changed.get("updated") and changed.get("entrypoint"):
                env = {**os.environ, "DJCODE_UPDATE_REEXEC": changed["commit"]}
                os.execve(changed["entrypoint"], [changed["entrypoint"], *sys.argv[1:]], env)
        provider, model = prepare(provider, model, force_setup=setup)
        if setup:
            return
        if wave:
            # Wave execution mode: run a task with multi-agent wave strategy
            from djcode.provider import Provider, ProviderConfig
            from djcode.orchestrator import Orchestrator
            from djcode.orchestrator.events import EventType

            config = ProviderConfig.from_config(
                provider_override=provider,
                model_override=model,
            )
            async def _run_wave() -> None:
                from djcode.orchestrator.engine import ExecutionStrategy
                from djcode.tools.agent_spawn import cancel_background_agents
                prov = Provider(config)
                try:
                    valid, message = prov.validate_model()
                    if not valid:
                        raise click.ClickException(message)
                    orch = Orchestrator(prov, auto_accept=auto_accept)
                    console.print(f"[bold #FFD700]Wave execution:[/] {wave}")
                    completed = False
                    async for event in orch._shadow.execute(wave, strategy_override=ExecutionStrategy.WAVE):
                        if event.event_type == EventType.AGENT_TOKEN:
                            token = event.data.get("token", "")
                            if token:
                                console.print(token, end="", markup=False)
                        elif event.event_type in (EventType.AGENT_ERROR, EventType.ORCHESTRATOR_ERROR):
                            raise click.ClickException(event.data.get("error", "Wave execution failed"))
                        elif event.event_type == EventType.WAVE_START:
                            console.print(f"\n[#FFD700]Wave {event.data.get('wave', '?')} starting...[/]")
                        elif event.event_type == EventType.WAVE_COMPLETE:
                            console.print(f"\n[green]Wave {event.data.get('wave', '?')} complete.[/]")
                        elif event.event_type == EventType.ORCHESTRATOR_COMPLETE:
                            completed = True
                    if not completed:
                        raise click.ClickException("Wave execution ended without completion")
                    console.print("\n[green]Wave execution finished.[/]")
                finally:
                    await cancel_background_agents()
                    await prov.close()
            asyncio.run(_run_wave())
        elif prompt:
            # One-shot mode
            from djcode.repl import run_oneshot

            asyncio.run(
                run_oneshot(
                    prompt,
                    provider=provider,
                    model=model,
                    bypass_rlhf=bypass_rlhf,
                    show_thinking=thinking,
                    auto_accept=auto_accept,
                )
            )
        elif use_repl:
            from djcode.repl import run_repl
            asyncio.run(run_repl(provider=provider, model=model, bypass_rlhf=bypass_rlhf,
                                 auto_accept=auto_accept, show_thinking=thinking))
        else:
            # Default: Textual TUI
            from djcode.app import run_tui

            run_tui(
                provider=provider,
                model=model,
                bypass_rlhf=bypass_rlhf,
                auto_accept=auto_accept,
                show_thinking=thinking,
                army=army,
            )
    except KeyboardInterrupt:
        console.print("\n[dim]Goodbye.[/]")
        sys.exit(130)


if __name__ == "__main__":
    main()
