"""First-run onboarding wizard for DJcode.

Interactive setup that runs when no ~/.djcode/config.json exists.
Uses questionary for beautiful arrow-key selection.
Detects available models, lets user pick provider and model.
"""

from __future__ import annotations

import httpx
import questionary
from rich.console import Console
from rich.panel import Panel

from djcode.auth import PROVIDERS, is_uncensored_model
from djcode.config import CONFIG_FILE, DEFAULT_CONFIG, ensure_dirs, save_config

console = Console()

GOLD = "#FFD700"

Q_STYLE = questionary.Style([
    ("selected", "fg:#FFD700 bold"),
    ("pointer", "fg:#FFD700 bold"),
    ("highlighted", "fg:#FFD700"),
    ("question", "fg:#FFD700 bold"),
    ("answer", "fg:#FFFFFF bold"),
])


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

    # --- Provider selection (interactive arrow keys) ---
    provider_choices = [
        questionary.Choice("Ollama (local, recommended)", value="ollama"),
        questionary.Choice("OpenAI", value="openai"),
        questionary.Choice("Anthropic", value="anthropic"),
        questionary.Choice("NVIDIA NIM", value="nvidia"),
        questionary.Choice("Google AI", value="google"),
        questionary.Choice("Groq (fast)", value="groq"),
        questionary.Choice("Together AI", value="together"),
        questionary.Choice("OpenRouter (multi-provider)", value="openrouter"),
        questionary.Choice("MLX-LM (Apple Silicon)", value="mlx"),
    ]

    provider_choice = questionary.select(
        "Choose your provider:",
        choices=provider_choices,
        style=Q_STYLE,
    ).ask()

    if not provider_choice:
        provider_choice = "ollama"

    config["provider"] = provider_choice

    # --- Provider-specific setup ---
    prov_info = PROVIDERS.get(provider_choice, {})

    if prov_info.get("needs_key"):
        # Cloud provider — needs API key
        console.print()
        env_var = prov_info.get("env", "")
        console.print(f"  [dim]You can also set the {env_var} environment variable.[/]")

        api_key = questionary.password(
            f"Enter API key for {prov_info['name']}:",
            style=Q_STYLE,
        ).ask()

        if api_key:
            config[f"{provider_choice}_api_key"] = api_key

        # Set base URL
        config[f"{provider_choice}_url"] = prov_info["base_url"]

        model_name = questionary.text(
            "Model name:",
            default=_default_model_for_provider(provider_choice),
            style=Q_STYLE,
        ).ask()
        config["model"] = model_name or _default_model_for_provider(provider_choice)

    elif provider_choice == "mlx":
        console.print()
        mlx_url = questionary.text(
            "MLX server URL:",
            default="http://localhost:8899",
            style=Q_STYLE,
        ).ask()
        config["mlx_url"] = mlx_url or "http://localhost:8899"

        model_name = questionary.text(
            "Model name:",
            default="mlx-community/gemma-2-2b-it-4bit",
            style=Q_STYLE,
        ).ask()
        config["model"] = model_name or "mlx-community/gemma-2-2b-it-4bit"

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
            model_name = questionary.text(
                "Model name:",
                default="gemma4",
                style=Q_STYLE,
            ).ask()
            config["model"] = model_name or "gemma4"
        else:
            console.print(f"[green]Found {len(models)} model(s)[/]")
            console.print()

            model_choices = []
            for m in models:
                name = m["name"]
                size = _format_size(m["size"])
                label = f"{name}  ({size})"
                if is_uncensored_model(name):
                    label += " \U0001f513 uncensored"
                model_choices.append(questionary.Choice(label, value=name))

            selected_model = questionary.select(
                "Select model:",
                choices=model_choices,
                style=Q_STYLE,
            ).ask()

            config["model"] = selected_model or models[0]["name"]

    # --- Auto-accept ---
    console.print()
    auto_accept = questionary.confirm(
        "Enable auto-accept tool calls?",
        default=False,
        style=Q_STYLE,
    ).ask()
    config["auto_accept"] = bool(auto_accept)

    # --- Save ---
    save_config(config)
    console.print()
    console.print(f"[green bold]Config saved to ~/.djcode/config.json[/]")
    console.print()

    return config


def _default_model_for_provider(provider_id: str) -> str:
    """Return a sensible default model name for each provider."""
    defaults = {
        "openai": "gpt-4o-mini",
        "anthropic": "claude-sonnet-4-20250514",
        "nvidia": "deepseek-ai/deepseek-coder-6.7b-instruct",
        "google": "gemini-2.0-flash",
        "groq": "llama-3.3-70b-versatile",
        "together": "meta-llama/Llama-3-70b-chat-hf",
        "openrouter": "meta-llama/llama-3-8b-instruct",
        "ollama": "gemma4",
        "mlx": "mlx-community/gemma-2-2b-it-4bit",
    }
    return defaults.get(provider_id, "gemma4")
