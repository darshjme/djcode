"""Provider selection preserves explicit auth methods without real network/login."""

import asyncio
from types import SimpleNamespace

import pytest
from textual.app import App
from textual.widgets import OptionList

from djcode import account_auth, auth, config
from djcode.app import ProviderPicker


@pytest.fixture
def settings(monkeypatch):
    value = {"provider": "xai", "model": "test-grok", "xai_auth_method": "account"}
    monkeypatch.setattr(auth, "load_config", lambda: dict(value))
    monkeypatch.setattr(config, "load_config", lambda: dict(value))
    monkeypatch.setattr(config, "save_config", lambda updated: value.update(updated))
    monkeypatch.setattr(account_auth, "has_account", lambda provider: provider == "xai")
    monkeypatch.setenv("DJCODE_XAI_OAUTH_CLIENT_ID", "test-approved-client")
    monkeypatch.setattr(auth, "get_api_key", lambda provider: "")
    return value


def answers(monkeypatch, items):
    values = iter(items)
    monkeypatch.setattr(
        auth.questionary, "select", lambda *a, **kw: SimpleNamespace(ask=lambda: next(values))
    )


def test_classic_connected_account_needs_no_password_or_login(settings, monkeypatch):
    answers(monkeypatch, ["xai", "account"])
    monkeypatch.setattr(auth.questionary, "password", lambda *a, **kw: pytest.fail("password"))
    monkeypatch.setattr(account_auth, "authenticate_account", lambda *a, **kw: pytest.fail("login"))
    assert auth.interactive_auth() == "xai"
    assert settings["xai_auth_method"] == "account"


def test_classic_cancel_auth_keeps_account(settings, monkeypatch):
    answers(monkeypatch, ["xai", None])
    before = dict(settings)
    assert auth.interactive_auth() is None
    assert settings == before


def test_classic_explicit_key_switch_is_persisted(settings, monkeypatch):
    answers(monkeypatch, ["xai", "api_key"])
    monkeypatch.setattr(
        auth.questionary, "password", lambda *a, **kw: SimpleNamespace(ask=lambda: "new-key")
    )
    assert auth.interactive_auth() == "xai"
    assert settings["xai_auth_method"] == "api_key"
    assert settings["xai_api_key"] == "new-key"


class PickerApp(App):
    def on_mount(self):
        self.result = None
        self.push_screen(ProviderPicker(), self.selected)

    def selected(self, result):
        self.result = result


def select(screen, name):
    options = screen.query_one("#provider-list", OptionList)
    option = options.get_option(name)
    screen.select_provider(
        OptionList.OptionSelected(options, option, options.get_option_index(name))
    )


@pytest.mark.parametrize("width", [60, 100])
def test_native_reuses_account_and_preserves_method(settings, monkeypatch, width):
    monkeypatch.setattr(account_auth, "begin_xai_login", lambda: pytest.fail("login"))

    async def run():
        app = PickerApp()
        async with app.run_test(size=(width, 28)) as pilot:
            await pilot.pause()
            screen = app.screen
            box = screen.query_one("#provider-box")
            assert box.region.right <= width and box.region.bottom <= 28
            select(screen, "xai")
            await pilot.pause()
            assert screen._mode == "auth"
            await pilot.press("enter")
            await pilot.pause()
            assert app.result == {"provider": "xai", "auth_method": "account"}
            assert settings["xai_auth_method"] == "account"

    asyncio.run(run())


def test_native_unavailable_methods_disabled(settings, monkeypatch):
    monkeypatch.delenv("DJCODE_XAI_OAUTH_CLIENT_ID")

    async def run():
        app = PickerApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            select(screen, "anthropic")
            methods = screen.query_one("#provider-list", OptionList)
            assert methods.get_option("account").disabled
            assert "Anthropic prohibits" in str(methods.get_option("account").prompt)
            await pilot.press("escape")
            await pilot.pause()
            assert settings["provider"] == "xai"

    asyncio.run(run())


def test_native_device_wait_escape_cancels(settings, monkeypatch):
    monkeypatch.setattr(account_auth, "has_account", lambda p: False)
    started, cancelled = [], []

    async def begin():
        return SimpleNamespace(verification_url="https://auth.x.ai/device", user_code="TEST")

    async def finish(device):
        started.append(True)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(account_auth, "begin_xai_login", begin)
    monkeypatch.setattr(account_auth, "finish_xai_login", finish)

    async def run():
        app = PickerApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            select(app.screen, "xai")
            select(app.screen, "account")
            await pilot.pause()
            assert started
            await pilot.press("escape")
            await pilot.pause()
            assert cancelled
            assert app.result is None
            assert settings["xai_auth_method"] == "account"

    asyncio.run(run())
