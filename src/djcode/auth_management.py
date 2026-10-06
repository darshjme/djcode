"""Provider-scoped authentication management for CLI and terminal surfaces.

Read helpers never perform network requests and return no credential values.
Stored keys take precedence over environment keys, matching runtime selection.
"""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from threading import RLock

from djcode import account_auth
from djcode import config as settings
from djcode.auth import PROVIDERS, get_key_source

AuthManagementError = account_auth.AccountAuthError
_mutation_lock = RLock()


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _custom(config: dict) -> dict:
    providers = config.get("custom_providers")
    return providers if isinstance(providers, dict) else {}


def _provider(provider: str, config: dict) -> dict:
    if not isinstance(provider, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", provider):
        raise AuthManagementError("Choose a valid provider ID from auth list.")
    custom = _custom(config).get(provider)
    if isinstance(custom, dict):
        return {**custom, "name": provider, "needs_key": True, "env": "DJCODE_API_KEY"}
    if provider in PROVIDERS:
        return PROVIDERS[provider]
    if provider == "remote":
        return {"name": "Remote (OpenAI-compatible)", "needs_key": True, "env": "DJCODE_API_KEY"}
    raise AuthManagementError("Unknown provider. Choose a provider from auth list.")


def _key_source(provider: str, config: dict, info: dict) -> tuple[str, str]:
    """Resolve readiness; credential strings remain private to this module."""
    _, source, env = get_key_source(provider, config)
    return source, env


def _account_origin(provider: str, config: dict) -> str:
    custom = _custom(config).get(provider)
    custom = custom if isinstance(custom, dict) else {}
    base = (
        custom.get("base_url")
        or config.get(f"{provider}_url")
        or PROVIDERS.get(provider, {}).get("base_url")
    )
    base = (
        os.environ.get("DJCODE_BASE_URL")
        or (config.get("base_url") if provider == config.get("provider") else "")
        or base
    )
    return _text(base).rstrip("/")


def auth_status(provider: str | None = None, config: dict | None = None) -> dict:
    """Report local readiness, source and offered methods; never verify a key online."""
    config = settings.load_config() if config is None else config
    selected = provider if provider is not None else config.get("provider", "ollama")
    info = _provider(selected, config)
    method = config.get(f"{selected}_auth_method", "api_key")
    method = method if isinstance(method, str) and method in {"api_key", "account"} else "invalid"
    local = not info.get("needs_key", True)
    methods = (
        [
            {
                "id": "local",
                "label": "Local endpoint",
                "available": True,
                "reason": "No account sign-in required.",
            }
        ]
        if local
        else account_auth.auth_methods(selected)
    )
    if local and info.get("optional_key"):
        methods.extend(account_auth.auth_methods(selected))
    source, env = _key_source(selected, config, info)
    stored_key = source == "stored"
    environment_present = any(
        _text(os.environ.get(variable))
        for variable in ([env] if env else [])
        + (
            ["DJCODE_API_KEY", "OPENAI_API_KEY"]
            if selected in {"custom", "remote"} or selected in _custom(config)
            else []
        )
    )
    account = account_auth.account_status(selected)
    if method == "account":
        offered = next((item for item in methods if item["id"] == "account"), None)
        official = _account_origin(selected, config) == "https://api.x.ai/v1"
        connected = bool(offered and offered["available"] and account["connected"] and official)
        source = "account" if account["stored"] else "none"
        status = (
            "connected"
            if connected
            else "unavailable"
            if not offered or not offered["available"]
            else "login_required"
        )
        message = (
            "Stored account session is locally ready; provider access is not verified."
            if connected
            else offered["reason"]
            if offered and not offered["available"]
            else "Account credentials require the official xAI API; gateways need API keys."
            if not official
            else "Account sign-in is required. API keys do not override account mode."
        )
    elif method == "invalid":
        connected, status, message = False, "invalid", "Select a supported authentication method."
    elif source != "none":
        connected, status = True, "configured"
        message = "Credential is configured; provider access is not verified."
    elif local:
        source, method = "local", "local"
        connected, status, message = True, "local", "Local endpoint; availability is not verified."
    else:
        connected, status, message = (
            False,
            "login_required",
            "Configure an API key or an available sign-in method.",
        )
    return {
        "provider": selected,
        # Custom display names are user-controlled and may contain secrets.
        "name": PROVIDERS.get(selected, {}).get("name", selected),
        "active": config.get("provider", "ollama") == selected,
        "method": method,
        "source": source,
        "status": status,
        "connected": connected,
        "stored_key": stored_key,
        "environment_present": bool(environment_present),
        "env_var": env,
        "account_stored": account["stored"],
        "methods": methods,
        "message": message,
    }


def list_auth(config: dict | None = None) -> list[dict]:
    config = settings.load_config() if config is None else config
    providers = list(PROVIDERS)
    for provider, value in _custom(config).items():
        if (
            isinstance(provider, str)
            and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", provider)
            and isinstance(value, dict)
            and provider not in providers
        ):
            providers.append(provider)
    if config.get("provider") == "remote" or _text(config.get("remote_url")):
        providers.append("remote")
    return [auth_status(provider, config) for provider in providers]


def _load_for_write() -> dict:
    """Refuse to replace settings that ordinary startup recovered from incorrectly."""
    try:
        raw = settings.CONFIG_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return deepcopy(settings.DEFAULT_CONFIG)
    except (OSError, UnicodeError):
        raise AuthManagementError(
            "Cannot read DJcode settings. Repair the configuration before changing authentication."
        ) from None
    try:
        value = json.loads(raw)
    except ValueError:
        raise AuthManagementError(
            "DJcode settings contain invalid JSON. "
            "Repair the configuration before changing authentication."
        ) from None
    if not isinstance(value, dict) or not isinstance(value.get("custom_providers", {}), dict):
        raise AuthManagementError(
            "DJcode settings must contain a JSON object and valid custom providers. "
            "Repair the configuration first."
        )
    return {**deepcopy(settings.DEFAULT_CONFIG), **value}


def _save(config: dict) -> None:
    try:
        settings.save_config(config)
    except (OSError, TypeError, ValueError):
        raise AuthManagementError(
            "Unable to save DJcode authentication settings; previous settings were preserved."
        ) from None


def login_api_key(provider: str, key: str, *, activate: bool = False) -> dict:
    """Atomically save an explicitly supplied key; callers should use hidden input."""
    if (
        not isinstance(key, str)
        or not key.strip()
        or len(key) > 16384
        or any(ord(c) < 33 or ord(c) > 126 for c in key.strip())
    ):
        raise AuthManagementError("Enter a nonempty API key without control characters.")
    with _mutation_lock:
        config = _load_for_write()
        info = _provider(provider, config)
        if not info.get("needs_key", True) and not info.get("optional_key"):
            raise AuthManagementError("This local provider does not require an API key.")
        custom = _custom(config).get(provider)
        if isinstance(custom, dict):
            custom["api_key"] = key.strip()
        else:
            config[f"{provider}_api_key"] = key.strip()
        config[f"{provider}_auth_method"] = "api_key"
        if activate:
            config["provider"] = provider
        _save(config)
        return auth_status(provider, config)


def select_auth_method(provider: str, method: str, *, activate: bool = False) -> dict:
    """Select a locally ready method without starting an implicit login or network call."""
    with _mutation_lock:
        config = _load_for_write()
        info = _provider(provider, config)
        if method == "local" and not info.get("needs_key", True):
            method = "api_key"
        if not isinstance(method, str) or method not in {"api_key", "account"}:
            raise AuthManagementError("Choose an available authentication method.")
        config[f"{provider}_auth_method"] = method
        result = auth_status(provider, config)
        if not result["connected"]:
            raise AuthManagementError(result["message"])
        if activate:
            config["provider"] = provider
        _save(config)
        return auth_status(provider, config)


def logout(provider: str) -> dict:
    """Remove this provider's saved credentials; environment keys remain caller-owned."""
    with _mutation_lock:
        config = _load_for_write()
        _provider(provider, config)
        previous = deepcopy(config)
        config.pop(f"{provider}_api_key", None)
        config.pop(f"{provider}_auth_method", None)
        custom = _custom(config).get(provider)
        if isinstance(custom, dict):
            custom.pop("api_key", None)
        if provider == "remote":
            config.pop("remote_api_key", None)
        _save(config)
        try:
            account_auth.forget_account(provider)
        except OSError:
            try:
                _save(previous)
            except AuthManagementError:
                raise AuthManagementError(
                    "Unable to remove the account credential or restore previous settings. "
                    "Review DJcode authentication storage."
                ) from None
            raise AuthManagementError(
                "Unable to remove the local account credential; logout was not completed."
            ) from None
        result = auth_status(provider, config)
        result["message"] = (
            "Saved credentials removed. Environment credentials remain available; "
            "unset the reported variable to disconnect completely."
            if result["environment_present"]
            else "Saved provider credentials removed. "
            "A shared fallback credential remains configured."
            if result["source"] == "stored"
            else "Saved credentials removed. Upstream grants were not revoked."
        )
        return result


async def login_account(provider: str, *, on_status=print, activate: bool = False) -> dict:
    """Explicit xAI device login, followed by selection of the account method."""
    with _mutation_lock:
        config = _load_for_write()
        _provider(provider, config)
        offered = next(
            (item for item in account_auth.auth_methods(provider) if item["id"] == "account"), None
        )
        if not offered or not offered["available"]:
            raise AuthManagementError(
                offered["reason"]
                if offered
                else "Account sign-in is unavailable for this provider."
            )
        if _account_origin(provider, config) != "https://api.x.ai/v1":
            raise AuthManagementError(
                "Account sign-in requires the official xAI API; gateways need API keys."
            )
    device = await account_auth.begin_xai_login()
    on_status(f"Open {device.verification_url} and enter code: {device.user_code}")
    on_status("Waiting for xAI authorization (Ctrl+C to cancel)…")
    tokens = await account_auth.finish_xai_login(device, store=False)
    with _mutation_lock:
        config = _load_for_write()
        if _account_origin(provider, config) != "https://api.x.ai/v1":
            raise AuthManagementError(
                "Account sign-in requires the official xAI API; gateways need API keys."
            )
        if tokens["client_id"] != account_auth._client_id():
            raise AuthManagementError(
                "The client registration changed during sign-in. Start again."
            )
        try:
            previous = account_auth._path().read_bytes()
        except FileNotFoundError:
            previous = None
        except OSError:
            raise AuthManagementError(
                "Unable to read the previous account credential; sign-in was not saved."
            ) from None
        config[f"{provider}_auth_method"] = "account"
        if activate:
            config["provider"] = provider
        try:
            account_auth._save(tokens)
        except OSError:
            raise AuthManagementError(
                "Unable to save DJcode account credentials; previous settings were preserved."
            ) from None
        try:
            _save(config)
        except AuthManagementError:
            try:
                if previous is None:
                    account_auth.forget_account(provider)
                else:
                    account_auth._save_raw(previous)
            except OSError:
                raise AuthManagementError(
                    "Unable to save settings or restore the previous account credential. "
                    "Review DJcode authentication storage."
                ) from None
            raise
        return auth_status(provider, config)


async def login_openrouter(
    provider: str, code: str, verifier: str, *, activate: bool = False
) -> dict:
    """Exchange an explicitly requested OpenRouter PKCE grant; never return its key."""
    if provider != "openrouter":
        raise AuthManagementError("Browser sign-in is available for OpenRouter only.")
    with _mutation_lock:
        _provider(provider, _load_for_write())
    from djcode.openrouter_auth import exchange

    key = await exchange(code, verifier)
    return login_api_key(provider, key, activate=activate)
