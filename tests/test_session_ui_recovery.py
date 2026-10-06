"""Resuming and switching sessions keeps display, context, and persistence aligned."""

import asyncio
from types import SimpleNamespace

import pytest
from textual.widgets import RichLog

from djcode.app import DJcodeApp
from djcode.context.manager import ContextWindowManager
from djcode.provider import Message
from djcode.session_commands import handle
from djcode.sessions import SessionDB


def operator_fixture(db, session_id):
    operator = SimpleNamespace(
        messages=[Message("system", "Current workspace rules"), Message("user", "Current draft")],
        provider=SimpleNamespace(config=SimpleNamespace(model="current-model", name="fixture")),
        context_manager=ContextWindowManager(model="fixture", max_context=4096),
        session_db=db,
        session_id=session_id,
        on_checkpoint=None,
    )
    operator.reset = lambda: setattr(operator, "messages", operator.messages[:1])
    operator.context_manager.replace_messages(operator.messages)
    operator.on_checkpoint = lambda messages: db.save_conversation(operator.session_id, messages)
    return operator


def test_session_prefix_is_unique_and_reopen_retains_record(tmp_path):
    db = SessionDB(tmp_path / "sessions.db")
    sid = db.create_session("model", "provider")
    db.end_session(sid, summary="Preserve summary")
    assert db.resolve_session_id(sid[:8]) == sid
    assert db.resolve_session_id("missing") is None
    assert db.resolve_session_id("s_%") is None
    db.reopen_session(sid)
    assert db.get_session(sid).end is None
    assert db.get_session(sid).summary == "Preserve summary"
    db.end_session(sid)
    assert db.get_session(sid).summary == "Preserve summary"
    db.create_session("other", "provider")
    with pytest.raises(ValueError, match="ambiguous"):
        db.resolve_session_id("s_")


@pytest.mark.parametrize("command", ["/new", "/fork"])
def test_session_switch_preserves_previous_and_syncs_context(tmp_path, command):
    db = SessionDB(tmp_path / "sessions.db")
    old_id = db.create_session("fixture", "fixture")
    operator = operator_fixture(db, old_id)
    result = asyncio.run(handle(operator, command))
    assert operator.session_id != old_id
    assert operator.session_id in result
    assert db.load_conversation(old_id)[-1]["content"] == "Current draft"
    assert db.get_session(old_id).end is not None
    expected = ["Current workspace rules"]
    if command == "/fork":
        expected.append("Current draft")
    assert [message.content for message in operator.context_manager.get_messages()] == expected
    assert [message["content"] for message in db.load_conversation(operator.session_id)] == expected


def test_resume_unique_prefix_renders_history_and_saves_to_target(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    db = SessionDB(tmp_path / "sessions.db")
    target = db.create_session("old-model", "fixture")
    db.save_conversation(target, [
        Message("system", "Old workspace rules"),
        Message("user", "Keep literal [red]markup[/red]"),
        Message("assistant", "", tool_calls=[{"id": "call", "function": {"name": "file_read", "arguments": "{}"}}]),
        Message("tool", "Read result", tool_call_id="call", name="file_read"),
        Message("assistant", "Recovered reply"),
    ])
    db.end_session(target)
    old_id = db.create_session("current-model", "fixture")
    operator = operator_fixture(db, old_id)
    async def initialize(self):
        pass
    monkeypatch.setattr(DJcodeApp, "_initialize", initialize)
    async def run():
        app = DJcodeApp()
        async with app.run_test(size=(60, 24)) as pilot:
            app._operator = operator
            app._session_db = db
            app._sqlite_session_id = old_id
            app._attach_session_checkpoint()
            app._handle_resume(target[:8])
            await pilot.pause()
            assert app._sqlite_session_id == operator.session_id == target
            assert operator.messages[0].content == "Current workspace rules"
            assert operator.messages[3].tool_call_id == "call"
            assert [message.content for message in operator.context_manager.get_messages()] == [message.content for message in operator.messages]
            text = "\n".join(line.text for line in app.query_one("#chat-log", RichLog).lines)
            assert "Keep literal [red]markup[/red]" in text
            assert "Recovered reply" in text and "Current draft" not in text
            operator.messages.append(Message("user", "Continued"))
            operator.on_checkpoint(operator.messages)
            assert db.load_conversation(target)[-1]["content"] == "Continued"
            assert db.load_conversation(old_id)[-1]["content"] == "Current draft"
            assert db.get_session(target).end is None
            assert db.get_session(old_id).end is not None
            # A rejected switch cannot replace the active transcript.
            app._is_generating = True
            app._handle_resume(old_id)
            assert app._sqlite_session_id == target
            app._is_generating = False
            app._operator = None
    asyncio.run(run())
