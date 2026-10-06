"""Deterministic approval, tool-result, admission and stream regressions."""
import asyncio
from types import SimpleNamespace
import pytest
from djcode.tool_router import ToolExtractionRouter, ToolIntent
from djcode.streaming import stream_turn
from djcode.orchestrator.engine import ShadowOrchestrator
from djcode.agents.registry import AgentRole


def intent(action, **kw):
    return ToolIntent(action, kw.pop("path", "target"), kw.pop("content", None), "test", 1.0, **kw)


@pytest.mark.parametrize("reply", ["maybe", "approve later", None, "no"])
def test_unknown_confirmation_denies(monkeypatch, reply):
    monkeypatch.setattr("djcode.tool_router.questionary.text", lambda *a, **k: SimpleNamespace(ask=lambda: reply))
    assert asyncio.run(ToolExtractionRouter()._confirm_intents([intent("mkdir")])) == []


def test_actual_empty_edit_and_error_word_filename(tmp_path):
    target = tmp_path / "Error.txt"
    target.write_text("remove keep")
    router = ToolExtractionRouter()
    result = asyncio.run(router._execute_intent(intent("file_edit", path=str(target), old_string="remove ", new_string="")))
    assert result.success and target.read_text() == "keep"


def test_actual_failed_mkdir(tmp_path):
    parent = tmp_path / "file"
    parent.write_text("occupied")
    result = asyncio.run(ToolExtractionRouter()._execute_intent(intent("mkdir", path=str(parent / "child"))))
    assert not result.success and "exit code" in result.output


def test_parallel_duplicate_admission_before_provider():
    class Forbidden:
        async def chat(self, *a, **k):
            raise AssertionError("provider contacted")
    async def run():
        with pytest.raises(ValueError, match="unique"):
            _ = [event async for event in ShadowOrchestrator(Forbidden()).execute_parallel([AgentRole.CODER] * 2, "task")]
    asyncio.run(run())


def test_post_completion_call_rejected_and_stream_closed():
    class Fixture:
        closed = False
        async def chat(self, *a, **kw):
            try:
                yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
                yield {"choices": [{"delta": {"tool_calls": [{"function": {"name": "bash", "arguments": "{}"}}]}}]}
            finally:
                self.closed = True
    fixture = Fixture()
    async def run():
        with pytest.raises(ValueError, match="after completion"):
            _ = [part async for part in stream_turn(fixture, [])]
        assert fixture.closed
    asyncio.run(run())

@pytest.mark.parametrize("index", [-1, True, "0"])
def test_invalid_index_rejected(index):
    class Fixture:
        async def chat(self, *a, **kw):
            yield {"choices": [{"delta": {"tool_calls": [{"index": index, "function": {"name": "bash", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]}
    async def run():
        with pytest.raises(ValueError, match="index"):
            _ = [part async for part in stream_turn(Fixture(), [])]
    asyncio.run(run())
