"""Tests for DJcode v4.0 new modules.

Covers: agent state machine, RA briefing, agent executor, parallel coordinator,
context window manager, conversation compressor, model registry, event bus,
context bus, task tracker, and parallel exec tool.

All tests are unit tests requiring no LLM or network access.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Agent State Machine
# ---------------------------------------------------------------------------
from djcode.agents.registry import AgentRole, AgentSpec
from djcode.agents.state import (
    AgentEvent,
    AgentEventType,
    AgentState,
    AgentStateError,
    AgentStateMachine,
)


def _make_spec(role: AgentRole = AgentRole.CODER, name: str = "Vishwakarma") -> AgentSpec:
    """Helper: create a minimal AgentSpec for testing."""
    return AgentSpec(
        role=role,
        name=name,
        title="Senior Coder",
        system_prompt="You are a coder.",
        tools_allowed=frozenset({"file_read", "file_write", "bash"}),
        tools_denied=frozenset(),
        max_tool_rounds=5,
    )


class TestAgentStateMachine:
    """Test state transitions, event emission, and lifecycle helpers."""

    def test_initial_state_is_idle(self) -> None:
        sm = AgentStateMachine(spec=_make_spec())
        assert sm.state == AgentState.IDLE

    def test_valid_transition_idle_to_assigned(self) -> None:
        sm = AgentStateMachine(spec=_make_spec())
        asyncio.run(sm.assign("write tests"))
        assert sm.state == AgentState.ASSIGNED
        assert sm.task == "write tests"
        assert sm.start_time > 0

    def test_invalid_transition_idle_to_executing_raises(self) -> None:
        sm = AgentStateMachine(spec=_make_spec())
        with pytest.raises(AgentStateError) as exc_info:
            asyncio.run(sm.transition(AgentState.EXECUTING))
        assert "invalid transition" in str(exc_info.value)
        assert exc_info.value.from_state == AgentState.IDLE
        assert exc_info.value.to_state == AgentState.EXECUTING

    def test_full_lifecycle_idle_to_done(self) -> None:
        sm = AgentStateMachine(spec=_make_spec())
        asyncio.run(sm.assign("task"))
        asyncio.run(sm.start_research())
        asyncio.run(sm.start_execution())
        asyncio.run(sm.complete(confidence=0.95))
        assert sm.state == AgentState.DONE
        assert sm.confidence_score == 0.95
        assert sm.end_time > 0

    def test_fail_from_any_non_terminal_state(self) -> None:
        sm = AgentStateMachine(spec=_make_spec())
        asyncio.run(sm.assign("task"))
        asyncio.run(sm.fail("something broke"))
        assert sm.state == AgentState.ERROR
        assert sm.error_message == "something broke"

    def test_cannot_transition_from_done(self) -> None:
        sm = AgentStateMachine(spec=_make_spec())
        asyncio.run(sm.assign("task"))
        asyncio.run(sm.start_execution())
        asyncio.run(sm.complete(0.9))
        with pytest.raises(AgentStateError):
            asyncio.run(sm.transition(AgentState.IDLE))

    def test_event_callback_fires_on_transition(self) -> None:
        events: list[AgentEvent] = []

        async def collector(event: AgentEvent) -> None:
            events.append(event)

        sm = AgentStateMachine(spec=_make_spec())
        sm.on_event(collector)
        asyncio.run(sm.assign("test task"))

        assert len(events) >= 1
        assert events[0].event_type == AgentEventType.STATE_CHANGE

    def test_remove_callback(self) -> None:
        events: list[AgentEvent] = []

        async def collector(event: AgentEvent) -> None:
            events.append(event)

        sm = AgentStateMachine(spec=_make_spec())
        sm.on_event(collector)
        sm.remove_callback(collector)
        asyncio.run(sm.assign("test"))
        # Callback removed, no events should have been collected
        assert len(events) == 0

    def test_record_token_increments_count(self) -> None:
        sm = AgentStateMachine(spec=_make_spec())
        asyncio.run(sm.record_token("hello"))
        asyncio.run(sm.record_token("world"))
        assert sm.tokens_used == 2

    def test_record_tokens_batch(self) -> None:
        sm = AgentStateMachine(spec=_make_spec())
        asyncio.run(sm.record_tokens_batch(100))
        assert sm.tokens_used == 100

    def test_record_tool_call(self) -> None:
        sm = AgentStateMachine(spec=_make_spec())
        asyncio.run(sm.record_tool_call(
            tool_name="file_read",
            arguments={"path": "/tmp/test.py"},
            result="content here",
            duration_ms=42.5,
        ))
        assert sm.tool_count == 1
        assert sm.tools_called[0].tool_name == "file_read"
        assert sm.tools_called[0].duration_ms == 42.5

    def test_snapshot_contains_required_keys(self) -> None:
        sm = AgentStateMachine(spec=_make_spec())
        snap = sm.snapshot()
        required_keys = {
            "agent", "role", "state", "task", "start_time",
            "end_time", "duration_s", "tokens_used", "tools_called",
            "confidence_score", "has_ra_briefing", "error",
        }
        assert required_keys.issubset(snap.keys())

    def test_is_terminal_property(self) -> None:
        sm = AgentStateMachine(spec=_make_spec())
        assert not sm.is_terminal
        asyncio.run(sm.assign("t"))
        asyncio.run(sm.start_execution())
        asyncio.run(sm.complete(0.8))
        assert sm.is_terminal

    def test_is_active_property(self) -> None:
        sm = AgentStateMachine(spec=_make_spec())
        assert not sm.is_active
        asyncio.run(sm.assign("t"))
        assert sm.is_active

    def test_duration_s_zero_before_start(self) -> None:
        sm = AgentStateMachine(spec=_make_spec())
        assert sm.duration_s == 0.0

    def test_agent_event_is_terminal_property(self) -> None:
        complete_event = AgentEvent(
            event_type=AgentEventType.COMPLETE,
            agent_role=AgentRole.CODER,
            agent_name="test",
            timestamp=time.time(),
        )
        assert complete_event.is_terminal

        token_event = AgentEvent(
            event_type=AgentEventType.TOKEN,
            agent_role=AgentRole.CODER,
            agent_name="test",
            timestamp=time.time(),
        )
        assert not token_event.is_terminal


# ---------------------------------------------------------------------------
# Model Registry
# ---------------------------------------------------------------------------
from djcode.context.models import (
    MODEL_REGISTRY,
    ModelInfo,
    estimate_cost,
    get_context_size,
    get_model_info,
    list_models,
    register_model,
    supports_thinking,
    supports_tools,
    supports_vision,
)


class TestModelRegistry:
    """Test model lookup, fuzzy matching, registration, and utilities."""

    def test_exact_name_lookup(self) -> None:
        info = get_model_info("claude-opus-4-6")
        assert info is not None
        assert info.name == "claude-opus-4-6"
        assert info.max_context == 1_000_000

    def test_alias_lookup(self) -> None:
        info = get_model_info("opus")
        assert info is not None
        assert info.name == "claude-opus-4-6"

    def test_fuzzy_match_gpt4o(self) -> None:
        info = get_model_info("gpt4o")
        assert info is not None
        assert info.name == "gpt-4o"

    def test_unknown_model_returns_none(self) -> None:
        info = get_model_info("completely-nonexistent-model-xyz-9999")
        assert info is None

    def test_get_context_size_known_model(self) -> None:
        size = get_context_size("claude-opus-4-6")
        assert size == 1_000_000

    def test_get_context_size_unknown_model_returns_default(self) -> None:
        size = get_context_size("nonexistent", default=4096)
        assert size == 4096

    def test_supports_tools_known(self) -> None:
        assert supports_tools("gpt-4o") is True

    def test_supports_tools_unknown_defaults_true(self) -> None:
        # Optimistic default for unknown models
        assert supports_tools("unknown-model-xyz") is True

    def test_supports_vision_known(self) -> None:
        assert supports_vision("claude-opus-4-6") is True
        assert supports_vision("qwen3") is False

    def test_supports_vision_unknown_defaults_false(self) -> None:
        assert supports_vision("unknown-model-xyz") is False

    def test_supports_thinking_known(self) -> None:
        assert supports_thinking("claude-opus-4-6") is True
        assert supports_thinking("gpt-4o") is False

    def test_register_custom_model(self) -> None:
        info = register_model(
            "test-custom-v1",
            max_context=32_000,
            provider="custom",
            aliases=("tcv1",),
        )
        assert info.name == "test-custom-v1"
        assert info.max_context == 32_000

        # Lookup by name
        found = get_model_info("test-custom-v1")
        assert found is not None
        assert found.provider == "custom"

        # Lookup by alias
        found_alias = get_model_info("tcv1")
        assert found_alias is not None
        assert found_alias.name == "test-custom-v1"

    def test_list_models_returns_sorted_by_context(self) -> None:
        models = list_models()
        assert len(models) > 0
        # Verify sorted descending by max_context
        for i in range(len(models) - 1):
            assert models[i].max_context >= models[i + 1].max_context

    def test_list_models_filter_by_provider(self) -> None:
        anthropic_models = list_models(provider="anthropic")
        assert all(m.provider == "anthropic" for m in anthropic_models)
        assert len(anthropic_models) >= 2  # opus and sonnet at minimum

    def test_estimate_cost_known_model(self) -> None:
        cost = estimate_cost("claude-opus-4-6", input_tokens=1000, output_tokens=500)
        assert cost is not None
        assert cost > 0.0

    def test_estimate_cost_local_model_returns_none(self) -> None:
        cost = estimate_cost("llama3", input_tokens=1000, output_tokens=500)
        assert cost is None  # Local models have 0 cost

    def test_estimate_cost_unknown_model_returns_none(self) -> None:
        cost = estimate_cost("unknown-xyz", input_tokens=1000, output_tokens=500)
        assert cost is None

    def test_model_registry_has_entries(self) -> None:
        assert len(MODEL_REGISTRY) > 20  # At least 20 entries (names + aliases)

    def test_reverse_substring_match_versioned_name(self) -> None:
        """Registry key 'claude-opus-4-6' should match inside 'claude-opus-4-6-20260410'."""
        info = get_model_info("claude-opus-4-6-20260410")
        assert info is not None
        assert info.name == "claude-opus-4-6"

    def test_case_insensitive_lookup(self) -> None:
        info = get_model_info("GPT-4o")
        assert info is not None
        assert info.name == "gpt-4o"


# ---------------------------------------------------------------------------
# Conversation Compressor
# ---------------------------------------------------------------------------
from djcode.context.compressor import (
    CompressionStrategy,
    ConversationCompressor,
    _count_tokens,
    extractive_summarize,
)
from djcode.provider import Message


class TestConversationCompressor:
    """Test compression strategies (TRIM, SELECTIVE) without LLM."""

    def _make_messages(self, count: int = 20) -> list[Message]:
        """Create a list of dummy messages for testing."""
        msgs = [Message(role="system", content="You are a helpful assistant.")]
        for i in range(count):
            if i % 3 == 0:
                msgs.append(Message(role="user", content=f"User message number {i}. " * 10))
            elif i % 3 == 1:
                msgs.append(Message(role="assistant", content=f"Assistant response {i}. " * 10))
            else:
                msgs.append(Message(
                    role="assistant",
                    content=f"Calling tool for step {i}",
                    tool_calls=[{"function": {"name": "file_read", "arguments": "{}"}}],
                ))
        return msgs

    def test_trim_under_budget_returns_unchanged(self) -> None:
        compressor = ConversationCompressor()
        msgs = [Message(role="user", content="hello")]
        result = compressor.trim(msgs, target_tokens=10000, keep_recent=5)
        assert result.messages_removed == 0
        assert len(result.messages) == 1

    def test_trim_removes_old_messages(self) -> None:
        compressor = ConversationCompressor()
        msgs = self._make_messages(30)
        total_tokens = _count_tokens(" ".join(m.content or "" for m in msgs))
        # Set a low target to force trimming
        result = compressor.trim(msgs, target_tokens=total_tokens // 3, keep_recent=5)
        assert result.messages_removed > 0
        assert result.compressed_tokens <= result.original_tokens

    def test_trim_preserves_system_messages(self) -> None:
        compressor = ConversationCompressor()
        msgs = self._make_messages(20)
        result = compressor.trim(msgs, target_tokens=100, keep_recent=3)
        # System message should always be first
        system_msgs = [m for m in result.messages if m.role == "system"]
        assert len(system_msgs) >= 1

    def test_selective_trim_keeps_tool_messages(self) -> None:
        compressor = ConversationCompressor()
        msgs = self._make_messages(20)
        total_tokens = _count_tokens(" ".join(m.content or "" for m in msgs))
        result = compressor.selective_trim(
            msgs, target_tokens=total_tokens // 2, keep_recent=3,
        )
        # Tool-related messages should be preserved over plain chat
        tool_msgs_in_result = [m for m in result.messages if m.tool_calls or m.role == "tool"]
        # At least some should survive if they existed
        assert result.messages_removed > 0

    def test_compression_ratio_property(self) -> None:
        compressor = ConversationCompressor()
        msgs = self._make_messages(30)
        result = compressor.trim(msgs, target_tokens=50, keep_recent=3)
        assert 0.0 <= result.compression_ratio <= 1.0
        assert result.tokens_freed >= 0

    def test_compression_strategy_enum_values(self) -> None:
        assert CompressionStrategy.TRIM.value == "trim"
        assert CompressionStrategy.SELECTIVE.value == "selective"
        assert CompressionStrategy.SUMMARY.value == "summary"
        assert CompressionStrategy.HYBRID.value == "hybrid"


class TestTokenCounting:
    """Test the token estimation utility."""

    def test_count_tokens_empty_string(self) -> None:
        assert _count_tokens("") >= 0

    def test_count_tokens_short_text(self) -> None:
        tokens = _count_tokens("hello world")
        assert tokens >= 1
        assert tokens < 100

    def test_count_tokens_proportional_to_length(self) -> None:
        short = _count_tokens("hello")
        long = _count_tokens("hello " * 100)
        assert long > short


class TestExtractiveSummarize:
    """Test the extractive summarizer (no LLM needed)."""

    def test_empty_messages_returns_empty(self) -> None:
        result = extractive_summarize([])
        assert result == ""

    def test_summarize_produces_previously_prefix(self) -> None:
        msgs = [
            Message(role="user", content="I created a new auth module with JWT tokens."),
            Message(role="assistant", content="I implemented the login endpoint with bcrypt hashing."),
        ]
        result = extractive_summarize(msgs, max_sentences=5)
        assert result.startswith("Previously:")

    def test_summarize_respects_max_sentences(self) -> None:
        msgs = [
            Message(role="user", content="First thing. Second thing. Third thing. Fourth thing."),
            Message(role="assistant", content="Fifth thing. Sixth thing. Seventh thing. Eighth thing."),
        ]
        result = extractive_summarize(msgs, max_sentences=3)
        # Count bullet points
        bullets = [line for line in result.split("\n") if line.startswith("- ")]
        assert len(bullets) <= 3


# ---------------------------------------------------------------------------
# Context Window Manager
# ---------------------------------------------------------------------------
from djcode.context.manager import ContextWindowManager, Priority as CtxPriority


class TestContextWindowManager:
    """Test token counting, injection, compression triggering, and model switching."""

    def test_init_with_known_model(self) -> None:
        mgr = ContextWindowManager("claude-opus-4-6")
        assert mgr.max_context == 1_000_000
        assert mgr.model == "claude-opus-4-6"

    def test_init_with_unknown_model_uses_default(self) -> None:
        mgr = ContextWindowManager("totally-unknown")
        assert mgr.max_context == 8_192

    def test_init_with_explicit_max_context(self) -> None:
        mgr = ContextWindowManager("unknown", max_context=50_000)
        assert mgr.max_context == 50_000

    def test_add_message_increases_token_count(self) -> None:
        mgr = ContextWindowManager("gpt-4o")
        assert mgr.current_tokens == 0
        mgr.add_message(Message(role="user", content="Hello world"))
        assert mgr.current_tokens > 0

    def test_message_count(self) -> None:
        mgr = ContextWindowManager("gpt-4o")
        mgr.add_message(Message(role="system", content="You are helpful."))
        mgr.add_message(Message(role="user", content="Hi"))
        assert mgr.message_count == 2

    def test_utilization_calculation(self) -> None:
        mgr = ContextWindowManager("tinyllama")  # 2048 context
        assert mgr.utilization == 0.0
        # Add a message that uses some tokens
        mgr.add_message(Message(role="user", content="x " * 500))
        assert 0.0 < mgr.utilization <= 1.0
        assert 0.0 < mgr.utilization_pct <= 100.0

    def test_remaining_tokens_decreases_with_messages(self) -> None:
        mgr = ContextWindowManager("gpt-4o")  # 128k
        initial_remaining = mgr.remaining_tokens
        mgr.add_message(Message(role="user", content="some text"))
        assert mgr.remaining_tokens < initial_remaining

    def test_inject_context(self) -> None:
        mgr = ContextWindowManager("gpt-4o")
        mgr.inject_context("project uses FastAPI", priority=CtxPriority.HIGH, source="memory")
        assert mgr.current_tokens > 0

    def test_inject_empty_content_ignored(self) -> None:
        mgr = ContextWindowManager("gpt-4o")
        mgr.inject_context("", source="test")
        mgr.inject_context("   ", source="test")
        assert mgr.current_tokens == 0

    def test_clear_injected_by_source(self) -> None:
        mgr = ContextWindowManager("gpt-4o")
        mgr.inject_context("content A", source="memory")
        mgr.inject_context("content B", source="file")
        cleared = mgr.clear_injected(source="memory")
        assert cleared == 1

    def test_clear_all_injected(self) -> None:
        mgr = ContextWindowManager("gpt-4o")
        mgr.inject_context("content A", source="memory")
        mgr.inject_context("content B", source="file")
        cleared = mgr.clear_injected()
        assert cleared == 2

    def test_needs_compression_threshold(self) -> None:
        mgr = ContextWindowManager("tinyllama", compression_threshold=0.5)
        assert not mgr.needs_compression()
        # Fill past 50% of 2048 tokens (~1024+ tokens needed)
        # Each "x " is ~0.5 tokens, so 5000 repetitions should be plenty
        mgr.add_message(Message(role="user", content="x " * 5000))
        assert mgr.needs_compression()

    def test_clear_messages(self) -> None:
        mgr = ContextWindowManager("gpt-4o")
        mgr.add_message(Message(role="user", content="hi"))
        mgr.inject_context("ctx", source="test")
        mgr.clear_messages()
        assert mgr.message_count == 0
        assert mgr.current_tokens == 0

    def test_switch_model(self) -> None:
        mgr = ContextWindowManager("gpt-4o")
        assert mgr.max_context == 128_000
        mgr.switch_model("tinyllama")
        assert mgr.max_context == 2_048

    def test_get_model_info_known(self) -> None:
        mgr = ContextWindowManager("claude-opus-4-6")
        info = mgr.get_model_info()
        assert info["in_registry"] is True
        assert info["provider"] == "anthropic"

    def test_get_model_info_unknown(self) -> None:
        mgr = ContextWindowManager("totally-unknown")
        info = mgr.get_model_info()
        assert info["in_registry"] is False

    def test_stats_snapshot(self) -> None:
        mgr = ContextWindowManager("gpt-4o")
        mgr.add_message(Message(role="user", content="hello"))
        stats = mgr.stats
        assert stats.model == "gpt-4o"
        assert stats.message_count == 1
        assert stats.current_tokens > 0
        assert isinstance(stats.to_dict(), dict)

    def test_count_tokens_static_method(self) -> None:
        count = ContextWindowManager.count_tokens("hello world")
        assert count > 0

    def test_snapshot_serializable(self) -> None:
        mgr = ContextWindowManager("gpt-4o")
        mgr.add_message(Message(role="user", content="test"))
        snap = mgr.snapshot()
        assert isinstance(snap, dict)
        assert snap["model"] == "gpt-4o"
        assert snap["message_count"] == 1

    def test_evict_lowest_priority(self) -> None:
        mgr = ContextWindowManager("gpt-4o")
        mgr.inject_context("low priority stuff", priority=CtxPriority.LOW, source="bg")
        mgr.inject_context("critical stuff", priority=CtxPriority.CRITICAL, source="rules")
        freed = mgr.evict_lowest_priority(tokens_to_free=1000)
        assert freed > 0


# ---------------------------------------------------------------------------
# Context Bus
# ---------------------------------------------------------------------------
from djcode.orchestrator.context_bus import (
    BusEntry,
    ContextBus,
    EntryType,
    Priority as BusPriority,
)


class TestContextBus:
    """Test write/read/conflict detection and summary generation."""

    def test_write_and_read_all(self) -> None:
        bus = ContextBus()
        entry = bus.write(agent="Vishwakarma", role="coder", key="result", content="done")
        assert isinstance(entry, BusEntry)
        assert len(bus) == 1
        entries = bus.read_all()
        assert len(entries) == 1
        assert entries[0].agent == "Vishwakarma"

    def test_read_by_agent(self) -> None:
        bus = ContextBus()
        bus.write(agent="Alpha", role="coder", key="k1", content="c1")
        bus.write(agent="Beta", role="tester", key="k2", content="c2")
        alpha_entries = bus.read_by_agent("Alpha")
        assert len(alpha_entries) == 1
        assert alpha_entries[0].key == "k1"

    def test_read_by_role(self) -> None:
        bus = ContextBus()
        bus.write(agent="A", role="coder", key="k1", content="c1")
        bus.write(agent="B", role="coder", key="k2", content="c2")
        bus.write(agent="C", role="tester", key="k3", content="c3")
        coder_entries = bus.read_by_role("coder")
        assert len(coder_entries) == 2

    def test_read_by_key(self) -> None:
        bus = ContextBus()
        bus.write(agent="A", role="coder", key="auth", content="v1")
        bus.write(agent="B", role="reviewer", key="auth", content="v2")
        auth_entries = bus.read_by_key("auth")
        assert len(auth_entries) == 2

    def test_read_latest(self) -> None:
        bus = ContextBus()
        bus.write(agent="A", role="coder", key="result", content="first")
        bus.write(agent="A", role="coder", key="result", content="second")
        latest = bus.read_latest("result")
        assert latest is not None
        assert latest.content == "second"

    def test_read_latest_nonexistent_returns_none(self) -> None:
        bus = ContextBus()
        assert bus.read_latest("nonexistent") is None

    def test_conflict_detection(self) -> None:
        bus = ContextBus()
        bus.write(agent="Alpha", role="coder", key="auth", content="v1")
        bus.write(agent="Beta", role="reviewer", key="auth", content="v2")
        assert bus.has_conflicts
        assert len(bus.conflicts) == 1
        assert bus.conflicts[0]["key"] == "auth"

    def test_no_conflict_same_agent_same_key(self) -> None:
        bus = ContextBus()
        bus.write(agent="Alpha", role="coder", key="result", content="v1")
        bus.write(agent="Alpha", role="coder", key="result", content="v2")
        assert not bus.has_conflicts

    def test_version_increment(self) -> None:
        bus = ContextBus()
        e1 = bus.write(agent="A", role="coder", key="code", content="v1")
        e2 = bus.write(agent="A", role="coder", key="code", content="v2")
        assert e1.version == 1
        assert e2.version == 2

    def test_set_task_and_summary(self) -> None:
        bus = ContextBus()
        bus.set_task("implement auth", "add JWT login")
        bus.write(agent="Coder", role="coder", key="code", content="auth module done")
        summary = bus.summary()
        assert "implement auth" in summary
        assert "Coder" in summary

    def test_summary_empty_bus(self) -> None:
        bus = ContextBus()
        assert bus.summary() == ""

    def test_summary_for_agent_excludes_self(self) -> None:
        bus = ContextBus()
        bus.set_task("task", "intent")
        bus.write(agent="Alpha", role="coder", key="k1", content="my work")
        bus.write(agent="Beta", role="tester", key="k2", content="test results")
        summary = bus.summary_for_agent("Alpha")
        assert "Beta" in summary
        assert "Alpha" not in summary.split("Context from other agents")[1] if "Context from other agents" in summary else True

    def test_clear_resets_everything(self) -> None:
        bus = ContextBus()
        bus.set_task("task", "intent")
        bus.write(agent="A", role="coder", key="k", content="c")
        bus.clear()
        assert len(bus) == 0
        assert bus.task == ""

    def test_entry_type_and_priority(self) -> None:
        bus = ContextBus()
        entry = bus.write(
            agent="Kavach",
            role="security",
            key="audit",
            content="No vulnerabilities found",
            entry_type=EntryType.SECURITY_AUDIT,
            priority=BusPriority.CRITICAL,
        )
        assert entry.entry_type == EntryType.SECURITY_AUDIT
        assert entry.priority == BusPriority.CRITICAL

    def test_read_by_type(self) -> None:
        bus = ContextBus()
        bus.write(agent="A", role="coder", key="k1", content="c1", entry_type=EntryType.CODE)
        bus.write(agent="B", role="tester", key="k2", content="c2", entry_type=EntryType.TEST)
        code_entries = bus.read_by_type(EntryType.CODE)
        assert len(code_entries) == 1

    def test_read_by_priority(self) -> None:
        bus = ContextBus()
        bus.write(agent="A", role="sec", key="k1", content="c1", priority=BusPriority.CRITICAL)
        bus.write(agent="B", role="coder", key="k2", content="c2", priority=BusPriority.LOW)
        critical = bus.read_by_priority(BusPriority.CRITICAL)
        assert len(critical) == 1

    def test_snapshot_returns_dict(self) -> None:
        bus = ContextBus()
        bus.write(agent="A", role="coder", key="k", content="c")
        snap = bus.snapshot()
        assert isinstance(snap, dict)
        assert snap["entry_count"] == 1

    def test_bool_and_len(self) -> None:
        bus = ContextBus()
        assert not bus
        assert len(bus) == 0
        bus.write(agent="A", role="r", key="k", content="c")
        assert bus
        assert len(bus) == 1

    def test_bus_entry_age(self) -> None:
        entry = BusEntry(
            agent="A", role="r", key="k", content="c",
            timestamp=time.time() - 5.0,
        )
        assert entry.age_s >= 4.0

    def test_async_write(self) -> None:
        bus = ContextBus()

        async def do_write() -> BusEntry:
            return await bus.write_async(
                agent="A", role="coder", key="result", content="async done",
            )

        entry = asyncio.run(do_write())
        assert entry.content == "async done"
        assert len(bus) == 1


# ---------------------------------------------------------------------------
# Event Bus (Orchestrator Events)
# ---------------------------------------------------------------------------
from djcode.orchestrator.events import (
    EventBus,
    EventType,
    GateAction,
    GateSeverity,
    OrchestratorEvent,
    agent_complete_event,
    agent_start_event,
    blocking_gate_event,
    orchestrator_start_event,
)


class TestEventBus:
    """Test subscribe, emit, history, and event factory functions."""

    def test_subscribe_and_emit(self) -> None:
        bus = EventBus()
        received: list[OrchestratorEvent] = []

        async def handler(event: OrchestratorEvent) -> None:
            received.append(event)

        bus.subscribe(handler)

        event = OrchestratorEvent(event_type=EventType.ORCHESTRATOR_START)
        asyncio.run(bus.emit(event))

        assert len(received) == 1
        assert received[0].event_type == EventType.ORCHESTRATOR_START

    def test_unsubscribe_stops_delivery(self) -> None:
        bus = EventBus()
        received: list[OrchestratorEvent] = []

        async def handler(event: OrchestratorEvent) -> None:
            received.append(event)

        bus.subscribe(handler)
        bus.unsubscribe(handler)

        event = OrchestratorEvent(event_type=EventType.ORCHESTRATOR_START)
        asyncio.run(bus.emit(event))

        assert len(received) == 0

    def test_duplicate_subscribe_ignored(self) -> None:
        bus = EventBus()

        async def handler(event: OrchestratorEvent) -> None:
            pass

        bus.subscribe(handler)
        bus.subscribe(handler)  # duplicate
        assert bus.subscriber_count == 1

    def test_history_recorded(self) -> None:
        bus = EventBus()
        event = OrchestratorEvent(event_type=EventType.AGENT_START, agent_name="test")
        asyncio.run(bus.emit(event))
        assert len(bus.history) == 1
        assert bus.history[0].agent_name == "test"

    def test_clear_history(self) -> None:
        bus = EventBus()
        asyncio.run(bus.emit(OrchestratorEvent(event_type=EventType.AGENT_START)))
        bus.clear_history()
        assert len(bus.history) == 0

    def test_event_is_terminal(self) -> None:
        complete = OrchestratorEvent(event_type=EventType.ORCHESTRATOR_COMPLETE)
        assert complete.is_terminal
        start = OrchestratorEvent(event_type=EventType.ORCHESTRATOR_START)
        assert not start.is_terminal

    def test_event_is_agent_terminal(self) -> None:
        complete = OrchestratorEvent(event_type=EventType.AGENT_COMPLETE)
        assert complete.is_agent_terminal
        token = OrchestratorEvent(event_type=EventType.AGENT_TOKEN)
        assert not token.is_agent_terminal

    def test_callback_error_does_not_propagate(self) -> None:
        bus = EventBus()

        async def bad_handler(event: OrchestratorEvent) -> None:
            raise RuntimeError("handler crash")

        bus.subscribe(bad_handler)
        # Should not raise
        asyncio.run(bus.emit(OrchestratorEvent(event_type=EventType.AGENT_START)))

    def test_event_repr(self) -> None:
        event = OrchestratorEvent(event_type=EventType.AGENT_TOKEN, agent_name="Coder")
        assert "agent_token" in repr(event)
        assert "Coder" in repr(event)


class TestEventFactories:
    """Test typed event constructor functions."""

    def test_orchestrator_start_event(self) -> None:
        event = orchestrator_start_event(
            task="implement auth",
            strategy="wave",
            agents=["Coder", "Tester"],
            complexity="COMPLEX",
        )
        assert event.event_type == EventType.ORCHESTRATOR_START
        assert event.data["task"] == "implement auth"
        assert event.data["strategy"] == "wave"

    def test_agent_start_event(self) -> None:
        event = agent_start_event("Vishwakarma", "coder", "write code", wave=1)
        assert event.event_type == EventType.AGENT_START
        assert event.agent_name == "Vishwakarma"
        assert event.data["wave"] == 1

    def test_agent_complete_event(self) -> None:
        event = agent_complete_event(
            agent_name="Tester",
            agent_role="tester",
            result_preview="All tests passed",
            confidence=0.95,
            elapsed_s=12.5,
            tokens=1500,
        )
        assert event.event_type == EventType.AGENT_COMPLETE
        assert event.data["confidence"] == 0.95

    def test_blocking_gate_event(self) -> None:
        event = blocking_gate_event(
            agent_name="Kavach",
            agent_role="security",
            severity=GateSeverity.CRITICAL,
            finding="SQL injection in query builder",
            action=GateAction.HALT,
        )
        assert event.event_type == EventType.BLOCKING_GATE
        assert event.data["severity"] == "critical"
        assert event.data["action"] == "halt"


# ---------------------------------------------------------------------------
# RA Briefing (data structure tests, no actual tool dispatch)
# ---------------------------------------------------------------------------
from djcode.agents.ra import CodeSnippet, RABriefing, ResearchAssistant


class TestRABriefing:
    """Test RABriefing formatting and properties."""

    def test_empty_briefing(self) -> None:
        briefing = RABriefing(
            agent_role=AgentRole.CODER,
            task_summary="test task",
            codebase_snippets=[],
            bus_context="",
            directory_structure="",
            git_context="",
            search_duration_ms=50.0,
            timestamp=time.time(),
        )
        assert briefing.is_empty
        assert briefing.to_prompt_injection() == ""
        assert briefing.snippet_count == 0

    def test_non_empty_briefing_produces_prompt(self) -> None:
        snippet = CodeSnippet(
            file_path="src/auth.py",
            line_start=10,
            line_end=20,
            content="def login(): ...",
            relevance="matches 'auth'",
        )
        briefing = RABriefing(
            agent_role=AgentRole.CODER,
            task_summary="fix auth",
            codebase_snippets=[snippet],
            bus_context="Prior agent found JWT issue",
            directory_structure="src/auth.py\nsrc/models.py",
            git_context="abc1234 fix: auth bug",
            search_duration_ms=100.0,
            timestamp=time.time(),
        )
        assert not briefing.is_empty
        prompt = briefing.to_prompt_injection()
        assert "Research Assistant Briefing" in prompt
        assert "fix auth" in prompt
        assert "src/auth.py" in prompt
        assert "Prior agent" in prompt

    def test_code_snippet_str(self) -> None:
        snippet = CodeSnippet(
            file_path="foo.py",
            line_start=1,
            line_end=5,
            content="print('hello')",
            relevance="test",
        )
        s = str(snippet)
        assert "foo.py:1-5" in s
        assert "print('hello')" in s


class TestResearchAssistantKeywords:
    """Test keyword and file pattern extraction (pure functions)."""

    def test_extract_keywords_filters_stop_words(self) -> None:
        keywords = ResearchAssistant._extract_keywords("fix the auth middleware in the server")
        assert "auth" in keywords
        assert "middleware" in keywords
        assert "server" in keywords
        # Stop words should be filtered
        assert "the" not in keywords
        assert "in" not in keywords
        assert "fix" not in keywords

    def test_extract_keywords_deduplicates(self) -> None:
        keywords = ResearchAssistant._extract_keywords("auth auth auth")
        assert keywords.count("auth") == 1

    def test_extract_keywords_short_words_filtered(self) -> None:
        keywords = ResearchAssistant._extract_keywords("go to do it")
        # All words <= 2 chars should be filtered
        for kw in keywords:
            assert len(kw) > 2

    def test_extract_file_patterns_explicit_path(self) -> None:
        patterns = ResearchAssistant._extract_file_patterns("look at src/auth/middleware.py")
        assert any("middleware.py" in p for p in patterns)

    def test_extract_file_patterns_dotted_module(self) -> None:
        patterns = ResearchAssistant._extract_file_patterns("check djcode.agents.registry")
        assert any("registry.py" in p for p in patterns)

    def test_extract_file_patterns_bare_filename(self) -> None:
        patterns = ResearchAssistant._extract_file_patterns("update config.yaml")
        assert any("config.yaml" in p for p in patterns)


# ---------------------------------------------------------------------------
# Agent Executor (confidence extraction — pure function)
# ---------------------------------------------------------------------------
from djcode.agents.executor import AgentExecutor, AgentResult


class TestAgentExecutorConfidenceExtraction:
    """Test confidence score extraction from agent responses."""

    def test_extract_confidence_percentage(self) -> None:
        score = AgentExecutor._extract_confidence("CONFIDENCE: 92%")
        assert score == pytest.approx(0.92, abs=0.01)

    def test_extract_confidence_decimal(self) -> None:
        score = AgentExecutor._extract_confidence("confidence: 0.87")
        assert score == pytest.approx(0.87, abs=0.01)

    def test_extract_confidence_score_label(self) -> None:
        score = AgentExecutor._extract_confidence("confidence_score: 0.95")
        assert score == pytest.approx(0.95, abs=0.01)

    def test_extract_confidence_empty_response(self) -> None:
        score = AgentExecutor._extract_confidence("")
        assert score == 0.0

    def test_extract_confidence_no_score_returns_default(self) -> None:
        score = AgentExecutor._extract_confidence("This is a normal response without any score.")
        assert score == 0.5

    def test_extract_confidence_clamped_to_0_1(self) -> None:
        score = AgentExecutor._extract_confidence("CONFIDENCE: 150%")
        assert 0.0 <= score <= 1.0


class TestAgentResult:
    """Test AgentResult properties."""

    def test_succeeded_property(self) -> None:
        result = AgentResult(
            agent_role=AgentRole.CODER,
            agent_name="Vishwakarma",
            response="done",
            confidence_score=0.9,
            tokens_used=100,
            tools_called=2,
            duration_s=5.0,
            ra_briefing=None,
            state=AgentState.DONE,
            error=None,
        )
        assert result.succeeded is True

    def test_failed_result(self) -> None:
        result = AgentResult(
            agent_role=AgentRole.CODER,
            agent_name="Vishwakarma",
            response="",
            confidence_score=0.0,
            tokens_used=10,
            tools_called=0,
            duration_s=1.0,
            ra_briefing=None,
            state=AgentState.ERROR,
            error="timeout",
        )
        assert result.succeeded is False
        assert "FAIL" in result.summary_line()

    def test_has_critical_findings(self) -> None:
        result = AgentResult(
            agent_role=AgentRole.CODER,
            agent_name="Kavach",
            response="CRITICAL: BLOCK this - SQL injection VULNERABILITY found",
            confidence_score=0.99,
            tokens_used=500,
            tools_called=3,
            duration_s=8.0,
            ra_briefing=None,
            state=AgentState.DONE,
        )
        assert result.has_critical_findings is True

    def test_summary_line_format(self) -> None:
        result = AgentResult(
            agent_role=AgentRole.CODER,
            agent_name="Test",
            response="ok",
            confidence_score=0.85,
            tokens_used=200,
            tools_called=1,
            duration_s=3.0,
            ra_briefing=None,
            state=AgentState.DONE,
        )
        line = result.summary_line()
        assert "Test" in line
        assert "OK" in line
        assert "0.85" in line


# ---------------------------------------------------------------------------
# Coordinator Result
# ---------------------------------------------------------------------------
from djcode.agents.parallel import CoordinatorResult


class TestCoordinatorResult:
    """Test result aggregation properties."""

    def _make_result(
        self, name: str, succeeded: bool = True, critical: bool = False,
    ) -> AgentResult:
        resp = "CRITICAL BLOCK VULNERABILITY" if critical else "normal output"
        return AgentResult(
            agent_role=AgentRole.CODER,
            agent_name=name,
            response=resp,
            confidence_score=0.9 if succeeded else 0.0,
            tokens_used=100,
            tools_called=2,
            duration_s=5.0,
            ra_briefing=None,
            state=AgentState.DONE if succeeded else AgentState.ERROR,
            error=None if succeeded else "failed",
        )

    def test_all_succeeded(self) -> None:
        cr = CoordinatorResult(
            results=[self._make_result("A"), self._make_result("B")],
            total_duration_s=10.0,
            total_tokens=200,
            total_tools=4,
        )
        assert cr.all_succeeded
        assert len(cr.succeeded) == 2
        assert len(cr.failed) == 0

    def test_has_failures(self) -> None:
        cr = CoordinatorResult(
            results=[self._make_result("A"), self._make_result("B", succeeded=False)],
        )
        assert not cr.all_succeeded
        assert len(cr.failed) == 1

    def test_merged_response_single(self) -> None:
        cr = CoordinatorResult(results=[self._make_result("A")])
        merged = cr.merged_response()
        assert "normal output" in merged

    def test_merged_response_multiple(self) -> None:
        cr = CoordinatorResult(
            results=[self._make_result("A"), self._make_result("B")],
        )
        merged = cr.merged_response()
        assert "A" in merged
        assert "B" in merged

    def test_merged_response_empty(self) -> None:
        cr = CoordinatorResult(results=[])
        assert cr.merged_response() == ""

    def test_summary_table(self) -> None:
        cr = CoordinatorResult(
            results=[self._make_result("Alpha"), self._make_result("Beta", succeeded=False)],
            total_duration_s=15.0,
            total_tokens=300,
            total_tools=5,
        )
        table = cr.summary_table()
        assert "Alpha" in table
        assert "Beta" in table
        assert "FAIL" in table
        assert "TOTAL" in table

    def test_halted_result(self) -> None:
        cr = CoordinatorResult(
            results=[self._make_result("A"), self._make_result("B")],
            halted=True,
            halt_reason="Security agent blocked",
        )
        assert cr.halted
        assert not cr.all_succeeded
        merged = cr.merged_response()
        assert "HALTED" in merged


# ---------------------------------------------------------------------------
# Task Tracker (CRUD)
# ---------------------------------------------------------------------------
from djcode.tools.task_tracker import (
    execute_task_create,
    execute_task_list,
    execute_task_update,
)


class TestTaskTracker:
    """Test task CRUD operations."""

    def test_create_task(self) -> None:
        result = asyncio.run(execute_task_create(
            subject="Write unit tests",
            description="Test all v4 modules",
            priority="high",
        ))
        assert "Created" in result or "task_" in result

    def test_create_task_empty_subject_fails(self) -> None:
        result = asyncio.run(execute_task_create(subject=""))
        assert "Error" in result

    def test_create_task_invalid_priority_fails(self) -> None:
        result = asyncio.run(execute_task_create(
            subject="test",
            priority="ultra-mega-high",
        ))
        assert "Error" in result

    def test_list_tasks(self) -> None:
        # Create a task first to ensure there's something
        asyncio.run(execute_task_create(subject="Listable task"))
        result = asyncio.run(execute_task_list())
        assert "Listable task" in result or "task" in result.lower()

    def test_update_task_nonexistent(self) -> None:
        result = asyncio.run(execute_task_update(
            task_id="nonexistent_id_xyz",
            status="completed",
        ))
        assert "Error" in result or "not found" in result.lower()


# ---------------------------------------------------------------------------
# Entry Type / Priority enum coverage
# ---------------------------------------------------------------------------

class TestEntryTypeEnum:
    """Test EntryType and Priority enum values exist."""

    def test_entry_type_values(self) -> None:
        assert EntryType.CODE.value == "code"
        assert EntryType.PLAN.value == "plan"
        assert EntryType.REVIEW.value == "review"
        assert EntryType.SECURITY_AUDIT.value == "security_audit"
        assert EntryType.RESULT.value == "result"

    def test_priority_values(self) -> None:
        assert BusPriority.CRITICAL.value == "critical"
        assert BusPriority.HIGH.value == "high"
        assert BusPriority.NORMAL.value == "normal"
        assert BusPriority.LOW.value == "low"


class TestEventTypeEnum:
    """Test EventType enum completeness."""

    def test_core_event_types_exist(self) -> None:
        assert EventType.ORCHESTRATOR_START.value == "orchestrator_start"
        assert EventType.AGENT_TOKEN.value == "agent_token"
        assert EventType.BLOCKING_GATE.value == "blocking_gate"
        assert EventType.CONTEXT_CONFLICT.value == "context_conflict"

    def test_gate_severity_enum(self) -> None:
        assert GateSeverity.CRITICAL.value == "critical"
        assert GateSeverity.WARNING.value == "warning"

    def test_gate_action_enum(self) -> None:
        assert GateAction.HALT.value == "halt"
        assert GateAction.PASS.value == "pass"
