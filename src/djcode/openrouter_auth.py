"""OpenRouter's documented browser PKCE flow, using a pasted one-time code."""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlencode

import httpx

from djcode.account_auth import AccountAuthError


def begin():
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    url = "https://openrouter.ai/auth?" + urlencode(
        {"code_challenge": challenge, "code_challenge_method": "S256", "key_label": "DJcode"}
    )
    return verifier, url


async def exchange(code, verifier, *, client=None):
    if not isinstance(code, str) or not code.strip() or not verifier:
        raise AccountAuthError("Enter the one-time authorization code from OpenRouter")
    if client is None:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as owned:
            return await exchange(code, verifier, client=owned)
    try:
        response = await client.post(
            "https://openrouter.ai/api/v1/auth/keys",
            json={"code": code.strip(), "code_verifier": verifier, "code_challenge_method": "S256"},
        )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError):
        raise AccountAuthError(
            "OpenRouter sign-in failed or expired; restart browser sign-in"
        ) from None
    if not isinstance(body, dict) or not isinstance(body.get("key"), str) or not body["key"]:
        raise AccountAuthError("OpenRouter returned an invalid sign-in response")
    return body["key"]
