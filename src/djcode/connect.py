"""Transactional provider → authentication → model onboarding in the terminal.

W8-3: the state machine this screen used to own moved to
``djcode.core.onboarding_flow``. What is left here is a **view** — it draws the
prompt the flow hands it, routes keystrokes back into the flow, and knows
nothing about providers, auth methods, discovery or config files. The
questionary wizard in ``djcode.startup`` is the same flow with a different view,
and W9's REPL wizard will be a third.

Two things that were wrong here and are now structurally impossible:

* ``await asyncio.to_thread(probe, self.candidate)`` — a thread hop around a
  ``probe`` that itself called ``asyncio.run``. The flow is async, so
  ``await flow.discover()`` runs on this screen's own loop.
* ``self.models`` held whatever the probe returned and was indexed as
  ``m["name"]``; a probe that answered with bare strings crashed the screen.
  The flow normalises the catalogue and hands back ready-made options.
"""

from __future__ import annotations

import asyncio
import webbrowser

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from djcode.config import load_config, save_config
from djcode.core.onboarding_flow import PROVIDERS, OnboardingFlow
from djcode.startup import probe_async


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
        # Every port is resolved from THIS module's globals on each call, so the
        # regression suite can still replace `connect.load_config`,
        # `connect.save_config` and `connect.probe_async`.
        self.flow = OnboardingFlow(
            load=lambda: load_config(),
            save=lambda config: save_config(config),
            probe=lambda config: probe_async(config),
        )
        self.busy = False

    # The flow owns this state; these stay as properties because the screen's
    # tests and `app.py` read them, and because a second copy would be a second
    # truth.
    @property
    def candidate(self) -> dict:
        return self.flow.candidate

    @property
    def stage(self) -> str:
        return self.flow.stage

    @property
    def provider(self) -> str:
        return self.flow.provider

    @property
    def models(self) -> list[dict]:
        return self.flow.models

    @property
    def cancelled(self) -> bool:
        return self.flow.cancelled

    def compose(self) -> ComposeResult:
        with Vertical(id="connect-box"):
            yield Label("Connect a model", id="connect-title")
            yield Static("1 Provider  →  2 Sign in  →  3 Model", id="connect-detail", markup=False)
            yield OptionList(id="connect-options")
            yield Input(placeholder="Search providers", id="connect-input")
            yield Button("Cancel", id="connect-cancel")

    def on_mount(self):
        self.flow.start()
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

    def _show(self, prompt) -> None:
        """Draw whatever the flow just asked for.

        Named ``_show`` and not ``_render``: ``textual.Widget._render`` exists
        and is called by the compositor with no arguments.
        """
        self.query_one("#connect-detail", Static).update(prompt.detail)
        field = self.query_one(Input)
        field.password = prompt.password
        field.placeholder = prompt.placeholder
        field.value = ""
        field.focus()
        if prompt.stage == "provider":
            self._providers("")
        elif prompt.stage == "unverified":
            self._options([("yes", "Save this setup anyway"), ("no", "Cancel")])
        else:
            self._options(prompt.options)

    @on(Input.Changed, "#connect-input")
    def filter(self, event):
        if self.stage == "provider":
            self._providers(event.value)
        elif self.stage == "model":
            query = event.value.lower()
            names = [m["name"] for m in self.models if isinstance(m, dict) and "name" in m]
            matched = [name for name in names if query in name.lower()]
            self._options([(name, name) for name in matched[:100]])

    @on(OptionList.OptionSelected)
    async def choose(self, event):
        if not self.busy:
            await self._choose(event.option.id)

    async def _choose(self, value):
        try:
            stage = self.flow.stage
            if stage == "provider":
                prompt = await self.flow.choose_provider(value)
            elif stage == "auth":
                prompt = await self.flow.choose_auth(value)
                if prompt.stage == "browser":
                    self._show(prompt)
                    await asyncio.to_thread(webbrowser.open, prompt.url)
                    return
                if prompt.stage == "account":
                    self._show(prompt)
                    self.run_worker(self._device_login(), group="connect", exclusive=True)
                    return
            elif stage == "model":
                prompt = await self.flow.choose_model(value)
            elif stage == "unverified":
                prompt = self.flow.confirm_unverified(value == "yes")
            else:
                return
            self._settle(prompt)
        except Exception as error:
            self.query_one("#connect-detail", Static).update(str(error))

    def _settle(self, prompt) -> None:
        if prompt.stage == "done":
            self.dismiss(self.flow.candidate)
            return
        self._show(prompt)

    async def _device_login(self):
        self.busy = True
        try:

            def announce(device):
                self.query_one("#connect-detail", Static).update(
                    f"Open {device.verification_url}\n"
                    f"Enter {device.user_code}. Waiting for authorization…"
                )
                return asyncio.to_thread(webbrowser.open, device.verification_url)

            self._settle(await self.flow.device_login(announce))
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
            stage = self.flow.stage
            if stage in {"provider", "auth", "unverified"}:
                options = self.query_one(OptionList)
                if options.highlighted is not None:
                    await self._choose(options.get_option_at_index(options.highlighted).id)
                return
            self.busy = True
            if stage == "browser":
                self._settle(await self.flow.submit_browser_code(value))
            elif stage == "key":
                self._settle(await self.flow.submit_key(value))
            elif stage == "url":
                self._settle(await self.flow.submit_url(value))
            elif stage == "model" and value:
                self._settle(await self.flow.choose_model(value))
        except Exception as error:
            self.query_one("#connect-detail", Static).update(str(error))
        finally:
            self.busy = False

    @on(Button.Pressed, "#connect-cancel")
    def cancel_button(self):
        self.action_cancel()

    def action_cancel(self):
        self.flow.cancel()
        self.workers.cancel_all()
        self.dismiss(None)
