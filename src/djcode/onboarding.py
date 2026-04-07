"""First-run onboarding wizard for DJcode.

Interactive setup that runs when no ~/.djcode/config.json exists.
Detects available models, lets user pick provider and model.
"""

from __future__ import annotations

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from djcode.config import CONFIG_FILE, DEFAULT_CONFIG, ensure_dirs, save_config

console = Console()

GOLD = "#FFD700"


def needs_onboarding() -> bool:
    """Check if first-run onboarding is needed."""
    return not CONFIG_FILE.exists()


def _fetch_ollama_models(base_url: str = "http://localhost:11434") -> list[dict]:
    """Fetch available models from Ollama. Returns list of model info dicts."""
    try:
        resp = httpx.get(f"{base_url}/api/tags", timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
        models = data.get("models", [])
        return [
            {
                "name": m.get("name", "unknown"),
                "size": m.get("size", 0),
            }
            for m in models
        ]
    except Exception:
        return []


def _format_size(size_bytes: int) -> str:
    """Format bytes to human readable."""
    if size_bytes <= 0:
        return "unknown"
    gb = size_bytes / (1024 ** 3)
    if gb >= 1.0:
        return f"{gb:.1f} GB"
    mb = size_bytes / (1024 ** 2)
    return f"{mb:.0f} MB"


def run_onboarding() -> dict:
    """Run the interactive first-run wizard. Returns the final config dict."""
    ensure_dirs()

    console.print()
    console.print(
        Panel(
            f"[bold {GOLD}]Welcome to DJcode![/]\n\n"
            "[white]Let's get you set up. This only takes a moment.[/]\n"
            "[dim]All settings are saved locally to ~/.djcode/config.json[/]",
            border_style=GOLD,
            padding=(1, 3),
        )
    )
    console.print()

    config = dict(DEFAULT_CONFIG)

    # --- Provider selection ---
    console.print(f"[bold {GOLD}]? Choose your provider:[/]")
    console.print("  [white]1)[/] Ollama [dim](local, recommended)[/]")
    console.print("  [white]2)[/] MLX [dim](Apple Silicon native)[/]")
    console.print("  [white]3)[/] Remote API [dim](OpenAI-compatible)[/]")
    console.print()

    provider_choice = Prompt.ask(
        f"[{GOLD}]Select[/]",
        choices=["1", "2", "3"],
        default="1",
    )

    provider_map = {"1": "ollama", "2": "mlx", "3": "remote"}
    config["provider"] = provider_map[provider_choice]

    # --- Provider-specific setup ---
    if config["provider"] == "remote":
        console.print()
        remote_url = Prompt.ask(
            f"[{GOLD}]API base URL[/]",
            default="https://api.openai.com",
        )
        config["remote_url"] = remote_url

        api_key = Prompt.ask(f"[{GOLD}]API key[/]", password=True)
        config["remote_api_key"] = api_key

        model_name = Prompt.ask(
            f"[{GOLD}]Model name[/]",
            default="gpt-4o-mini",
        )
        config["model"] = model_name

    elif config["provider"] == "mlx":
        console.print()
        mlx_url = Prompt.ask(
            f"[{GOLD}]MLX server URL[/]",
            default="http://localhost:8080",
        )
        config["mlx_url"] = mlx_url

        model_name = Prompt.ask(
            f"[{GOLD}]Model name[/]",
            default="mlx-community/gemma-2-2b-it-4bit",
        )
        config["model"] = model_name

    else:
        # Ollama — auto-detect models
        console.print()
        base_url = config.get("ollama_url", "http://localhost:11434")
        console.print(f"[dim]Checking Ollama at {base_url}...[/]")

        models = _fetch_ollama_models(base_url)

        if not models:
            console.print(
                f"[yellow]Could not reach Ollama.[/] "
                f"[dim]Make sure it's running: ollama serve[/]"
            )
            console.print()
            model_name = Prompt.ask(
                f"[{GOLD}]Model name[/]",
                default="gemma4",
            )
            config["model"] = model_name
        else:
            console.print(f"[green]Found {len(models)} model(s)[/]")
            console.print()

            table = Table(
                border_style=GOLD,
                show_header=True,
                header_style=f"bold {GOLD}",
            )
            table.add_column("#", style="white", width=4)
            table.add_column("Model", style="white")
            table.add_column("Size", style="dim")

            for i, m in enumerate(models, 1):
                table.add_row(str(i), m["name"], _format_size(m["size"]))

            console.print(table)
            console.print()

            choice = Prompt.ask(
                f"[{GOLD}]Select model number[/]",
                default="1",
            )
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(models):
                    config["model"] = models[idx]["name"]
                else:
                    config["model"] = models[0]["name"]
            except ValueError:
                # User typed a model name directly
                config["model"] = choice

    # --- Auto-accept ---
    console.print()
    auto_accept = Confirm.ask(
        f"[{GOLD}]Enable auto-accept tool calls?[/]",
        default=False,
    )
    config["auto_accept"] = auto_accept

    # --- Save ---
    save_config(config)
    console.print()
    console.print(f"[green bold]Config saved to ~/.djcode/config.json[/]")
    console.print()

    return config
