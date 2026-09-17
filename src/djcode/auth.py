"""Authentication and provider registry for DJcode.

Manages API providers, keys, and connection settings.
Supports Ollama, OpenAI, Anthropic, NVIDIA NIM, Google AI, Groq, Together AI, OpenRouter, and MLX.
"""

from __future__ import annotations

import questionary
from rich.console import Console

from djcode.config import load_config, set_value

console = Console()

GOLD = "#FFD700"

# ── Provider registry and its pure helpers ─────────────────────────

# W8 moved ``PROVIDERS`` itself to ``djcode.core.onboarding_flow``: it is pure
# data, and this module -- which owns the interactive pickers and imports
# questionary and rich at module level -- is one ``djcode.core`` may never
# import.
#
# W10 moved the other four for the same reason plus one W8 missed.
# ``ProviderConfig.from_config`` reached in here for ``get_api_key`` /
# ``get_base_url`` and ``prompt.build_system_prompt`` for
# ``is_uncensored_model``, both at CALL time -- so importing ``djcode.core``
# stayed clean while RUNNING a single turn imported this module and, through its
# ``import questionary`` above, loaded ``prompt_toolkit`` into a process that may
# have no terminal at all. ``tests/test_core_contract.py`` measures the running
# process rather than the import graph, which is how that was finally caught.
#
# All five are re-exported here, so ``from djcode.auth import get_api_key`` and
# every ``monkeypatch.setattr(auth, "get_api_key", ...)`` keep working: these
# ARE this module's globals, which is what the pickers below resolve against.
from djcode.core.onboarding_flow import (  # noqa: E402, F401
    PROVIDERS,
    UNCENSORED_KEYWORDS,
    get_api_key,
    get_base_url,
    is_uncensored_model,
)


def set_api_key(provider_id: str, key: str) -> None:
    """Store an API key in config."""
    config_key = f"{provider_id}_api_key"
    set_value(config_key, key)


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
