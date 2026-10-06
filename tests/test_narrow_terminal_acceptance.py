"""Real keyboard actions and dialog controls fit constrained terminal viewports."""

import asyncio

import pytest
from textual.widgets import Button, Input, OptionList, Tabs, TabbedContent

from djcode.app import (
    AgentsScreen, CommandPalette, DJcodeApp, HelpScreen, ModelPicker,
    ToolApprovalScreen,
)
from djcode.studio_screen import StudioScreen
from djcode.connect import ConnectScreen


@pytest.mark.parametrize("size", [(40, 18), (60, 20), (80, 24)])
def test_narrow_keyboard_dialogs_and_sidebar(monkeypatch, tmp_path, size):
    monkeypatch.chdir(tmp_path)
    async def initialize(self):
        pass
    async def load_models(self):
        self._models = [{"name": "fixture-model"}]
        self._render_models("")
    monkeypatch.setattr(DJcodeApp, "_initialize", initialize)
    monkeypatch.setattr(ModelPicker, "_load_models", load_models)
    monkeypatch.setattr(ModelPicker, "_save_recent", lambda *args: None)
    async def run():
        app = DJcodeApp()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            assert app.query_one("#prompt-input").region.bottom <= size[1]
            for key, kind, box in [
                ("f1", HelpScreen, "#help-box"),
                ("f2", ModelPicker, "#model-box"),
                ("f3", ConnectScreen, "#connect-box"),
                ("f4", CommandPalette, "#palette-box"),
                ("f5", AgentsScreen, "#agents-box"),
                ("ctrl+e", StudioScreen, "#studio-dialog"),
            ]:
                await pilot.press(key)
                await pilot.pause()
                assert isinstance(app.screen, kind)
                region = app.screen.query_one(box).region
                assert region.x >= 0 and region.right <= size[0]
                assert region.y >= 0 and region.bottom <= size[1]
                if kind is HelpScreen:
                    text = str(app.screen.query_one("#help-box").children[0].render())
                    assert "Ctrl+B" in text and "Ctrl+E" in text and "F6" in text
                if kind is AgentsScreen:
                    assert app.screen.query_one(box).max_scroll_y > 0
                await pilot.press("escape")
                await pilot.pause()
                assert not isinstance(app.screen, kind)
            # F6 reveals the panel and focuses native tabs, including off-screen tabs.
            await pilot.press("f6")
            await pilot.pause()
            tabs = app.query_one("#side-panel Tabs", Tabs)
            assert app.focused is tabs
            content = app.query_one("#side-panel TabbedContent", TabbedContent)
            for expected in ["agents", "stats", "mcp", "todos", "cost", "army", "intel"]:
                await pilot.press("right")
                await pilot.pause()
                assert content.active == f"{expected}-tab"
            assert app.query_one("#side-panel").display
            await pilot.press("escape")
            assert app.focused is app.query_one("#prompt-input", Input)
            await pilot.press("ctrl+b")
            assert not app.query_one("#side-panel").display
            # Long permission details scroll while both choices stay on screen.
            pending = asyncio.create_task(app._approve_tool("file_write", {"content": "line\n" * 200}))
            await pilot.pause()
            assert isinstance(app.screen, ToolApprovalScreen)
            for button in app.screen.query(Button):
                assert button.region.x >= 0 and button.region.right <= size[0]
                assert button.region.bottom <= size[1]
            assert app.focused.id == "deny-tool"
            await pilot.press("tab", "enter")
            assert await pending is True
            # Actual onboarding input and cancel stay reachable at each stage.
            app.push_screen(ConnectScreen())
            await pilot.pause()
            connect = app.screen
            for stage, text, password in [("key", "Enter a key", True), ("url", "Enter the endpoint", False), ("model", "Choose a model", False)]:
                connect._field(stage, text, text, password)
                await pilot.pause()
                field = connect.query_one("#connect-input", Input)
                cancel = connect.query_one("#connect-cancel", Button)
                assert field.region.bottom <= size[1]
                assert cancel.region.bottom <= size[1]
                assert field.password is password
                assert app.focused is field
            await pilot.press("escape")
    asyncio.run(run())
