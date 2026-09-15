"""startup's blocking wrappers must refuse a running loop and name the coroutine to await.

The defect this pins down: every network entry point in ``startup`` used to be a
sync function that called ``asyncio.run`` internally. Called from inside a live
loop it raised "asyncio.run() cannot be called from a running event loop" -- a
message that names the wrong mistake -- and leaked a never-awaited coroutine
each time. Five call sites survived only because each author remembered
``asyncio.to_thread``; nothing tested that, and nothing said so in a signature.
"""

import asyncio
import gc
import os
import subprocess
import sys
import warnings

import httpx
import pytest

from djcode import startup

TAGS = {"models": [{"name": "qwen3:8b", "size": 5_200_000_000}, {"name": "tiny:latest"}]}
CONFIG = {"provider": "ollama", "model": "qwen3:8b", "ollama_url": "http://models.test"}


@pytest.fixture
def requests(monkeypatch):
    """Serve one Ollama /api/tags payload in-process; returns the URLs actually fetched."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=TAGS)

    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(startup.httpx, "AsyncClient", lambda **kw: original(transport=transport))
    return seen


@pytest.fixture
def no_runtime_warning():
    """Fail the test on any RuntimeWarning; 'coroutine was never awaited' is the hunted one.

    That warning is emitted when the orphaned coroutine is collected, which can
    be well after the call that created it, so the collection is forced here
    rather than left to chance.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        yield
        gc.collect()
    leaked = [str(item.message) for item in caught if issubclass(item.category, RuntimeWarning)]
    assert not leaked, f"RuntimeWarning leaked: {leaked}"


def test_probe_inside_a_running_loop_names_probe_async(requests, no_runtime_warning):
    async def caller():
        with pytest.raises(RuntimeError) as error:
            startup.probe(dict(CONFIG))
        return str(error.value)

    message = asyncio.run(caller())
    assert "probe_async" in message, message
    assert "startup.probe()" in message, message
    # The old failure named asyncio.run -- the mechanism -- instead of the mistake.
    assert "asyncio.run()" not in message, message
    assert requests == [], "the refusal must happen before any request is made"


def test_probe_async_works_inside_a_running_loop(requests, no_runtime_warning):
    async def caller():
        return await startup.probe_async(dict(CONFIG))

    found = asyncio.run(caller())
    assert found["status"] == "ready"
    assert found["provider"] == "ollama"
    assert requests == ["http://models.test/api/tags"]


def test_to_thread_probe_works_inside_a_running_loop(requests, no_runtime_warning):
    async def caller():
        return await asyncio.to_thread(startup.probe, dict(CONFIG))

    found = asyncio.run(caller())
    assert found["status"] == "ready"
    assert requests == ["http://models.test/api/tags"]


def test_probe_outside_a_loop_still_works(requests, no_runtime_warning):
    found = startup.probe(dict(CONFIG))
    assert found["status"] == "ready"
    assert requests == ["http://models.test/api/tags"]


def test_discover_inside_a_running_loop_names_discover_async(requests, no_runtime_warning):
    async def caller():
        with pytest.raises(RuntimeError) as error:
            startup.discover("http://models.test/api/tags", {})
        assert "discover_async" in str(error.value)
        response = await startup.discover_async("http://models.test/api/tags", {})
        return response.json()

    assert asyncio.run(caller()) == TAGS
    assert requests == ["http://models.test/api/tags"]


def test_prepare_inside_a_running_loop_names_prepare_async(monkeypatch, no_runtime_warning):
    monkeypatch.setenv("DJCODE_SKIP_STARTUP_CHECK", "1")

    async def caller():
        with pytest.raises(RuntimeError) as error:
            startup.prepare("ollama", "qwen3:8b")
        message = str(error.value)
        assert "prepare_async" in message, message
        return await startup.prepare_async("ollama", "qwen3:8b")

    assert asyncio.run(caller()) == ("ollama", "qwen3:8b")


def test_probe_returns_named_models_with_their_sizes(requests):
    found = startup.probe(dict(CONFIG))
    assert found["models"] == [
        {"name": "qwen3:8b", "size": 5_200_000_000},
        {"name": "tiny:latest", "size": 0},
    ]


def test_ollama_latest_suffix_still_matches(requests):
    config = dict(CONFIG, model="tiny")
    assert startup.probe(config)["status"] == "ready"


def test_unknown_model_reports_the_catalogue(requests):
    config = dict(CONFIG, model="not-installed")
    found = startup.probe(config)
    assert found["status"] == "missing"
    assert [item["name"] for item in found["models"]] == ["qwen3:8b", "tiny:latest"]


def test_catalogue_ignores_unusable_entries():
    payload = {"data": [{"id": "b"}, "junk", {"id": ""}, {"id": "a", "size": "big"}, {"id": "a"}]}
    assert startup._catalogue("openai", payload) == [
        {"name": "a", "size": 0},
        {"name": "b", "size": 0},
    ]


def test_google_model_prefix_is_stripped():
    payload = {"models": [{"name": "models/gemini-2.0-flash"}]}
    assert startup._catalogue("google", payload) == [{"name": "gemini-2.0-flash", "size": 0}]


def _do_not_track_under(value: str | None) -> str:
    """Import djcode.startup in a clean interpreter and report DO_NOT_TRACK."""
    env = dict(os.environ)
    env.pop("DO_NOT_TRACK", None)
    if value is not None:
        env["DO_NOT_TRACK"] = value
    result = subprocess.run(
        [sys.executable, "-c", "import os, djcode.startup; print(os.environ['DO_NOT_TRACK'])"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_do_not_track_is_announced_by_default():
    assert "DO_NOT_TRACK" in os.environ, "importing djcode.startup must announce it"
    assert _do_not_track_under(None) == "1"


def test_do_not_track_never_overrides_a_deliberate_opt_out():
    assert _do_not_track_under("0") == "0"
