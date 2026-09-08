"""Colibri integration uses protocol fixtures only; no engine or weights required."""
import asyncio
import json

import httpx
import pytest

from djcode.auth import PROVIDERS, get_api_key
from djcode.provider import Provider, ProviderConfig, Message
from djcode.agents.operator import Operator


@pytest.fixture
def settings(monkeypatch):
    cfg = {'provider': 'ollama', 'model': 'unrelated-cloud-model', 'max_tokens': 9000}
    monkeypatch.setattr('djcode.provider.load_config', lambda: cfg)
    monkeypatch.setattr('djcode.auth.load_config', lambda: cfg)
    for variable in ('DJCODE_BASE_URL', 'DJCODE_COLIBRI_CONTEXT', 'DJCODE_COLIBRI_MAX_TOKENS', 'COLI_API_KEY'):
        monkeypatch.delenv(variable, raising=False)
    return cfg


def test_colibri_defaults_do_not_inherit_other_provider_model(settings):
    cfg = ProviderConfig.from_config('colibri')
    assert cfg.model == 'djcode-colibri'
    assert cfg.base_url == 'http://127.0.0.1:8000/v1'
    assert cfg.context_window == 8192 and cfg.max_tokens == 256
    assert cfg.api_key == '' and not PROVIDERS['colibri']['needs_key']
    assert next(iter(PROVIDERS)) == 'ollama'
    assert settings['model'] == 'unrelated-cloud-model'


def test_saved_explicit_model_url_and_optional_key(settings, monkeypatch):
    settings.update(provider='colibri', model='saved-alias', colibri_url='http://127.0.0.1:9191/v1')
    monkeypatch.setenv('COLI_API_KEY', 'fixture-token')
    cfg = ProviderConfig.from_config()
    assert cfg.model == 'saved-alias' and cfg.api_key == 'fixture-token'
    assert cfg.base_url == settings['colibri_url']
    assert ProviderConfig.from_config('colibri', 'explicit-alias').model == 'explicit-alias'
    monkeypatch.setenv('DJCODE_BASE_URL', 'http://127.0.0.1:9292/v1/')
    assert ProviderConfig.from_config('colibri').base_url == 'http://127.0.0.1:9292/v1'


def test_colibri_environment_does_not_change_other_providers(settings, monkeypatch):
    monkeypatch.setenv('DJCODE_COLIBRI_CONTEXT', 'invalid')
    remote = ProviderConfig.from_config('https://example.invalid/v1', 'explicit')
    assert remote.name == 'custom' and remote.model == 'explicit'
    assert remote.max_tokens == 9000 and remote.context_window is None


@pytest.mark.parametrize('context,output', [('0','256'), ('1048577','256'), ('8192','0'), ('8192','8192'), ('no','256')])
def test_invalid_colibri_budgets_rejected(settings, monkeypatch, context, output):
    monkeypatch.setenv('DJCODE_COLIBRI_CONTEXT', context)
    monkeypatch.setenv('DJCODE_COLIBRI_MAX_TOKENS', output)
    with pytest.raises(ValueError, match='DJCODE_COLIBRI'):
        ProviderConfig.from_config('colibri')


def test_operator_uses_explicit_colibri_context(settings, monkeypatch):
    monkeypatch.setenv('DJCODE_COLIBRI_CONTEXT', '16384')
    async def run():
        provider = Provider(ProviderConfig.from_config('colibri'))
        try:
            assert Operator(provider).context_manager.max_context == 16384
        finally:
            await provider.close()
    asyncio.run(run())


@pytest.mark.parametrize('available,expected', [(['djcode-colibri'], True), (['different-alias'], False), ([], False)])
def test_model_discovery_is_exact_and_bounded(settings, monkeypatch, available, expected):
    seen = []
    def get(url, **kwargs):
        seen.append((url, kwargs))
        return httpx.Response(200, json={'data': [{'id': name} for name in available]}, request=httpx.Request('GET', url))
    monkeypatch.setattr(httpx, 'get', get)
    async def run():
        provider = Provider(ProviderConfig.from_config('colibri'))
        try:
            success, message = provider.validate_model()
            assert success == expected
            assert not success or message == ''
            assert seen[0][0].endswith('/v1/models')
            assert seen[0][1]['timeout'] == 5.0
        finally:
            await provider.close()
    asyncio.run(run())


def test_unreachable_colibri_fails_discovery(settings, monkeypatch):
    def fail(*args, **kwargs):
        raise httpx.ConnectError('offline')
    monkeypatch.setattr(httpx, 'get', fail)
    async def run():
        provider = Provider(ProviderConfig.from_config('colibri'))
        try:
            ok, message = provider.validate_model()
            assert not ok and 'Start your existing Colibri server' in message
        finally:
            await provider.close()
    asyncio.run(run())


def test_colibri_budget_includes_tools_and_prevents_request(settings):
    async def run():
        provider = Provider(ProviderConfig.from_config('colibri'))
        await provider._client.aclose()
        requests = []
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: requests.append(r)))
        try:
            with pytest.raises(ValueError, match='approximate token budget'):
                _ = [chunk async for chunk in provider.chat([Message('user', 'long context ' * 12000)])]
            assert not requests
        finally:
            await provider.close()
    asyncio.run(run())


def test_colibri_uses_shared_tool_protocol_and_does_not_fallback_on_400(settings):
    async def run():
        provider = Provider(ProviderConfig.from_config('colibri'))
        await provider._client.aclose()
        requests = []
        def handler(request):
            payload = json.loads(request.content)
            requests.append(payload)
            assert payload['model'] == 'djcode-colibri'
            assert payload['max_tokens'] == 256
            assert payload['tools']
            return httpx.Response(400, json={'error': {'message': 'Tool use is not wired up for this engine'}})
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(ConnectionError, match='Tool use is not wired up'):
                _ = [chunk async for chunk in provider.chat([Message('user', 'test')])]
            assert len(requests) == 1
        finally:
            await provider.close()
    asyncio.run(run())


def test_tools_alone_count_against_colibri_context(settings, monkeypatch):
    monkeypatch.setenv('DJCODE_COLIBRI_CONTEXT', '1000')
    async def run():
        provider = Provider(ProviderConfig.from_config('colibri'))
        try:
            with pytest.raises(ValueError, match='messages \\+ tools \\+ output'):
                provider._check_colibri_context([Message('user', 'hi')])
        finally:
            await provider.close()
    asyncio.run(run())


def test_colibri_onboarding_never_fetches_ollama_or_installs(monkeypatch):
    from types import SimpleNamespace
    from djcode import onboarding
    answer = lambda value: SimpleNamespace(ask=lambda: value)
    fields = iter(['http://127.0.0.1:9191/v1', 'served-alias'])
    monkeypatch.setattr(onboarding.questionary, 'select', lambda *a, **kw: answer('colibri'))
    monkeypatch.setattr(onboarding.questionary, 'text', lambda *a, **kw: answer(next(fields)))
    monkeypatch.setattr(onboarding.questionary, 'password', lambda *a, **kw: answer(''))
    monkeypatch.setattr(onboarding.questionary, 'confirm', lambda *a, **kw: answer(False))
    def unexpected(*args, **kwargs):
        raise AssertionError('Colibri setup must not probe Ollama')
    monkeypatch.setattr(onboarding, '_fetch_ollama_models', unexpected)
    monkeypatch.setattr(onboarding, 'ensure_dirs', lambda: None)
    saved = []
    monkeypatch.setattr(onboarding, 'save_config', lambda cfg: saved.append(dict(cfg)))
    cfg = onboarding.run_onboarding()
    assert cfg['provider'] == 'colibri'
    assert cfg['model'] == 'served-alias'
    assert cfg['colibri_url'] == 'http://127.0.0.1:9191/v1'
    assert saved[0]['provider'] == 'colibri'


def test_colibri_fixture_runs_native_file_tool(settings, monkeypatch, tmp_path):
    monkeypatch.setenv('COLI_API_KEY', 'fixture-token')
    target = tmp_path / 'colibri.txt'
    async def run():
        provider = Provider(ProviderConfig.from_config('colibri'))
        await provider._client.aclose()
        requests = []
        def handler(request):
            assert request.url.path == '/v1/chat/completions'
            assert request.headers['Authorization'] == 'Bearer fixture-token'
            payload = json.loads(request.content)
            requests.append(payload)
            if len(requests) == 1:
                delta = {'tool_calls': [{'index': 0, 'id': 'colibri-call', 'function': {'name': 'file_write', 'arguments': json.dumps({'path': str(target), 'content': 'fixture verified'})}}]}
                reason = 'tool_calls'
            else:
                assert payload['messages'][-1]['tool_call_id'] == 'colibri-call'
                delta, reason = {'content': 'File written.'}, 'stop'
            return httpx.Response(200, text='data: ' + json.dumps({'choices': [{'delta': delta, 'finish_reason': reason}]}) + '\n\ndata: [DONE]\n\n')
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            operator = Operator(provider, auto_accept=True, raw=True)
            text = ''.join([part async for part in operator.send('Write the fixture file')])
            assert text == 'File written.' and len(requests) == 2
            assert target.read_text() == 'fixture verified'
        finally:
            await provider.close()
    asyncio.run(run())


def test_interactive_switch_to_colibri_resets_unrelated_model(settings, monkeypatch):
    from types import SimpleNamespace
    from djcode import auth
    monkeypatch.setattr(auth.questionary, 'select', lambda *a, **kw: SimpleNamespace(ask=lambda: 'colibri'))
    monkeypatch.setattr(auth.questionary, 'password', lambda *a, **kw: SimpleNamespace(ask=lambda: ''))
    monkeypatch.setattr(auth, 'set_value', lambda key, value: settings.update({key: value}))
    assert auth.interactive_auth() == 'colibri'
    assert settings['provider'] == 'colibri' and settings['model'] == 'djcode-colibri'
