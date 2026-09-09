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


def test_real_loopback_model_discovery():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/api/tags"
            body = b'{"models":[{"name":"existing:latest"}]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = {"provider": "ollama", "model": "existing", "ollama_url": f"http://127.0.0.1:{server.server_port}"}
        assert startup.probe(config)["status"] == "ready"
        config["model"] = "not-installed"
        assert startup.probe(config)["status"] == "missing"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_account_refresh_failure_requires_nonblank_model(monkeypatch):
    from djcode import account_auth
    monkeypatch.setattr(account_auth, "has_account", lambda provider: True)
    monkeypatch.setattr(account_auth, "get_account_token", Mock(side_effect=account_auth.AccountAuthError("offline")))
    config = {"provider": "xai", "model": "grok", "xai_auth_method": "account"}
    assert startup.probe(config)["status"] == "offline"
    for model in ("", "   ", None):
        config["model"] = model
        assert startup.probe(config)["status"] == "missing"


def test_empty_model_still_discovers_choices(monkeypatch, cfg):
    cfg["model"] = ""
    reply(monkeypatch, payload={"data": [{"id": "available-model"}]})
    result = startup.probe(cfg)
    assert result["status"] == "missing"
    assert result["models"] == ["available-model"]


def test_setup_reuses_account_and_defaults_current_method(monkeypatch):
    from djcode import account_auth
    config = {"provider": "xai", "model": "grok", "xai_auth_method": "account"}
    before = deepcopy(config)
    calls = []
    def select(label, **kwargs):
        calls.append((label, kwargs))
        return Mock(ask=lambda: "xai" if label == "Provider" else kwargs["default"])
    monkeypatch.setenv("DJCODE_XAI_OAUTH_CLIENT_ID", "test-registration")
    monkeypatch.setattr(startup.questionary, "select", select)
    monkeypatch.setattr(startup.questionary, "autocomplete", lambda *a, **kw: Mock(ask=lambda: "grok"))
    monkeypatch.setattr(startup.questionary, "password", Mock(side_effect=AssertionError("no password needed")))
    monkeypatch.setattr(account_auth, "has_account", lambda provider: True)
    login = Mock(side_effect=AssertionError("connected account must not log in again"))
    monkeypatch.setattr(account_auth, "authenticate_account", login)
    monkeypatch.setattr(startup, "probe", lambda *a: {"status": "ready", "models": ["grok"]})
    saved = Mock()
    monkeypatch.setattr(startup, "save_config", saved)
    result = startup.setup(config)
    assert result["xai_auth_method"] == "account"
    assert config == before
    assert calls[1][1]["default"] == "account"
    saved.assert_called_once_with(result)
    login.assert_not_called()


def test_cancel_after_new_key_does_not_save_or_mutate(monkeypatch, cfg):
    before = deepcopy(cfg)
    selections = iter(["openai", "api_key"])
    monkeypatch.setattr(startup.questionary, "select", lambda *a, **kw: Mock(ask=lambda: next(selections)))
    monkeypatch.setattr(startup.questionary, "password", lambda *a, **kw: Mock(ask=lambda: "replacement-key"))
    monkeypatch.setattr(startup.questionary, "autocomplete", lambda *a, **kw: Mock(ask=lambda: None))
    monkeypatch.setattr(startup, "probe", lambda *a: {"status": "ready", "models": ["chosen-model"]})
    saved = Mock()
    monkeypatch.setattr(startup, "save_config", saved)
    with pytest.raises(KeyboardInterrupt):
        startup.setup(cfg)
    saved.assert_not_called()
    assert cfg == before


def test_discovery_total_deadline_cancels_slow_body(monkeypatch):
    import asyncio
    from time import monotonic
    closed = []
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b" "
                await asyncio.sleep(0.03)
        async def aclose(self):
            closed.append(True)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(startup, "DISCOVERY_DEADLINE", 0.08)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=SlowBody()))
    monkeypatch.setattr(startup.httpx, "AsyncClient", lambda **kw: original_client(transport=transport))
    started = monotonic()
    with pytest.raises(ValueError, match="time budget"):
        startup.discover("https://example.com/models", {})
    assert monotonic() - started < 1.0
    assert closed


def test_discovery_body_size_cap(monkeypatch):
    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1)))
    monkeypatch.setattr(startup.httpx, "AsyncClient", lambda **kw: original_client(transport=transport))
    with pytest.raises(ValueError, match="size budget"):
        startup.discover("https://example.com/models", {})
