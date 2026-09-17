"""Getting back into a past session without retyping a uuid4.

`/resume` accepted exactly one thing: a full uuid, character for character. The
only way to get one was to run `/history`, read a table and copy thirty-six
characters out of it. There was no `--continue`, no `--resume`, no picker, and
`SessionDB` had no way at all to answer "what was I doing in this folder" --
`list_sessions` took a limit and an offset and ordered by start time.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from djcode.frontends.repl import sessions_ui
from djcode.sessions import SessionDB


@pytest.fixture
def db(tmp_path, monkeypatch):
    from djcode import sessions

    monkeypatch.setattr(sessions, "DB_PATH", tmp_path / "sessions.db")
    return SessionDB()


def _make(db, cwd, model="gemma4", messages=2, summary="did a thing"):
    session_id = db.create_session(model, "ollama")
    conn = db._connect()
    try:
        conn.execute(
            "UPDATE sessions SET cwd=?, summary=?, messages_count=? WHERE id=?",
            (cwd, summary, messages, session_id),
        )
        conn.commit()
    finally:
        conn.close()
    return session_id


# -- the query SessionDB did not have --------------------------------------


def test_sessions_are_findable_by_directory(db, tmp_path):
    here = _make(db, str(tmp_path / "a"))
    _make(db, str(tmp_path / "b"))
    found = db.sessions_for_cwd(str(tmp_path / "a"))
    assert [s.id for s in found] == [here]


def test_directory_listing_is_ordered_by_last_interaction_not_start(db, tmp_path):
    """The session you want back is the one you last spoke to."""
    cwd = str(tmp_path)
    first = _make(db, cwd)
    second = _make(db, cwd)
    conn = db._connect()
    try:
        conn.execute(
            "UPDATE sessions SET last_interaction_at=? WHERE id=?", ("2030-01-01T00:00", first)
        )
        conn.execute(
            "UPDATE sessions SET last_interaction_at=? WHERE id=?", ("2020-01-01T00:00", second)
        )
        conn.commit()
    finally:
        conn.close()
    assert [s.id for s in db.sessions_for_cwd(cwd)][0] == first


def test_an_empty_directory_returns_nothing_rather_than_everything(db, tmp_path):
    _make(db, str(tmp_path / "a"))
    assert db.sessions_for_cwd(str(tmp_path / "nowhere")) == []


def test_most_recent_session_prefers_this_directory(db, tmp_path):
    _make(db, str(tmp_path / "elsewhere"))
    here = _make(db, str(tmp_path / "here"))
    assert db.most_recent_session(str(tmp_path / "here")).id == here


def test_most_recent_session_is_none_when_there_are_none(db, tmp_path):
    assert db.most_recent_session(str(tmp_path)) is None


# -- prefix resolution -----------------------------------------------------


def test_a_unique_prefix_resolves(db, tmp_path):
    session_id = _make(db, str(tmp_path))
    assert db.resolve_session_id(session_id[:8]) == session_id


def test_a_prefix_containing_an_underscore_resolves(db, tmp_path):
    """Session ids are "s_<hex>", so a real prefix contains `_` -- which is a
    LIKE metacharacter. It has to be ESCAPED, not rejected, or every prefix
    matches by luck instead of by equality."""
    session_id = _make(db, str(tmp_path))
    assert session_id.startswith("s_")
    assert db.resolve_session_id(session_id[:6]) == session_id
    # "sX" would match nothing even though "s_" matches everything under a
    # naive LIKE.
    assert db.resolve_session_id("sX") is None


def test_a_full_id_still_resolves(db, tmp_path):
    session_id = _make(db, str(tmp_path))
    assert sessions_ui.resolve(session_id, db) == (session_id, "")


def test_an_ambiguous_prefix_resolves_to_nothing(db, tmp_path, monkeypatch):
    """Resuming the wrong conversation because two uuids shared four characters
    would be a silent loss of the one the user meant."""
    ids = [_make(db, str(tmp_path)) for _ in range(2)]
    conn = db._connect()
    try:
        for index, old in enumerate(ids):
            conn.execute("UPDATE sessions SET id=? WHERE id=?", (f"abcd{index}0000", old))
        conn.commit()
    finally:
        conn.close()
    assert db.resolve_session_id("abcd") is None
    resolved, reason = sessions_ui.resolve("abcd", db)
    assert resolved is None
    assert "2 sessions start with" in reason
    assert "more characters" in reason


def test_an_unknown_prefix_says_so_rather_than_being_ambiguous(db, tmp_path):
    _make(db, str(tmp_path))
    resolved, reason = sessions_ui.resolve("zzzzzz", db)
    assert resolved is None
    assert "No session matches" in reason


def test_an_empty_reference_is_rejected(db):
    assert sessions_ui.resolve("", db) == (None, "No session id given.")


def test_resolve_never_raises_on_sql_wildcards(db, tmp_path):
    """`%` and `_` are LIKE metacharacters. A prefix containing one must not
    silently match everything."""
    _make(db, str(tmp_path))
    resolved, _ = sessions_ui.resolve("%", db)
    assert resolved is None


# -- restoring -------------------------------------------------------------


def _operator():
    from djcode.provider import Message

    return SimpleNamespace(
        messages=[Message(role="system", content="CURRENT system prompt")],
        session_id="old",
        context_manager=None,
    )


def test_restoring_replays_the_conversation(db, tmp_path):
    from djcode.provider import Message

    session_id = _make(db, str(tmp_path))
    db.append_messages(
        session_id,
        [
            Message(role="system", content="STALE system prompt"),
            Message(role="user", content="hello"),
            Message(role="assistant", content="hi"),
        ],
    )
    operator = _operator()
    assert sessions_ui.restore_into(operator, session_id, db) == 2
    assert operator.session_id == session_id


def test_restoring_keeps_the_current_system_prompt(db, tmp_path):
    """A stored system prompt carries the tool schemas and model name from the
    build that wrote it. Replaying last week's would describe tools this build
    may no longer have."""
    from djcode.provider import Message

    session_id = _make(db, str(tmp_path))
    db.append_messages(
        session_id,
        [Message(role="system", content="STALE"), Message(role="user", content="hello")],
    )
    operator = _operator()
    sessions_ui.restore_into(operator, session_id, db)
    assert operator.messages[0].content == "CURRENT system prompt"
    assert not any(m.content == "STALE" for m in operator.messages)


def test_restoring_an_empty_session_changes_nothing_and_says_zero(db, tmp_path):
    session_id = _make(db, str(tmp_path))
    operator = _operator()
    assert sessions_ui.restore_into(operator, session_id, db) == 0


def test_restoring_refreshes_the_context_meter(db, tmp_path):
    """Without this the meter keeps reporting the empty session that was just
    replaced, so a resumed 80%-full conversation reads as 0%."""
    from djcode.provider import Message

    session_id = _make(db, str(tmp_path))
    db.append_messages(session_id, [Message(role="user", content="hello")])
    seen = {}
    operator = _operator()
    operator.context_manager = SimpleNamespace(
        replace_messages=lambda messages: seen.update(count=len(messages))
    )
    sessions_ui.restore_into(operator, session_id, db)
    assert seen["count"] == 2


def test_a_context_manager_that_raises_does_not_fail_the_resume(db, tmp_path):
    from djcode.provider import Message

    session_id = _make(db, str(tmp_path))
    db.append_messages(session_id, [Message(role="user", content="hello")])

    def explode(_messages):
        raise RuntimeError("no")

    operator = _operator()
    operator.context_manager = SimpleNamespace(replace_messages=explode)
    assert sessions_ui.restore_into(operator, session_id, db) == 1


# -- presentation ----------------------------------------------------------


def test_describe_shows_the_short_id_and_the_summary(db, tmp_path):
    session_id = _make(db, str(tmp_path), summary="fixed the parser")
    session = db.get_session(session_id)
    line = sessions_ui.describe(session)
    assert session_id[:8] in line
    assert "fixed the parser" in line


def test_describe_handles_a_session_with_no_summary(db, tmp_path):
    session = db.get_session(_make(db, str(tmp_path), summary=""))
    assert "(no summary)" in sessions_ui.describe(session)


def test_describe_truncates_a_long_summary(db, tmp_path):
    session = db.get_session(_make(db, str(tmp_path), summary="x" * 500))
    assert len(sessions_ui.describe(session)) < 200


# -- the picker ------------------------------------------------------------


def test_the_picker_returns_nothing_when_there_are_no_sessions(db, tmp_path):
    assert asyncio.run(sessions_ui.pick_session(db, str(tmp_path))) is None


def test_the_picker_offers_this_folder_first(db, tmp_path, monkeypatch):
    elsewhere = _make(db, str(tmp_path / "other"))
    here = _make(db, str(tmp_path / "here"))
    captured = {}

    class FakeSelect:
        def __init__(self, *_a, **kw):
            captured["choices"] = kw["choices"]

        async def ask_async(self):
            return None

    import questionary

    monkeypatch.setattr(questionary, "select", FakeSelect)
    asyncio.run(sessions_ui.pick_session(db, str(tmp_path / "here")))
    values = [getattr(c, "value", None) for c in captured["choices"]]
    assert values.index(here) < values.index(elsewhere)


# -- the CLI flags ---------------------------------------------------------


def test_the_cli_advertises_continue_and_resume():
    from click.testing import CliRunner

    from djcode.cli import main

    out = CliRunner().invoke(main, ["--help"]).output
    assert "--continue" in out and "-c," in out
    assert "--resume" in out


def test_resume_takes_an_optional_value():
    """`--resume` with no value must mean "show me a picker", not consume the
    next argument."""
    from djcode.cli import main

    option = next(p for p in main.params if p.name == "resume_session")
    assert option.is_flag is False
    assert option.flag_value == ""


def test_run_repl_accepts_the_resume_arguments():
    import inspect

    from djcode.repl import run_repl

    params = inspect.signature(run_repl).parameters
    assert "resume" in params and "continue_last" in params
