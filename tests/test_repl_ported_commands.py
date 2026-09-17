"""The seven commands that existed only in the Textual TUI, now driven for real.

SSOT §3.1 listed nine `repl=False` rows. The TUI is in maintenance and the REPL
is where people work, so for most users "it exists in the TUI" has meant "it
does not exist". These tests run each ported handler and check what it prints,
rather than asserting it is merely importable.

`/cost` gets the most attention, because it is the one that can lie. The TUI's
CostPanel multiplied a per-1k rate by token counts produced by `len(text) // 4`
and presented the product as dollars. Blueprint W9-7 says not to port it, and
these tests pin the replacement's honesty: real accounting or no number.
"""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

import pytest
from rich.console import Console

from djcode.frontends.repl import commands as ported


@pytest.fixture
def cap(monkeypatch):
    console = Console(file=io.StringIO(), width=100, no_color=True, highlight=False)
    from djcode.frontends.repl import render

    monkeypatch.setattr(render, "console", console)
    return lambda: console.file.getvalue()


def _stats(**kw):
    defaults = dict(
        model="gpt-4o-mini",
        max_context_tokens=128_000,
        current_tokens=12_800,
        message_count=9,
        pinned_count=1,
        injected_count=0,
        injected_tokens=0,
        utilization_pct=10.0,
        remaining_tokens=115_200,
        compression_triggered=False,
        compressions_performed=2,
        last_compression_strategy=None,
        last_compression_ratio=0.0,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _operator(**kw):
    manager = SimpleNamespace(stats=_stats(), _injected=[])
    base = dict(
        context_manager=manager,
        messages=[
            SimpleNamespace(role="system", content="x" * 4000),
            SimpleNamespace(role="user", content="y" * 400),
        ],
        provider=SimpleNamespace(config=SimpleNamespace(model="gpt-4o-mini")),
        auto_accept=False,
        approval_callback=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# -- /context --------------------------------------------------------------


def test_context_shows_the_meter_and_the_numbers(cap):
    asyncio.run(ported.handle_context(_operator()))
    out = cap()
    assert "Context window" in out
    assert "10.0%" in out
    assert "128,000" in out
    assert "115,200" in out


def test_context_shows_where_the_tokens_came_from(cap):
    """"83% full" is not actionable; "62% of it is one injected file" is."""
    asyncio.run(ported.handle_context(_operator()))
    out = cap()
    assert "message: system" in out
    assert "message: user" in out


def test_context_counts_injected_sources_separately(cap):
    manager = SimpleNamespace(
        stats=_stats(),
        _injected=[SimpleNamespace(source="djcode.md", tokens=900, is_expired=False)],
    )
    asyncio.run(ported.handle_context(_operator(context_manager=manager)))
    assert "injected: djcode.md" in cap()


def test_context_ignores_expired_injections(cap):
    manager = SimpleNamespace(
        stats=_stats(),
        _injected=[SimpleNamespace(source="stale.md", tokens=900, is_expired=True)],
    )
    asyncio.run(ported.handle_context(_operator(context_manager=manager)))
    assert "stale.md" not in cap()


def test_context_does_not_invent_a_tool_schema_row(cap):
    """DESIGN-CLI §7's mock lists "tool schemas (22)". Tool schemas are not
    messages and ContextWindowManager does not count them, so that row would be
    a number this function made up. Deviation is deliberate."""
    asyncio.run(ported.handle_context(_operator()))
    assert "tool schema" not in cap().lower()


def test_context_survives_a_manager_that_raises(cap):
    class Exploding:
        @property
        def stats(self):
            raise RuntimeError("no manager")

    asyncio.run(ported.handle_context(_operator(context_manager=Exploding())))
    assert "Context unavailable" in cap()


# -- /cost -----------------------------------------------------------------


def _usage(input_tokens=0, output_tokens=0, requests=0, total_cost=0.0):
    from djcode.providers.base import TokenUsage
    from djcode.streaming import UsageSink

    sink = UsageSink()
    if requests:
        sink.record(
            TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_cost=total_cost,
            )
        )
        for _ in range(requests - 1):
            sink.requests += 1
    return sink


def test_cost_refuses_to_guess_when_the_provider_reported_nothing(cap):
    """This is the whole reason CostPanel was not ported. A cost derived from
    len(text)//4 is a made-up number wearing a dollar sign."""
    asyncio.run(ported.handle_cost(_operator(session_usage=_usage())))
    out = cap()
    assert "$" not in out
    assert "reported no token accounting" in out


def test_cost_refuses_when_there_is_no_sink_at_all(cap):
    asyncio.run(ported.handle_cost(_operator()))
    assert "$" not in cap()


def test_cost_prints_the_real_numbers_when_they_exist(cap):
    usage = _usage(input_tokens=1_000_000, output_tokens=500_000, requests=3)
    asyncio.run(ported.handle_cost(_operator(session_usage=usage)))
    out = cap()
    assert "1,000,000" in out
    assert "500,000" in out
    assert "$" in out


def test_cost_says_whether_the_price_came_from_the_provider(cap):
    usage = _usage(input_tokens=10, output_tokens=10, requests=1, total_cost=1.25)
    asyncio.run(ported.handle_cost(_operator(session_usage=usage)))
    out = cap()
    assert "$1.2500" in out
    assert "provider" in out


def test_cost_says_so_when_no_price_is_known_for_the_model(cap):
    usage = _usage(input_tokens=10, output_tokens=10, requests=1)
    operator = _operator(
        session_usage=usage,
        provider=SimpleNamespace(config=SimpleNamespace(model="some-local-model")),
    )
    asyncio.run(ported.handle_cost(operator))
    out = cap()
    assert "No price is known" in out


# -- /search, /tasks, /todo, /spawn ----------------------------------------


def test_search_without_a_query_says_how_to_use_it(cap):
    asyncio.run(ported.handle_search(_operator(), ""))
    assert "Usage: /search" in cap()


def test_search_dispatches_the_web_search_tool(cap, monkeypatch):
    seen = {}

    async def fake(name, args):
        seen["call"] = (name, args)
        return SimpleNamespace(
            content="three results", details={"ok_source": "unverified"}, ok=True
        )

    import djcode.tools as tools

    monkeypatch.setattr(tools, "dispatch_tool", fake)
    asyncio.run(ported.handle_search(_operator(), "rust async"))
    assert seen["call"] == ("web_search", {"query": "rust async"})
    out = cap()
    assert "three results" in out
    assert "✓" not in out, "web_search is ok_source=unverified; no tick"


def test_tasks_lists_by_default(cap, monkeypatch):
    calls = []

    async def fake(name, args):
        calls.append(name)
        return SimpleNamespace(content="no tasks", details={}, ok=True)

    import djcode.tools as tools

    monkeypatch.setattr(tools, "dispatch_tool", fake)
    asyncio.run(ported.handle_tasks(_operator(), ""))
    assert calls == ["task_list"]
    assert "Session tasks" in cap()


def test_todo_is_the_same_store_with_a_different_label(cap, monkeypatch):
    """The TUI's /todo was backed by a sidebar widget whose state was never
    persisted. Rather than invent a second task store, /todo presents the real
    one -- stated here so the alias is not a surprise."""
    calls = []

    async def fake(name, args):
        calls.append(name)
        return SimpleNamespace(content="none", details={}, ok=True)

    import djcode.tools as tools

    monkeypatch.setattr(tools, "dispatch_tool", fake)
    asyncio.run(ported.handle_todo(_operator(), ""))
    assert calls == ["task_list"]
    assert "Todos" in cap()


def test_tasks_add_creates(cap, monkeypatch):
    seen = {}

    async def fake(name, args):
        seen[name] = args
        return SimpleNamespace(content="created", details={}, ok=True)

    import djcode.tools as tools

    monkeypatch.setattr(tools, "dispatch_tool", fake)
    asyncio.run(ported.handle_tasks(_operator(), "add write the report"))
    assert seen["task_create"] == {"title": "write the report"}


def test_tasks_rejects_a_verb_it_does_not_have(cap, monkeypatch):
    import djcode.tools as tools

    async def fake(name, args):  # pragma: no cover - must not be reached
        raise AssertionError("no tool should run")

    monkeypatch.setattr(tools, "dispatch_tool", fake)
    asyncio.run(ported.handle_tasks(_operator(), "frobnicate x"))
    assert "Usage: /tasks" in cap()


def test_spawn_without_an_argument_says_how_to_use_it(cap):
    asyncio.run(ported.handle_spawn(_operator(), ""))
    assert "Usage: /spawn" in cap()


def test_spawn_defaults_the_task_but_never_the_role(cap, monkeypatch):
    seen = {}

    async def fake(name, args):
        seen["args"] = args
        return SimpleNamespace(content="agent finished", details={}, ok=True)

    import djcode.tools as tools

    monkeypatch.setattr(tools, "dispatch_tool", fake)
    asyncio.run(ported.handle_spawn(_operator(), "reviewer"))
    assert seen["args"]["role"] == "reviewer"
    assert seen["args"]["task"]


# -- /waves ----------------------------------------------------------------


def test_waves_without_an_argument_says_how_to_use_it(cap):
    asyncio.run(ported.handle_waves(_operator(), ""))
    assert "Usage: /waves" in cap()


def test_waves_without_an_orchestrator_says_so_rather_than_crashing(cap):
    asyncio.run(ported.handle_waves(_operator(), "do a thing", orchestrator=None))
    assert "orchestrator is not available" in cap()


def test_waves_reports_each_wave_and_agent(cap):
    from djcode.orchestrator.events import EventType

    class Shadow:
        async def execute(self, task, strategy_override=None):
            for kind, data in [
                (EventType.WAVE_START, {"wave": 1}),
                (EventType.AGENT_START, {"agent_name": "scout"}),
                (EventType.AGENT_COMPLETE, {"agent_name": "scout"}),
                (EventType.WAVE_COMPLETE, {"wave": 1}),
                (EventType.ORCHESTRATOR_COMPLETE, {}),
            ]:
                yield SimpleNamespace(event_type=kind, data=data)

    orchestrator = SimpleNamespace(_shadow=Shadow())
    asyncio.run(ported.handle_waves(_operator(), "build it", orchestrator=orchestrator))
    out = cap()
    assert "wave 1" in out
    assert "scout" in out
    assert "ended without reporting completion" not in out


def test_waves_says_so_when_the_run_never_reports_completion(cap):
    from djcode.orchestrator.events import EventType

    class Shadow:
        async def execute(self, task, strategy_override=None):
            yield SimpleNamespace(event_type=EventType.WAVE_START, data={"wave": 1})

    orchestrator = SimpleNamespace(_shadow=Shadow())
    asyncio.run(ported.handle_waves(_operator(), "build it", orchestrator=orchestrator))
    assert "ended without reporting completion" in cap()


def test_waves_surfaces_an_agent_error_instead_of_finishing_quietly(cap):
    from djcode.orchestrator.events import EventType

    class Shadow:
        async def execute(self, task, strategy_override=None):
            yield SimpleNamespace(
                event_type=EventType.AGENT_ERROR, data={"error": "provider offline"}
            )

    orchestrator = SimpleNamespace(_shadow=Shadow())
    asyncio.run(ported.handle_waves(_operator(), "build it", orchestrator=orchestrator))
    assert "provider offline" in cap()
