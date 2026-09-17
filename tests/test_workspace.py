"""Workspace navigation reaches real capabilities without implicit task dispatch."""

import asyncio
import json
import subprocess

import pytest
from textual.widgets import Input, RichLog

from djcode.app import DJcodeApp
from djcode.commands import commands_for, plan_blocks_command
from djcode.session_commands import handle
from djcode.workspace import fleet_command, project_context


def test_project_reports_real_git_state(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q", "-b", "workspace-test"], check=True)
    (tmp_path / "new.py").write_text("x = 1\n")
    text = project_context()
    assert "workspace-test" in text and "?? new.py" in text


def test_workspace_commands_available_without_provider():
    async def run():
        text = await handle(None, "/features")
        assert "/design" in text and "/fleet" in text and "Semantic retrieval" in text

    asyncio.run(run())
    for interface in ("tui", "repl"):
        assert {"/features", "/project", "/fleet", "/build"} <= {c.name for c in commands_for(interface)}
    assert plan_blocks_command("/build", "change code")
    assert plan_blocks_command("/fleet", '{"text":"change code"}')
    assert not plan_blocks_command("/fleet", "")


def test_fleet_dispatch_and_input_validation(monkeypatch):
    from djcode import vyasa

    calls = []

    def request(**kwargs):
        calls.append(kwargs)
        return {"result": "fixture"}

    monkeypatch.setattr(vyasa, "request_fleet", request)

    async def run():
        assert json.loads(await fleet_command("")) == {"result": "fixture"}
        await fleet_command('{"text":"Review","employee":"agni","session":"release"}')
        for arg in ("[]", '{"token":"never"}', '{"text":2}', '{"employee":"agni"}'):
            with pytest.raises(ValueError):
                await fleet_command(arg)

    asyncio.run(run())
    assert calls == [{}, {"text": "Review", "employee": "agni", "session": "release"}]


def test_workspace_navigation_and_resize(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    async def initialize(self):
        pass

    monkeypatch.setattr(DJcodeApp, "_initialize", initialize)

    async def run():
        app = DJcodeApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            assert app.query_one("#workspace-nav").display
            await pilot.click("#nav-design")
            await pilot.pause()
            text = "\n".join(line.text for line in app.query_one("#chat-log", RichLog).lines)
            assert "dashboard" in text and "empty-error" in text
            await pilot.click("#nav-scout")
            assert app.query_one("#prompt-input", Input).value == "/scout "
            assert not app._is_generating
            await pilot.click("#nav-build-agent")
            assert app.query_one("#prompt-input", Input).value == "/build "
            assert not app._is_generating
            await pilot.resize_terminal(60, 24)
            await pilot.pause()
            assert not app.query_one("#workspace-nav").display
            assert app.query_one("#prompt-input").region.right <= 60
            assert app.query_one("#prompt-input").region.bottom < 24

    asyncio.run(run())
