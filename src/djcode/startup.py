"""Bounded provider discovery and an explicit, configuration-preserving setup flow."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import os
import sys

import click
import httpx
import questionary
from rich.console import Console

from djcode.auth import PROVIDERS
from djcode.config import CONFIG_FILE, load_config, save_config

console = Console(stderr=True)
DISCOVERY_DEADLINE = 5.0


def connection(config: dict, provider: str | None = None, model: str | None = None) -> dict:
    selected = provider or config.get("provider", "ollama")
    info = PROVIDERS.get(selected, {})
    custom = config.get("custom_providers", {})
    custom = custom.get(selected, {}) if isinstance(custom, dict) else {}
    custom = custom if isinstance(custom, dict) else {}
    url_provider = selected.startswith(("http://", "https://"))
    base = (selected if url_provider else custom.get("base_url") or config.get(f"{selected}_url") or info.get("base_url", ""))
    # A saved global override belongs to the selected provider, not a different
    # provider being browsed. The explicit environment override remains global.
    base = os.environ.get("DJCODE_BASE_URL") or (config.get("base_url") if selected == config.get("provider") else "") or base
    from djcode.auth import get_key_source
    key = get_key_source(selected, config)[0]
    method = config.get(f"{selected}_auth_method", "api_key")
    selected_model = model if model is not None else (config.get("model", "") if selected == config.get("provider") else custom.get("model", ""))
    if selected == "colibri":
        selected_model = model or (config.get("model") if config.get("provider") == "colibri" else None) or "djcode-colibri"
    selected_model = selected_model.strip() if isinstance(selected_model, str) else ""
    return {"provider": selected, "base": base.rstrip("/") if isinstance(base, str) else "",
            "key": key.strip() if isinstance(key, str) else "",
            "model": selected_model, "method": method, "needs_key": bool(info.get("needs_key"))}


def model_details(name: str, items: list) -> list[dict]:
    """Expose a small allowlist of provider-reported metadata, never defaults."""
    result = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        ident = item.get("name" if name in {"ollama", "google"} else "id")
        if not isinstance(ident, str) or not ident.strip() or len(ident) > 512 or any(ord(c) < 32 for c in ident):
            continue
        ident = ident.removeprefix("models/") if name == "google" else ident
        label = item.get("display_name") or item.get("displayName") or item.get("name") or ident
        if not isinstance(label, str) or any(ord(c) < 32 for c in label):
            label = ident
        capabilities = {}
        for field, keys in {
            "context_tokens": ("context_length", "inputTokenLimit", "max_input_tokens"),
            "max_output_tokens": ("outputTokenLimit", "max_tokens"),
        }.items():
            value = next((item[k] for k in keys if k in item), None)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                capabilities[field] = value
        reported = item.get("capabilities", {})
        if isinstance(reported, dict):
            for field, keys in {"tools": ("tool_use",), "vision": ("image_input",), "reasoning": ("thinking",)}.items():
                value = next((reported[k] for k in keys if k in reported), None)
                supported = value.get("supported") if isinstance(value, dict) else value
                if isinstance(supported, bool):
                    capabilities[field] = supported
        if name == "google" and isinstance(item.get("thinking"), bool):
            capabilities["reasoning"] = item["thinking"]
        parameters = item.get("supported_parameters")
        if isinstance(parameters, list) and all(isinstance(v, str) for v in parameters):
            capabilities["tools"] = "tools" in parameters
            capabilities["reasoning"] = "reasoning" in parameters
        architecture = item.get("architecture", {})
        modalities = architecture.get("input_modalities") if isinstance(architecture, dict) else None
        if isinstance(modalities, list) and all(isinstance(v, str) for v in modalities):
            capabilities["vision"] = "image" in modalities
        result[ident] = {"id": ident, "name": label[:512], "capabilities": capabilities}
    return [result[key] for key in sorted(result)]


def discover(endpoint: str, headers: dict) -> httpx.Response:
    """Bound the whole discovery request, including headers and slow response bodies."""
    async def request() -> httpx.Response:
        size = 0
        async with httpx.AsyncClient(timeout=2.0, follow_redirects=False) as client:
            current = httpx.URL(endpoint)
            collected = []
            seen = set()
            for _ in range(10):
                chunks = []
                async with client.stream("GET", current, headers=headers) as response:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 2 * 1024 * 1024:
                            raise ValueError("Provider discovery exceeded its size budget")
                        chunks.append(chunk)
                    reply = httpx.Response(response.status_code, content=b"".join(chunks), request=response.request)
                if not reply.is_success:
                    return reply
                payload = reply.json()
                if not isinstance(payload, dict):
                    raise ValueError("Invalid model catalog")
                field = "models" if "models" in payload else "data"
                items = payload.get(field, [])
                if not isinstance(items, list):
                    raise ValueError("Invalid model catalog")
                collected.extend(items)
                # Follow provider cursor values on the original endpoint only.
                # Never forward credentials to a server-supplied next URL.
                cursor = payload.get("nextPageToken") or (payload.get("last_id") if payload.get("has_more") else None)
                if not cursor:
                    payload[field] = collected
                    return httpx.Response(reply.status_code, json=payload, request=reply.request)
                if not isinstance(cursor, str) or cursor in seen:
                    raise ValueError("Invalid model pagination")
                seen.add(cursor)
                current = httpx.URL(endpoint).copy_set_param("pageToken" if payload.get("nextPageToken") else "after_id", cursor)
            raise ValueError("Provider discovery exceeded its page budget")

    async def bounded() -> httpx.Response:
        return await asyncio.wait_for(request(), timeout=DISCOVERY_DEADLINE)

    try:
        return asyncio.run(bounded())
    except TimeoutError:
        raise ValueError("Provider discovery exceeded its time budget") from None


def probe(config: dict, provider: str | None = None, model: str | None = None) -> dict:
    details = connection(config, provider, model)
    name, base, key = details["provider"], details["base"], details["key"]
    def outcome(status, message, models=None, *, source="none", error=None, metadata=None):
        return {"status": status, "message": message, "models": models or [], "provider": name,
                "source": source, "error": error, "model_details": metadata or []}
    if not base.startswith(("http://", "https://")):
        return outcome("missing", "Choose a provider endpoint.", error="endpoint_missing")
    if details["method"] == "account":
        from djcode.account_auth import has_account, get_account_token, AccountAuthError
        if name != "xai" or base != "https://api.x.ai/v1":
            return outcome("missing", "Account authentication requires the supported provider endpoint.", error="auth_endpoint")
        if not has_account(name):
            return outcome("missing", "Account sign-in is required.", error="auth_missing")
        try:
            key = get_account_token(name)
        except AccountAuthError:
            if not details["model"]:
                return outcome("missing", "Select an explicit model ID.")
            return outcome("offline", "Account refresh unavailable; saved setup retained.")
    elif details["needs_key"] and not key:
        return outcome("missing", "An API key or supported account sign-in is required.", error="auth_missing")
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
            return outcome("missing", "The provider rejected authentication; choose or reconnect an account.", error="auth_rejected")
        if response.status_code in {404, 405, 501}:
            if not details["model"]:
                return outcome("missing", "This endpoint does not expose model discovery. Connect with an explicit model ID.", source="unsupported", error="discovery_unsupported")
            return outcome("unverified", "This endpoint does not expose model discovery; the explicit model will be checked during use.", source="unsupported", error="discovery_unsupported")
        response.raise_for_status()
        payload = response.json()
        items = payload.get("models" if name in {"ollama", "google"} else "data", [])
        if not isinstance(items, list):
            raise ValueError("Invalid model catalog")
        metadata = model_details(name, items)
        models = [item["id"] for item in metadata]
        selected = details["model"]
        matched = selected in models or (name == "ollama" and selected + ":latest" in models)
        if not selected or not matched:
            return outcome("missing", "Select a model available at this provider." if models else "The provider returned no models for this account.", models, source="live", error="model_missing" if models else "catalog_empty", metadata=metadata)
        return outcome("ready", f"Connected to {name} · {selected}", models, source="live", metadata=metadata)
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        if not details["model"]:
            return outcome("missing", "Model discovery unavailable. Reconnect or retry when the provider is reachable.", source="unavailable", error="discovery_failed")
        return outcome("offline", "Provider check unavailable; existing configuration retained.", source="unavailable", error="discovery_failed")


def answer(value):
    if value is None:
        raise KeyboardInterrupt("Setup cancelled; existing configuration retained")
    return value


def setup(existing: dict | None = None) -> dict:
    """Commit only after selection/validation; cancellation preserves old config."""
    from djcode.account_auth import auth_methods, authenticate_account, has_account
    config = deepcopy(existing or load_config())
    console.print("\n[bold]DJcode setup[/] · project by Darshankumar Joshi")
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
        current_method = config.get(f"{selected}_auth_method", "api_key")
        default_method = current_method if current_method in {item["id"] for item in available} else "api_key"
        method = answer(questionary.select("Authentication", choices=choices, default=default_method).ask())
        config[f"{selected}_auth_method"] = method
        if method == "browser" and selected == "openrouter":
            import webbrowser
            from djcode.openrouter_auth import begin, exchange
            verifier, url = begin()
            console.print(url, markup=False)
            webbrowser.open(url)
            code = answer(questionary.password("One-time code from OpenRouter").ask())
            config[f"{selected}_api_key"] = asyncio.run(exchange(code, verifier))
            config[f"{selected}_auth_method"] = "api_key"
        elif method == "account":
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
    from djcode.config import load_config_for_write
    load_config_for_write()
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
