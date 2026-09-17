"""W8: ``CoreSession``, the steer drain point, and the queue that cannot lose text.

The first test in this file is the wave's real deliverable. Every other property
here is recoverable by reading the code; *that* one is not, because the bug it
guards against is invisible until a provider rejects the transcript -- and on
Ollama it is never rejected at all, only silently mis-answered
(``provider.py::_msg_to_ollama`` never sends ``tool_call_id``, so pairing there
is positional).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil

import pytest

from djcode.core.events import EventBus, EventType
from djcode.core.outcome import ToolOutcome
from djcode.core.permissions import Decision, DecisionAction
from djcode.core.session import CoreSession, SessionOptions
from djcode.provider import Message, Provider, ProviderConfig

requires_cargo = pytest.mark.skipif(
    shutil.which("cargo") is None and not os.environ.get("DJCODE_DAF_ENGINE"),
    reason="DAF engine needs a Rust toolchain; set DJCODE_DAF_ENGINE for a prebuilt one",
)


def _tool_call(index, call_id, name, arguments):
    return {
        "index": index,
        "id": call_id,
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _ok(content="ok"):
    # `ok_source="handler"` is what makes `ToolOutcome.ok` mean anything (W3).
    return ToolOutcome(content=content, ok=True, details={"ok_source": "handler"})


def _session(provider, *, engine="native", **kwargs):
    """A session whose workflow engine is pinned.

    Default ``native``: the DAF branch builds a Rust binary on first use, and a
    test that only needs a tool to run should not pay for that. The one test
    that MUST see the real subprocess await -- the steer drain point -- asks for
    ``daf`` explicitly and takes the prebuilt engine from the `daf_runtime`
    fixture.
    """
    options = SessionOptions(auto_accept=True, **kwargs)
    session = CoreSession({}, provider=provider, event_bus=EventBus(), options=options)
    session.operator.workflow.mode = engine
    return session


# ── The drain point ────────────────────────────────────────────────────────


@requires_cargo
def test_steer_mid_turn_lands_at_a_round_boundary_and_keeps_tool_pairing(
    monkeypatch, daf_runtime
):
    """A steer typed WHILE two tool calls are in flight must not split the pair.

    The steer is issued from inside the dispatcher -- point E of the audit, the
    exact moment `_send` is parked on `workflow.one` and the user is most likely
    to be typing -- and the assertion is on the shape of `messages` as the
    SECOND request sees it. The pattern is lifted from
    `test_operator_cancel_completes_tool_protocol`, which proves the same class
    of property for cancellation.

    Runs on the DAF engine deliberately: `workflow.one` really does spawn the
    Rust subprocess here, so the await the steer races is the production one and
    not a monkeypatched stand-in.
    """
    import djcode.agents.operator as module
    import djcode.workflow as workflow

    monkeypatch.setattr(workflow, "CONFIG_DIR", daf_runtime)
    seen: dict = {}

    class Fixture(Provider):
        calls = 0

        async def chat_openai_compat(self, messages, stream=True):
            Fixture.calls += 1
            if Fixture.calls == 1:
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    _tool_call(0, "call-a", "file_read", {"path": "alpha"}),
                                    _tool_call(1, "call-b", "file_read", {"path": "beta"}),
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            else:
                seen["roles"] = [m.role for m in messages[-4:]]
                seen["ids"] = [m.tool_call_id for m in messages[-3:-1]]
                seen["last"] = messages[-1].content
                seen["names"] = [m.name for m in messages[-3:-1]]
                yield {"choices": [{"delta": {"content": "acknowledged"}, "finish_reason": "stop"}]}

    async def run():
        provider = Fixture(
            ProviderConfig("custom", "https://example.invalid", "fixture", "fixture")
        )
        session = _session(provider, engine="daf")

        async def steering_dispatch(name, arguments):
            # Fires while the first of two tool calls is being answered: the
            # assistant(tool_calls) message is already appended and only ONE of
            # its two ids has a `tool` answer. A `steer()` that touched
            # `messages` here would produce assistant -> tool -> user -> tool.
            if arguments.get("path") == "alpha":
                session.steer("Use uv, not pip.")
                assert session.operator.pending_steer == ["Use uv, not pip."]
            return _ok()

        monkeypatch.setattr(module, "dispatch_tool", steering_dispatch)
        try:
            events = [event async for event in session.send("Read both files")]
        finally:
            await session.close()
        return events

    events = asyncio.run(run())

    # The shape the second request went out with. This is the whole test.
    assert seen["roles"] == ["assistant", "tool", "tool", "user"], seen
    assert seen["ids"] == ["call-a", "call-b"], seen
    assert seen["names"] == ["file_read", "file_read"], seen
    assert seen["last"] == "Use uv, not pip."
    # Delivered exactly once, and the slot is empty afterwards.
    assert [e.event_type for e in events].count(EventType.COMPLETE) == 1
    delivered = [e for e in events if e.event_type is EventType.STEER and e.data["delivered"]]
    assert [e.data["text"] for e in delivered] == ["Use uv, not pip."]


def test_steer_merges_rather_than_emitting_two_user_messages_in_a_row(monkeypatch):
    """Round 0 has no tool round behind it: the last message is the prompt itself.

    Appending there would produce user -> user. Every provider adapter in
    ``providers/`` emits one turn per message, so the merge is what keeps the
    transcript in a shape they all already handle.
    """
    from djcode.agents.operator import Operator

    class Fixture(Provider):
        async def chat_openai_compat(self, messages, stream=True):
            yield {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]}

    provider = Fixture(ProviderConfig("custom", "https://example.invalid", "fixture", "fixture"))
    operator = Operator(provider, auto_accept=True)
    operator.messages.append(Message(role="user", content="original prompt"))
    operator.steer("and use uv")
    operator._drain_steer()

    assert [m.role for m in operator.messages] == ["system", "user"]
    assert operator.messages[-1].content == "original prompt\n\nand use uv"
    assert operator.pending_steer == []


def test_a_drained_steer_cannot_split_an_assistant_tool_group(monkeypatch):
    """The compaction guard. ``_drop_oldest_group`` keeps a request and its results
    together only while they are adjacent -- a mis-injected ``user`` message makes
    the tool messages independently evictable, and W4 persists that order to disk,
    so ``/resume`` would replay the corruption forever."""
    from djcode.context.compressor import ConversationCompressor

    call = Message(role="assistant", content="", tool_calls=[{"id": "a"}, {"id": "b"}])
    good = [
        call,
        Message(role="tool", tool_call_id="a", content="ra"),
        Message(role="tool", tool_call_id="b", content="rb"),
        Message(role="user", content="steer"),
    ]
    assert ConversationCompressor._drop_oldest_group(list(good)) == 3

    bad = [
        call,
        Message(role="user", content="steer"),
        Message(role="tool", tool_call_id="a", content="ra"),
        Message(role="tool", tool_call_id="b", content="rb"),
    ]
    # Only the assistant goes; both tool results survive it as orphans.
    assert ConversationCompressor._drop_oldest_group(list(bad)) == 1


# ── Queue, cancel and take_queued (P0-5) ───────────────────────────────────


def test_queue_is_delivered_as_the_next_turn_after_complete(monkeypatch):
    class Fixture(Provider):
        prompts: list[str] = []

        async def chat_openai_compat(self, messages, stream=True):
            Fixture.prompts.append(messages[-1].content)
            yield {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}

    async def run():
        provider = Fixture(
            ProviderConfig("custom", "https://example.invalid", "fixture", "fixture")
        )
        session = _session(provider)
        session.queue("second question")
        try:
            events = [event async for event in session.send("first question")]
        finally:
            await session.close()
        return events

    Fixture.prompts = []
    events = asyncio.run(run())

    kinds = [e.event_type for e in events]
    assert kinds.count(EventType.COMPLETE) == 2, kinds
    # The queued text is delivered AFTER the first COMPLETE, never before.
    first_complete = kinds.index(EventType.COMPLETE)
    queue_delivery = next(
        i
        for i, e in enumerate(events)
        if e.event_type is EventType.QUEUE and e.data["action"] == "delivered"
    )
    assert queue_delivery > first_complete
    assert Fixture.prompts[0].startswith("first question")
    assert Fixture.prompts[1].startswith("second question")


def test_cancel_returns_both_slots_and_take_queued_is_the_only_exit(monkeypatch):
    """B5: ``app.py``'s ``finally`` could neither deliver queued text nor hand it
    back. Here a cancel moves the queue AND the undelivered steer into one sink,
    and ``take_queued`` is idempotent so a composer cannot double-paste."""
    import djcode.agents.operator as module

    class Fixture(Provider):
        async def chat_openai_compat(self, messages, stream=True):
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [_tool_call(0, "hang", "file_read", {"path": "x"})]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }

    async def run():
        started = asyncio.Event()

        async def hang(name, arguments):
            started.set()
            await asyncio.sleep(60)
            return _ok()

        monkeypatch.setattr(module, "dispatch_tool", hang)
        provider = Fixture(
            ProviderConfig("custom", "https://example.invalid", "fixture", "fixture")
        )
        session = _session(provider)

        async def consume():
            return [event async for event in session.send("start")]

        task = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), timeout=10)
        session.queue("queued while busy")
        session.steer("steered while busy")
        session.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        try:
            return session.take_queued(), session.take_queued()
        finally:
            await session.close()

    returned, again = asyncio.run(run())
    assert sorted(returned) == ["queued while busy", "steered while busy"]
    assert again == [], "take_queued must be idempotent or the composer double-pastes"


def test_keyboard_interrupt_also_returns_the_text(monkeypatch):
    """Windows gate: ``repl_runtime`` cannot install a SIGINT handler on the
    Proactor loop, so Ctrl+C during a turn arrives as ``KeyboardInterrupt``, not
    ``CancelledError``. Both exits must reach ``take_queued``."""
    import djcode.agents.operator as module

    class Fixture(Provider):
        async def chat_openai_compat(self, messages, stream=True):
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [_tool_call(0, "hang", "file_read", {"path": "x"})]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }

    async def run():
        started = asyncio.Event()

        async def hang(name, arguments):
            started.set()
            await asyncio.sleep(60)
            return _ok()

        monkeypatch.setattr(module, "dispatch_tool", hang)
        provider = Fixture(
            ProviderConfig("custom", "https://example.invalid", "fixture", "fixture")
        )
        session = _session(provider)
        session.queue("survives the interrupt")

        stream = session.send("start")

        async def pump():
            async for _event in stream:
                pass

        task = asyncio.create_task(pump())
        await asyncio.wait_for(started.wait(), timeout=10)
        # Simulate the interrupt reaching the generator, which is what
        # `run_interruptible`'s KeyboardInterrupt path does on this box.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        try:
            return session.take_queued()
        finally:
            await session.close()

    assert asyncio.run(run()) == ["survives the interrupt"]


# ── Composition ────────────────────────────────────────────────────────────


def test_approval_receives_a_toolrequest_and_a_decision_is_honoured(monkeypatch):
    """SSOT 2.3's ``approval: (ToolRequest) -> Awaitable[Decision]`` adapted onto
    ``Operator.approval_callback``'s ``(name, args) -> bool`` WITHOUT changing the
    Operator's arity -- ``_approve_repeated_call`` is registered as the 3-argument
    dispatch callback and forwards to the 2-argument one."""
    import djcode.agents.operator as module

    requests = []

    class Fixture(Provider):
        calls = 0

        async def chat_openai_compat(self, messages, stream=True):
            Fixture.calls += 1
            if Fixture.calls == 1:
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    _tool_call(
                                        0,
                                        "c1",
                                        "file_write",
                                        {"path": "w8-approval.txt", "content": "x"},
                                    )
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            else:
                yield {"choices": [{"delta": {"content": "fine"}, "finish_reason": "stop"}]}

    async def approve(request):
        requests.append(request)
        return Decision(DecisionAction.DENY, comment="not that one")

    async def run():
        async def never(name, arguments):
            raise AssertionError("a denied tool must not dispatch")

        monkeypatch.setattr(module, "dispatch_tool", never)
        provider = Fixture(
            ProviderConfig("custom", "https://example.invalid", "fixture", "fixture")
        )
        session = CoreSession(
            {},
            provider=provider,
            event_bus=EventBus(),
            approval=approve,
            options=SessionOptions(),
        )
        session.operator.workflow.mode = "native"
        try:
            events = [event async for event in session.send("do it")]
        finally:
            await session.close()
        return events

    Fixture.calls = 0
    events = asyncio.run(run())
    assert [r.tool for r in requests] == ["file_write"]
    assert requests[0].path == "w8-approval.txt"
    kinds = [e.event_type for e in events]
    assert EventType.PERMISSION_REQUEST in kinds
    assert EventType.PERMISSION_DECIDED in kinds
    decided = next(e for e in events if e.event_type is EventType.PERMISSION_DECIDED)
    assert decided.data["allowed"] is False


def test_permission_engine_is_the_operators_own(monkeypatch):
    """Two engines would be two rule sets; the front-end holding the wrong one
    would be enforcing nothing. ``CoreSession`` reads, never constructs."""

    class Fixture(Provider):
        async def chat_openai_compat(self, messages, stream=True):
            yield {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]}

    provider = Fixture(ProviderConfig("custom", "https://example.invalid", "fixture", "fixture"))
    session = _session(provider)
    assert session.permissions is session.operator.permissions
    # W5's store was surface-attached and only repl.py ever did it; holding a
    # CoreSession is now enough.
    assert session.operator.dispatch_ctx.checkpoints is session.checkpoint_store
    assert session.operator.session_id == session.session_id


def test_close_releases_the_provider_and_is_idempotent():
    class Fixture(Provider):
        closed = 0

        async def chat_openai_compat(self, messages, stream=True):
            yield {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]}

        async def close(self):
            Fixture.closed += 1
            await super().close()

    async def run():
        provider = Fixture(
            ProviderConfig("custom", "https://example.invalid", "fixture", "fixture")
        )
        session = _session(provider)
        await session.close()
        await session.close()
        with pytest.raises(RuntimeError):
            async for _ in session.send("too late"):
                pass

    Fixture.closed = 0
    asyncio.run(run())
    assert Fixture.closed == 1


def test_fork_shares_history_up_to_the_cut_and_is_independent():
    """``at_message_id`` is a ``conversations`` row id, not an index into
    ``messages``: ``Message`` has no ``id`` field and ``SessionDB.fork_session``
    keys on the row. The fork must not close the parent's provider."""

    class Fixture(Provider):
        closed = 0

        async def chat_openai_compat(self, messages, stream=True):
            yield {"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}]}

        async def close(self):
            Fixture.closed += 1
            await super().close()

    async def run():
        provider = Fixture(
            ProviderConfig("custom", "https://example.invalid", "fixture", "fixture")
        )
        session = _session(provider)
        async for _ in session.send("first"):
            pass
        async for _ in session.send("second"):
            pass
        child = await session.fork()
        try:
            assert child.session_id != session.session_id
            assert child.event_bus is not session.event_bus
            contents = [m.content for m in child.operator.messages if m.role == "user"]
            assert any("first" in c for c in contents)
            assert any("second" in c for c in contents)
            # Independent from here on.
            child.operator.messages.append(Message(role="user", content="only in the fork"))
            assert all(
                "only in the fork" not in (m.content or "") for m in session.operator.messages
            )
            await child.close()
            assert Fixture.closed == 0, "a fork must not close its parent's provider"
        finally:
            await session.close()

    Fixture.closed = 0
    asyncio.run(run())
    assert Fixture.closed == 1


def test_context_stats_and_bill_of_materials():
    class Fixture(Provider):
        async def chat_openai_compat(self, messages, stream=True):
            yield {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]}

    provider = Fixture(ProviderConfig("custom", "https://example.invalid", "fixture", "fixture"))
    session = _session(provider)
    session.operator.messages.append(Message(role="user", content="a question " * 50))
    session.operator.context_manager.replace_messages(session.operator.messages)

    stats = session.context_stats()
    assert stats.message_count == len(session.operator.messages)

    bill = session.bill_of_materials()
    assert bill, "the bill of materials must account for the transcript"
    assert [item.tokens for item in bill] == sorted(
        [item.tokens for item in bill], reverse=True
    )
    assert any(item.source == "message:user" for item in bill)
    assert round(sum(item.share_pct for item in bill)) == 100
