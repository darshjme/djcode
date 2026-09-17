"""Tool cards, rendered from `ToolOutcome.details` -- and the `ok_source` trap.

W3 built `details` and nothing rendered it. The reason this file exists at all
is the sentence in `W3-VERIFICATION.md`: `ToolOutcome.ok` is only meaningful
when `details["ok_source"]` is handler / raised / dispatch. For the seventeen
tools that still report `"unverified"`, `ok=True` means "dispatch observed no
failure signal", which is NOT "the tool succeeded" -- and two shipped
regressions already came from treating it as if it were.

A renderer is the easiest place in the program to make that mistake for a third
time, because a tick is such a natural thing to print. So the rule is pinned
here in both directions: unverified never draws a tick, and it never draws a
cross either.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from djcode.frontends.repl import render


@pytest.fixture
def cap(monkeypatch):
    """Capture what the renderer prints, styles stripped."""
    console = Console(file=io.StringIO(), width=100, no_color=True, highlight=False)
    monkeypatch.setattr(render, "console", console)
    return lambda: console.file.getvalue()


# -- the verdict function (the whole point of the file) --------------------


@pytest.mark.parametrize("source", ["handler", "raised", "dispatch"])
def test_an_authoritative_source_makes_ok_a_verdict(source):
    assert render.verdict_of(True, {"ok_source": source}) == "ok"
    assert render.verdict_of(False, {"ok_source": source}) == "failed"


@pytest.mark.parametrize("ok", [True, False])
def test_unverified_is_never_a_verdict_in_either_direction(ok):
    """`ok=True` with ok_source="unverified" means dispatch saw no failure
    signal. Rendering that as success is the regression W3-VERIFICATION.md
    catalogues; rendering it as failure would be just as wrong."""
    assert render.verdict_of(ok, {"ok_source": "unverified"}) == "unverified"


def test_missing_details_are_treated_as_unverified():
    """Absent is not authoritative. A bare `str` result carries no ok_source,
    and `tool_result_event` documents that its `ok` is True only because there
    is no honest answer."""
    assert render.verdict_of(True, None) == "unverified"
    assert render.verdict_of(True, {}) == "unverified"


def test_a_refusal_outranks_everything():
    assert render.verdict_of(False, {"ok_source": "dispatch", "refused_by": "hook"}) == "blocked"
    assert render.verdict_of(True, {"ok_source": "handler", "permission": "deny"}) == "blocked"


def test_the_verdict_never_looks_at_the_content_string():
    """P0-8 deleted the "does it start with Error:" sniff from the engine. A
    renderer must not smuggle it back in."""
    import inspect

    source = inspect.getsource(render.verdict_of)
    # Strip the docstring; it discusses the sniff in order to forbid it.
    body = source.split('"""')[-1]
    for smell in ("startswith", "content", "Error:", "traceback"):
        assert smell not in body, f"verdict_of looks at {smell!r}"


# -- what reaches the screen ----------------------------------------------


def test_an_unverified_result_draws_no_tick(cap):
    render.render_tool_result("found 3 results", {"ok_source": "unverified"}, ok=True)
    out = cap()
    assert "found 3 results" in out
    assert "✓" not in out and "ok" not in out.split("found")[0]


def test_a_handler_verified_success_does_draw_a_tick(cap):
    render.render_tool_result("done", {"ok_source": "handler"}, ok=True, duration_ms=41)
    out = cap()
    assert "✓" in out
    assert "41ms" in out


def test_a_failure_shows_the_tail_because_that_is_where_the_error_is(cap):
    render.render_tool_result(
        "step one\nstep two\nstep three\nTraceback\nAssertionError: boom",
        {"ok_source": "raised", "exit_code": 1},
        ok=False,
    )
    out = cap()
    assert "AssertionError: boom" in out, "the last line is the one that matters"
    assert "step one" not in out
    assert "exit 1" in out
    assert "2 more lines" in out


def test_a_success_shows_the_head(cap):
    render.render_tool_result(
        "first\nsecond\nthird\nfourth", {"ok_source": "handler"}, ok=True
    )
    out = cap()
    assert "first" in out and "fourth" not in out


def test_a_blocked_call_names_who_refused(cap):
    render.render_tool_result(
        "refused", {"ok_source": "dispatch", "refused_by": "hardline floor"}, ok=False
    )
    assert "hardline floor" in cap()


def test_the_spill_path_is_shown_when_output_was_bounded(cap):
    """P1-4 writes the full output to a file and the user was never told."""
    render.render_tool_result(
        "truncated", {"ok_source": "handler"}, ok=True, spill_path="/tmp/spill.txt"
    )
    assert "/tmp/spill.txt" in cap()


def test_an_empty_result_still_reports_its_verdict(cap):
    render.render_tool_result("", {"ok_source": "handler"}, ok=False, duration_ms=5)
    assert "failed" in cap()


def test_an_empty_unverified_result_prints_nothing_at_all(cap):
    """No content and no verdict means there is genuinely nothing to say."""
    render.render_tool_result("", {"ok_source": "unverified"}, ok=True)
    assert cap().strip() == ""


# -- the event seam --------------------------------------------------------


def test_the_event_carries_ok_and_duration_into_the_card(cap):
    import asyncio

    from djcode.core.events import EventType, tool_result_event
    from djcode.core.outcome import ToolOutcome

    outcome = ToolOutcome(
        content="all good",
        details={"ok_source": "handler"},
        ok=True,
        duration_ms=1900,
        spill_path=None,
    )
    event = tool_result_event("c1", outcome, name="bash")
    assert event.event_type is EventType.TOOL_RESULT
    asyncio.run(render.render_event(event))
    out = cap()
    assert "all good" in out and "1.9s" in out and "✓" in out


def test_a_bare_string_result_renders_as_unverified(cap):
    """`tool_result_event` is duck-typed and gives a plain str ok=True with no
    ok_source. That must reach the screen as a neutral card."""
    import asyncio

    from djcode.core.events import tool_result_event

    asyncio.run(render.render_event(tool_result_event("c1", "just text", name="grep")))
    out = cap()
    assert "just text" in out
    assert "✓" not in out


def test_token_events_are_still_not_rendered_here(cap):
    """repl.py writes the streamed answer itself; rendering it again would
    double every character."""
    import asyncio

    from djcode.core.events import token_event

    asyncio.run(render.render_event(token_event("hello")))
    assert cap() == ""


# -- formatting ------------------------------------------------------------


@pytest.mark.parametrize(
    "ms,expected", [(0, "0ms"), (41, "41ms"), (999, "999ms"), (1900, "1.9s"), (72000, "1m 12s")]
)
def test_duration_formatting(ms, expected):
    assert render.format_duration(ms) == expected


def test_tool_display_covers_every_dispatchable_tool():
    """A tool with no display name renders as `Spawn_Agent`, which is how the
    user finds out a tool exists that nobody designed a card for."""
    from djcode.tools import TOOL_DISPATCH

    missing = set(TOOL_DISPATCH) - set(render.TOOL_DISPLAY)
    assert not missing, f"tools with no display name: {sorted(missing)}"


def test_tool_display_lists_no_tool_that_does_not_exist():
    """The other direction. The table used to name nine tools that were never
    dispatchable -- dead rows nobody could have noticed from the UI."""
    from djcode.tools import TOOL_DISPATCH

    ghosts = set(render.TOOL_DISPLAY) - set(TOOL_DISPATCH)
    assert not ghosts, f"display names for tools that do not exist: {sorted(ghosts)}"


def test_every_model_facing_tool_has_a_display_name():
    """TOOL_DEFINITIONS is what the model can call; TOOL_DISPATCH is what runs.
    A name in the first and not the second would be a different bug, but either
    way the user has to see a real word on the card."""
    from djcode.provider import TOOL_DEFINITIONS

    names = {
        (d.get("function", {}) or {}).get("name") or d.get("name") for d in TOOL_DEFINITIONS
    }
    missing = {n for n in names if n and n not in render.TOOL_DISPLAY}
    assert not missing, f"advertised tools with no display name: {sorted(missing)}"
