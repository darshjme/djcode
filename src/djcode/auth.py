"""Authentication and provider registry for DJcode.

Manages API providers, keys, and connection settings.
Supports Ollama, OpenAI, Anthropic, NVIDIA NIM, Google AI, Groq, Together AI, OpenRouter, and MLX.
"""

from __future__ import annotations

import os
from typing import Any

import questionary
from rich.console import Console

from djcode.config import load_config, set_value

console = Console()

GOLD = "#FFD700"

# ── Provider Registry ──────────────────────────────────────────────────────

PROVIDERS: dict[str, dict[str, Any]] = {
    "ollama": {
        "name": "Ollama (Local)",
        "needs_key": False,
        "base_url": "http://localhost:11434",
        "description": "Local inference, no API key needed",
    },
    "openai": {
        "name": "OpenAI",
        "needs_key": True,
        "env": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
        "description": "GPT-4o, o1, o3 models",
    },
    "anthropic": {
        "name": "Anthropic",
        "needs_key": True,
        "env": "ANTHROPIC_API_KEY",
        "base_url": "https://api.anthropic.com",
        "description": "Sonnet, Opus, Haiku models",
    },
    "nvidia": {
        "name": "NVIDIA NIM",
        "needs_key": True,
        "env": "NVIDIA_API_KEY",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "description": "DeepSeek, Kimik2, GLM models via NIM",
    },
    "google": {
        "name": "Google AI",
        "needs_key": True,
        "env": "GOOGLE_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "description": "Gemini models",
    },
    "groq": {
        "name": "Groq",
        "needs_key": True,
        "env": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1",
        "description": "Ultra-fast inference",
    },
    "together": {
        "name": "Together AI",
        "needs_key": True,
        "env": "TOGETHER_API_KEY",
        "base_url": "https://api.together.xyz/v1",
        "description": "Open-source model hosting",
    },
    "openrouter": {
        "name": "OpenRouter",
        "needs_key": True,
        "env": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
        "description": "Multi-provider router",
    },
    "mlx": {
        "name": "MLX-LM (Local)",
        "needs_key": False,
        "base_url": "http://localhost:8899",
        "description": "Apple Silicon native inference",
    },
}

# ── Uncensored model detection ─────────────────────────────────────────────

UNCENSORED_KEYWORDS = {"dolphin", "abliterated", "uncensored", "wizard-vicuna", "nous-hermes"}


def is_uncensored_model(model_name: str) -> bool:
    """Check if a model name indicates an uncensored/unfiltered model."""
    name_lower = model_name.lower()
    return any(kw in name_lower for kw in UNCENSORED_KEYWORDS)


# ── API key management ─────────────────────────────────────────────────────


def get_api_key(provider_id: str) -> str:
    """Get API key for a provider from config or environment."""
    prov = PROVIDERS.get(provider_id)
    if not prov or not prov.get("needs_key"):
        return ""

    cfg = load_config()
    env_var = prov.get("env", "")

    # Check config first
    config_key = f"{provider_id}_api_key"
    key = cfg.get(config_key, "")
    if key:
        return key

    # Fall back to environment variable
    if env_var:
        key = os.environ.get(env_var, "")
    return key


def set_api_key(provider_id: str, key: str) -> None:
    """Store an API key in config."""
    config_key = f"{provider_id}_api_key"
    set_value(config_key, key)


def get_base_url(provider_id: str) -> str:
    """Get the base URL for a provider."""
    prov = PROVIDERS.get(provider_id)
    if not prov:
        return "http://localhost:11434"

    cfg = load_config()
    # Check for user-overridden URL first
    url_key = f"{provider_id}_url"
    custom_url = cfg.get(url_key, "")
    if custom_url:
        return custom_url

    return prov["base_url"]


# ── Interactive auth flow ──────────────────────────────────────────────────


def interactive_auth() -> str | None:
    """Interactive provider authentication. Returns provider_id or None."""
    choices = []
    for pid, prov in PROVIDERS.items():
        key_status = ""
        if prov["needs_key"]:
            has_key = bool(get_api_key(pid))
            key_status = " [configured]" if has_key else " [needs key]"
        else:
            key_status = " [local]"

        choices.append(
            questionary.Choice(
                title=f"{prov['name']}{key_status} -- {prov.get('description', '')}",
                value=pid,
            )
        )

    provider_id = questionary.select(
        "Select provider to configure:",
        choices=choices,
        style=questionary.Style([
            ("selected", "fg:#FFD700 bold"),
            ("pointer", "fg:#FFD700 bold"),
            ("highlighted", "fg:#FFD700"),
        ]),
    ).ask()

    if not provider_id:
        return None

    prov = PROVIDERS[provider_id]

    if prov["needs_key"]:
        current_key = get_api_key(provider_id)
        masked = f"***{current_key[-4:]}" if current_key and len(current_key) > 4 else "(none)"
        console.print(f"\n  [dim]Current key: {masked}[/]")
        console.print(f"  [dim]Environment variable: {prov.get('env', 'N/A')}[/]")

        new_key = questionary.password(
            f"Enter API key for {prov['name']} (leave blank to keep current):"
        ).ask()

        if new_key:
            set_api_key(provider_id, new_key)
            console.print(f"  [green]API key saved for {prov['name']}[/]")
        elif not current_key:
            env_var = prov.get("env", "")
            console.print(
                f"  [yellow]No key configured.[/] "
                f"[dim]Set {env_var} or run /auth again.[/]"
            )

    # Set as active provider
    set_value("provider", provider_id)
    console.print(f"\n  [green]Active provider:[/] {prov['name']}")

    return provider_id


def interactive_provider_picker() -> str | None:
    """Quick provider picker (no key entry). Returns provider_id or None."""
    choices = []
    cfg = load_config()
    current = cfg.get("provider", "ollama")

    for pid, prov in PROVIDERS.items():
        marker = " (current)" if pid == current else ""
        ready = ""
        if prov["needs_key"]:
            has_key = bool(get_api_key(pid))
            ready = " [ready]" if has_key else " [no key]"
        else:
            ready = " [local]"

        choices.append(
            questionary.Choice(
                title=f"{prov['name']}{marker}{ready}",
                value=pid,
            )
        )

    provider_id = questionary.select(
        "Switch provider:",
        choices=choices,
        style=questionary.Style([
            ("selected", "fg:#FFD700 bold"),
            ("pointer", "fg:#FFD700 bold"),
            ("highlighted", "fg:#FFD700"),
        ]),
    ).ask()

    return provider_id
