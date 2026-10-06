"""Malformed authentication responses and interrupted lifecycle acceptance."""

import asyncio
import time

import httpx
import pytest

from djcode import account_auth, config, openrouter_auth


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setenv("DJCODE_XAI_OAUTH_CLIENT_ID", "approved-test")


@pytest.mark.parametrize(
    "address",
    [
        "https://auth.x.ai:bad/device",
        "https://auth.x.ai:444/device",
        "https://auth.x.ai/\nprivate",
        "https://[broken",
    ],
)
def test_malformed_device_address_is_sanitized(address):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200,
                    json={
                        "verification_uri": address,
                        "user_code": "USER",
                        "device_code": "private-device",
                    },
                )
            )
        ) as client:
            await account_auth.begin_xai_login(client=client)

    with pytest.raises(account_auth.AccountAuthError, match="unexpected") as error:
        asyncio.run(run())
    assert "private" not in str(error.value)


@pytest.mark.parametrize("code", ["USER\x1b[2J", "USER\nsecret", "x" * 129, {}, ""])
def test_invalid_display_code_never_reaches_terminal(code):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200,
                    json={
                        "verification_uri": "https://auth.x.ai/device",
                        "user_code": code,
                        "device_code": "private-device",
                    },
                )
            )
        ) as client:
            await account_auth.begin_xai_login(client=client)

    with pytest.raises(account_auth.AccountAuthError, match="invalid"):
        asyncio.run(run())


@pytest.mark.parametrize("expiry", ["secret", {}, [], True, float("nan"), float("inf"), 10**1000])
def test_corrupt_account_expiry_is_not_ready_or_returned(expiry):
    account_auth._save(
        {"access_token": "private", "client_id": "approved-test", "expires_at": expiry}
    )
    assert not account_auth.has_account("xai")
    with pytest.raises(account_auth.AccountAuthError, match="again"):
        asyncio.run(account_auth.account_token("xai", "https://api.x.ai/v1"))


def test_empty_registration_cannot_activate_an_account(monkeypatch):
    monkeypatch.delenv("DJCODE_XAI_OAUTH_CLIENT_ID")
    account_auth._save(
        {"access_token": "private", "client_id": "", "expires_at": time.time() + 3600}
    )
    assert not account_auth.has_account("xai")


def test_logout_cancels_successful_in_flight_device_grant(monkeypatch):
    async def sleep(delay):
        pass

    monkeypatch.setattr(account_auth.asyncio, "sleep", sleep)

    def response(request):
        account_auth.forget_account("xai")
        return httpx.Response(200, json={"access_token": "private-new"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            await account_auth.finish_xai_login(
                account_auth.DeviceSignIn(
                    "https://auth.x.ai/device",
                    "USER",
                    "private-device",
                    "approved-test",
                    time.monotonic() + 10,
                ),
                client=client,
            )

    with pytest.raises(account_auth.AccountAuthError, match="logout"):
        asyncio.run(run())
    assert not account_auth._path().exists()


@pytest.mark.parametrize(
    "code,verifier",
    [
        ("secret\nnext", "a" * 64),
        ("code", {}),
        ("code", "short-secret"),
        ("code", "a" * 129),
        ("x" * 2049, "a" * 64),
    ],
)
def test_invalid_pkce_input_fails_before_network(code, verifier):
    class NoNetwork:
        async def post(self, *args, **kwargs):
            pytest.fail("malformed PKCE must fail before network")

    with pytest.raises(account_auth.AccountAuthError, match="one-time") as error:
        asyncio.run(openrouter_auth.exchange(code, verifier, client=NoNetwork()))
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("key", ["key\nsecret", {}, "", "x" * 16385, "é"])
def test_invalid_browser_key_response_is_sanitized(key):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"key": key}))
        ) as client:
            await openrouter_auth.exchange("code", "a" * 64, client=client)

    with pytest.raises(account_auth.AccountAuthError, match="invalid") as error:
        asyncio.run(run())
    assert "secret" not in str(error.value)
