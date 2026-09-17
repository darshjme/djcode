"""W8-3: the onboarding state machine runs inside a live loop, headless.

The gate this file enforces, in the blueprint's own words: *the full flow runs
inside a live event loop with no ``asyncio.to_thread`` and no ``RuntimeWarning``*.
Both halves are checked literally -- ``asyncio.to_thread`` is replaced with a
failing stub for the duration of the run, and the flow module is swept with
``ast`` so a later edit cannot reintroduce the hop out of sight of a test.
"""

from __future__ import annotations

import ast
import asyncio
import gc
import warnings
from pathlib import Path

import httpx
import pytest

from djcode.core.onboarding_flow import (
    OnboardingCancelled,
    OnboardingError,
    OnboardingFlow,
    catalogue,
    connection,
    probe_async,
)

FLOW_SOURCE = (
    Path(__file__).resolve().parent.parent / "src" / "djcode" / "core" / "onboarding_flow.py"
)


@pytest.fixture
def no_runtime_warning():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        yield
        gc.collect()
    leaked = [str(item.message) for item in caught if issubclass(item.category, RuntimeWarning)]
    assert not leaked, f"RuntimeWarning leaked: {leaked}"


@pytest.fixture
def no_thread_hop(monkeypatch):
    """Any ``asyncio.to_thread`` during the flow fails the test outright."""

    def refuse(*args, **kwargs):
        raise AssertionError(
            "the onboarding flow must not hop to a thread; it is async end to end"
        )

    monkeypatch.setattr(asyncio, "to_thread", refuse)


def _probe(models, status="ready", message="Connected"):
    calls = []

    async def probe(config):
        calls.append(dict(config))
        return {
            "status": status,
            "message": message,
            "models": [{"name": name, "size": 0} for name in models],
            "provider": config.get("provider", ""),
        }

    return probe, calls


# ── The full run ───────────────────────────────────────────────────────────


def test_full_flow_runs_in_a_live_loop_without_threads(no_runtime_warning, no_thread_hop):
    saved: list[dict] = []
    probe, probed = _probe(["gpt-4o", "o3"])

    async def run():
        flow = OnboardingFlow(
            {"provider": "ollama", "model": "old"},
            save=saved.append,
            probe=probe,
            methods=lambda provider: [
                {"id": "api_key", "label": "API key", "available": True, "reason": ""},
                {"id": "account", "label": "Account", "available": False, "reason": "no client id"},
            ],
        )
        prompt = flow.start()
        assert prompt.stage == "provider"
        assert ("openai", "OpenAI") in prompt.options

        prompt = await flow.choose_provider("openai")
        assert prompt.stage == "auth"
        # Methods that exist but cannot be used are reported, not hidden.
        assert prompt.unavailable == [("Account", "no client id")]
        assert prompt.options == [("api_key", "API key")]

        prompt = await flow.choose_auth("api_key")
        assert prompt.stage == "key"
        assert prompt.password is True

        prompt = await flow.submit_key("sk-fixture")
        assert prompt.stage == "model"
        assert [name for name, _ in prompt.options] == ["gpt-4o", "o3"]
        # Switching provider cleared the stale model, so the default is the
        # first thing the endpoint actually serves.
        assert prompt.default == "gpt-4o"

        prompt = await flow.choose_model("o3")
        assert prompt.stage == "done"
        return flow

    flow = asyncio.run(run())
    assert len(saved) == 1
    assert saved[0]["provider"] == "openai"
    assert saved[0]["model"] == "o3"
    assert saved[0]["openai_api_key"] == "sk-fixture"
    assert saved[0]["setup_complete"] is True
    # Probe twice: once to discover, once to verify the chosen model.
    assert len(probed) == 2
    assert flow.candidate is saved[0]


def test_the_flow_module_contains_no_thread_hop_and_no_asyncio_run():
    """A sweep, not a run: the hop could reappear on a branch no test walks."""
    tree = ast.parse(FLOW_SOURCE.read_text(encoding="utf-8"))
    banned = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "asyncio" and node.attr in {"to_thread", "run"}:
                banned.add(f"asyncio.{node.attr} at line {node.lineno}")
    assert not banned, sorted(banned)


def test_the_flow_never_mutates_the_config_it_was_given():
    """The transactional rule. A cancelled setup must leave the old config alone."""
    original = {"provider": "openai", "model": "old", "openai_api_key": "existing"}
    snapshot = dict(original)
    saved: list[dict] = []
    probe, _ = _probe([])

    async def run():
        flow = OnboardingFlow(original, save=saved.append, probe=probe)
        flow.start()
        await flow.choose_provider("anthropic")
        await flow.choose_auth("api_key")
        await flow.submit_key("sk-new")
        flow.cancel()
        with pytest.raises(OnboardingCancelled):
            await flow.discover()

    asyncio.run(run())
    assert original == snapshot
    assert saved == []


def test_unverified_is_a_question_and_a_refusal_saves_nothing():
    saved: list[dict] = []
    probe, _ = _probe(["m"], status="offline", message="Provider check unavailable")

    async def run():
        flow = OnboardingFlow({"provider": "openai"}, save=saved.append, probe=probe)
        flow.start()
        await flow.choose_provider("openai")
        await flow.choose_auth("api_key")
        await flow.submit_key("sk")
        prompt = await flow.choose_model("m")
        assert prompt.stage == "unverified"
        assert saved == []
        with pytest.raises(OnboardingCancelled):
            flow.confirm_unverified(False)
        assert saved == []
        prompt = flow.confirm_unverified(True)
        assert prompt.stage == "done"

    asyncio.run(run())
    assert len(saved) == 1


def test_a_missing_model_refuses_instead_of_saving():
    saved: list[dict] = []
    probe, _ = _probe([], status="missing", message="Select a model available at this provider.")

    async def run():
        flow = OnboardingFlow({"provider": "openai"}, save=saved.append, probe=probe)
        flow.start()
        await flow.choose_provider("openai")
        await flow.choose_auth("api_key")
        await flow.submit_key("sk")
        with pytest.raises(OnboardingError, match="was not saved"):
            await flow.choose_model("nope")

    asyncio.run(run())
    assert saved == []


def test_self_hosted_providers_are_asked_for_their_endpoint_first():
    """Only ``startup.setup`` ever asked this; ``ConnectScreen`` asked only when
    the base came out empty, so a Colibri server on a non-default port could not
    be configured from the TUI at all. The union asks."""
    saved: list[dict] = []
    probe, probed = _probe(["served-alias"])

    async def run():
        flow = OnboardingFlow({}, save=saved.append, probe=probe)
        flow.start()
        prompt = await flow.choose_provider("colibri")
        assert prompt.stage == "url"
        assert prompt.default == "http://127.0.0.1:8000/v1"
        with pytest.raises(OnboardingError, match="HTTP"):
            await flow.submit_url("127.0.0.1:9191")
        prompt = await flow.submit_url("http://127.0.0.1:9191/v1/")
        # needs_key is False for colibri, so the endpoint leads straight to
        # discovery with no auth question in between.
        assert prompt.stage == "model"
        await flow.choose_model("served-alias")

    asyncio.run(run())
    assert saved[0]["colibri_url"] == "http://127.0.0.1:9191/v1"
    assert probed[0]["colibri_url"] == "http://127.0.0.1:9191/v1"


def test_a_blank_key_keeps_the_existing_one_and_no_key_at_all_refuses():
    probe, _ = _probe(["m"])

    async def run():
        flow = OnboardingFlow(
            {"provider": "openai", "openai_api_key": "existing"}, save=lambda c: None, probe=probe
        )
        flow.start()
        await flow.choose_provider("openai")
        await flow.choose_auth("api_key")
        prompt = await flow.submit_key("")
        assert prompt.stage == "model"
        assert flow.candidate["openai_api_key"] == "existing"

        bare = OnboardingFlow({"provider": "openai"}, save=lambda c: None, probe=probe)
        bare.start()
        await bare.choose_provider("openai")
        await bare.choose_auth("api_key")
        with pytest.raises(OnboardingError, match="API key is required"):
            await bare.submit_key("")

    asyncio.run(run())


# ── The network half, now in core ──────────────────────────────────────────


def test_probe_async_runs_on_the_callers_loop(no_runtime_warning, no_thread_hop, monkeypatch):
    """It used to reach ``get_account_token`` -> ``asyncio.run`` through
    ``asyncio.to_thread``. With ``to_thread`` sabotaged the plain API-key path
    must still work end to end."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": [{"id": "gpt-4o"}]})

    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=transport))

    config = {"provider": "openai", "model": "gpt-4o", "openai_api_key": "k"}
    result = asyncio.run(probe_async(config))
    assert result["status"] == "ready"
    assert seen == ["https://api.openai.com/v1/models"]


def test_connection_and_catalogue_moved_without_changing_behaviour():
    from djcode import startup

    # `startup` re-exports them, so every existing caller keeps working.
    assert startup.connection is connection
    assert startup._catalogue is catalogue
    assert connection({"provider": "colibri"}, "colibri")["model"] == "djcode-colibri"
    assert catalogue("google", {"models": [{"name": "models/gemini-2.0-flash"}]}) == [
        {"name": "gemini-2.0-flash", "size": 0}
    ]


def test_core_exports_the_flow():
    import djcode.core as core

    assert core.OnboardingFlow is OnboardingFlow
    assert "OnboardingFlow" in core.__all__
    assert "CoreSession" in core.__all__
    assert "SessionOptions" in core.__all__
