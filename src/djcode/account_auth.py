"""Explicit, provider-approved account authentication.

DJcode never borrows another application's OAuth registration or credential files.
xAI device auth is opt-in and requires an xAI-approved public client registration.
Protocol references and availability limits are documented in docs/ACCOUNT-AUTH.md.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx

from djcode import config

TOKEN_URL = "https://auth.x.ai/oauth2/token"
DEVICE_URL = "https://auth.x.ai/oauth2/device/code"
SCOPE = "openid profile email offline_access grok-cli:access api:access"


class AccountAuthError(RuntimeError):
    """Sanitized account error, safe to display without token response bodies."""


def auth_methods(provider: str) -> list[dict]:
    """UI contract: id, label, available and reason; no network or secrets."""
    methods = [
        {
            "id": "api_key",
            "label": "API key",
            "available": True,
            "reason": "API usage is billed separately from consumer subscriptions.",
        }
    ]
    if provider == "xai":
        available = bool(os.environ.get("DJCODE_XAI_OAUTH_CLIENT_ID", "").strip())
        methods.append(
            {
                "id": "account",
                "label": "SuperGrok account (device sign-in)",
                "available": available,
                "reason": "Requires approved DJCODE_XAI_OAUTH_CLIENT_ID; xAI controls access.",
            }
        )
    elif provider == "openai":
        methods.append(
            {
                "id": "account",
                "label": "ChatGPT / Codex account",
                "available": False,
                "reason": "Use official Codex for subscription login; DJcode supports API keys.",
            }
        )
    elif provider == "anthropic":
        methods.append(
            {
                "id": "account",
                "label": "Claude subscription",
                "available": False,
                "reason": "Anthropic prohibits third-party subscription OAuth. Use an API key.",
            }
        )
    return methods


def _client_id() -> str:
    value = os.environ.get("DJCODE_XAI_OAUTH_CLIENT_ID", "").strip()
    if not value:
        raise AccountAuthError(
            "Set an approved DJCODE_XAI_OAUTH_CLIENT_ID or choose API key."
        )
    return value


def _path() -> Path:
    return config.CONFIG_DIR / "accounts" / "xai.json"


def _read() -> dict:
    try:
        value = json.loads(_path().read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def has_account(provider: str) -> bool:
    if provider != "xai":
        return False
    value = _read()
    return bool(
        value.get("access_token")
        and value.get("client_id") == os.environ.get("DJCODE_XAI_OAUTH_CLIENT_ID", "").strip()
    )


def _save(value: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".xai-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def forget_account(provider: str) -> None:
    """Remove only DJcode's own local account credential, on explicit logout."""
    if provider == "xai":
        _path().unlink(missing_ok=True)


def _positive(value, fallback: float, maximum: float) -> float:
    try:
        number = float(value)
        return min(number, maximum) if math.isfinite(number) and number > 0 else fallback
    except (TypeError, ValueError):
        return fallback


def _tokens(body: dict, client_id: str, previous: dict | None = None) -> dict:
    access = body.get("access_token")
    if not isinstance(access, str) or not access:
        raise AccountAuthError("xAI returned an invalid token response. Sign in again.")
    refresh = body.get("refresh_token") or (previous or {}).get("refresh_token", "")
    if not isinstance(refresh, str):
        raise AccountAuthError("xAI returned an invalid refresh token.")
    return {
        "access_token": access,
        "refresh_token": refresh,
        "client_id": client_id,
        "expires_at": time.time() + _positive(body.get("expires_in"), 3600, 86400),
    }


@dataclass(repr=False)
class DeviceSignIn:
    """Only verification_url and user_code should be shown to the user."""

    verification_url: str
    user_code: str
    device_code: str = field(repr=False)
    client_id: str = field(repr=False)
    deadline: float = 0
    interval: float = 5


async def begin_xai_login(*, client: httpx.AsyncClient | None = None) -> DeviceSignIn:
    """Call only after the user explicitly selects account sign-in."""
    if client is None:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as owned:
            return await begin_xai_login(client=owned)
    client_id = _client_id()
    try:
        response = await client.post(
            DEVICE_URL, data={"client_id": client_id, "scope": SCOPE}, timeout=10
        )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError):
        raise AccountAuthError(
            "Unable to start xAI sign-in. Check your network and approved client registration."
        ) from None
    if not isinstance(body, dict):
        raise AccountAuthError("xAI returned an invalid device authorization response.")
    uri = body.get("verification_uri_complete") or body.get("verification_uri", "")
    parsed = urlparse(uri) if isinstance(uri, str) else None
    if (
        not parsed
        or parsed.scheme != "https"
        or parsed.hostname not in {"auth.x.ai", "accounts.x.ai", "grok.com", "x.ai"}
        or parsed.username
        or parsed.password
    ):
        raise AccountAuthError("xAI returned an unexpected verification address.")
    if not all(isinstance(body.get(k), str) and body[k] for k in ("device_code", "user_code")):
        raise AccountAuthError("xAI returned an invalid device authorization response.")
    return DeviceSignIn(
        uri,
        body["user_code"],
        body["device_code"],
        client_id,
        time.monotonic() + _positive(body.get("expires_in"), 300, 600),
        max(1, _positive(body.get("interval"), 5, 60)),
    )


async def finish_xai_login(
    device: DeviceSignIn, *, client: httpx.AsyncClient | None = None
) -> None:
    """Bounded RFC8628 polling. Cancelling the task interrupts requests and waits."""
    if client is None:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as owned:
            return await finish_xai_login(device, client=owned)
    interval = device.interval
    while time.monotonic() < device.deadline:
        await asyncio.sleep(min(interval, max(0, device.deadline - time.monotonic())))
        remaining = device.deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            response = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": device.client_id,
                    "device_code": device.device_code,
                },
                timeout=min(10, remaining),
            )
            body = response.json()
        except (httpx.HTTPError, ValueError):
            raise AccountAuthError("xAI sign-in connection failed. Start sign-in again.") from None
        if not isinstance(body, dict):
            raise AccountAuthError("xAI returned an invalid sign-in response.")
        if response.is_success:
            _save(_tokens(body, device.client_id))
            return
        error = body.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error in {"access_denied", "authorization_denied"}:
            raise AccountAuthError("xAI account sign-in was denied.")
        if error == "expired_token":
            break
        raise AccountAuthError(f"xAI account sign-in failed (HTTP {response.status_code}).")
    raise AccountAuthError("xAI account sign-in expired. Start sign-in again.")


# Provider instances in one event loop share a refresh lock, including subagents.
_refresh_lock = asyncio.Lock()


async def account_token(provider: str, base_url: str) -> str:
    """Get/refresh an account token only for the exact provider API origin."""
    if provider != "xai" or base_url.rstrip("/") != "https://api.x.ai/v1":
        raise AccountAuthError(
            "Account credentials require the official xAI API; gateways need API keys."
        )
    async with _refresh_lock:
        value = _read()
        if not value.get("access_token") or value.get("client_id") != _client_id():
            raise AccountAuthError("Sign in to your xAI account again.")
        if value.get("expires_at", 0) > time.time() + 120:
            return value["access_token"]
        if not value.get("refresh_token"):
            raise AccountAuthError("xAI session expired. Sign in again.")
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                response = await client.post(
                    TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "client_id": value["client_id"],
                        "refresh_token": value["refresh_token"],
                    },
                )
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError):
            raise AccountAuthError(
                "Unable to refresh xAI sign-in. Check your network or sign in again."
            ) from None
        if not isinstance(body, dict):
            raise AccountAuthError("xAI returned an invalid refresh response.")
        value = _tokens(body, value["client_id"], value)
        _save(value)
        return value["access_token"]


def get_account_token(provider: str) -> str:
    """Synchronous startup helper; callers must not store or display the result."""
    return asyncio.run(account_token(provider, "https://api.x.ai/v1"))


def authenticate_account(provider: str, method: str = "account", on_status=print) -> bool:
    """Explicit interactive entrypoint; never invoked during import or discovery."""

    async def run() -> None:
        if provider != "xai" or method != "account":
            raise AccountAuthError("This account sign-in method is unavailable. Choose API key.")
        device = await begin_xai_login()
        on_status(f"Open {device.verification_url} and enter code: {device.user_code}")
        on_status("Waiting for xAI authorization (Ctrl+C to cancel)…")
        await finish_xai_login(device)
        on_status(
            "xAI account connected. Model access remains subject to your account permissions."
        )

    try:
        asyncio.run(run())
        return True
    except KeyboardInterrupt:
        on_status("Account sign-in cancelled.")
    except (AccountAuthError, OSError) as exc:
        on_status(
            str(exc)
            if isinstance(exc, AccountAuthError)
            else "Unable to save DJcode account credentials."
        )
    return False
