"""Headless UI interaction tests; no provider or network access."""
import asyncio
import pytest
from textual.widgets import Input, OptionList
from djcode.app import DJcodeApp, CommandPalette, ToolApprovalScreen


@pytest.mark.parametrize('width', [60, 80, 120])
def test_responsive_layout_palette_and_approval(monkeypatch, tmp_path, width):
    monkeypatch.chdir(tmp_path)
    async def initialize(self):
        pass
    monkeypatch.setattr(DJcodeApp, '_initialize', initialize)
    async def run():
        app = DJcodeApp()
        async with app.run_test(size=(width, 28)) as pilot:
            await pilot.pause()
            side = app.query_one('#side-panel')
            assert side.display == (width >= 110)
            assert app.query_one('#prompt-input').region.bottom < 28
            assert app.query_one('#chat-log').size.height >= 12
            await pilot.press('ctrl+p')
            assert app._plan_mode
            await pilot.press('ctrl+p')
            assert not app._plan_mode
            await pilot.press('ctrl+b')
            assert side.display == (width < 110)
            await pilot.press('ctrl+b')
            selected = []
            app.push_screen(CommandPalette(), selected.append)
            await pilot.pause()
            palette = app.screen
            palette.query_one('#palette-input', Input).value = '/'
            await pilot.pause()
            await pilot.press('down', 'enter')
            await pilot.pause()
            assert selected == ['/check']
            pending = asyncio.create_task(app._approve_tool('file_write', {'path': 'sample.py', 'content': '\n'.join(str(i) for i in range(200))}))
            await pilot.pause()
            assert isinstance(app.screen, ToolApprovalScreen)
            box = app.screen.query_one('#approval-box')
            assert box.region.x >= 0 and box.region.right <= width
            deny = app.screen.query_one('#deny-tool')
            assert deny.region.bottom <= 28
            assert app.focused is deny
            await pilot.press('escape')
            assert await pending is False
    asyncio.run(run())


def test_maintenance_commands_keep_ui_responsive(monkeypatch, tmp_path):
    import sys
    import threading
    from types import ModuleType
    from textual.widgets import RichLog
    from djcode import updater
    monkeypatch.chdir(tmp_path)
    async def initialize(self):
        pass
    monkeypatch.setattr(DJcodeApp, '_initialize', initialize)
    maintenance = ModuleType('djcode.maintenance')
    def check():
        assert threading.current_thread() is not threading.main_thread()
        return {'ok': True, 'summary': 'Checks passed', 'checks': [{'name': 'runtime', 'status': 'pass', 'detail': 'available'}]}
    maintenance.run_checks = check
    monkeypatch.setitem(sys.modules, 'djcode.maintenance', maintenance)
    def update(force):
        assert force is True
        assert threading.current_thread() is not threading.main_thread()
        return {'ok': True, 'status': 'updated', 'message': 'Updated fixture', 'updated': True}
    monkeypatch.setattr(updater, 'perform_update', update, raising=False)
    async def run():
        app = DJcodeApp()
        async with app.run_test(size=(80, 28)) as pilot:
            for cmd in ['/check', '/lint', '/update']:
                await app._handle_slash_command(cmd)
            await pilot.pause()
            text = '\n'.join(line.text for line in app.query_one('#chat-log', RichLog).lines)
            assert 'Checks passed' in text and 'runtime: pass' in text
            assert 'Updated fixture' in text and 'Restart DJcode' in text
            assert app.is_running
    asyncio.run(run())
