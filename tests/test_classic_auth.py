import asyncio
from types import SimpleNamespace
import pytest

from djcode import repl
from djcode.provider import ProviderConfig
from djcode.status import StatusBar


@pytest.mark.parametrize('interactive', [False, True])
def test_verified_provider_switch_preserves_account_auth(monkeypatch, tmp_path, interactive):
    from djcode import config as settings, model_selection
    from djcode.context.manager import ContextWindowManager
    from djcode.provider import Provider
    monkeypatch.setattr(settings, 'CONFIG_DIR', tmp_path)
    monkeypatch.setattr(settings, 'CONFIG_FILE', tmp_path / 'config.json')
    settings.save_config({**settings.DEFAULT_CONFIG, 'provider': 'ollama', 'model': 'old', 'xai_auth_method': 'account'})
    monkeypatch.setattr(repl, 'load_config', settings.load_config)
    monkeypatch.setattr(repl, 'save_config', settings.save_config)
    monkeypatch.setattr(model_selection, 'probe', lambda cfg: {'status': 'ready', 'message': 'Verified account', 'models': ['account-model'], 'source': 'live'})
    async def ask():
        return 'account-model'
    monkeypatch.setattr(repl.questionary, 'autocomplete', lambda *a, **k: SimpleNamespace(ask_async=ask))
    old = Provider(ProviderConfig(name='ollama', base_url='http://localhost:11434', model='old'))
    manager = ContextWindowManager(model='old', provider=old)
    operator = SimpleNamespace(provider=old, context_manager=manager)
    bar = StatusBar()
    if interactive:
        asyncio.run(repl._handle_provider_switch_interactive(operator, bar, provider_id='xai'))
    else:
        asyncio.run(repl.handle_slash_command('/provider xai', operator, None, bar))
    assert operator.provider.config.auth_method == 'account'
    assert bar.model == 'account-model'
    assert bar.provider == 'xai'
    assert operator.context_manager is manager
    assert manager._provider is operator.provider
    assert settings.load_config()['provider'] == 'xai'


def test_auth_status_does_not_replace_active_provider(monkeypatch):
    from djcode import auth_management
    provider = object()
    operator = SimpleNamespace(provider=provider)
    calls = []
    monkeypatch.setattr(auth_management, 'auth_status', lambda selected: calls.append(selected) or {'provider': 'xai', 'method': 'account', 'source': 'stored', 'status': 'ready'})
    asyncio.run(repl.handle_slash_command('/auth status xai', operator, None, StatusBar()))
    assert calls == ['xai']
    assert operator.provider is provider


@pytest.mark.parametrize('command', ['/image', '/video', '/social'])
def test_classic_content_uses_live_provider_and_permissions(monkeypatch, command):
    from djcode.orchestrator import engine
    provider = object()
    callback = object()
    seen = []
    class Runner:
        def __init__(self, selected, spec, bus, **kw):
            seen.append((selected, kw))
        async def run_streaming(self, task):
            yield 'fixture complete'
    monkeypatch.setattr(engine, 'AgentRunner', Runner)
    operator = SimpleNamespace(provider=provider, auto_accept=False, approval_callback=callback)
    asyncio.run(repl.handle_slash_command(command, operator, None, StatusBar(), SimpleNamespace(bus=object())))
    assert seen == [(provider, {'auto_accept': False, 'approval_callback': callback})]
