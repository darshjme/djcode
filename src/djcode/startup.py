"""Bounded provider discovery and an explicit, configuration-preserving setup flow.

Async contract (W1-7)
---------------------
Every network-touching entry point exists twice: a coroutine holding the real
implementation (``discover_async``, ``probe_async``, ``prepare_async``) and a
synchronous wrapper under the bare name. The wrapper drives the coroutine with
:func:`asyncio.run` and **refuses**, with a message naming its ``_async`` twin,
when a loop is already running in the calling thread.

The refusal is deliberate and must not be softened into a silent
``asyncio.to_thread`` hop: a sync function that secretly moves work onto a
worker thread hides a thread-safety contract from its caller, which is exactly
how "questionary driven from a worker thread while prompt_toolkit owns stdin"
got written. Callers already inside a loop either await the ``_async`` form, or
hop to a thread themselves where that is what they actually mean.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys

import click
import httpx
import questionary
from rich.console import Console

from djcode.config import CONFIG_FILE, load_config, save_config
from djcode.core.onboarding_flow import (
    OnboardingCancelled,
    OnboardingError,
    OnboardingFlow,
)
from djcode.core.onboarding_flow import catalogue as _catalogue  # noqa: F401 re-export (W8)
from djcode.core.onboarding_flow import connection as connection  # noqa: PLC0414 re-export
from djcode.core.onboarding_flow import discover_async as _discover_async
from djcode.core.onboarding_flow import probe_async as _probe_async

console = Console(stderr=True)
DISCOVERY_DEADLINE = 5.0
DISCOVERY_SIZE_BUDGET = 2 * 1024 * 1024

# DJcode ships no telemetry, and says so to every library and child process that
# honours the convention. `setdefault`, never assignment: a user who deliberately
# exported DO_NOT_TRACK=0 for some downstream tool keeps their choice.
os.environ.setdefault("DO_NOT_TRACK", "1")


def _refuse_inside_loop(sync_name: str, async_name: str) -> None:
    """Return if this thread may block; otherwise raise, naming the coroutine to await."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        f"startup.{sync_name}() is blocking and cannot run inside an event loop. "
        f"Use 'await startup.{async_name}(...)' or "
        f"'await asyncio.to_thread(startup.{sync_name}, ...)'."
    )


async def discover_async(endpoint: str, headers: dict) -> httpx.Response:
    """Bounded, cancellable, loop-native discovery.

    The implementation moved to ``djcode.core.onboarding_flow`` in W8 -- core
    may not import this module, which pulls in click, questionary and rich at
    import time, so the dependency had to point the other way. The budgets stay
    readable (and patchable) HERE and are passed down per call, because
    ``DISCOVERY_DEADLINE`` is a knob callers tighten on this module.
    """
    return await _discover_async(
        endpoint, headers, deadline=DISCOVERY_DEADLINE, size_budget=DISCOVERY_SIZE_BUDGET
    )


def discover(endpoint: str, headers: dict) -> httpx.Response:
    """Blocking wrapper around discover_async; refuses inside a running loop."""
    _refuse_inside_loop("discover", "discover_async")
    return asyncio.run(discover_async(endpoint, headers))


async def probe_async(config: dict, provider: str | None = None, model: str | None = None) -> dict:
    """Check the configured provider/model and return the discovered model catalogue."""

    async def _discover(endpoint: str, headers: dict) -> httpx.Response:
        # Resolved from this module's globals on every call, so replacing
        # `startup.discover_async` still redirects discovery -- the seam the
        # regression suite drives the whole setup flow through.
        return await discover_async(endpoint, headers)

    return await _probe_async(config, provider, model, discover=_discover)


def probe(config: dict, provider: str | None = None, model: str | None = None) -> dict:
    """Blocking wrapper around probe_async; refuses inside a running loop."""
    _refuse_inside_loop("probe", "probe_async")
    return asyncio.run(probe_async(config, provider, model))


def answer(value):
    if value is None:
        raise KeyboardInterrupt("Setup cancelled; existing configuration retained")
    return value


async def _prompt(widget):
    """Run one questionary widget and refuse ``None`` the way ``answer`` always has.

    ``ask_async`` when the widget has it, ``ask`` when it does not. The
    distinction is not cosmetic: ``setup`` now drives an async state machine, so
    the blocking ``ask()`` -- which calls ``Application.run()`` and therefore
    ``run_until_complete`` on the loop it is already inside -- would raise
    "this event loop is already running" (B21) the first time a real user ran
    it. Every test double in the suite supplies only ``ask``, so both branches
    are live and both are exercised.
    """
    runner = getattr(widget, "ask_async", None)
    if inspect.iscoroutinefunction(runner):
        return answer(await runner())
    # `iscoroutinefunction`, not `is not None`: a `unittest.Mock` auto-creates
    # an `ask_async` attribute that is not awaitable, and every scripted answer
    # in the regression suite is a Mock.
    return answer(widget.ask())


def setup(existing: dict | None = None) -> dict:
    """The questionary VIEW over :class:`djcode.core.OnboardingFlow` (W8-4).

    Every state transition, the candidate-config deepcopy, the
    probe-before-commit guard and the cancel-preserves-config rule now live in
    core and are shared with ``connect.ConnectScreen``; what is left here is
    asking the questions and translating core's errors into the exceptions this
    function's callers have always caught -- ``KeyboardInterrupt`` for a
    cancellation, ``click.ClickException`` for a refusal.

    Still synchronous at the boundary because ``prepare_async`` reaches it
    through ``asyncio.to_thread`` and questionary owns stdin; W9 replaces this
    view with ``frontends/repl/setup.py`` and the hop disappears with it.
    """
    return asyncio.run(_setup_async(existing))


async def _setup_async(existing: dict | None = None) -> dict:
    flow = OnboardingFlow(
        existing if existing is not None else load_config(),
        # Resolved from this module's globals on every call. `save_config`,
        # `load_config` and `probe_async` are all seams the regression suite
        # replaces on `djcode.startup`, and a direct reference captured at
        # construction time would make every one of those patches inert.
        load=lambda: load_config(),
        save=lambda config: save_config(config),
        probe=lambda config: probe_async(config),
    )
    console.print("\n[bold]DJcode setup[/] · project by Darshan Kumar Joshi")
    console.print("[dim]Choose a provider, authentication method and model. No model downloads.[/]")
    try:
        prompt = flow.start()
        while prompt.stage != "done":
            prompt = await _answer_stage(flow, prompt)
    except OnboardingCancelled as error:
        raise KeyboardInterrupt(str(error)) from None
    except OnboardingError as error:
        raise click.ClickException(str(error)) from None
    console.print("[green]Setup saved.[/]")
    return flow.candidate


async def _answer_stage(flow: OnboardingFlow, prompt) -> object:
    """Ask the one question ``prompt`` describes and hand the answer back."""
    if prompt.stage == "provider":
        choices = [questionary.Choice(label, value=name) for name, label in prompt.options]
        return await flow.choose_provider(
            await _prompt(questionary.select("Provider", choices=choices))
        )
    if prompt.stage == "url":
        endpoint = await _prompt(questionary.text("API endpoint", default=prompt.default))
        return await flow.submit_url(endpoint.strip())
    if prompt.stage == "auth":
        for label, reason in prompt.unavailable:
            console.print(f"[dim]{label}: {reason}[/]")
        choices = [questionary.Choice(label, value=ident) for ident, label in prompt.options]
        method = await _prompt(
            questionary.select("Authentication", choices=choices, default=prompt.default)
        )
        return await flow.choose_auth(method)
    if prompt.stage == "browser":
        import webbrowser

        console.print(prompt.url, markup=False)
        webbrowser.open(prompt.url)
        code = await _prompt(questionary.password("One-time code from OpenRouter"))
        return await flow.submit_browser_code(code)
    if prompt.stage == "account":

        def announce(device) -> None:
            console.print(
                f"Open {device.verification_url} and enter code: {device.user_code}", markup=False
            )
            console.print("Waiting for authorization…", markup=False)

        return await flow.device_login(announce)
    if prompt.stage == "key":
        key = await _prompt(
            questionary.password("API key (leave blank to keep existing/environment key)")
        )
        return await flow.submit_key(key.strip())
    if prompt.stage == "model":
        names = [name for name, _ in prompt.options]
        if names:
            chosen = await _prompt(
                questionary.autocomplete(
                    "Model",
                    choices=names,
                    default=prompt.default,
                    ignore_case=True,
                    match_middle=True,
                )
            )
        else:
            console.print(flow.message, markup=False)
            chosen = await _prompt(questionary.text("Exact model ID", default=prompt.default))
        return await flow.choose_model(chosen.strip())
    if prompt.stage == "unverified":
        accepted = await _prompt(questionary.confirm(prompt.detail, default=False))
        return flow.confirm_unverified(bool(accepted))
    raise click.ClickException(
        f"Unknown setup stage {prompt.stage!r}; configuration was not saved."
    )


async def prepare_async(
    provider=None, model=None, *, force_setup=False
) -> tuple[str | None, str | None]:
    """Resolve provider/model for this run, repairing setup only when it is broken."""
    if os.environ.get("DJCODE_SKIP_STARTUP_CHECK") == "1" and not force_setup:
        return provider, model
    config = load_config()
    interactive = sys.stdin.isatty()
    first_run = not CONFIG_FILE.exists() and not provider
    checked = (
        await probe_async(config, provider, model)
        if not force_setup and not first_run
        else {"status": "missing", "message": "Configure a provider and model."}
    )
    if force_setup or checked["status"] == "missing":
        if not interactive:
            raise click.ClickException(
                checked["message"]
                + " Run djcode --setup in an interactive terminal, or supply valid provider/model "
                "credentials."
            )
        # The FLOW is async end to end after W8; this hop remains because the
        # questionary VIEW still owns stdin and blocks. W9 replaces the view
        # with `frontends/repl/setup.py` and the hop goes with it.
        configured = await asyncio.to_thread(setup, config)
        return configured["provider"], configured["model"]
    if checked["status"] != "ready":
        console.print(checked["message"], markup=False)
    return provider, model


def prepare(provider=None, model=None, *, force_setup=False) -> tuple[str | None, str | None]:
    """Blocking wrapper around prepare_async; refuses inside a running loop."""
    _refuse_inside_loop("prepare", "prepare_async")
    return asyncio.run(prepare_async(provider, model, force_setup=force_setup))
