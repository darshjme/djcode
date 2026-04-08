"""DJcode CLI — the main entry point.

Usage:
    djcode                         Interactive REPL
    djcode "write a function"     One-shot mode
    djcode --provider mlx          Use MLX backend
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
    type=click.Choice([
        "ollama", "openai", "anthropic", "nvidia", "google",
        "groq", "together", "openrouter", "mlx", "remote",
    ]),
    default=None,
    help="LLM provider (default: ollama)",
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
    "--raw",
    is_flag=True,
    default=False,
    help="Raw output — no Rich formatting",
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
    "--tui",
    is_flag=True,
    default=False,
    help="Launch Textual TUI (split-pane interface)",
)
@click.option(
    "--config",
    "show_config",
    is_flag=True,
    default=False,
    help="Show current configuration",
)
@click.version_option(version=__version__, prog_name="djcode")
def main(
    prompt: str | None,
    provider: str | None,
    model: str | None,
    bypass_rlhf: bool,
    raw: bool,
    auto_accept: bool,
    thinking: bool,
    tui: bool,
    show_config: bool,
) -> None:
    """DJcode — Local-first AI coding CLI by DarshJ.AI

    Run without arguments for interactive REPL, or pass a prompt for one-shot mode.
    """
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

    # TUI mode — launch Textual split-pane interface
    if tui:
        from djcode.app import run_tui

        run_tui(
            provider=provider,
            model=model,
            bypass_rlhf=bypass_rlhf,
            auto_accept=auto_accept,
            show_thinking=thinking,
        )
        return

    from djcode.repl import run_oneshot, run_repl

    try:
        if prompt:
            # One-shot mode
            asyncio.run(
                run_oneshot(
                    prompt,
                    provider=provider,
                    model=model,
                    bypass_rlhf=bypass_rlhf,
                    raw=raw,
                    show_thinking=thinking,
                )
            )
        else:
            # Interactive REPL
            asyncio.run(
                run_repl(
                    provider=provider,
                    model=model,
                    bypass_rlhf=bypass_rlhf,
                    raw=raw,
                    auto_accept=auto_accept,
                    show_thinking=thinking,
                )
            )
    except KeyboardInterrupt:
        console.print("\n[dim]Goodbye.[/]")
        sys.exit(0)


if __name__ == "__main__":
    main()
