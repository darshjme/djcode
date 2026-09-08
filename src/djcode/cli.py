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
import sys

import click
from rich.console import Console

from djcode import __version__

console = Console()


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
) -> None:
    """DJcode — Local-first AI coding CLI by DarshJ.AI

    Run without arguments for the interactive TUI, or pass a prompt for one-shot mode.
    """
    # --url / -u takes precedence; --provider with an http value also works
    if url:
        provider = url
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
            display = "***" if "key" in k.lower() and v else str(v)
            table.add_row(k, display)
        console.print(table)
        return

    try:
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
