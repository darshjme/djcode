"""Actual mounted UI choices, keyboard discovery and retained runtime state."""
import asyncio
from types import SimpleNamespace

import pytest
from prompt_toolkit.buffer import Buffer
from textual.widgets import Input, OptionList, Select, Static

from djcode import config as settings, model_picker, model_selection
from djcode.app import CommandPalette, DJcodeApp
from djcode.context.manager import ContextWindowManager
from djcode.model_picker import ModelPicker
from djcode.provider import Message, Provider, ProviderConfig
from djcode.tui_hacker import HackerHeader


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, 'CONFIG_DIR', tmp_path)
    monkeypatch.setattr(settings, 'CONFIG_FILE', tmp_path / 'config.json')
    monkeypatch.delenv('DJCODE_BASE_URL', raising=False)
    settings.save_config({**settings.DEFAULT_CONFIG, 'provider': 'ollama', 'model': 'Old'})
    async def initialize(self):
        pass
    monkeypatch.setattr(DJcodeApp, '_initialize', initialize)


@pytest.mark.parametrize('size', [(40, 18), (80, 28)])
def test_search_keyboard_selects_highlighted_case_sensitive_model(isolated, monkeypatch, size):
    records = [{'id': 'Case/First', 'name': 'Fixture first', 'capabilities': {}},
               {'id': 'Case/Second', 'name': 'Fixture second', 'capabilities': {'tools': True}}]
    monkeypatch.setattr(model_picker, 'model_catalog', lambda cfg, provider: {'models': records, 'source': 'live', 'message': 'Fixture', 'selected': None})
    async def run():
        selected = []
        app = DJcodeApp()
        async with app.run_test(size=size) as pilot:
            screen = ModelPicker('openrouter', config=settings.load_config())
            app.push_screen(screen, selected.append)
            await pilot.pause()
            for _ in range(10):
                if screen._models:
                    break
                await pilot.pause()
            screen.query_one('#model-search', Input).value = 'CASE'
            await pilot.pause()
            options = screen.query_one('#model-list', OptionList)
            assert options.option_count == 2
            await pilot.press('down')
            assert options.get_option_at_index(options.highlighted).id == 'Case/Second'
            assert 'tools yes' in str(screen.query_one('#model-capabilities', Static).render())
            assert screen.query_one('#model-keys').region.bottom <= size[1]
            await pilot.press('enter')
            assert selected == [{'provider': 'openrouter', 'model': 'Case/Second'}]
            assert settings.load_config()['model'] == 'Old'
    asyncio.run(run())


def test_model_picker_changes_provider_without_carrying_old_model(isolated, monkeypatch):
    seen = []
    def catalog(cfg, provider):
        seen.append(provider)
        return {'models': [{'id': provider + '-model', 'name': provider + '-model', 'capabilities': {}}], 'source': 'live', 'message': 'Fixture'}
    monkeypatch.setattr(model_picker, 'model_catalog', catalog)
    async def run():
        app = DJcodeApp()
        async with app.run_test(size=(80, 24)) as pilot:
            screen = ModelPicker('ollama', config=settings.load_config())
            app.push_screen(screen)
            await pilot.pause()
            screen.query_one('#model-provider', Select).value = 'openai'
            await pilot.pause()
            for _ in range(10):
                if screen._models:
                    break
                await pilot.pause()
            options = screen.query_one('#model-list', OptionList)
            assert options.option_count == 1 and options.get_option_at_index(0).id == 'openai-model'
            assert seen[-1] == 'openai'
            await pilot.press('escape')
    asyncio.run(run())


def test_catalog_auth_failure_has_clear_empty_state_and_cannot_select(isolated, monkeypatch):
    monkeypatch.setattr(model_picker, 'model_catalog', lambda *args: {'models': [], 'message': 'Account sign-in is required.', 'source': 'none'})
    async def run():
        app = DJcodeApp()
        selected = []
        async with app.run_test(size=(40, 18)) as pilot:
            screen = ModelPicker('xai', config=settings.load_config())
            app.push_screen(screen, selected.append)
            await pilot.pause()
            assert 'sign-in' in str(screen.query_one('#model-info', Static).render())
            await pilot.press('enter')
            assert app.screen is screen and selected == []
            await pilot.press('escape')
            assert selected == [None]
    asyncio.run(run())


def test_successful_tui_selection_updates_runtime_context_session_header_and_config(isolated, monkeypatch):
    monkeypatch.setattr(model_selection, 'probe', lambda cfg: {'status': 'ready', 'message': 'Fixture verified', 'models': [cfg['model']]})
    async def run():
        old = Provider(ProviderConfig(name='ollama', base_url='http://localhost:11434', model='Old', provider_id='ollama'))
        manager = ContextWindowManager(model='Old', provider=old)
        messages = [Message(role='system', content='Fixture system'), Message(role='user', content='Keep conversation')]
        manager.add_message(messages[0])
        manager.add_message(messages[1], pinned=True)
        manager.inject_context('Keep injected context', source='fixture')
        metadata = []
        class DB:
            def set_session_model(self, *args): metadata.append(args)
            def save_conversation(self, *args): pass
            def end_session(self, *args): pass
        app = DJcodeApp()
        async with app.run_test(size=(80, 28)) as pilot:
            app._provider = old
            app._provider_name = 'ollama'
            app._operator = SimpleNamespace(provider=old, context_manager=manager, messages=messages, capabilities=object())
            app._context_mgr = manager
            app._orchestrator = SimpleNamespace(provider=old, _shadow=SimpleNamespace(provider=old))
            app._session_db, app._sqlite_session_id = DB(), 'fixture-session'
            old._session_runtimes = []
            await app._handle_model_switch('openrouter/Case/New')
            await pilot.pause()
            assert app._provider is app._operator.provider
            assert app._provider.config.provider_id == 'openrouter'
            assert app._provider.config.model == 'Case/New'
            assert app._operator.context_manager is manager
            assert manager.stats.pinned_count == 1 and manager.stats.injected_count == 1
            assert app._operator.messages is messages
            assert manager._provider is app._provider and manager._compressor._provider is app._provider
            assert app._orchestrator._shadow.provider is app._provider
            assert app.query_one(HackerHeader).model_name == 'Case/New'
            assert metadata == [('fixture-session', 'openrouter', 'Case/New')]
            saved = settings.load_config()
            assert saved['provider'] == 'openrouter' and saved['model'] == 'Case/New'
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['offline', 'save', 'damaged', 'busy'])
def test_rejected_switch_preserves_runtime_and_saved_selection(isolated, monkeypatch, failure):
    original = settings.CONFIG_FILE.read_text()
    monkeypatch.setattr(model_selection, 'probe', lambda cfg: {'status': 'offline' if failure == 'offline' else 'ready', 'message': 'Fixture failure', 'models': [cfg['model']]})
    if failure == 'save':
        def failed_save(cfg): raise OSError('Fixture save failure')
        monkeypatch.setattr(settings, 'save_config', failed_save)
    if failure == 'damaged':
        settings.CONFIG_FILE.write_text('{broken')
        original = settings.CONFIG_FILE.read_text()
    async def run():
        old = Provider(ProviderConfig(name='ollama', base_url='http://localhost:11434', model='Old'))
        app = DJcodeApp()
        async with app.run_test(size=(80, 28)) as pilot:
            app._provider = old
            app._provider_name = 'ollama'
            app._is_generating = failure == 'busy'
            await app._handle_model_switch('New')
            assert app._provider is old and old.config.model == 'Old'
            assert settings.CONFIG_FILE.read_text() == original
            app._is_generating = False
    asyncio.run(run())


def test_ctrl_p_palette_preserves_draft_on_escape_and_discovers_operations(isolated):
    async def run():
        app = DJcodeApp()
        async with app.run_test(size=(40, 18)) as pilot:
            prompt = app.query_one('#prompt-input', Input)
            prompt.value = 'Keep this draft'
            await pilot.press('ctrl+p')
            assert isinstance(app.screen, CommandPalette)
            screen = app.screen
            screen.query_one('#palette-input', Input).value = 'update'
            await pilot.pause()
            options = screen.query_one('#palette-list', OptionList)
            assert options.option_count >= 1 and options.get_option_at_index(0).id == '/update'
            await pilot.press('escape')
            assert prompt.value == 'Keep this draft' and prompt.has_focus
            await pilot.press('ctrl+g')
            assert app._plan_mode
    asyncio.run(run())


def test_repl_ctrl_p_completes_commands_without_destroying_draft():
    from djcode import tui
    from djcode.status import StatusBar
    keys = tui.register_keybindings(SimpleNamespace(), SimpleNamespace(), StatusBar())
    binding = next(binding for binding in keys.bindings if binding.keys == ('c-p',))
    seen = []
    buffer = Buffer()
    buffer.start_completion = lambda **kwargs: seen.append(kwargs)
    event = SimpleNamespace(app=SimpleNamespace(current_buffer=buffer))
    binding.handler(event)
    assert buffer.text == '/' and seen == [{'select_first': False}]
    buffer.text = 'Keep draft'
    binding.handler(event)
    assert buffer.text == 'Keep draft' and len(seen) == 1
