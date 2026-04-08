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
) -> None:
    """DJcode — Local-first AI coding CLI by DarshJ.AI

    Run without arguments for the interactive TUI, or pass a prompt for one-shot mode.
    """
    # --url / -u takes precedence; --provider with an http value also works
    if url:
        provider = url
    if provider and provider.startswith("http"):
        # Stash the raw URL so downstream can use it as a custom endpoint
        import os
        os.environ["DJCODE_CUSTOM_URL"] = provider
        provider = "custom"

    # Validate named providers (skip validation for "custom" — already resolved)
    _known_providers = {
        "ollama", "openai", "anthropic", "nvidia", "google",
        "groq", "together", "openrouter", "mlx", "remote", "custom",
    }
    if provider and provider not in _known_providers:
        console.print(
            f"[red]Unknown provider:[/] {provider}\n"
            f"[dim]Valid providers: {', '.join(sorted(_known_providers))}[/]"
        )
        sys.exit(1)

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
        if prompt:
            # One-shot mode
            from djcode.repl import run_oneshot

            asyncio.run(
                run_oneshot(
                    prompt,
                    provider=provider,
                    model=model,
                    bypass_rlhf=bypass_rlhf,
                    show_thinking=thinking,
                )
            )
        else:
            # Default: Textual TUI
            from djcode.app import run_tui

            run_tui(
                provider=provider,
                model=model,
                bypass_rlhf=bypass_rlhf,
                auto_accept=auto_accept,
                show_thinking=thinking,
            )
    except KeyboardInterrupt:
        console.print("\n[dim]Goodbye.[/]")
        sys.exit(0)


if __name__ == "__main__":
    main()
