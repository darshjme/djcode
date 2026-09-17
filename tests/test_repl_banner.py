"""The first two lines of the session.

The old banner was a Rich Panel: four lines of box drawing around three facts.
It also never said whether the provider was on this machine or billing by the
token, and never mentioned that a previous conversation in this directory was
still there and one flag away.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from rich.console import Console

from djcode.frontends.repl import banner


@pytest.fixture
def cap(monkeypatch):
    console = Console(file=io.StringIO(), width=110, no_color=True, highlight=False)
    from djcode.frontends.repl import render

    monkeypatch.setattr(render, "console", console)
    return lambda: console.file.getvalue()


def _provider(name="ollama", model="gemma4"):
    return SimpleNamespace(config=SimpleNamespace(name=name, model=model))


# -- shape -----------------------------------------------------------------


def test_the_banner_is_two_lines_and_has_no_box(cap):
    banner.print_banner(_provider(), offer_resume=False)
    out = cap()
    body = [line for line in out.splitlines() if line.strip()]
    assert len(body) == 2
    for glyph in "╭╮╰╯│─":
        assert glyph not in out


def test_the_first_line_carries_version_model_and_provider(cap):
    from djcode import __version__

    banner.print_banner(_provider(), offer_resume=False)
    first = [line for line in cap().splitlines() if line.strip()][0]
    assert __version__ in first
    assert "gemma4" in first and "ollama" in first


def test_the_second_line_carries_the_approval_mode(cap):
    banner.print_banner(_provider(), auto_accept=True, offer_resume=False)
    second = [line for line in cap().splitlines() if line.strip()][1]
    assert "auto" in second


def test_the_second_line_points_at_undo_and_diff(cap):
    """W5 and W7 shipped these and nothing on screen ever mentioned them."""
    banner.print_banner(_provider(), offer_resume=False)
    second = [line for line in cap().splitlines() if line.strip()][1]
    assert "/undo" in second and "/diff" in second


# -- local vs hosted -------------------------------------------------------


def test_a_local_provider_is_labelled_local():
    assert banner.is_local_provider("ollama") is True


def test_a_hosted_provider_is_labelled_hosted():
    assert banner.is_local_provider("anthropic") is False


def test_an_unknown_provider_is_not_labelled_at_all():
    """Claiming "local" about a provider the registry has never heard of is the
    exact mistake this badge exists to prevent."""
    assert banner.is_local_provider("something-nobody-registered") is None


def test_the_badge_reaches_the_screen(cap):
    banner.print_banner(_provider("anthropic", "claude-opus-5"), offer_resume=False)
    assert "hosted" in cap()


def test_an_unknown_provider_prints_neither_badge(cap):
    banner.print_banner(_provider("mystery", "m"), offer_resume=False)
    out = cap()
    assert "local" not in out and "hosted" not in out


# -- the recent-session offer ----------------------------------------------


class _FakeDB:
    def __init__(self, sessions):
        self._sessions = sessions

    def sessions_for_cwd(self, cwd, limit=1):
        return self._sessions[:limit]


def _session(minutes_ago=10, messages=12, summary="fix os.uname on windows"):
    stamp = (datetime.now() - timedelta(minutes=minutes_ago)).isoformat()
    return SimpleNamespace(
        last_interaction_at=stamp,
        updated_at=stamp,
        start=stamp,
        messages_count=messages,
        summary=summary,
    )


def test_a_recent_session_is_offered():
    line = banner.recent_session_line(_FakeDB([_session(minutes_ago=14)]), "/tmp")
    assert "14m ago" in line
    assert "12 msgs" in line
    assert "fix os.uname" in line


def test_an_old_session_is_not_offered():
    assert banner.recent_session_line(_FakeDB([_session(minutes_ago=60 * 48)]), "/tmp") is None


def test_no_sessions_means_no_offer():
    assert banner.recent_session_line(_FakeDB([]), "/tmp") is None


def test_a_session_with_no_summary_is_still_offered():
    line = banner.recent_session_line(_FakeDB([_session(summary="")]), "/tmp")
    assert line and "msgs" in line


def test_a_broken_timestamp_is_not_offered():
    bad = SimpleNamespace(
        last_interaction_at="not a date",
        updated_at=None,
        start=None,
        messages_count=1,
        summary="",
    )
    assert banner.recent_session_line(_FakeDB([bad]), "/tmp") is None


def test_a_database_that_raises_does_not_break_the_launch():
    class Exploding:
        def sessions_for_cwd(self, cwd, limit=1):
            raise RuntimeError("db is gone")

    assert banner.recent_session_line(Exploding(), "/tmp") is None


@pytest.mark.parametrize(
    "minutes,expected",
    [(0, "just now"), (5, "5m ago"), (90, "1h ago"), (60 * 30, "1d ago")],
)
def test_ago_formatting(minutes, expected):
    stamp = (datetime.now() - timedelta(minutes=minutes)).isoformat()
    assert banner._ago(stamp) == expected


# -- the shim --------------------------------------------------------------


def test_repl_print_banner_still_exists_for_its_six_callers(cap):
    """Six test files and the TUI import `repl.print_banner` by name."""
    from djcode import repl

    repl.print_banner(_provider(), auto_accept=False)
    assert "DJcode" in cap()


def test_short_cwd_uses_forward_slashes_under_home(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "proj").mkdir()
    monkeypatch.chdir(tmp_path / "proj")
    assert banner.short_cwd() == "~/proj"
