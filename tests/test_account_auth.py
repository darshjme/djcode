import asyncio
import json
import os
import time

import httpx
import pytest

from djcode import account_auth as auth
from djcode.provider import Message, Provider, ProviderConfig


@pytest.fixture(autouse=True)
def isolated_account(tmp_path, monkeypatch):
    monkeypatch.setattr(auth.config, "CONFIG_DIR", tmp_path)
    monkeypatch.setenv("DJCODE_XAI_OAUTH_CLIENT_ID", "djcode-test-registration")


def run(coro):
    return asyncio.run(coro)


def test_availability_is_honest(monkeypatch):
    monkeypatch.delenv("DJCODE_XAI_OAUTH_CLIENT_ID")
    assert not auth.auth_methods("xai")[1]["available"]
    assert not auth.auth_methods("openai")[1]["available"]
    assert not auth.auth_methods("anthropic")[1]["available"]
    assert "third-party" in auth.auth_methods("anthropic")[1]["reason"]
    assert auth.auth_methods("xai")[0]["available"]
    with pytest.raises(auth.AccountAuthError, match="approved"):
        run(auth.begin_xai_login())


def test_grant_pending_slowdown_persistence(monkeypatch):
    responses = [
        httpx.Response(
            200,
            json={
                "device_code": "private-device",
                "user_code": "USER-CODE",
                "verification_uri": "https://auth.x.ai/device",
                "interval": 1,
            },
        ),
        httpx.Response(400, json={"error": "authorization_pending"}),
        httpx.Response(400, json={"error": "slow_down"}),
        httpx.Response(
            200,
            json={
                "access_token": "private-access",
                "refresh_token": "private-refresh",
                "expires_in": 3600,
            },
        ),
    ]
    requests, sleeps = [], []

    def handle(request):
        requests.append(request)
        return responses.pop(0)

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(auth.asyncio, "sleep", sleep)

    async def flow():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            device = await auth.begin_xai_login(client=client)
            assert "private-device" not in repr(device)
            await auth.finish_xai_login(device, client=client)

    run(flow())
    assert sleeps == [1, 1, 6]
    assert b"djcode-test-registration" in requests[0].content
    assert b"device_code" in requests[1].content
    assert auth.has_account("xai")
    assert os.stat(auth._path()).st_mode & 0o777 == 0o600
    assert run(auth.account_token("xai", "https://api.x.ai/v1")) == "private-access"
    auth.forget_account("openai")
    assert auth._path().exists()
    auth.forget_account("xai")
    assert not auth.has_account("xai")


@pytest.mark.parametrize(
    "uri",
    [
        "http://auth.x.ai/verify",
        "https://attacker.example/verify",
        "https://auth.x.ai@attacker.example/",
    ],
)
def test_device_url_validation(uri):
    async def flow():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200, json={"device_code": "d", "user_code": "u", "verification_uri": uri}
                )
            )
        ) as client:
            await auth.begin_xai_login(client=client)

    with pytest.raises(auth.AccountAuthError, match="unexpected"):
        run(flow())


@pytest.mark.parametrize(
    "error,match",
    [
        ("access_denied", "denied"),
        ("expired_token", "expired"),
        ("unknown_secret_response", "HTTP 400"),
    ],
)
def test_denied_expired_sanitized(error, match, monkeypatch):
    async def sleep(delay):
        pass

    monkeypatch.setattr(auth.asyncio, "sleep", sleep)

    async def flow():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    400, json={"error": error, "error_description": "private-secret"}
                )
            )
        ) as client:
            await auth.finish_xai_login(
                auth.DeviceSignIn("https://auth.x.ai", "u", "d", "c", time.monotonic() + 20),
                client=client,
            )

    with pytest.raises(auth.AccountAuthError, match=match) as caught:
        run(flow())
    assert "private-secret" not in str(caught.value)
    assert not auth.has_account("xai")


def test_cancellation_does_not_store(monkeypatch):
    async def cancelled(delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(auth.asyncio, "sleep", cancelled)
    with pytest.raises(asyncio.CancelledError):
        run(
            auth.finish_xai_login(
                auth.DeviceSignIn("https://auth.x.ai", "u", "d", "c", time.monotonic() + 20)
            )
        )
    assert not auth._path().exists()


def test_deadline_and_bad_timing():
    assert auth._positive(float("nan"), 5, 600) == 5
    assert auth._positive(-1, 5, 600) == 5
    assert auth._positive(float("inf"), 5, 600) == 5
    assert auth._positive(90000, 5, 600) == 600
    with pytest.raises(auth.AccountAuthError, match="expired"):
        run(
            auth.finish_xai_login(
                auth.DeviceSignIn("https://auth.x.ai", "u", "d", "c", time.monotonic() - 1)
            )
        )


def test_refresh_rotates_own_store(monkeypatch):
    auth._save(
        {
            "client_id": "djcode-test-registration",
            "access_token": "old",
            "refresh_token": "old-refresh",
            "expires_at": 0,
        }
    )
    real_client = httpx.AsyncClient
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200, json={"access_token": "new", "refresh_token": "new-refresh", "expires_in": 3600}
        )

    monkeypatch.setattr(
        auth.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle)),
    )
    assert auth.get_account_token("xai") == "new"
    assert b"old-refresh" in requests[0].content
    assert auth._read()["refresh_token"] == "new-refresh"
    assert auth.get_account_token("xai") == "new"
    assert len(requests) == 1


def test_token_never_sent_to_gateway():
    auth._save(
        {
            "client_id": "djcode-test-registration",
            "access_token": "secret",
            "expires_at": time.time() + 3600,
        }
    )
    with pytest.raises(auth.AccountAuthError, match="official"):
        run(auth.account_token("xai", "https://gateway.example/v1"))


def test_provider_routes_account_token_and_preserves_tool_protocol(monkeypatch):
    requests = []

    async def token(provider, base_url):
        assert provider == "xai" and base_url == "https://api.x.ai/v1"
        return "account-secret"

    monkeypatch.setattr(auth, "account_token", token)

    async def flow():
        provider = Provider(
            ProviderConfig(
                "xai",
                "https://api.x.ai/v1",
                "test-model",
                api_key="unused-key",
                auth_method="account",
            )
        )
        await provider._client.aclose()

        def handle(request):
            requests.append(request)
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
            )

        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        try:
            return [
                chunk async for chunk in provider.chat([Message("user", "hello")], stream=False)
            ]
        finally:
            await provider._client.aclose()

    assert run(flow())
    assert requests[0].headers["authorization"] == "Bearer account-secret"
    payload = json.loads(requests[0].content)
    assert payload["tools"] and payload["messages"][0]["content"] == "hello"


def test_no_silent_account_fallback():
    async def flow():
        provider = Provider(
            ProviderConfig("openai", "https://api.openai.com/v1", "test", auth_method="account")
        )
        try:
            return [chunk async for chunk in provider.chat([])]
        finally:
            await provider._client.aclose()

    with pytest.raises(ValueError, match="unavailable"):
        run(flow())


def test_failed_refresh_preserves_credentials_and_sanitizes(monkeypatch):
    previous = {
        "client_id": "djcode-test-registration",
        "access_token": "old",
        "refresh_token": "old-refresh",
        "expires_at": 0,
    }
    auth._save(previous)
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda r: httpx.Response(401, json={"error_description": "old-refresh-secret"})
    )
    monkeypatch.setattr(
        auth.httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport)
    )
    with pytest.raises(auth.AccountAuthError, match="refresh") as caught:
        auth.get_account_token("xai")
    assert "old-refresh" not in str(caught.value)
    assert auth._read() == previous


def test_changed_registration_requires_new_login(monkeypatch):
    auth._save({"client_id": "different", "access_token": "access", "expires_at": 9999999999})
    assert not auth.has_account("xai")
    with pytest.raises(auth.AccountAuthError, match="again"):
        auth.get_account_token("xai")


def test_network_failure_interactive_is_bounded_and_safe(monkeypatch):
    async def fail():
        raise auth.AccountAuthError("Connection unavailable.")

    monkeypatch.setattr(auth, "begin_xai_login", fail)
    statuses = []
    assert not auth.authenticate_account("xai", "account", statuses.append)
    assert statuses == ["Connection unavailable."]
    assert not auth._path().exists()


def test_provider_config_preserves_account_method(monkeypatch):
    from djcode import provider

    monkeypatch.setattr(
        provider,
        "load_config",
        lambda: {"provider": "xai", "model": "grok-test", "xai_auth_method": "account"},
    )
    result = ProviderConfig.from_config()
    assert result.name == "xai"
    assert result.auth_method == "account"
    assert not result.api_key
