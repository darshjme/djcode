"""Exercise real keyboard input, running work, and classic mode state."""

import asyncio
from types import SimpleNamespace

import pytest
from textual.widgets import Input

from djcode.app import DJcodeApp


def test_busy_submit_preserves_draft_and_running_worker(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    async def initialize(self):
        self._operator = SimpleNamespace()

    cancelled = []

    async def send(self, text):
        self._is_generating = True
        self._generation_task = asyncio.current_task()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(text)
            self._is_generating = False

    monkeypatch.setattr(DJcodeApp, "_initialize", initialize)
    monkeypatch.setattr(DJcodeApp, "_send_message", send)

    async def run():
        app = DJcodeApp()
        async with app.run_test(size=(80, 28)) as pilot:
            prompt = app.query_one("#prompt-input", Input)
            prompt.value = "first task"
            await pilot.press("enter")
            await pilot.pause()
            prompt.value = "keep this draft"
            await pilot.press("enter")
            await pilot.pause()
            assert prompt.value == ""
            assert app._followups == ["keep this draft"]
            assert cancelled == []
            await pilot.press("ctrl+k")
            await pilot.pause()
            assert cancelled == ["first task"]
            assert prompt.value == ""
            assert app._followups == ["keep this draft"]

    asyncio.run(run())


def test_repl_plan_shortcut_sets_operator_gate():
    from djcode import tui
    from djcode.status import StatusBar

    old_mode = tui._mode
    tui._mode = tui.ModeState()
    try:
        operator = SimpleNamespace(plan_mode=False)
        status = StatusBar()
        keys = tui.register_keybindings(SimpleNamespace(), operator, status)
        output = SimpleNamespace(write=lambda _: None, flush=lambda: None)
        event = SimpleNamespace(app=SimpleNamespace(output=output))
        binding = next(binding for binding in keys.bindings if binding.keys == ("c-p",))
        binding.handler(event)
        assert operator.plan_mode is True
        assert status.mode == "PLAN"
    finally:
        tui._mode = old_mode


def test_tui_completion_submission_and_draft_history(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    dispatched = []

    async def initialize(self):
        pass

    async def handle(self, text):
        dispatched.append(text)

    monkeypatch.setattr(DJcodeApp, "_initialize", initialize)
    monkeypatch.setattr(DJcodeApp, "_handle_slash_command", handle)

    async def run():
        app = DJcodeApp()
        async with app.run_test(size=(80, 28)) as pilot:
            prompt = app.query_one("#prompt-input", Input)
            await pilot.press("/", "s", "t", "a")
            await pilot.press("tab")
            assert prompt.value == "/stats "
            assert prompt.has_focus
            await pilot.press("enter")
            await pilot.pause()
            assert dispatched == ["/stats"]
            prompt.value = "/model actual-model"
            await pilot.press("enter")
            await pilot.pause()
            assert dispatched[-1] == "/model actual-model"
            prompt.value = "unsent draft"
            await pilot.press("up")
            assert prompt.value == "/model actual-model"
            await pilot.press("down")
            assert prompt.value == "unsent draft"
            prompt.value = "/sta"
            await pilot.pause()
            await pilot.press("enter")
            assert prompt.value == "/stats "
            assert len(dispatched) == 2
            prompt.value = "/stats"
            await pilot.press("enter")
            await pilot.pause()
            assert dispatched[-1] == "/stats"
            assert len(dispatched) == 3

    asyncio.run(run())


@pytest.mark.parametrize(
    "query, expected",
    [
        ("/sta", "/stats"),
        ("/upd", "/update"),
        ("/pla", "/plan"),
    ],
)
def test_repl_completion_replaces_only_command(query, expected):
    from prompt_toolkit.document import Document

    from djcode.commands import SlashCompleter

    completions = list(SlashCompleter().get_completions(Document(query), None))
    assert completions[0].text == expected
    assert completions[0].start_position == -len(query)
    for text in ("/model some-name", "ordinary prompt", "/stats ", "/model\nname"):
        assert list(SlashCompleter().get_completions(Document(text), None)) == []


def test_repl_help_and_picker_only_advertise_supported_commands():
    from djcode.commands import command_help, commands_for
    from djcode.tui import COMMAND_GROUPS

    names = {command.name for command in commands_for("repl")}
    assert {"/check", "/lint", "/update", "/design", "/plan", "/thinking"} <= names
    assert "/spawn" not in names
    assert names == {name for group in COMMAND_GROUPS.values() for name, _ in group}
    assert all(name in command_help("repl") for name in names)


def test_classic_docs_browser_does_not_call_provider(monkeypatch):
    from djcode import docs, repl
    from djcode.status import StatusBar

    observed = []
    monkeypatch.setattr(docs, "render_docs_index", lambda _: observed.append("index"))
    monkeypatch.setattr(docs, "render_docs", lambda _, topic: observed.append(topic))
    operator = SimpleNamespace(plan_mode=True)
    for command in ("/docs", "/docs overview"):
        assert asyncio.run(repl.handle_slash_command(command, operator, None, StatusBar()))
    assert observed == ["index", "overview"]


def test_plan_mode_denies_real_model_tool_request(tmp_path):
    import json

    from djcode import repl, tui
    from djcode.agents.operator import Operator
    from djcode.status import StatusBar

    target = tmp_path / "must-not-exist"

    class Provider:
        config = SimpleNamespace(model="test-model")

        async def chat(self, messages, stream=True):
            if messages[-1].role == "user":
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call1",
                                        "function": {
                                            "name": "file_write",
                                            "arguments": json.dumps(
                                                {
                                                    "path": str(target),
                                                    "content": "bad",
                                                }
                                            ),
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            else:
                yield {
                    "choices": [{"delta": {"content": "Planning only."}, "finish_reason": "stop"}]
                }

    async def run():
        operator = Operator(Provider(), auto_accept=True, raw=True)
        try:
            await repl.handle_slash_command("/plan", operator, None, StatusBar())
            assert operator.plan_mode
            _ = [token async for token in operator.send("write a file")]
            assert not target.exists()
            assert any(m.role == "tool" and "denied" in m.content for m in operator.messages)
        finally:
            tui._mode = tui.ModeState()

    asyncio.run(run())


@pytest.mark.parametrize("command", ["/review", "/orchestra build", "/recipe run build"])
def test_classic_plan_blocks_specialist_bypass(command):
    from djcode import repl
    from djcode.status import StatusBar

    assert asyncio.run(
        repl.handle_slash_command(
            command,
            SimpleNamespace(plan_mode=True),
            None,
            StatusBar(),
        )
    )


def test_status_escapes_workspace_and_provider_text(monkeypatch, tmp_path):
    from prompt_toolkit.formatted_text import to_formatted_text

    from djcode.status import StatusBar

    folder = tmp_path / "a<b&c>"
    folder.mkdir()
    monkeypatch.chdir(folder)
    status = StatusBar()
    status.update(model="a<b>", provider="custom&local", auto_accept=True)
    text = "".join(fragment[1] for fragment in to_formatted_text(status.render()))
    assert "a<b>" in text and "custom&local" in text and "a<b&c>" in text
    assert "Approvals: auto" in text


def test_tui_real_initialization_persists_conversation(monkeypatch, tmp_path):
    from djcode import sessions
    from djcode.provider import Message, Provider, ProviderConfig

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sessions, "DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(
        ProviderConfig,
        "from_config",
        staticmethod(
            lambda **_: ProviderConfig(
                name="openai",
                model="fixture",
                base_url="https://example.invalid",
                api_key="fixture",
            )
        ),
    )
    monkeypatch.setattr(Provider, "validate_model", lambda _: (True, ""))

    async def run():
        app = DJcodeApp(provider_name="openai")
        async with app.run_test(size=(80, 28)) as pilot:
            for _ in range(50):
                if app._sqlite_session_id:
                    break
                await pilot.pause(0.02)
            assert app._session_db is not None
            session_id = app._sqlite_session_id
            assert isinstance(session_id, str)
            app._operator.messages.extend(
                [
                    Message(role="user", content="Retain this prompt"),
                    Message(role="assistant", content="Retain this reply"),
                ]
            )
        database = sessions.SessionDB()
        restored = database.load_conversation(session_id)
        assert [item["content"] for item in restored[-2:]] == [
            "Retain this prompt",
            "Retain this reply",
        ]

    asyncio.run(run())


def test_interrupt_cancels_only_foreground_operation(monkeypatch):
    import signal

    from djcode.repl_runtime import run_interruptible

    async def run():
        loop = asyncio.get_running_loop()
        handlers = {}
        removed = []
        monkeypatch.setattr(loop, "add_signal_handler", lambda sig, fn: handlers.update({sig: fn}))
        monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: removed.append(sig))
        closed = []

        async def hanging():
            loop.call_soon(handlers[signal.SIGINT])
            try:
                await asyncio.Event().wait()
            finally:
                closed.append(True)

        assert await run_interruptible(hanging()) is None
        assert closed == [True]
        assert removed == [signal.SIGINT]
        assert await run_interruptible(asyncio.sleep(0, result="next prompt")) == "next prompt"
        task = asyncio.create_task(run_interruptible(asyncio.sleep(100)))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


def test_repl_error_returns_to_prompt(monkeypatch):
    from djcode import repl

    async def failing(*_):
        raise RuntimeError("fixture failure")

    monkeypatch.setattr(repl, "handle_slash_command", failing)
    assert asyncio.run(repl._run_repl_command("/check", None, None, None, None))


def test_repl_auto_toggle_uses_effective_state(monkeypatch):
    from djcode import repl, tui
    from djcode.status import StatusBar

    writes = []
    monkeypatch.setattr(repl, "set_value", lambda key, value: writes.append((key, value)))
    operator = SimpleNamespace(auto_accept=True, plan_mode=False, provider=object())
    orchestrator = SimpleNamespace(_shadow=SimpleNamespace())
    try:
        asyncio.run(repl.handle_slash_command("/auto", operator, None, StatusBar(), orchestrator))
        assert not operator.auto_accept
        assert not orchestrator._shadow.auto_accept
        assert writes == [("auto_accept", False)]
    finally:
        tui._mode = tui.ModeState()


@pytest.mark.parametrize("width", [60, 80, 120])
def test_narrow_chat_wraps_and_controls_do_not_overlap(monkeypatch, tmp_path, width):
    from textual.widgets import Footer, OptionList, RichLog

    monkeypatch.chdir(tmp_path)

    async def initialize(self):
        pass

    monkeypatch.setattr(DJcodeApp, "_initialize", initialize)

    async def run():
        app = DJcodeApp()
        async with app.run_test(size=(width, 28)) as pilot:
            chat = app.query_one("#chat-log", RichLog)
            chat.write("This sentence should wrap without horizontal scrolling. " * 4)
            prompt = app.query_one("#prompt-input", Input)
            prompt.value = "/"
            await pilot.pause()
            suggestions = app.query_one("#cmd-suggest", OptionList)
            status = app.query_one("#status-bar")
            assert chat.max_scroll_x == 0
            assert chat.region.bottom <= suggestions.region.y
            assert suggestions.region.bottom <= prompt.region.y
            assert prompt.region.bottom <= status.region.y
            assert status.region.bottom <= app.query_one(Footer).region.y
            assert prompt.region.bottom < 28
            prompt.value = ""
            chat.write("BEGIN_REFLOW " + "readable content " * 10 + " END_REFLOW")
            await pilot.resize_terminal(60, 28)
            await pilot.pause()
            assert chat.max_scroll_x == 0
            rendered = " ".join(line.text for line in chat.lines)
            assert "BEGIN_REFLOW" in rendered and "END_REFLOW" in rendered
            chat.clear()
            await pilot.resize_terminal(100, 28)
            await pilot.pause()
            assert "BEGIN_REFLOW" not in " ".join(line.text for line in chat.lines)

    asyncio.run(run())
