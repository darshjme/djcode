"""Project definitions remain editable and actionable in compact terminals."""

import asyncio
import json

import pytest
from textual.widgets import Button, Input, Label, Select, TextArea

from djcode import studio
from djcode.app import DJcodeApp
from djcode.studio_screen import StudioScreen, TEMPLATES


@pytest.mark.parametrize("size", [(40, 18), (60, 20), (100, 30)])
def test_compact_studio_actions_and_keyboard_save(monkeypatch, tmp_path, size):
    monkeypatch.chdir(tmp_path)
    async def initialize(self):
        pass
    monkeypatch.setattr(DJcodeApp, "_initialize", initialize)
    async def run():
        app = DJcodeApp()
        captured = []
        async with app.run_test(size=size) as pilot:
            app.push_screen(StudioScreen(), captured.append)
            await pilot.pause()
            screen = app.screen
            assert screen.query_one("#studio-editor").size.height >= 2
            for button in screen.query(Button):
                assert button.region.x >= 0 and button.region.right <= size[0]
                assert button.region.bottom <= size[1]
            assert screen.query_one("#studio-run", Button).disabled
            editor = screen.query_one("#studio-editor", TextArea)
            editor.load_text("[]")
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app.screen is screen
            assert "JSON object" in str(screen.query_one("#studio-error", Label).render())
            await pilot.press("ctrl+n", "ctrl+s")
            await pilot.pause()
            assert captured and json.loads(captured[0].split(" ", 2)[2]) == TEMPLATES["agent"]
    asyncio.run(run())


def test_kind_changes_keep_drafts_and_load_saved_definition(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(studio, "CONFIG_DIR", tmp_path / "config")
    store = studio.Studio()
    store.save("agent", {"name": "saved", "role": "coder", "prompt": "Saved prompt"})
    async def initialize(self):
        pass
    monkeypatch.setattr(DJcodeApp, "_initialize", initialize)
    async def run():
        app = DJcodeApp()
        async with app.run_test(size=(60, 22)) as pilot:
            app.push_screen(StudioScreen())
            await pilot.pause()
            screen = app.screen
            kind = screen.query_one("#studio-kind", Select)
            editor = screen.query_one("#studio-editor", TextArea)
            kind.value = "flow"
            await pilot.pause()
            assert json.loads(editor.text) == TEMPLATES["flow"]
            screen.query_one("#studio-name", Input).value = "release"
            await pilot.pause()
            assert not screen.query_one("#studio-run", Button).disabled
            editor.load_text('{"name":"edited-flow","nodes":[]}')
            kind.value = "agent"
            await pilot.pause()
            assert "edited-flow" in editor.text
            assert screen.query_one("#studio-run", Button).disabled
            screen.query_one("#studio-name", Input).value = "saved"
            await pilot.press("ctrl+r")
            await pilot.pause()
            assert json.loads(editor.text)["prompt"] == "Saved prompt"
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is not screen
    asyncio.run(run())
