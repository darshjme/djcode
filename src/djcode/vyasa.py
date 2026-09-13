"""Authenticated client for a self-hosted Vyasa fleet."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

import httpx


def endpoint() -> str:
    value = os.environ.get("VYASA_URL", "http://127.0.0.1:19000").rstrip("/")
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("VYASA_URL must be a server URL without credentials, query or fragment")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        raise ValueError("Use HTTPS for a remote VYASA_URL (HTTP is allowed only on loopback)")
    return value


def request_fleet(text=None, *, employee=None, session="default", transport=None):
    token = os.environ.get("VYASA_TOKEN", "")
    if not token.startswith("vya_live_"):
        raise ValueError("Set VYASA_TOKEN to a token created with 'vyasa token' on the fleet host")
    with httpx.Client(
        base_url=endpoint(),
        headers={"Authorization": f"Bearer {token}"},
        timeout=150,
        follow_redirects=False,
        transport=transport,
    ) as client:
        if text is None:
            response = client.get("/v1/fleet")
        else:
            response = client.post(
                "/v1/chat", json={"text": text, "employee": employee, "session": session}
            )
        if response.status_code != 200:
            raise ValueError(
                f"Vyasa request failed (HTTP {response.status_code}); "
                "check the server, token and employee"
            )
        return response.json()
