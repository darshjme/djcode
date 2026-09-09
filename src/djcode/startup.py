"""Bounded provider discovery and an explicit, configuration-preserving setup flow."""
from __future__ import annotations

from copy import deepcopy
import os
import time
import sys

import click
import httpx
import questionary
from rich.console import Console

from djcode.auth import PROVIDERS
from djcode.config import CONFIG_FILE, load_config, save_config

console = Console(stderr=True)


def connection(config: dict, provider: str | None = None, model: str | None = None) -> dict:
    selected = provider or config.get("provider", "ollama")
    info = PROVIDERS.get(selected, {})
    custom = config.get("custom_providers", {}).get(selected, {})
    url_provider = selected.startswith(("http://", "https://"))
    base = (selected if url_provider else custom.get("base_url") or config.get(f"{selected}_url") or info.get("base_url", ""))
    base = os.environ.get("DJCODE_BASE_URL") or config.get("base_url") or base
    key = custom.get("api_key") or config.get(f"{selected}_api_key") or os.environ.get(info.get("env", ""), "")
    if url_provider or selected in {"custom", "remote"}:
        key = key or os.environ.get("DJCODE_API_KEY") or os.environ.get("OPENAI_API_KEY") or config.get("remote_api_key", "")
    method = config.get(f"{selected}_auth_method", "api_key")
    selected_model = model or custom.get("model") or config.get("model", "")
    if selected == "colibri":
        selected_model = model or (config.get("model") if config.get("provider") == "colibri" else None) or "djcode-colibri"
    return {"provider": selected, "base": (base or "").rstrip("/"), "key": key or "",
            "model": selected_model, "method": method, "needs_key": bool(info.get("needs_key"))}


def discover(endpoint: str, headers: dict) -> httpx.Response:
    """Cap streaming discovery time and size, including slow response bodies."""
    deadline = time.monotonic() + 5
    chunks = []
    size = 0
    with httpx.stream("GET", endpoint, headers=headers, timeout=2.0) as response:
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > 2 * 1024 * 1024 or time.monotonic() > deadline:
                raise ValueError("Provider discovery exceeded its budget")
            chunks.append(chunk)
        return httpx.Response(response.status_code, content=b"".join(chunks), request=response.request)


def probe(config: dict, provider: str | None = None, model: str | None = None) -> dict:
    details = connection(config, provider, model)
    name, base, key = details["provider"], details["base"], details["key"]
    def outcome(status, message, models=None):
        return {"status": status, "message": message, "models": models or [], "provider": name}
    if not base.startswith(("http://", "https://")):
        return outcome("missing", "Choose a provider endpoint.")
    if details["method"] == "account":
        from djcode.account_auth import has_account, get_account_token, AccountAuthError
        if name != "xai" or base != "https://api.x.ai/v1":
            return outcome("missing", "Account authentication requires the supported provider endpoint.")
        if not has_account(name):
            return outcome("missing", "Account sign-in is required.")
        try:
            key = get_account_token(name)
        except AccountAuthError:
            return outcome("offline", "Account refresh unavailable; saved setup retained.")
    elif details["needs_key"] and not key:
        return outcome("missing", "An API key or supported account sign-in is required.")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    if name == "ollama":
        endpoint = base + "/api/tags"
    elif name == "anthropic":
        endpoint = base + ("/models" if base.endswith("/v1") else "/v1/models")
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    elif name == "google":
        endpoint = base + "/models"
        headers = {"x-goog-api-key": key}
    else:
        endpoint = base + ("/models" if base.endswith("/v1") else "/v1/models")
    try:
        response = discover(endpoint, headers)
        if response.status_code in {401, 403}:
            return outcome("missing", "The provider rejected authentication; choose or reconnect an account.")
        if response.status_code in {404, 405, 501}:
            if not details["model"]:
                return outcome("missing", "Select an explicit model ID.")
            return outcome("unverified", "This endpoint does not expose model discovery; the explicit model will be checked during use.")
        response.raise_for_status()
        payload = response.json()
        items = payload.get("models" if name in {"ollama", "google"} else "data", [])
        models = [item.get("name" if name in {"ollama", "google"} else "id", "") for item in items if isinstance(item, dict)]
        models = sorted({value.removeprefix("models/") if name == "google" else value for value in models if isinstance(value, str) and value})
        selected = details["model"]
        matched = selected in models or (name == "ollama" and selected + ":latest" in models)
        if not selected or not matched:
            return outcome("missing", "Select a model available at this provider.", models)
        return outcome("ready", f"Connected to {name} · {selected}", models)
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        if not details["model"]:
            return outcome("missing", "Select an explicit model ID.")
        return outcome("offline", "Provider check unavailable; existing configuration retained.")


def answer(value):
    if value is None:
        raise KeyboardInterrupt("Setup cancelled; existing configuration retained")
    return value


def setup(existing: dict | None = None) -> dict:
    """Commit only after selection/validation; cancellation preserves old config."""
    from djcode.account_auth import auth_methods, authenticate_account, has_account
    config = deepcopy(existing or load_config())
    console.print("\n[bold]DJcode setup[/] · project by Darshan Kumar Joshi")
    console.print("[dim]Choose a provider, authentication method and model. No model downloads.[/]")
    selected = answer(questionary.select("Provider", choices=[questionary.Choice(item["name"], value=name) for name, item in PROVIDERS.items()]).ask())
    info = PROVIDERS[selected]
    if selected != config.get("provider"):
        config["base_url"] = ""
        config["model"] = "djcode-colibri" if selected == "colibri" else ""
    config["provider"] = selected
    config[f"{selected}_url"] = config.get(f"{selected}_url") or info["base_url"]
    if selected in {"ollama", "mlx", "colibri", "custom"}:
        endpoint = answer(questionary.text("API endpoint", default=config[f"{selected}_url"]).ask()).strip()
        if not endpoint.startswith(("http://", "https://")):
            raise click.ClickException("Enter an HTTP(S) endpoint; configuration was not saved.")
        config[f"{selected}_url"] = endpoint.rstrip("/")
    if info.get("needs_key"):
        methods = auth_methods(selected)
        available = [item for item in methods if item["available"]]
        for item in methods:
            if not item["available"]:
                console.print(f"[dim]{item['label']}: {item['reason']}[/]")
        choices = [questionary.Choice(item["label"], value=item["id"]) for item in available]
        method = answer(questionary.select("Authentication", choices=choices).ask())
        config[f"{selected}_auth_method"] = method
        if method == "account":
            if not has_account(selected) and not authenticate_account(selected, method, on_status=lambda text: console.print(text, markup=False)):
                raise click.ClickException("Sign-in did not complete; provider configuration retained.")
        else:
            current_key = connection(config)["key"]
            key = answer(questionary.password("API key (leave blank to keep existing/environment key)").ask()).strip()
            if key:
                config[f"{selected}_api_key"] = key
            elif not current_key:
                raise click.ClickException("An API key is required; configuration was not saved.")
    else:
        config[f"{selected}_auth_method"] = "api_key"
    discovered = probe(config)
    default = config.get("model", "")
    models = discovered["models"]
    if models:
        if default not in models:
            default = models[0]
        selected_model = answer(questionary.autocomplete("Model", choices=models, default=default,
                                                         ignore_case=True, match_middle=True).ask()).strip()
    else:
        console.print(discovered["message"], markup=False)
        selected_model = answer(questionary.text("Exact model ID", default=default).ask()).strip()
    if not selected_model:
        raise click.ClickException("A model ID is required; configuration was not saved.")
    config["model"] = selected_model
    checked = probe(config)
    if checked["status"] == "missing":
        raise click.ClickException(checked["message"] + " Configuration was not saved.")
    if checked["status"] != "ready":
        if not answer(questionary.confirm("Connection could not be fully verified. Save this setup for later?", default=False).ask()):
            raise KeyboardInterrupt("Setup cancelled; existing configuration retained")
    config["setup_complete"] = True
    config.setdefault("update_mode", "auto")
    save_config(config)
    console.print("[green]Setup saved.[/]")
    return config


def prepare(provider=None, model=None, *, force_setup=False) -> tuple[str | None, str | None]:
    if os.environ.get("DJCODE_SKIP_STARTUP_CHECK") == "1" and not force_setup:
        return provider, model
    config = load_config()
    interactive = sys.stdin.isatty()
    first_run = not CONFIG_FILE.exists() and not provider
    checked = probe(config, provider, model) if not force_setup and not first_run else {"status": "missing", "message": "Configure a provider and model."}
    if force_setup or checked["status"] == "missing":
        if not interactive:
            raise click.ClickException(checked["message"] + " Run djcode --setup in an interactive terminal, or supply valid provider/model credentials.")
        configured = setup(config)
        return configured["provider"], configured["model"]
    if checked["status"] != "ready":
        console.print(checked["message"], markup=False)
    return provider, model
