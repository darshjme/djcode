"""Startup should repair missing setup without destroying working offline setup."""
from copy import deepcopy
from unittest.mock import Mock

import click
import httpx
import pytest

from djcode import startup


@pytest.fixture
def cfg():
    return {"provider": "openai", "model": "chosen-model", "openai_api_key": "test-key"}


def reply(monkeypatch, status=200, payload=None):
    response = httpx.Response(status, json=payload or {}, request=httpx.Request("GET", "https://example.com/models"))
    monkeypatch.setattr(startup, "discover", lambda *args: response)


def test_ready_only_when_model_is_listed(monkeypatch, cfg):
    reply(monkeypatch, payload={"data": [{"id": "chosen-model"}]})
    assert startup.probe(cfg)["status"] == "ready"
    cfg["model"] = "missing"
    assert startup.probe(cfg)["status"] == "missing"


def test_auth_rejection_needs_setup(monkeypatch, cfg):
    reply(monkeypatch, 401)
    assert startup.probe(cfg)["status"] == "missing"
    cfg["openai_api_key"] = ""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert startup.probe(cfg)["status"] == "missing"


def test_offline_preserves_existing_config(monkeypatch, cfg, tmp_path):
    original = deepcopy(cfg)
    monkeypatch.delenv("DJCODE_SKIP_STARTUP_CHECK", raising=False)
    config_file = tmp_path / "config.json"
    config_file.write_text("{}")
    monkeypatch.setattr(startup, "CONFIG_FILE", config_file)
    monkeypatch.setattr(startup, "load_config", lambda: cfg)
    monkeypatch.setattr(startup, "discover", Mock(side_effect=httpx.ConnectError("offline")))
    monkeypatch.setattr(startup, "setup", Mock(side_effect=AssertionError("must not reset offline setup")))
    assert startup.prepare() == (None, None)
    assert cfg == original
    cfg["model"] = ""
    assert startup.probe(cfg)["status"] == "missing"


def test_noninteractive_first_run_is_actionable(monkeypatch, tmp_path):
    monkeypatch.delenv("DJCODE_SKIP_STARTUP_CHECK", raising=False)
    monkeypatch.setattr(startup, "CONFIG_FILE", tmp_path / "absent")
    monkeypatch.setattr(startup.sys.stdin, "isatty", lambda: False)
    with pytest.raises(click.ClickException, match="djcode --setup"):
        startup.prepare()


def test_interactive_missing_setup_returns_new_selection(monkeypatch, tmp_path):
    monkeypatch.delenv("DJCODE_SKIP_STARTUP_CHECK", raising=False)
    monkeypatch.setattr(startup, "CONFIG_FILE", tmp_path / "absent")
    monkeypatch.setattr(startup.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(startup, "setup", lambda config: {"provider": "xai", "model": "grok"})
    assert startup.prepare() == ("xai", "grok")


def test_cancelled_setup_does_not_save(monkeypatch, cfg):
    before = deepcopy(cfg)
    monkeypatch.setattr(startup.questionary, "select", lambda *a, **k: Mock(ask=lambda: None))
    save = Mock()
    monkeypatch.setattr(startup, "save_config", save)
    with pytest.raises(KeyboardInterrupt):
        startup.setup(cfg)
    save.assert_not_called()
    assert cfg == before


def test_colibri_override_uses_served_default_model(cfg):
    assert startup.connection(cfg, "colibri")["model"] == "djcode-colibri"
    assert startup.connection(cfg, "colibri", "explicit")["model"] == "explicit"


def test_model_discovery_unsupported_requires_explicit_model(monkeypatch, cfg):
    reply(monkeypatch, 404)
    assert startup.probe(cfg)["status"] == "unverified"
    cfg["model"] = ""
    assert startup.probe(cfg)["status"] == "missing"


def test_account_auth_never_forwarded_to_gateway(monkeypatch, cfg):
    cfg.update(provider="xai", xai_auth_method="account", base_url="https://gateway.example/v1")
    discover = Mock(side_effect=AssertionError("must not contact gateway"))
    monkeypatch.setattr(startup, "discover", discover)
    assert startup.probe(cfg)["status"] == "missing"
    discover.assert_not_called()


def test_nested_configuration_redacts_secrets():
    from djcode.cli import redact_config
    config = {"custom_providers": {"example": {"api_key": "private", "model": "public"}}, "refresh_token": "private"}
    redacted = redact_config(config)
    assert "private" not in str(redacted)
    assert "public" in str(redacted)
    assert config["refresh_token"] == "private"
