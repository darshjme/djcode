"""Transactional provider → authentication → model onboarding in the terminal."""

from __future__ import annotations

import asyncio
import webbrowser
from copy import deepcopy

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from djcode.account_auth import auth_methods, begin_xai_login, finish_xai_login
from djcode.auth import PROVIDERS
from djcode.config import load_config, save_config
from djcode.startup import connection, probe


class ConnectScreen(ModalScreen[dict | None]):
    DEFAULT_CSS = """
    ConnectScreen { align: center middle; background: rgba(0,0,0,0.65); }
    #connect-box { width: 76; max-width: 95%; height: auto; max-height: 95%;
        border: round #C79B7A; background: #17191D; padding: 1 2; }
    #connect-title { height: 2; color: #EEEAE3; text-style: bold; }
    #connect-detail { height: auto; max-height: 5; color: #A6A4A0; margin-bottom: 1; }
    #connect-options { height: 10; max-height: 40%; border: none; }
    #connect-input { height: 3; margin-top: 1; border: round #55514A; color: #EEEAE3; }
    #connect-input:focus { border: round #C79B7A; }
    #connect-options > .option-list--option-highlighted { background: #A2BA9A 15%; color: #A2BA9A; }
    #connect-cancel { height: 3; margin-top: 1; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self):
        super().__init__()
        self.candidate = deepcopy(load_config())
        self.stage = "provider"
        self.provider = ""
        self.verifier = ""
        self.models = []
        self.busy = False
        self.cancelled = False

    def compose(self) -> ComposeResult:
        with Vertical(id="connect-box"):
            yield Label("Connect a model", id="connect-title")
            yield Static("1 Provider  →  2 Sign in  →  3 Model", id="connect-detail", markup=False)
            yield OptionList(id="connect-options")
            yield Input(placeholder="Search providers", id="connect-input")
            yield Button("Cancel", id="connect-cancel")

    def on_mount(self):
        self._providers("")
        self.query_one(Input).focus()

    def on_key(self, event):
        if event.key in {"up", "down"}:
            options = self.query_one(OptionList)
            if options.option_count:
                options.action_cursor_down() if event.key == "down" else options.action_cursor_up()
                event.prevent_default()
                event.stop()

    def _options(self, items):
        options = self.query_one(OptionList)
        options.clear_options()
        options.add_options([Option(label, id=ident) for ident, label in items])
        if options.option_count:
            options.highlighted = 0

    def _providers(self, query):
        names = list(
            dict.fromkeys(
                [
                    "openrouter",
                    "openai",
                    "anthropic",
                    *PROVIDERS,
                    *self.candidate.get("custom_providers", {}),
                ]
            )
        )
        self._options(
            [
                (name, f"{PROVIDERS.get(name, {}).get('name', name)} · {name}")
                for name in names
                if query.lower() in name.lower()
            ]
        )

    def _field(self, stage, detail, placeholder, password=False):
        self.stage = stage
        self.query_one("#connect-detail", Static).update(detail)
        field = self.query_one(Input)
        field.password = password
        field.placeholder = placeholder
        field.value = ""
        field.focus()

    @on(Input.Changed, "#connect-input")
    def filter(self, event):
        if self.stage == "provider":
            self._providers(event.value)
        elif self.stage == "model":
            self._options([(m, m) for m in self.models if event.value.lower() in m.lower()][:100])

    @on(OptionList.OptionSelected)
    async def choose(self, event):
        if not self.busy:
            await self._choose(event.option.id)

    async def _choose(self, value):
        if self.stage == "provider":
            self.provider = value
            if self.candidate.get("provider") != value:
                self.candidate.update(provider=value, model="", base_url="")
            info = PROVIDERS.get(value, {})
            if info.get("needs_key", True):
                self._field(
                    "auth",
                    f"Connect {value}: choose how to authenticate.",
                    "Choose an option above",
                )
                self._options(
                    [(m["id"], m["label"]) for m in auth_methods(value) if m["available"]]
                )
            elif not connection(self.candidate)["base"]:
                self._options([])
                self._field("url", "Enter your server URL.", "http://localhost:8000/v1")
            else:
                await self._discover()
        elif self.stage == "auth":
            self.candidate[f"{self.provider}_auth_method"] = (
                "api_key" if value == "browser" else value
            )
            self._options([])
            if value == "browser":
                from djcode.openrouter_auth import begin

                self.verifier, url = begin()
                self._field(
                    "browser",
                    f"Authorize DJcode in your browser, then paste the one-time code.\n{url}",
                    "One-time authorization code",
                    True,
                )
                await asyncio.to_thread(webbrowser.open, url)
            elif value == "account":
                self.run_worker(self._device_login(), group="connect", exclusive=True)
            else:
                self._field(
                    "key",
                    (
                        "API key stays in your local DJcode configuration. Blank keeps an "
                        "existing key."
                    ),
                    "API key",
                    True,
                )
        elif self.stage == "model":
            self.candidate["model"] = value
            await self._finish()

    async def _device_login(self):
        self.busy = True
        try:
            device = await begin_xai_login()
            self.query_one("#connect-detail", Static).update(
                f"Open {device.verification_url}\n"
                f"Enter {device.user_code}. Waiting for authorization…"
            )
            await asyncio.to_thread(webbrowser.open, device.verification_url)
            await finish_xai_login(device)
            await self._discover()
        except Exception as error:
            self.query_one("#connect-detail", Static).update(str(error))
        finally:
            self.busy = False

    @on(Input.Submitted, "#connect-input")
    async def submit(self, event):
        if self.busy:
            return
        value = event.value.strip()
        try:
            if self.stage in {"provider", "auth"}:
                options = self.query_one(OptionList)
                if options.highlighted is not None:
                    await self._choose(options.get_option_at_index(options.highlighted).id)
            elif self.stage == "browser":
                from djcode.openrouter_auth import exchange

                self.busy = True
                self.candidate[f"{self.provider}_api_key"] = await exchange(value, self.verifier)
                self.verifier = ""
                await self._discover()
            elif self.stage == "key":
                if value:
                    self.candidate[f"{self.provider}_api_key"] = value
                if not connection(self.candidate)["key"]:
                    raise ValueError("An API key is required")
                if not connection(self.candidate)["base"]:
                    self._field("url", "Enter the provider endpoint.", "https://server.example/v1")
                else:
                    await self._discover()
            elif self.stage == "url":
                self.candidate[f"{self.provider}_url"] = value
                await self._discover()
            elif self.stage == "model" and value:
                self.candidate["model"] = value
                await self._finish()
        except Exception as error:
            self.query_one("#connect-detail", Static).update(str(error))
        finally:
            self.busy = False

    async def _discover(self):
        self.query_one(Input).value = ""
        self.query_one("#connect-detail", Static).update("Discovering models…")
        found = await asyncio.to_thread(probe, self.candidate)
        if self.cancelled:
            return
        self.models = found.get("models", [])
        self._field(
            "model",
            f"Choose a model from {self.provider}, or enter its exact ID.\n{found['message']}",
            "Search or enter model ID",
        )
        self._options([(m, m) for m in self.models[:100]])

    async def _finish(self):
        self.busy = True
        try:
            checked = await asyncio.to_thread(probe, self.candidate)
            if self.cancelled:
                return
            if checked["status"] != "ready":
                raise ValueError(
                    checked["message"] + " Setup has not been saved; retry when reachable."
                )
            self.candidate["setup_complete"] = True
            save_config(self.candidate)
            self.dismiss(self.candidate)
        finally:
            self.busy = False

    @on(Button.Pressed, "#connect-cancel")
    def cancel_button(self):
        self.action_cancel()

    def action_cancel(self):
        self.cancelled = True
        self.workers.cancel_all()
        self.verifier = ""
        self.dismiss(None)
