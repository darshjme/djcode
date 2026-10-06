"""Local auth management acceptance, with isolated settings and mock grants."""

import asyncio
import json
import os
import time

import httpx
import pytest

from djcode import account_auth, auth, config
from djcode import auth_management as manager


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "memory")
    for info in auth.PROVIDERS.values():
        if "env" in info:
            monkeypatch.delenv(info["env"], raising=False)
    monkeypatch.delenv("DJCODE_XAI_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("DJCODE_BASE_URL", raising=False)
    return tmp_path


def saved(value):
    config.save_config(value)


def test_list_reports_sources_and_honest_methods_without_secrets(monkeypatch):
    saved(
        {
            "provider": "openai",
            "openai_api_key": "fixture-stored-secret",
            "custom_providers": {
                "acme": {"api_key": "fixture-custom-secret", "name": "private-display-secret"}
            },
        }
    )
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-env-secret")
    rows = {row["provider"]: row for row in manager.list_auth()}
    assert rows["openai"]["source"] == "stored"
    assert rows["openai"]["environment_present"]
    assert rows["openai"]["active"]
    assert rows["ollama"]["source"] == "local"
    assert rows["acme"]["source"] == "stored"
    assert rows["xai"]["methods"][1]["available"] is False
    assert rows["openai"]["methods"][1]["available"] is False
    assert "fixture-" not in json.dumps(rows)
    assert "private-display-secret" not in json.dumps(rows)


@pytest.mark.parametrize("invalid", [None, 42, {}, [], "", "   ", "invalid\nsecret", "é"])
def test_invalid_stored_key_does_not_shadow_environment(monkeypatch, invalid):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-env")
    cfg = {"openai_api_key": invalid}
    assert auth.get_key_source("openai", cfg) == ("fixture-env", "env", "OPENAI_API_KEY")
    assert manager.auth_status("openai", cfg)["source"] == "env"


def test_custom_and_legacy_precedence(monkeypatch):
    monkeypatch.setenv("DJCODE_API_KEY", "environment")
    monkeypatch.setenv("OPENAI_API_KEY", "other-environment")
    assert auth.get_key_source("acme", {"custom_providers": {"acme": {}}})[0] == "environment"
    assert (
        auth.get_key_source(
            "acme", {"custom_providers": {"acme": {"api_key": "custom"}}, "acme_api_key": "flat"}
        )[0]
        == "custom"
    )
    assert auth.get_key_source("remote", {"remote_api_key": "legacy"})[0] == "legacy"
    assert auth.get_key_source("custom", {"remote_api_key": "legacy"})[0] == "environment"


def test_custom_stored_status_reports_secondary_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fallback-environment")
    cfg = {"custom_providers": {"acme": {"api_key": "stored"}}}
    assert manager.auth_status("acme", cfg)["environment_present"]


def test_login_persists_secret_privately_without_switching_active_model():
    saved({"provider": "ollama", "model": "local", "groq_api_key": "neighbor"})
    result = manager.login_api_key("openai", " new-key ")
    assert result["source"] == "stored"
    assert not result["active"]
    assert "new-key" not in json.dumps(result)
    updated = config.load_config()
    assert updated["provider"] == "ollama" and updated["model"] == "local"
    assert updated["groq_api_key"] == "neighbor" and updated["openai_api_key"] == "new-key"
    assert os.stat(config.CONFIG_FILE).st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "key", ["", " ", None, [], "first\nsecond", "secret\x00", "é", "a" * 16385]
)
def test_bad_key_has_no_effect_and_no_secret_error(key):
    saved({"openai_api_key": "previous"})
    before = config.CONFIG_FILE.read_bytes()
    with pytest.raises(manager.AuthManagementError, match="nonempty") as error:
        manager.login_api_key("openai", key)
    assert "secret" not in str(error.value)
    assert config.CONFIG_FILE.read_bytes() == before


@pytest.mark.parametrize(
    "provider", ["unknown", "../xai", "https://credential-secret@host", {}, None]
)
def test_invalid_provider_is_sanitized_without_effect(provider):
    with pytest.raises(manager.AuthManagementError) as error:
        manager.login_api_key(provider, "fixture-key")
    assert "credential-secret" not in str(error.value)
    assert not config.CONFIG_FILE.exists()


@pytest.mark.parametrize("raw", [b"{bad-json-secret", b"[]", b'{"custom_providers":[]}'])
def test_mutations_refuse_recovery_defaults(raw):
    config.CONFIG_FILE.write_bytes(raw)
    for action in (
        lambda: manager.login_api_key("openai", "key"),
        lambda: manager.logout("openai"),
        lambda: manager.select_auth_method("ollama", "local"),
    ):
        with pytest.raises(manager.AuthManagementError, match="Repair") as error:
            action()
        assert "bad-json-secret" not in str(error.value)
        assert config.CONFIG_FILE.read_bytes() == raw


def test_failed_atomic_save_preserves_config_and_sanitizes(monkeypatch):
    saved({"openai_api_key": "old"})
    before = config.CONFIG_FILE.read_bytes()

    def fail(*args):
        raise OSError("fixture-private-new-key")

    monkeypatch.setattr(config.os, "replace", fail)
    with pytest.raises(manager.AuthManagementError, match="preserved") as error:
        manager.login_api_key("openai", "fixture-private-new-key")
    assert "fixture-private" not in str(error.value)
    assert config.CONFIG_FILE.read_bytes() == before
    assert not list(config.CONFIG_DIR.glob(".config-*"))


def test_logout_one_provider_preserves_neighbors_and_environment(monkeypatch):
    saved(
        {
            "provider": "openai",
            "model": "chosen",
            "openai_api_key": "local",
            "openai_auth_method": "api_key",
            "xai_api_key": "neighbor",
        }
    )
    account_auth._save({"access_token": "account-neighbor"})
    monkeypatch.setenv("OPENAI_API_KEY", "environment")
    result = manager.logout("openai")
    assert result["source"] == "env" and result["environment_present"]
    assert "Environment" in result["message"]
    updated = config.load_config()
    assert "openai_api_key" not in updated and "openai_auth_method" not in updated
    assert updated["xai_api_key"] == "neighbor" and account_auth._path().exists()
    assert updated["provider"] == "openai" and updated["model"] == "chosen"


def test_logout_custom_removes_only_key():
    saved(
        {
            "custom_providers": {
                "acme": {"api_key": "private", "base_url": "https://host/v1", "model": "chosen"},
                "neighbor": {"api_key": "keep"},
            },
            "acme_api_key": "also-private",
        }
    )
    result = manager.logout("acme")
    assert not result["connected"]
    updated = config.load_config()
    assert updated["custom_providers"]["acme"] == {"base_url": "https://host/v1", "model": "chosen"}
    assert updated["custom_providers"]["neighbor"]["api_key"] == "keep"
    assert "acme_api_key" not in updated


def test_logout_account_write_failure_preserves_both_files(monkeypatch):
    saved({"xai_auth_method": "account", "xai_api_key": "private"})
    account_auth._save({"access_token": "private-account"})
    before_config, before_account = (
        config.CONFIG_FILE.read_bytes(),
        account_auth._path().read_bytes(),
    )
    monkeypatch.setattr(
        manager, "_save", lambda *args: (_ for _ in ()).throw(manager.AuthManagementError("failed"))
    )
    with pytest.raises(manager.AuthManagementError):
        manager.logout("xai")
    assert config.CONFIG_FILE.read_bytes() == before_config
    assert account_auth._path().read_bytes() == before_account


def test_logout_unlink_failure_restores_configuration_and_keeps_account(monkeypatch):
    saved({"xai_auth_method": "account", "xai_api_key": "private", "groq_api_key": "neighbor"})
    account_auth._save({"access_token": "private-account"})
    before_config = config.load_config()
    before_account = account_auth._path().read_bytes()

    def fail(provider):
        raise OSError("private-file-error")

    monkeypatch.setattr(account_auth, "forget_account", fail)
    with pytest.raises(manager.AuthManagementError, match="not completed") as error:
        manager.logout("xai")
    assert "private" not in str(error.value)
    assert config.load_config() == before_config
    assert account_auth._path().read_bytes() == before_account


def test_account_mode_does_not_fallback_to_key(monkeypatch):
    cfg = {"provider": "xai", "xai_auth_method": "account", "xai_api_key": "private"}
    monkeypatch.setenv("DJCODE_XAI_OAUTH_CLIENT_ID", "approved-test")
    assert manager.auth_status("xai", cfg)["status"] == "login_required"
    assert not manager.auth_status("xai", cfg)["connected"]


def test_account_status_rejects_gateway_override(monkeypatch):
    monkeypatch.setenv("DJCODE_XAI_OAUTH_CLIENT_ID", "approved-test")
    account_auth._save(
        {"access_token": "access", "client_id": "approved-test", "expires_at": time.time() + 3600}
    )
    cfg = {"provider": "xai", "xai_auth_method": "account"}
    assert manager.auth_status("xai", cfg)["connected"]
    monkeypatch.setenv("DJCODE_BASE_URL", "https://gateway.example/v1")
    assert not manager.auth_status("xai", cfg)["connected"]
    assert "official" in manager.auth_status("xai", cfg)["message"]


def test_invalid_method_is_reported_and_cannot_persist():
    assert manager.auth_status("openai", {"openai_auth_method": []})["status"] == "invalid"
    with pytest.raises(manager.AuthManagementError, match="available"):
        manager.select_auth_method("openai", [])
    assert not config.CONFIG_FILE.exists()


def test_select_local_and_existing_key_without_network(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "environment")
    assert manager.select_auth_method("ollama", "local")["source"] == "local"
    assert manager.select_auth_method("openai", "api_key", activate=True)["active"]
    assert config.load_config()["provider"] == "openai"


def test_account_login_configuration_failure_rolls_back_token_store(monkeypatch):
    saved({"provider": "ollama", "model": "keep"})
    account_auth._path().parent.mkdir()
    previous = b'{"malformed-previous-secret"'
    account_auth._path().write_bytes(previous)
    monkeypatch.setenv("DJCODE_XAI_OAUTH_CLIENT_ID", "approved-test")

    async def begin():
        return account_auth.DeviceSignIn(
            "https://auth.x.ai/device", "USER", "private-device", "approved-test"
        )

    async def finish(device, *, store):
        assert not store
        return {
            "access_token": "new-private-token",
            "client_id": "approved-test",
            "expires_at": time.time() + 3600,
        }

    monkeypatch.setattr(account_auth, "begin_xai_login", begin)
    monkeypatch.setattr(account_auth, "finish_xai_login", finish)
    monkeypatch.setattr(
        manager,
        "_save",
        lambda *args: (_ for _ in ()).throw(manager.AuthManagementError("config failure")),
    )
    before = config.CONFIG_FILE.read_bytes()
    with pytest.raises(manager.AuthManagementError, match="config failure"):
        asyncio.run(manager.login_account("xai", on_status=lambda value: None))
    assert account_auth._path().read_bytes() == previous
    assert config.CONFIG_FILE.read_bytes() == before


def test_account_login_success_returns_only_status(monkeypatch):
    saved({"provider": "ollama"})
    monkeypatch.setenv("DJCODE_XAI_OAUTH_CLIENT_ID", "approved-test")

    async def begin():
        return account_auth.DeviceSignIn(
            "https://auth.x.ai/device", "USER", "private-device", "approved-test"
        )

    async def finish(device, *, store):
        assert not store
        return {
            "access_token": "private-access",
            "client_id": "approved-test",
            "expires_at": time.time() + 3600,
        }

    monkeypatch.setattr(account_auth, "begin_xai_login", begin)
    monkeypatch.setattr(account_auth, "finish_xai_login", finish)
    result = asyncio.run(manager.login_account("xai", on_status=lambda value: None))
    assert result["connected"] and result["source"] == "account"
    assert "private" not in json.dumps(result)
    assert config.load_config()["xai_auth_method"] == "account"


def test_logout_during_refresh_cannot_resurrect_account(monkeypatch):
    monkeypatch.setenv("DJCODE_XAI_OAUTH_CLIENT_ID", "approved-test")
    account_auth._save(
        {
            "access_token": "old",
            "refresh_token": "private-refresh",
            "client_id": "approved-test",
            "expires_at": 0,
        }
    )
    real_client = httpx.AsyncClient

    def response(request):
        account_auth.forget_account("xai")
        return httpx.Response(200, json={"access_token": "new-private", "expires_in": 3600})

    monkeypatch.setattr(
        account_auth.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(response)),
    )
    with pytest.raises(account_auth.AccountAuthError, match="disconnected"):
        asyncio.run(account_auth.account_token("xai", "https://api.x.ai/v1"))
    assert not account_auth._path().exists()
