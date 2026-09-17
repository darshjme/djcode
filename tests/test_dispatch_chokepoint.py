"""W3 green gate for the one chokepoint: ``djcode.tools.dispatch_tool``.

Scope note. The blueprint's W3 gate lists six obligations:

    outcome shape | spill file round-trip | ok computed not sniffed |
    CancelledError propagates | doom-loop trips on the third identical call |
    DAF path preserves details

This file covers five of them -- everything that belongs to the chokepoint
itself (W3-1, W3-2, W3-4, W3-5). The DAF/native ``details`` passthrough arrives
with W3-3 (``workflow.py``), a later stage of this same wave that extends this
file rather than starting another.

Why some of these tests look paranoid: before W3, ``dispatch_tool`` returned a
``str`` and swallowed every exception into an f-string, so EVERY property below
was unobservable. Four separate silent-failure modes were found by audit in code
that consumed that string, none of which any existing test could see.
"""

import asyncio
import json
from pathlib import Path

import pytest

from djcode import config
from djcode.core import spill
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
    OUTCOME_TOOLS,
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
    assert (
        STRUCTURED_FAILURE_TOOLS | OUTCOME_TOOLS | UNVERIFIED_FAILURE_TOOLS
        == set(TOOL_DISPATCH)
    )
    assert not STRUCTURED_FAILURE_TOOLS & UNVERIFIED_FAILURE_TOOLS
    assert not OUTCOME_TOOLS & UNVERIFIED_FAILURE_TOOLS
    assert not OUTCOME_TOOLS & STRUCTURED_FAILURE_TOOLS
    # 15 tools still report failure as text. Lower this number when a handler
    # is converted to return a ToolOutcome (W7-2 converted the first two:
    # file_edit and file_write, now in OUTCOME_TOOLS). It must never go up.
    assert len(UNVERIFIED_FAILURE_TOOLS) <= 15


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


# ── bounded output + spill file (P1-4, W3-2) ───────────────────────────────


@pytest.fixture
def spill_home(tmp_path, monkeypatch):
    """Point CONFIG_DIR at tmp_path and hand back the spill root.

    This works only because ``core/spill.py`` reads ``config.CONFIG_DIR`` at
    call time. Every other CONFIG_DIR consumer in this codebase binds a derived
    module constant at import time and cannot be redirected like this --
    ``tests/conftest.py`` has to set an environment variable before any djcode
    import, and then rebind ``workflow.CONFIG_DIR`` by hand, to work around
    exactly that. The pattern is deliberate; do not "tidy" it into a constant.
    """
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    return tmp_path / "tool-output"


def big(n=20_000, fill="abcdefghij"):
    """Text whose every offset is identifiable, so a bad slice cannot pass."""
    return "".join(f"{i:06d}-{fill}\n" for i in range(n // 17 + 1))[:n]


def test_small_output_is_passed_through_untouched(fake_tool, spill_home):
    """Most calls never reach the policy at all."""
    text = "x" * spill.SPILL_THRESHOLD

    async def handler():
        return text

    fake_tool("probe", handler)
    outcome = run(dispatch_tool("probe", {}))

    assert outcome.content == text
    assert outcome.spill_path is None
    assert "output_chars" not in outcome.details
    assert not spill_home.exists()


def test_one_character_over_the_threshold_spills(fake_tool, spill_home):
    text = "x" * (spill.SPILL_THRESHOLD + 1)

    async def handler():
        return text

    fake_tool("probe", handler)
    outcome = run(dispatch_tool("probe", {}))

    assert outcome.spill_path is not None
    assert Path(outcome.spill_path).read_text(encoding="utf-8") == text


def test_large_output_keeps_the_head_and_the_tail(fake_tool, spill_home):
    """Both ends. A build log's verdict is in the last lines, a traceback's
    cause in the first; head-only truncation -- what all five removed tool-level
    cuts did -- throws away whichever one the model needed."""
    text = big()

    async def handler():
        return text

    fake_tool("probe", handler)
    outcome = run(dispatch_tool("probe", {}))

    assert outcome.content.startswith(text[: spill.HEAD_CHARS])
    assert outcome.content.endswith(f"[djcode] full output: {outcome.spill_path}")
    assert text[-spill.TAIL_CHARS :] in outcome.content
    # Bounded: head + tail + the marker, which carries the path twice.
    assert len(outcome.content) <= (
        spill.HEAD_CHARS + spill.TAIL_CHARS + 2 * len(outcome.spill_path) + 200
    )
    # And the elided middle really is absent.
    assert text[spill.HEAD_CHARS + 10 : spill.HEAD_CHARS + 60] not in outcome.content
    assert outcome.details["output_chars"] == len(text)
    assert outcome.details["output_elided"] == len(text) - spill.HEAD_CHARS - spill.TAIL_CHARS


def test_spill_round_trip_is_byte_exact_utf8(fake_tool, spill_home):
    """The file must be the FULL text, in UTF-8, unmangled.

    Non-ASCII and CRLF are both in the fixture on purpose. ``open()`` still
    defaults to the locale codepage on Windows (cp1252 here), which cannot
    encode U+FFFD -- and ``run_process`` decodes with ``errors="replace"``, so
    real tool output contains it. The text layer also rewrites ``\\n`` as
    ``\\r\\n`` on Windows unless ``newline=""`` is passed, which would make the
    file a re-encoding rather than a copy.
    """
    text = "héllo ✓ 中文 �\r\n" + big()

    async def handler():
        return text

    fake_tool("probe", handler)
    outcome = run(dispatch_tool("probe", {}))

    path = Path(outcome.spill_path)
    assert path.read_bytes() == text.encode("utf-8")
    assert path.read_bytes().decode("utf-8") == text


def test_the_absolute_path_is_in_both_content_and_the_field(fake_tool, spill_home):
    """Two readers, two channels.

    ``details`` is documented as never shown to the model and ``__str__``
    returns ``content`` alone, so if the path were only in ``spill_path`` the
    model could never read the elided text back -- and a front-end must not have
    to parse English out of ``content`` to find the file.
    """

    async def handler():
        return big()

    fake_tool("probe", handler)
    outcome = run(dispatch_tool("probe", {}))

    assert Path(outcome.spill_path).is_absolute()
    assert outcome.content.count(outcome.spill_path) == 2
    # file_read is the tool the model would reach for; prove the link works.
    read_back = run(dispatch_tool("file_read", {"path": outcome.spill_path}))
    assert "000000-abcdefghij" in str(read_back)


def test_spill_lands_in_the_session_bucket(fake_tool, spill_home):
    async def handler():
        return big()

    fake_tool("probe", handler)
    ctx = DispatchContext(session_id="s_0123456789abcdef")
    outcome = run(dispatch_tool("probe", {}, ctx=ctx))

    assert Path(outcome.spill_path).parent.name == "s_0123456789abcdef"
    assert Path(outcome.spill_path).is_relative_to(spill_home.resolve())


def test_a_sessionless_call_gets_a_process_bucket_not_a_None_directory(fake_tool, spill_home):
    """``Operator`` has no ``session_id`` until a front-end grafts one on, so
    this is the common case, not the exotic one."""

    async def handler():
        return big()

    fake_tool("probe", handler)
    outcome = run(dispatch_tool("probe", {}))

    bucket = Path(outcome.spill_path).parent.name
    assert bucket.startswith("proc-")
    assert bucket != "None"


def test_a_hostile_session_id_cannot_escape_the_spill_root(spill_home):
    """The id reaches the filesystem, so it is sanitised rather than trusted."""
    assert spill.session_bucket("../../etc") == ".._.._etc"
    assert spill.session_bucket("..").startswith("proc-")
    assert spill.session_bucket("a/b\\c").startswith("a_b_c")
    assert spill.spill_dir("s_ok") == spill_home / "s_ok"


def test_an_unwritable_spill_never_fails_the_tool(fake_tool, spill_home, monkeypatch):
    """This step runs AFTER the handler: the side effect already happened, so a
    disk problem here must not be reported to the model as a failed tool."""

    def boom(text, *, session_id=None):
        raise OSError("No space left on device")

    monkeypatch.setattr(spill, "write_spill", boom)

    async def handler():
        return big()

    fake_tool("probe", handler)
    outcome = run(dispatch_tool("probe", {}))

    assert outcome.ok is True
    assert outcome.spill_path is None
    assert "spill file could not be written" in outcome.content
    assert outcome.details["spill_error"].startswith("OSError")
    assert outcome.details["output_elided"] > 0


def test_a_handlers_exception_message_is_bounded_too(fake_tool, spill_home):
    """An exception message is not automatically short -- a subprocess error can
    carry a whole stderr dump."""

    async def handler():
        raise RuntimeError(big())

    fake_tool("probe", handler)
    outcome = run(dispatch_tool("probe", {}))

    assert outcome.ok is False
    assert outcome.details["ok_source"] == "raised"
    assert outcome.spill_path is not None
    assert len(outcome.content) < 6000 + 2 * len(outcome.spill_path)


def test_an_outcome_the_handler_built_itself_is_bounded(fake_tool, spill_home):
    """The W7-2 seam: a handler that returns its own ToolOutcome (with a diff in
    ``details``) must still be bounded, which is why the policy runs on the
    constructed outcome rather than on the raw handler return."""
    text = big()

    async def handler():
        return ToolOutcome(content=text, details={"diff": "pretend"}, ok=True)

    fake_tool("probe", handler)
    outcome = run(dispatch_tool("probe", {}))

    assert outcome.details["diff"] == "pretend"
    assert outcome.spill_path is not None
    assert Path(outcome.spill_path).read_text(encoding="utf-8") == text


def test_config_dir_is_resolved_at_call_time(fake_tool, tmp_path, monkeypatch):
    """Pinning this: a later 'tidy-up' into a module-level constant would make
    every spill land in the developer's real ~/.djcode during a test run."""

    async def handler():
        return big()

    fake_tool("probe", handler)
    first = tmp_path / "first"
    second = tmp_path / "second"

    monkeypatch.setattr(config, "CONFIG_DIR", first)
    a = run(dispatch_tool("probe", {}))
    monkeypatch.setattr(config, "CONFIG_DIR", second)
    b = run(dispatch_tool("probe", {}))

    assert Path(a.spill_path).is_relative_to(first.resolve())
    assert Path(b.spill_path).is_relative_to(second.resolve())


#: The six ad-hoc truncations W3-2 deleted, each pinned by a literal that only
#: existed in the removed code. Six limits, five different messages, four
#: different units (bytes / lines / chars / chars-per-cell) and one that said
#: nothing at all -- and in every case the dropped text was unrecoverable.
#: If one of these strings comes back, a tool has started inventing its own
#: output policy again and the chokepoint is no longer the only one.
REMOVED_TOOL_TRUNCATIONS = {
    "bash.py": ["output_limit: int = 50_000", "... (output truncated)"],
    "git.py": ["output_limit=30_000"],
    "grep.py": ["matches shown, more truncated"],
    "parallel_exec.py": ["output truncated at 10000 chars"],
    "notebook.py": ["max_output_chars: int", "def _truncate("],
    "agent_spawn.py": ["chars dropped", "result[:5000]"],
    "web_fetch.py": ["max_chars: int", "resp.text[:max_chars]"],
}


@pytest.mark.parametrize("filename,literals", sorted(REMOVED_TOOL_TRUNCATIONS.items()))
def test_no_tool_reinvents_its_own_output_bound(filename, literals):
    import djcode.tools

    source = (Path(djcode.tools.__file__).parent / filename).read_text(encoding="utf-8")
    for literal in literals:
        assert literal not in source, f"{filename} reintroduced an ad-hoc truncation: {literal}"


# ── W3-3: the outcome passthrough (the trap) ────────────────────────────────
# BLUEPRINT-CLI.md section 2.4 calls this "non-obvious": dispatch_tool's result
# does not reach Operator directly. It passes through WorkflowEngine, which used
# to do `str(await dispatch(...))` on BOTH branches -- so a ToolOutcome was
# flattened to text and `details` was destroyed before anyone saw it. P0-8 would
# have silently done nothing. These tests fail if that str() ever comes back.


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["native", "daf"])
async def test_workflow_one_preserves_the_outcome(mode, tmp_path, monkeypatch):
    """one() must hand back a ToolOutcome, not its string form."""
    import shutil

    from djcode.workflow import WorkflowEngine

    if mode == "daf" and shutil.which("cargo") is None:
        pytest.skip("DAF engine needs a Rust toolchain; native branch covers the logic")

    from djcode.tools import dispatch_tool

    engine = WorkflowEngine(mode=mode)
    outcome = await engine.one("file_read", {"path": str(tmp_path / "nope.txt")}, dispatch_tool)

    assert isinstance(outcome, ToolOutcome), (
        f"{engine.mode} branch returned {type(outcome).__name__}; the str() in "
        "workflow.py is back and ToolOutcome.details is being destroyed"
    )
    assert outcome.details, "details were dropped crossing the workflow engine"
    assert outcome.details.get("tool") == "file_read"
    assert str(outcome) == outcome.content


@pytest.mark.asyncio
async def test_workflow_native_and_daf_agree_on_type(tmp_path):
    """Both branches must return the same TYPE.

    The asymmetry this guards against is real and was shipped for one commit:
    str on a box with Rust, ToolOutcome on a box without, which lands on
    state.py's result[:200] and capabilities.py's json.dumps -- neither of which
    a test on a Rust-equipped box would ever catch.
    """
    import shutil

    from djcode.tools import dispatch_tool
    from djcode.workflow import WorkflowEngine

    args = {"command": "echo agree"}
    native = await WorkflowEngine(mode="native").one("bash", args, dispatch_tool)
    assert isinstance(native, ToolOutcome)

    if shutil.which("cargo") is None:
        pytest.skip("no Rust toolchain: DAF half of the comparison cannot run here")
    daf_engine = WorkflowEngine(mode="daf")
    daf = await daf_engine.one("bash", args, dispatch_tool)
    assert daf_engine.mode == "daf", "engine fell back; this assertion proved nothing"
    assert type(daf) is type(native), f"DAF returned {type(daf)}, native returned {type(native)}"
    assert daf.details.keys() == native.details.keys()


@pytest.mark.asyncio
async def test_workflow_ok_comes_from_the_outcome_not_a_prefix_sniff():
    """_result_ok must trust the flag, not the text."""
    from djcode.workflow import _result_ok

    # A successful outcome whose content merely starts with the word "error".
    assert _result_ok(ToolOutcome(content="Error: none found", ok=True)) is True
    # A failed outcome whose content looks perfectly cheerful.
    assert _result_ok(ToolOutcome(content="all good", ok=False)) is False
    # Bare strings still fall back to the heuristic the DAF host itself applies.
    assert _result_ok("Error: something") is False
    assert _result_ok("fine") is True
