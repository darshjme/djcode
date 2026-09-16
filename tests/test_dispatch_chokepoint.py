"""W3 green gate for the one chokepoint: ``djcode.tools.dispatch_tool``.

Scope note. The blueprint's W3 gate lists six obligations:

    outcome shape | spill file round-trip | ok computed not sniffed |
    CancelledError propagates | doom-loop trips on the third identical call |
    DAF path preserves details

This file covers four of them -- everything that belongs to the chokepoint
itself (W3-1, W3-4, W3-5). The spill round-trip arrives with W3-2 (bounded
output) and the DAF/native ``details`` passthrough with W3-3 (``workflow.py``);
both are later stages of this same wave and both extend this file rather than
starting another.

Why some of these tests look paranoid: before W3, ``dispatch_tool`` returned a
``str`` and swallowed every exception into an f-string, so EVERY property below
was unobservable. Four separate silent-failure modes were found by audit in code
that consumed that string, none of which any existing test could see.
"""

import asyncio
import json

import pytest

from djcode.core.dispatch import (
    DOOM_LOOP_THRESHOLD,
    DispatchContext,
    call_signature,
    canonical_arguments,
    dispatch_context,
)
from djcode.core.hooks import HookBus, HookEvent, HookResult
from djcode.core.outcome import ToolOutcome
from djcode.tools import (
    STRUCTURED_FAILURE_TOOLS,
    TOOL_DISPATCH,
    UNVERIFIED_FAILURE_TOOLS,
    dispatch_tool,
)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def fake_tool(monkeypatch):
    """Install a handler under a tool name and hand back a call log."""

    def install(name, handler):
        monkeypatch.setitem(TOOL_DISPATCH, name, handler)
        return name

    return install


# ── outcome shape ──────────────────────────────────────────────────────────


def test_dispatch_returns_a_tool_outcome_whose_str_is_its_content(fake_tool):
    async def handler(value):
        return f"handled {value}"

    fake_tool("probe", handler)
    outcome = run(dispatch_tool("probe", {"value": 7}))

    assert isinstance(outcome, ToolOutcome)
    assert outcome.content == "handled 7"
    # The compatibility shim W2 shipped: every `str(await dispatch(...))` call
    # site in the tree keeps producing byte-identical text.
    assert str(outcome) == "handled 7"
    assert f"{outcome}" == "handled 7"
    assert outcome.ok is True
    assert outcome.details["tool"] == "probe"
    assert outcome.spill_path is None


def test_handler_may_return_its_own_outcome(fake_tool):
    """The seam W7-2 uses to put a file's pre/post image into details."""

    async def handler():
        return ToolOutcome(content="rich", ok=False, details={"pre": "a", "post": "b"})

    fake_tool("probe", handler)
    outcome = run(dispatch_tool("probe", {}))

    assert outcome.content == "rich"
    assert outcome.ok is False
    assert outcome.details["pre"] == "a"
    assert outcome.details["ok_source"] == "handler"


def test_details_stay_json_serialisable():
    """W10 serialises details to JSONL; a non-encodable value would break it."""
    outcome = run(dispatch_tool("definitely_not_a_tool", {}))
    json.dumps(outcome.details)


def test_unknown_tool_fails_closed():
    outcome = run(dispatch_tool("definitely_not_a_tool", {}))

    assert outcome.ok is False
    assert outcome.content == "Error: Unknown tool 'definitely_not_a_tool'"
    assert outcome.details["refused_by"] == "unknown_tool"
    assert outcome.details["ok_source"] == "dispatch"


def test_non_mapping_arguments_are_refused_not_crashed():
    outcome = run(dispatch_tool("file_read", "not a dict"))

    assert outcome.ok is False
    assert outcome.details["refused_by"] == "bad_arguments"


def test_exception_is_classified_rather_than_swallowed(fake_tool):
    async def handler():
        raise ValueError("kaboom")

    fake_tool("probe", handler)
    outcome = run(dispatch_tool("probe", {}))

    assert outcome.ok is False
    assert outcome.content == "Error executing probe: kaboom"
    assert outcome.details["exception"] == "ValueError"
    assert outcome.details["ok_source"] == "raised"


def test_a_bad_argument_name_is_reported_as_a_bind_error():
    """"The model called the tool wrong" is a different repair from "the tool broke".

    This is not hypothetical: ``app.py``'s ``/tasks add`` passes ``title=`` to a
    handler whose parameter is ``subject=``, and the old dispatcher hid it
    inside an f-string.
    """
    outcome = run(dispatch_tool("file_read", {"bogus_kwarg": 1}))

    assert outcome.ok is False
    assert outcome.details["exception"] == "TypeError"
    assert outcome.details["phase"] == "bind"


# ── ok is computed, never sniffed ──────────────────────────────────────────


def test_ok_is_not_sniffed_from_content_for_a_text_only_tool(fake_tool):
    """A handler that returns "Error: ..." must NOT be downgraded by a sniff.

    Seventeen of the 24 tools report failure only as text. The honest answer for
    those is ok=True with ok_source="unverified" -- "dispatch observed no
    failure" -- not a guess dressed up as a structured field. This test is the
    thing that fails if anyone reintroduces the startswith("error") sniff.
    """

    async def handler():
        return "Error: the handler said so, in words"

    fake_tool("grep", handler)
    outcome = run(dispatch_tool("grep", {}))

    assert outcome.ok is True
    assert outcome.details["ok_source"] == "unverified"


def test_ok_is_verified_for_a_tool_that_raises_on_failure(fake_tool):
    """The seven raising tools get a real verdict from a normal return."""

    async def handler(*_args, **_kwargs):
        return "Error: still just words"

    fake_tool("skill", handler)
    outcome = run(dispatch_tool("skill", {}))

    assert outcome.ok is True
    assert outcome.details["ok_source"] == "handler"


def test_ok_source_inventory_is_complete_and_only_shrinks():
    assert STRUCTURED_FAILURE_TOOLS <= set(TOOL_DISPATCH)
    assert STRUCTURED_FAILURE_TOOLS | UNVERIFIED_FAILURE_TOOLS == set(TOOL_DISPATCH)
    assert not STRUCTURED_FAILURE_TOOLS & UNVERIFIED_FAILURE_TOOLS
    # 17 tools still report failure as text. Lower this number when a handler
    # is converted to return a ToolOutcome (W7-2 converts the first two).
    # It must never go up.
    assert len(UNVERIFIED_FAILURE_TOOLS) <= 17


# ── CancelledError propagates ──────────────────────────────────────────────


def test_cancelled_error_raised_by_a_handler_propagates(fake_tool):
    """Ctrl+C mid-tool must reach ``Operator.send``.

    ``operator.py``'s cancellation handler synthesises a ``role="tool"`` message
    for every unanswered ``tool_call`` id, without which the next provider
    request is rejected outright. Convert the cancel to a ToolOutcome here and
    that bookkeeping never runs: the tool loop would simply `continue` into
    another billed round-trip after the user asked it to stop.
    """

    async def handler():
        raise asyncio.CancelledError()

    fake_tool("probe", handler)

    with pytest.raises(asyncio.CancelledError):
        run(dispatch_tool("probe", {}))


def test_outer_cancellation_of_a_running_tool_propagates(fake_tool):
    started = asyncio.Event()

    async def handler():
        started.set()
        await asyncio.sleep(60)
        return "never"

    fake_tool("probe", handler)

    async def scenario():
        task = asyncio.create_task(dispatch_tool("probe", {}))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return task.cancelled()

    assert run(scenario()) is True


def test_cancelled_error_is_not_an_exception_subclass():
    """Why the `except Exception` clause was already safe, stated as a test.

    The explicit `except asyncio.CancelledError: raise` in dispatch_tool is
    documentation, not repair. This pins the language fact it documents, so that
    widening the clause to BaseException fails here too.
    """
    assert not issubclass(asyncio.CancelledError, Exception)


# ── doom-loop breaker (P1-9) ───────────────────────────────────────────────


def test_doom_loop_trips_on_the_third_identical_call(fake_tool):
    calls = []

    async def handler(path):
        calls.append(path)
        return "same answer every time"

    fake_tool("file_read", handler)
    ctx = DispatchContext()

    first = run(dispatch_tool("file_read", {"path": "a.py"}, ctx=ctx))
    second = run(dispatch_tool("file_read", {"path": "a.py"}, ctx=ctx))
    third = run(dispatch_tool("file_read", {"path": "a.py"}, ctx=ctx))

    assert first.ok and second.ok
    assert len(calls) == 2, "the third identical call must not reach the handler"
    assert third.ok is False
    assert third.details["refused_by"] == "doom_loop"
    assert third.details["doom_loop"] is True
    assert str(DOOM_LOOP_THRESHOLD) in third.content


def test_doom_loop_window_resets_after_it_fires(fake_tool):
    """Otherwise the breaker becomes its own doom loop: 4 trips, 5 trips, ..."""
    calls = []

    async def handler(path):
        calls.append(path)
        return "x"

    fake_tool("file_read", handler)
    ctx = DispatchContext()

    for _ in range(3):
        run(dispatch_tool("file_read", {"path": "a.py"}, ctx=ctx))
    assert len(calls) == 2

    fourth = run(dispatch_tool("file_read", {"path": "a.py"}, ctx=ctx))
    assert fourth.ok is True
    assert len(calls) == 3


def test_doom_loop_routes_through_the_approval_callback(fake_tool):
    seen = []

    async def handler(path):
        return "x"

    async def approve(name, args, reason):
        seen.append((name, args, reason))
        return True

    fake_tool("file_read", handler)
    ctx = DispatchContext(approval_callback=approve)

    for _ in range(3):
        outcome = run(dispatch_tool("file_read", {"path": "a.py"}, ctx=ctx))

    assert seen == [("file_read", {"path": "a.py"}, "doom_loop")]
    assert outcome.ok is True, "an approved repeat must execute"


def test_doom_loop_ignores_argument_key_order(fake_tool):
    """A model that reorders keys is making the SAME call."""

    async def handler(**_kwargs):
        return "x"

    fake_tool("file_write", handler)
    ctx = DispatchContext()

    run(dispatch_tool("file_write", {"path": "a", "content": "b"}, ctx=ctx))
    run(dispatch_tool("file_write", {"content": "b", "path": "a"}, ctx=ctx))
    third = run(dispatch_tool("file_write", {"path": "a", "content": "b"}, ctx=ctx))

    assert third.details.get("refused_by") == "doom_loop"


def test_doom_loop_does_not_fire_on_different_arguments(fake_tool):
    calls = []

    async def handler(path):
        calls.append(path)
        return "x"

    fake_tool("file_read", handler)
    ctx = DispatchContext()

    for path in ("a.py", "b.py", "c.py", "d.py"):
        assert run(dispatch_tool("file_read", {"path": path}, ctx=ctx)).ok
    assert calls == ["a.py", "b.py", "c.py", "d.py"]


def test_doom_loop_is_inert_without_a_context(fake_tool):
    """No ctx, no state, no breaker. This is what keeps every legacy caller working."""
    calls = []

    async def handler(path):
        calls.append(path)
        return "x"

    fake_tool("file_read", handler)
    for _ in range(5):
        assert run(dispatch_tool("file_read", {"path": "a.py"})).ok
    assert len(calls) == 5


def test_canonical_arguments_are_stable_and_fail_open():
    assert canonical_arguments({"a": 1, "b": 2}) == canonical_arguments({"b": 2, "a": 1})
    assert canonical_arguments([1, 2]) != canonical_arguments([2, 1])

    circular: dict = {}
    circular["self"] = circular
    # json.dumps raises ValueError here even with default= set. The signature
    # must degrade to "never matches", not raise out of the chokepoint.
    assert call_signature("t", circular) != call_signature("t", circular)


def test_unserialisable_arguments_do_not_break_dispatch(fake_tool):
    async def handler(**_kwargs):
        return "x"

    fake_tool("probe", handler)
    circular: dict = {}
    circular["self"] = circular
    ctx = DispatchContext()

    outcome = run(dispatch_tool("probe", {"weird": circular}, ctx=ctx))
    assert outcome.ok is True


# ── hook seam (W3-4) ───────────────────────────────────────────────────────


def test_empty_hook_bus_allows():
    bus = HookBus()
    result = run(bus.fire(HookEvent.PRE_TOOL_USE, {}))
    assert result.allow is True
    assert bus.handlers(HookEvent.PRE_TOOL_USE) == ()


def test_pre_tool_use_hook_can_veto_before_any_side_effect(fake_tool):
    calls = []

    async def handler(path):
        calls.append(path)
        return "written"

    def veto(event, payload):
        return HookResult(allow=False, reason="Error: policy says no", details={"rule": "demo"})

    fake_tool("file_write", handler)
    bus = HookBus()
    bus.register(HookEvent.PRE_TOOL_USE, veto)

    outcome = run(dispatch_tool("file_write", {"path": "a"}, ctx=DispatchContext(hooks=bus)))

    assert calls == [], "a veto must land before the handler runs"
    assert outcome.ok is False
    assert outcome.content == "Error: policy says no"
    assert outcome.details["refused_by"] == "hook"
    assert outcome.details["rule"] == "demo"


def test_post_tool_use_hook_annotates_but_cannot_undo(fake_tool):
    async def handler():
        return "done"

    seen = []

    def objector(event, payload):
        seen.append(payload["outcome"].content)
        return HookResult(allow=False, reason="too late", details={"noted": True})

    fake_tool("probe", handler)
    bus = HookBus()
    bus.register(HookEvent.POST_TOOL_USE, objector)

    outcome = run(dispatch_tool("probe", {}, ctx=DispatchContext(hooks=bus)))

    assert seen == ["done"], "the post hook must see the built outcome"
    assert outcome.ok is True, "the side effect already happened; post hooks are advisory"
    assert outcome.details["noted"] is True
    assert outcome.details["post_hook_reason"] == "too late"


def test_a_broken_hook_does_not_brick_the_tool(fake_tool):
    async def handler():
        return "done"

    def explode(event, payload):
        raise RuntimeError("bad hook")

    fake_tool("probe", handler)
    bus = HookBus()
    bus.register(HookEvent.PRE_TOOL_USE, explode)

    outcome = run(dispatch_tool("probe", {}, ctx=DispatchContext(hooks=bus)))

    assert outcome.ok is True
    assert outcome.details["hook_errors"][0]["exception"] == "RuntimeError"


def test_hook_may_be_sync_or_async(fake_tool):
    order = []

    async def handler():
        return "done"

    def sync_hook(event, payload):
        order.append("sync")
        return None

    async def async_hook(event, payload):
        order.append("async")
        return HookResult()

    fake_tool("probe", handler)
    bus = HookBus()
    bus.register(HookEvent.PRE_TOOL_USE, sync_hook)
    bus.register(HookEvent.PRE_TOOL_USE, async_hook)

    assert run(dispatch_tool("probe", {}, ctx=DispatchContext(hooks=bus))).ok
    assert order == ["sync", "async"]


# ── how a context actually reaches the chokepoint ──────────────────────────


def test_context_var_installs_a_context_for_a_positional_call(fake_tool):
    """The production path never passes ctx=; it is installed for a block.

    ``Operator`` hands the bare ``dispatch_tool`` to ``WorkflowEngine.one``,
    which calls it positionally -- and ``tests/test_capabilities.py`` replaces
    that global with ``async def hang(*args)``, a signature that accepts no
    keyword arguments. So the context cannot be bound into the call.
    """
    calls = []

    async def handler(path):
        calls.append(path)
        return "x"

    fake_tool("file_read", handler)
    ctx = DispatchContext(session_id="s_test")

    async def scenario():
        with dispatch_context(ctx):
            for _ in range(3):
                last = await dispatch_tool("file_read", {"path": "a.py"})
        return last

    outcome = run(scenario())
    assert len(calls) == 2
    assert outcome.details["refused_by"] == "doom_loop"


def test_context_is_not_leaked_outside_the_block(fake_tool):
    from djcode.core.dispatch import active_context

    ctx = DispatchContext()

    async def scenario():
        with dispatch_context(ctx):
            inside = active_context()
        return inside, active_context()

    inside, outside = run(scenario())
    assert inside is ctx
    assert outside is None


def test_legacy_two_argument_call_still_works():
    """All 13 direct call sites in src/ and both test patches pass no ctx."""
    outcome = run(dispatch_tool("task_list", {}))
    assert isinstance(outcome, ToolOutcome)


def test_duration_is_recorded(fake_tool):
    async def handler():
        await asyncio.sleep(0.02)
        return "slow"

    fake_tool("probe", handler)
    outcome = run(dispatch_tool("probe", {}))
    assert outcome.duration_ms >= 10
