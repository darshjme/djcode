"""Deterministic runtime integration tests; providers are explicit protocol fixtures."""
import asyncio
from dataclasses import replace

import pytest

from djcode.agents.executor import AgentExecutor
from djcode.agents.parallel import ParallelCoordinator
from djcode.agents.registry import AgentRole, get_agent
from djcode.agents.state import AgentState
from djcode.orchestrator.context_bus import ContextBus
from djcode.orchestrator.engine import AgentRunner
from djcode.tools import agent_spawn


def final(text="done"):
    return {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}


def call(name, arguments='{}'):
    return {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "abc", "function": {"name": name, "arguments": arguments}}]}, "finish_reason": "tool_calls"}]}


class ScriptedProvider:
    def __init__(self, turns):
        self.turns = iter(turns)
        self.messages = []
    async def chat(self, messages, stream=True):
        self.messages.append(list(messages))
        for chunk in next(self.turns):
            yield chunk


class HangingProvider:
    def __init__(self):
        self.cancelled = 0
    async def chat(self, messages, stream=True):
        try:
            await asyncio.sleep(60)
            yield final()
        finally:
            self.cancelled += 1


def test_real_file_tool_and_parent_provider(tmp_path):
    target = tmp_path / "answer.txt"
    import json
    provider = ScriptedProvider([[call("file_write", json.dumps({"path": str(target), "content": "verified"}))], [final("wrote file")]])
    async def run():
        with agent_spawn.agent_context(provider, True):
            return await agent_spawn.execute_spawn_agent("coder", "write answer", max_tool_rounds=3)
    assert "wrote file" in asyncio.run(run())
    assert target.read_text() == "verified"
    tool = provider.messages[1][-1]
    assert tool.role == "tool" and tool.tool_call_id == "abc"


@pytest.mark.parametrize("readonly,auto", [(False, False), (True, True)])
def test_denied_mutation(tmp_path, readonly, auto):
    target = tmp_path / "must-not-exist"
    import json
    provider = ScriptedProvider([[call("file_write", json.dumps({"path": str(target), "content": "no"}))], [final()]])
    spec = replace(get_agent(AgentRole.CODER), read_only=readonly)
    result = asyncio.run(AgentExecutor(spec, provider, ContextBus(), enable_ra=False, auto_accept=auto).execute("test"))
    assert result.succeeded
    assert not target.exists()
    assert "requires write approval" in provider.messages[1][-1].content


def test_round_exhaustion_not_success():
    provider = ScriptedProvider([[call("task_list")]])
    bus = ContextBus()
    spec = replace(get_agent(AgentRole.CODER), max_tool_rounds=1)
    result = asyncio.run(AgentExecutor(spec, provider, bus, enable_ra=False).execute("test"))
    assert result.state == AgentState.ERROR
    assert "limit reached" in result.error
    assert len(bus) == 0


def test_runner_propagates_provider_failure():
    provider = ScriptedProvider([[{"error": "authentication failed"}]])
    runner = AgentRunner(provider, get_agent(AgentRole.CODER), ContextBus())
    with pytest.raises(RuntimeError, match="authentication failed"):
        asyncio.run(runner.run("test"))


def test_executor_timeout_cancels_provider():
    provider = HangingProvider()
    executor = AgentExecutor(get_agent(AgentRole.CODER), provider, ContextBus(), enable_ra=False, execution_timeout_s=.02)
    result = asyncio.run(executor.execute("test"))
    assert not result.succeeded and "timed out" in result.error
    assert provider.cancelled == 1


def test_parallel_overall_deadline_and_shared_empty_bus():
    async def run():
        provider = HangingProvider()
        bus = ContextBus()
        coordinator = ParallelCoordinator(provider, bus, enable_ra=False, overall_timeout_s=.02)
        assert coordinator.bus is bus
        result = await coordinator.run_parallel([get_agent(AgentRole.CODER), get_agent(AgentRole.TESTER)], "test")
        assert result.halted and len(result.failed) == 2
        assert provider.cancelled == 2
    asyncio.run(run())


def test_stream_close_cancels_children():
    async def run():
        provider = HangingProvider()
        coordinator = ParallelCoordinator(provider, enable_ra=False)
        stream = coordinator.run_parallel_streaming([get_agent(AgentRole.CODER)], "test")
        await anext(stream)
        await stream.aclose()
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    asyncio.run(run())


def test_pipeline_stops_after_failure():
    provider = ScriptedProvider([[{"error": "offline"}]])
    coordinator = ParallelCoordinator(provider, enable_ra=False)
    result = asyncio.run(coordinator.run_pipeline([get_agent(AgentRole.CODER), get_agent(AgentRole.TESTER)], "test"))
    assert result.halted and len(result.results) == 1


def test_background_failure_reported_and_status_alias():
    async def run():
        provider = ScriptedProvider([[{"error": "offline"}]])
        with agent_spawn.agent_context(provider, False):
            await agent_spawn.execute_spawn_agent("coder", "test", background=True)
        info = list(agent_spawn._background_tasks.values())[-1]
        await info["async_task"]
        assert info["status"] == "failed"
        assert "offline" in await agent_spawn.execute_spawn_agent("status", info["id"])
    asyncio.run(run())


def test_spawn_bounds():
    assert "between 1 and 100" in asyncio.run(agent_spawn.execute_spawn_agent("coder", "test", max_tool_rounds=0))


def test_tui_mount_permission_and_immediate_cancel(monkeypatch, tmp_path):
    from djcode.app import DJcodeApp, ToolApprovalScreen, AgentsScreen
    from djcode.provider import Provider, ProviderConfig
    from types import SimpleNamespace
    from djcode import sessions, stats
    from djcode.memory import manager as memory_manager
    from djcode.orchestrator import vector_context
    monkeypatch.setattr(sessions, "DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(stats, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(stats, "STATS_FILE", tmp_path / "stats.json")
    monkeypatch.setattr(memory_manager, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(memory_manager, "FACTS_FILE", tmp_path / "memory/facts.json")
    monkeypatch.setattr(memory_manager, "CONVERSATIONS_DIR", tmp_path / "conversations")
    monkeypatch.setattr(vector_context, "VECTOR_DIR", tmp_path / "vectors")
    monkeypatch.chdir(tmp_path)
    # Keep startup itself real; prevent provider validation from contacting the network.
    monkeypatch.setattr(ProviderConfig, "from_config", staticmethod(lambda **kw: ProviderConfig(name="openai", model="test-model", base_url="https://example.invalid", api_key="test")))
    monkeypatch.setattr(Provider, "validate_model", lambda self: (True, ""))
    async def run():
        app = DJcodeApp()
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(50):
                if app._operator is not None:
                    break
                await pilot.pause(.02)
            assert app._operator is not None
            assert app._operator.approval_callback is not None
            # /spawn must use this live provider and preserve auto-accept OFF.
            import json
            target = tmp_path / "denied.txt"
            spawn_provider = ScriptedProvider([[call("file_write", json.dumps({"path": str(target), "content": "no"}))], [final("permission denied")]])
            original_provider = app._provider
            app._provider = spawn_provider
            spawning = asyncio.create_task(app._handle_spawn("coder write a file"))
            await pilot.pause()
            assert isinstance(app.screen, ToolApprovalScreen)
            await pilot.click("#deny-tool")
            await spawning
            assert not target.exists()
            assert "User denied" in spawn_provider.messages[1][-1].content
            app._provider = original_provider
            app.action_show_agents()
            await pilot.pause()
            assert isinstance(app.screen, AgentsScreen)
            await pilot.press("escape")
            pending = asyncio.create_task(app._approve_tool("file_write", {"path": "test.txt"}))
            await pilot.pause()
            assert isinstance(app.screen, ToolApprovalScreen)
            import os
            if os.environ.get("DJCODE_TUI_SCREENSHOT_DIR"):
                app.save_screenshot("runtime-approval.svg", path=os.environ["DJCODE_TUI_SCREENSHOT_DIR"])
            await pilot.click("#deny-tool")
            assert await pending is False
            provider = HangingProvider()
            app._operator.provider = provider
            generation = asyncio.create_task(app._send_message("test"))
            await pilot.pause(.02)
            app.action_cancel()
            with pytest.raises(asyncio.CancelledError):
                await generation
            assert not app._is_generating
            assert provider.cancelled == 1
    asyncio.run(run())


@pytest.mark.parametrize("allow,readonly", [(True, False), (False, False), (True, True)])
def test_specialist_approval_precedes_write(tmp_path, allow, readonly):
    import json
    target = tmp_path / "approved.txt"
    provider = ScriptedProvider([[call("file_write", json.dumps({"path": str(target), "content": "yes"}))], [final()]])
    decisions = []
    async def approve(name, args):
        decisions.append((name, args))
        return allow
    executor = AgentExecutor(replace(get_agent(AgentRole.CODER), read_only=readonly), provider, ContextBus(), enable_ra=False, approval_callback=approve)
    assert asyncio.run(executor.execute("test")).succeeded
    assert target.exists() == (allow and not readonly)
    assert bool(decisions) == (not readonly)


def test_nested_spawn_inherits_approval(tmp_path):
    import json
    target = tmp_path / "nested.txt"
    provider = ScriptedProvider([[call("spawn_agent", json.dumps({"role": "coder", "task": "write"}))], [call("file_write", json.dumps({"path": str(target), "content": "nested"}))], [final("child done")], [final("parent done")]])
    decisions = []
    async def approve(name, args):
        decisions.append(name)
        return True
    async def run():
        with agent_spawn.agent_context(provider, False, approve):
            return await agent_spawn.execute_spawn_agent("coder", "delegate")
    assert "parent done" in asyncio.run(run())
    assert target.read_text() == "nested"
    assert decisions == ["spawn_agent", "file_write"]


def test_wave_cli_failure_closes_provider(monkeypatch):
    from click.testing import CliRunner
    from djcode.cli import main
    from djcode.provider import Provider, ProviderConfig
    from djcode.orchestrator import Orchestrator
    from djcode.orchestrator.events import orchestrator_error_event
    from djcode.orchestrator.engine import ExecutionStrategy
    from types import SimpleNamespace
    closed = []
    monkeypatch.setattr(ProviderConfig, "from_config", staticmethod(lambda **kw: ProviderConfig(name="openai", model="test", base_url="https://example.invalid", api_key="test")))
    monkeypatch.setattr(Provider, "validate_model", lambda self: (True, ""))
    async def close(self):
        closed.append(True)
    monkeypatch.setattr(Provider, "close", close)
    async def execute(task, strategy_override=None):
        assert strategy_override == ExecutionStrategy.WAVE
        yield orchestrator_error_event(task, "provider unavailable", [])
    monkeypatch.setattr(Orchestrator, "__init__", lambda self, *a, **kw: setattr(self, "_shadow", SimpleNamespace(execute=execute)))
    result = CliRunner().invoke(main, ["--wave", "test"])
    assert result.exit_code != 0
    assert "provider unavailable" in result.output
    assert "Wave execution finished" not in result.output
    assert closed == [True]
