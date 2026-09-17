"""Authentication and provider registry for DJcode.

Manages API providers, keys, and connection settings.
Supports Ollama, OpenAI, Anthropic, NVIDIA NIM, Google AI, Groq, Together AI, OpenRouter, and MLX.
"""

from __future__ import annotations

import os

import questionary
from rich.console import Console

from djcode.config import load_config, set_value

console = Console()

GOLD = "#FFD700"

# ── Provider Registry ──────────────────────────────────────────────────────

# W8: the registry itself moved to ``djcode.core.onboarding_flow``. It is pure
# data, and this module -- which owns the interactive pickers and imports
# questionary and rich at module level -- is one ``djcode.core`` may never
# import. Re-exported here so every existing ``from djcode.auth import
# PROVIDERS`` keeps working unchanged.
from djcode.core.onboarding_flow import PROVIDERS  # noqa: E402

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
    if method == "browser" and provider_id == "openrouter":
        import asyncio
        import webbrowser

        from djcode.openrouter_auth import begin, exchange

        verifier, url = begin()
        console.print(url, markup=False)
        webbrowser.open(url)
        code = questionary.password("One-time code from OpenRouter:").ask()
        if code is None:
            return None
        cfg[f"{provider_id}_api_key"] = asyncio.run(exchange(code, verifier))
        method = "api_key"
    elif method == "account":
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
