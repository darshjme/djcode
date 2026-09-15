"""Bounded provider discovery and an explicit, configuration-preserving setup flow.

Async contract (W1-7)
---------------------
Every network-touching entry point exists twice: a coroutine holding the real
implementation (``discover_async``, ``probe_async``, ``prepare_async``) and a
synchronous wrapper under the bare name. The wrapper drives the coroutine with
:func:`asyncio.run` and **refuses**, with a message naming its ``_async`` twin,
when a loop is already running in the calling thread.

The refusal is deliberate and must not be softened into a silent
``asyncio.to_thread`` hop: a sync function that secretly moves work onto a
worker thread hides a thread-safety contract from its caller, which is exactly
how "questionary driven from a worker thread while prompt_toolkit owns stdin"
got written. Callers already inside a loop either await the ``_async`` form, or
hop to a thread themselves where that is what they actually mean.
"""

from __future__ import annotations

import asyncio
import os
import sys
from copy import deepcopy

import click
import httpx
import questionary
from rich.console import Console

from djcode.auth import PROVIDERS
from djcode.config import CONFIG_FILE, load_config, save_config

console = Console(stderr=True)
DISCOVERY_DEADLINE = 5.0
DISCOVERY_SIZE_BUDGET = 2 * 1024 * 1024

# DJcode ships no telemetry, and says so to every library and child process that
# honours the convention. `setdefault`, never assignment: a user who deliberately
# exported DO_NOT_TRACK=0 for some downstream tool keeps their choice.
os.environ.setdefault("DO_NOT_TRACK", "1")


def _refuse_inside_loop(sync_name: str, async_name: str) -> None:
    """Return if this thread may block; otherwise raise, naming the coroutine to await."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        f"startup.{sync_name}() is blocking and cannot run inside an event loop. "
        f"Use 'await startup.{async_name}(...)' or "
        f"'await asyncio.to_thread(startup.{sync_name}, ...)'."
    )


def connection(config: dict, provider: str | None = None, model: str | None = None) -> dict:
    selected = provider or config.get("provider", "ollama")
    info = PROVIDERS.get(selected, {})
    custom = config.get("custom_providers", {}).get(selected, {})
    url_provider = selected.startswith(("http://", "https://"))
    base = (
        selected
        if url_provider
        else custom.get("base_url") or config.get(f"{selected}_url") or info.get("base_url", "")
    )
    base = os.environ.get("DJCODE_BASE_URL") or config.get("base_url") or base
    key = (
        custom.get("api_key")
        or config.get(f"{selected}_api_key")
        or os.environ.get(info.get("env", ""), "")
    )
    if url_provider or selected in {"custom", "remote"}:
        key = (
            key
            or os.environ.get("DJCODE_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or config.get("remote_api_key", "")
        )
    method = config.get(f"{selected}_auth_method", "api_key")
    selected_model = model or custom.get("model") or config.get("model", "")
    if selected == "colibri":
        selected_model = (
            model
            or (config.get("model") if config.get("provider") == "colibri" else None)
            or "djcode-colibri"
        )
    selected_model = selected_model.strip() if isinstance(selected_model, str) else ""
    return {
        "provider": selected,
        "base": (base or "").rstrip("/"),
        "key": key or "",
        "model": selected_model,
        "method": method,
        "needs_key": bool(info.get("needs_key")),
    }


async def _request(endpoint: str, headers: dict) -> httpx.Response:
    """Stream the discovery response under a hard body-size budget."""
    chunks = []
    size = 0
    async with httpx.AsyncClient(timeout=2.0, follow_redirects=False) as client:
        async with client.stream("GET", endpoint, headers=headers) as response:
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > DISCOVERY_SIZE_BUDGET:
                    raise ValueError("Provider discovery exceeded its size budget")
                chunks.append(chunk)
            return httpx.Response(
                response.status_code, content=b"".join(chunks), request=response.request
            )


async def discover_async(endpoint: str, headers: dict) -> httpx.Response:
    """The real implementation: bounded, cancellable, loop-native."""
    try:
        return await asyncio.wait_for(_request(endpoint, headers), timeout=DISCOVERY_DEADLINE)
    except TimeoutError:
        raise ValueError("Provider discovery exceeded its time budget") from None


def discover(endpoint: str, headers: dict) -> httpx.Response:
    """Blocking wrapper around discover_async; refuses inside a running loop."""
    _refuse_inside_loop("discover", "discover_async")
    return asyncio.run(discover_async(endpoint, headers))


def _catalogue(name: str, payload: dict) -> list[dict]:
    """Normalise a provider's model listing to ``[{"name": str, "size": int}, ...]``.

    ``size`` is Ollama's on-disk byte count, and ``0`` wherever the provider does
    not report one. Discarding it here forced every consumer that wants a model
    table to re-query the provider.
    """
    items = payload.get("models" if name in {"ollama", "google"} else "data", [])
    if not isinstance(items, list):
        return []
    key = "name" if name in {"ollama", "google"} else "id"
    sizes: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get(key, "")
        if not isinstance(value, str) or not value:
            continue
        if name == "google":
            value = value.removeprefix("models/")
        raw = item.get("size", 0)
        size = int(raw) if isinstance(raw, int | float) and not isinstance(raw, bool) else 0
        sizes[value] = max(sizes.get(value, 0), max(size, 0))
    return [{"name": value, "size": sizes[value]} for value in sorted(sizes)]


async def probe_async(config: dict, provider: str | None = None, model: str | None = None) -> dict:
    """Check the configured provider/model and return the discovered model catalogue."""
    details = connection(config, provider, model)
    name, base, key = details["provider"], details["base"], details["key"]

    def outcome(status, message, models=None):
        return {"status": status, "message": message, "models": models or [], "provider": name}

    if not base.startswith(("http://", "https://")):
        return outcome("missing", "Choose a provider endpoint.")
    if details["method"] == "account":
        from djcode.account_auth import AccountAuthError, get_account_token, has_account

        if name != "xai" or base != "https://api.x.ai/v1":
            return outcome(
                "missing", "Account authentication requires the supported provider endpoint."
            )
        if not has_account(name):
            return outcome("missing", "Account sign-in is required.")
        try:
            # get_account_token refreshes the token over the network and blocks;
            # the hop is explicit because this coroutine may own the only loop.
            key = await asyncio.to_thread(get_account_token, name)
        except AccountAuthError:
            if not details["model"]:
                return outcome("missing", "Select an explicit model ID.")
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
        response = await discover_async(endpoint, headers)
        if response.status_code in {401, 403}:
            return outcome(
                "missing", "The provider rejected authentication; choose or reconnect an account."
            )
        if response.status_code in {404, 405, 501}:
            if not details["model"]:
                return outcome("missing", "Select an explicit model ID.")
            return outcome(
                "unverified",
                "This endpoint does not expose model discovery; the explicit model will be checked "
                "during use.",
            )
        response.raise_for_status()
        models = _catalogue(name, response.json())
        available = {item["name"] for item in models}
        selected = details["model"]
        matched = selected in available or (name == "ollama" and selected + ":latest" in available)
        if not selected or not matched:
            return outcome("missing", "Select a model available at this provider.", models)
        return outcome("ready", f"Connected to {name} · {selected}", models)
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        if not details["model"]:
            return outcome("missing", "Select an explicit model ID.")
        return outcome("offline", "Provider check unavailable; existing configuration retained.")


def probe(config: dict, provider: str | None = None, model: str | None = None) -> dict:
    """Blocking wrapper around probe_async; refuses inside a running loop."""
    _refuse_inside_loop("probe", "probe_async")
    return asyncio.run(probe_async(config, provider, model))


def answer(value):
    if value is None:
        raise KeyboardInterrupt("Setup cancelled; existing configuration retained")
    return value


def setup(existing: dict | None = None) -> dict:
    """Commit only after selection/validation; cancellation preserves old config.

    Synchronous and questionary-driven until W8 replaces the state machine with
    ``core/onboarding_flow.py``. Callers inside a loop reach it through
    ``asyncio.to_thread``, which is what ``prepare_async`` does.
    """
    from djcode.account_auth import auth_methods, authenticate_account, has_account

    config = deepcopy(existing or load_config())
    console.print("\n[bold]DJcode setup[/] · project by Darshan Kumar Joshi")
    console.print("[dim]Choose a provider, authentication method and model. No model downloads.[/]")
    selected = answer(
        questionary.select(
            "Provider",
            choices=[
                questionary.Choice(item["name"], value=name) for name, item in PROVIDERS.items()
            ],
        ).ask()
    )
    info = PROVIDERS[selected]
    if selected != config.get("provider"):
        config["base_url"] = ""
        config["model"] = "djcode-colibri" if selected == "colibri" else ""
    config["provider"] = selected
    config[f"{selected}_url"] = config.get(f"{selected}_url") or info["base_url"]
    if selected in {"ollama", "mlx", "colibri", "custom"}:
        endpoint = answer(
            questionary.text("API endpoint", default=config[f"{selected}_url"]).ask()
        ).strip()
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
        default_method = (
            current_method if current_method in {item["id"] for item in available} else "api_key"
        )
        method = answer(
            questionary.select("Authentication", choices=choices, default=default_method).ask()
        )
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
            if not has_account(selected) and not authenticate_account(
                selected, method, on_status=lambda text: console.print(text, markup=False)
            ):
                raise click.ClickException(
                    "Sign-in did not complete; provider configuration retained."
                )
        else:
            current_key = connection(config)["key"]
            key = answer(
                questionary.password("API key (leave blank to keep existing/environment key)").ask()
            ).strip()
            if key:
                config[f"{selected}_api_key"] = key
            elif not current_key:
                raise click.ClickException("An API key is required; configuration was not saved.")
    else:
        config[f"{selected}_auth_method"] = "api_key"
    discovered = probe(config)
    default = config.get("model", "")
    names = [item["name"] for item in discovered["models"]]
    if names:
        if default not in names:
            default = names[0]
        selected_model = answer(
            questionary.autocomplete(
                "Model", choices=names, default=default, ignore_case=True, match_middle=True
            ).ask()
        ).strip()
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
        if not answer(
            questionary.confirm(
                "Connection could not be fully verified. Save this setup for later?", default=False
            ).ask()
        ):
            raise KeyboardInterrupt("Setup cancelled; existing configuration retained")
    config["setup_complete"] = True
    config.setdefault("update_mode", "auto")
    save_config(config)
    console.print("[green]Setup saved.[/]")
    return config


async def prepare_async(
    provider=None, model=None, *, force_setup=False
) -> tuple[str | None, str | None]:
    """Resolve provider/model for this run, repairing setup only when it is broken."""
    if os.environ.get("DJCODE_SKIP_STARTUP_CHECK") == "1" and not force_setup:
        return provider, model
    config = load_config()
    interactive = sys.stdin.isatty()
    first_run = not CONFIG_FILE.exists() and not provider
    checked = (
        await probe_async(config, provider, model)
        if not force_setup and not first_run
        else {"status": "missing", "message": "Configure a provider and model."}
    )
    if force_setup or checked["status"] == "missing":
        if not interactive:
            raise click.ClickException(
                checked["message"]
                + " Run djcode --setup in an interactive terminal, or supply valid provider/model "
                "credentials."
            )
        # setup() owns stdin through questionary and blocks; the hop is explicit,
        # and disappears in W8 when the flow becomes async end to end.
        configured = await asyncio.to_thread(setup, config)
        return configured["provider"], configured["model"]
    if checked["status"] != "ready":
        console.print(checked["message"], markup=False)
    return provider, model


def prepare(provider=None, model=None, *, force_setup=False) -> tuple[str | None, str | None]:
    """Blocking wrapper around prepare_async; refuses inside a running loop."""
    _refuse_inside_loop("prepare", "prepare_async")
    return asyncio.run(prepare_async(provider, model, force_setup=force_setup))
