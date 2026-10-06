"""Model catalogs are factual; selection is provider-scoped and nonmutating."""
import copy
import json

import httpx
import pytest

from djcode import model_selection as selection, startup
from djcode.provider import ProviderConfig


def ready(config):
    return {'status': 'ready', 'message': 'Verified fixture', 'models': [config['model']], 'source': 'live'}


def test_selection_pairs_provider_model_without_cross_provider_endpoint_or_secret(monkeypatch):
    original = {'provider': 'ollama', 'model': 'old', 'base_url': 'http://old.invalid',
                'openrouter_api_key': 'secret-fixture', 'unrelated': {'keep': True}}
    before = copy.deepcopy(original)
    seen = []
    monkeypatch.setattr(selection, 'probe', lambda cfg: seen.append(copy.deepcopy(cfg)) or ready(cfg))
    picked = selection.select_model(original, 'openrouter/anthropic/Case-Sensitive-ID')
    assert picked['provider'] == 'openrouter'
    assert picked['model'] == 'anthropic/Case-Sensitive-ID'
    assert picked['base_url'] == ''
    assert picked['recent_model_selections'][0] == {'provider': 'openrouter', 'model': 'anthropic/Case-Sensitive-ID'}
    assert original == before
    assert seen[0]['unrelated'] == {'keep': True}


@pytest.mark.parametrize('status', ['offline', 'unverified', 'missing'])
def test_failed_selection_never_changes_config_or_recent_choices(monkeypatch, status):
    original = {'provider': 'openai', 'model': 'old', 'recent_model_selections': [{'provider': 'openai', 'model': 'old'}]}
    before = copy.deepcopy(original)
    monkeypatch.setattr(selection, 'probe', lambda _: {'status': status, 'message': 'Provider unavailable', 'models': []})
    with pytest.raises(selection.ModelSelectionError, match='not been saved'):
        selection.select_model(original, 'new')
    assert original == before


def test_named_custom_provider_keeps_exact_model_and_shared_resolver(monkeypatch):
    monkeypatch.delenv('DJCODE_BASE_URL', raising=False)
    monkeypatch.setenv('DJCODE_API_KEY', 'env-fixture')
    cfg = {'provider': 'ollama', 'model': 'old', 'base_url': 'http://old.invalid', 'custom_providers': {
        'team-api': {'base_url': 'https://custom.invalid/v1', 'model': 'Before', 'api_key': ''}}, 'team-api_api_key': 'stored-fixture'}
    monkeypatch.setattr(selection, 'probe', ready)
    result = selection.select_model(cfg, 'Case/Model', 'team-api')
    runtime = ProviderConfig.from_mapping(result)
    assert runtime.name == 'custom' and runtime.provider_id == 'team-api'
    assert runtime.model == 'Case/Model' and runtime.api_key == 'stored-fixture'
    assert runtime.base_url == 'https://custom.invalid/v1'
    assert result['custom_providers']['team-api']['model'] == 'Case/Model'
    assert cfg['custom_providers']['team-api']['model'] == 'Before'


def test_catalog_failure_and_auth_state_never_leak_provider_response(monkeypatch):
    cfg = {'provider': 'openai', 'model': 'old', 'openai_api_key': 'secret-fixture', 'base_url': 'https://private.invalid/v1'}
    monkeypatch.setattr(startup, 'discover', lambda *args: httpx.Response(403, json={'error': 'secret-fixture https://private.invalid'}, request=httpx.Request('GET', 'https://private.invalid')))
    result = selection.model_catalog(cfg)
    assert result['error'] == 'auth_rejected' and result['models'] == []
    public = json.dumps(result)
    assert 'secret-fixture' not in public and 'private.invalid' not in public
    monkeypatch.setattr(startup, 'discover', lambda *args: (_ for _ in ()).throw(httpx.ConnectError('secret-fixture private.invalid')))
    result = selection.model_catalog(cfg)
    assert result['status'] == 'offline' and result['source'] == 'unavailable'
    assert 'secret-fixture' not in json.dumps(result)


def test_metadata_only_reports_explicit_capabilities():
    assert startup.model_details('openai', [{'id': 'fixture'}]) == [{'id': 'fixture', 'name': 'fixture', 'capabilities': {}}]
    anthropic = startup.model_details('anthropic', [{'id': 'fixture', 'display_name': 'Fixture', 'max_input_tokens': 1234, 'capabilities': {'tool_use': {'supported': True}, 'image_input': {'supported': False}}}])[0]
    assert anthropic['capabilities'] == {'context_tokens': 1234, 'tools': True, 'vision': False}
    google = startup.model_details('google', [{'name': 'models/CaseID', 'inputTokenLimit': 256, 'thinking': True}])[0]
    assert google['id'] == 'CaseID' and google['capabilities'] == {'context_tokens': 256, 'reasoning': True}
    assert 'not reported' in selection.capability_summary({'capabilities': {}})


@pytest.mark.parametrize('invalid', ['bad/provider', 'token\nprovider', 'https://user:secret@host.invalid'])
def test_unknown_provider_is_not_discovered_or_exposed(monkeypatch, invalid):
    monkeypatch.setattr(selection, 'probe', lambda _: pytest.fail('Must not discover invalid IDs'))
    result = selection.model_catalog({'provider': 'ollama'}, invalid)
    assert result['provider'] == 'unknown' and result['error'] == 'provider_unknown'
    assert invalid not in json.dumps(result)


@pytest.mark.parametrize('style', ['anthropic', 'google'])
def test_paginated_discovery_stays_on_original_endpoint(monkeypatch, style):
    seen = []
    def handle(request):
        seen.append(request)
        if len(seen) == 1:
            data = {'models': [{'name': 'models/First'}], 'nextPageToken': 'cursor'} if style == 'google' else {'data': [{'id': 'First'}], 'has_more': True, 'last_id': 'cursor'}
        else:
            data = {'models': [{'name': 'models/Second'}]} if style == 'google' else {'data': [{'id': 'Second'}], 'has_more': False}
        # Arbitrary next links are ignored; credentials remain on the original origin.
        data['next'] = 'https://malicious.invalid/steal'
        return httpx.Response(200, json=data)
    client = httpx.AsyncClient
    monkeypatch.setattr(startup.httpx, 'AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs))
    result = startup.discover('https://provider.invalid/v1/models', {'Authorization': 'Bearer fixture'})
    assert len(seen) == 2
    assert all(request.url.host == 'provider.invalid' for request in seen)
    assert seen[1].url.params['pageToken' if style == 'google' else 'after_id'] == 'cursor'
    assert len(result.json()['models' if style == 'google' else 'data']) == 2


def test_repeated_pagination_cursor_fails_bounded(monkeypatch):
    client = httpx.AsyncClient
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={'data': [{'id': 'fixture'}], 'has_more': True, 'last_id': 'same'})
    monkeypatch.setattr(startup.httpx, 'AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs))
    with pytest.raises(ValueError, match='pagination'):
        startup.discover('https://provider.invalid/v1/models', {})
    assert len(calls) == 2
