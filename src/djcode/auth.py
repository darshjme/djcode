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
    "xai": {
        "name": "xAI (Grok)",
        "needs_key": True,
        "env": "XAI_API_KEY",
        "base_url": "https://api.x.ai/v1",
        "description": "Grok via xAI API; distinct from Groq",
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
    "colibri": {
        "name": "Colibri (Local)",
        "needs_key": False,
        "optional_key": True,
        "env": "COLI_API_KEY",
        "base_url": "http://127.0.0.1:8000/v1",
        "description": "Opt-in existing Colibri server; no model downloads",
    },
    "featherless": {
        "name": "Featherless AI",
        "needs_key": True,
        "env": "FEATHERLESS_API_KEY",
        "base_url": "https://api.featherless.ai/v1",
        "description": "Hosted open models via OpenAI-compatible API",
    },
    "custom": {
        "name": "Custom (OpenAI-compatible)",
        "needs_key": True,
        "env": "DJCODE_API_KEY",
        "base_url": "",
        "description": "Any OpenAI-compatible endpoint",
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
    if not prov or not (prov.get("needs_key") or prov.get("optional_key")):
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
    """Select an auth method; cancellation leaves the current configuration intact."""
    from copy import deepcopy

    from djcode.account_auth import auth_methods, authenticate_account, has_account
    from djcode.config import save_config

    cfg = deepcopy(load_config())
    choices = []
    for pid, prov in PROVIDERS.items():
        account = cfg.get(f"{pid}_auth_method") == "account" and has_account(pid)
        status = (
            "account connected"
            if account
            else ("key configured" if get_api_key(pid) else "needs setup")
            if prov["needs_key"]
            else "local"
        )
        choices.append(questionary.Choice(f"{prov['name']} [{status}]", value=pid))
    provider_id = questionary.select("Select provider to configure:", choices=choices).ask()
    if not provider_id:
        return None
    prov = PROVIDERS[provider_id]
    method = "api_key"
    if prov["needs_key"]:
        methods = auth_methods(provider_id)
        available = [item for item in methods if item["available"]]
        for item in methods:
            if not item["available"]:
                console.print(f"{item['label']}: {item['reason']}", markup=False)
        choices = [questionary.Choice(item["label"], value=item["id"]) for item in available]
        current = cfg.get(f"{provider_id}_auth_method", "api_key")
        default = current if current in {item["id"] for item in available} else "api_key"
        method = questionary.select(
            "Authentication method:", choices=choices, default=default
        ).ask()
        if not method:
            return None
    if method == "account":
        if not has_account(provider_id) and not authenticate_account(
            provider_id, method, on_status=lambda text: console.print(text, markup=False)
        ):
            return None
    elif prov["needs_key"] or prov.get("optional_key"):
        current_key = get_api_key(provider_id)
        new_key = questionary.password(
            f"API key for {prov['name']} (leave blank to keep existing/environment key):"
        ).ask()
        if new_key is None:
            return None
        if new_key.strip():
            cfg[f"{provider_id}_api_key"] = new_key.strip()
        elif not current_key and prov["needs_key"]:
            console.print("No key configured; existing provider retained.", markup=False)
            return None
    cfg[f"{provider_id}_auth_method"] = method
    if provider_id == "colibri" and cfg.get("provider") != "colibri":
        cfg["model"] = "djcode-colibri"
    cfg["provider"] = provider_id
    save_config(cfg)
    console.print(f"Active provider: {prov['name']}", markup=False)
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
            from djcode.account_auth import has_account

            account = cfg.get(f"{pid}_auth_method") == "account" and has_account(pid)
            has_key = bool(get_api_key(pid))
            ready = " [account]" if account else " [ready]" if has_key else " [needs setup]"
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
        style=questionary.Style(
            [
                ("selected", "fg:#FFD700 bold"),
                ("pointer", "fg:#FFD700 bold"),
                ("highlighted", "fg:#FFD700"),
            ]
        ),
    ).ask()

    return provider_id
