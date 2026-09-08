import asyncio
from types import SimpleNamespace
import pytest

from djcode import repl
from djcode.provider import ProviderConfig
from djcode.status import StatusBar


@pytest.mark.parametrize('interactive', [False, True, 'auth'])
def test_provider_switch_preserves_account_auth(monkeypatch, interactive):
    from djcode import account_auth
    config = ProviderConfig(name='xai', model='account-model', base_url='https://api.x.ai/v1', auth_method='account')
    monkeypatch.setitem(repl.PROVIDERS, 'xai', {'name': 'xAI', 'needs_key': True})
    monkeypatch.setattr(ProviderConfig, 'from_config', staticmethod(lambda **kw: config))
    monkeypatch.setattr(account_auth, 'has_account', lambda name: name == 'xai')
    monkeypatch.setattr(repl, 'Provider', lambda cfg: SimpleNamespace(config=cfg))
    monkeypatch.setattr(repl, 'set_value', lambda *a: None)
    monkeypatch.setattr(repl, 'interactive_provider_picker', lambda: 'xai')
    monkeypatch.setattr(repl, 'interactive_auth', lambda: None)
    monkeypatch.setattr(repl, 'load_config', lambda: {'provider': 'xai'})
    operator = SimpleNamespace(provider=SimpleNamespace(config=ProviderConfig(name="ollama", base_url="http://localhost:11434", model="old")))
    bar = StatusBar()
    if interactive == 'auth':
        asyncio.run(repl.handle_slash_command('/auth', operator, None, bar))
    elif interactive:
        repl._handle_provider_switch_interactive(operator, bar)
    else:
        asyncio.run(repl.handle_slash_command('/provider xai', operator, None, bar))
    assert operator.provider.config.auth_method == 'account'
    assert bar.model == 'account-model'


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
