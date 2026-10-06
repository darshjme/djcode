"""Non-installing update checks, structured lint and rollback failure proofs."""
import json
from types import SimpleNamespace
import httpx
import pytest
from djcode import config, updater, managed_update, maintenance


@pytest.fixture
def status(monkeypatch, tmp_path):
    monkeypatch.delenv('DJCODE_NO_UPDATE_CHECK', raising=False)
    monkeypatch.setattr(config, 'load_config', lambda: {'update_mode': 'manual'})
    monkeypatch.setattr(updater, 'UPDATE_CHECK_FILE', tmp_path / 'last_update_check.json')
    monkeypatch.setattr(updater, '_installation_status', lambda: {'kind': 'pip', 'managed': False, 'update_command': 'manual', 'rollback_available': False})
    return tmp_path


def test_offline_status_never_opens_client(status, monkeypatch):
    monkeypatch.setattr(updater.httpx, 'Client', lambda **kw: pytest.fail('network'))
    result = updater.get_update_status()
    assert result['status'] == 'unchecked' and result['update_mode'] == 'manual'
    assert not result['automatic_install'] and not list(status.iterdir())


@pytest.mark.parametrize('mode,env', [('disabled', ''), ('auto', '1')])
def test_disabled_overrides_force(status, monkeypatch, mode, env):
    monkeypatch.setattr(config, 'load_config', lambda: {'update_mode': mode})
    monkeypatch.setenv('DJCODE_NO_UPDATE_CHECK', env)
    monkeypatch.setattr(updater.httpx, 'Client', lambda **kw: pytest.fail('network'))
    assert updater.get_update_status(check=True, force=True)['status'] == 'disabled'


def test_error_cache_never_claims_current_and_sanitizes(status, monkeypatch):
    calls = []
    def offline(**kw):
        calls.append(True)
        raise httpx.ConnectError('fixture-secret-token')
    monkeypatch.setattr(updater.httpx, 'Client', offline)
    result = updater.get_update_status(check=True)
    assert not result['ok'] and result['status'] == 'unavailable'
    assert result['update_available'] is None and 'fixture-secret-token' not in json.dumps(result)
    assert updater.get_update_status(check=True)['cached']
    assert len(calls) == 1
    updater.get_update_status(check=True, force=True)
    assert len(calls) == 2


def test_explicit_check_reads_stable_metadata_only(status, monkeypatch):
    factory = httpx.Client
    requests = []
    def fixture(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={'tag_name': 'v99.0.0'})
    monkeypatch.setattr(updater.httpx, 'Client', lambda **kw: factory(transport=httpx.MockTransport(fixture), **kw))
    result = updater.get_update_status(check=True)
    assert result['ok'] and result['status'] == 'available'
    assert result['latest_version'] == '99.0.0'
    assert requests == [updater.GITHUB_API]
    assert updater.get_update_status()['cached']


def test_malformed_cache_is_ignored(status, monkeypatch):
    (status / 'update_status.json').write_text('[]')
    assert updater.get_update_status()['status'] == 'unchecked'


def test_actual_lint_actionable_json_and_no_mutation(tmp_path):
    target = tmp_path / 'broken.py'
    target.write_text('def broken(:\n')
    result = maintenance.run_lint(target)
    assert not result['ok'] and result['exit_code'] == 1
    assert result['diagnostics'][0]['filename'] == str(target)
    assert result['diagnostics'][0]['location']['row'] == 1
    assert target.read_text() == 'def broken(:\n'
    target.write_text('value = 1\n')
    assert maintenance.run_lint(target)['exit_code'] == 0


def test_missing_lint_path_and_missing_ruff(tmp_path, monkeypatch):
    assert maintenance.run_lint(tmp_path / 'missing')['exit_code'] == 2
    monkeypatch.setattr(maintenance.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=2, stdout='', stderr='secret-url'))
    result = maintenance.run_lint(tmp_path)
    assert result['exit_code'] == 2 and 'secret-url' not in json.dumps(result)


def test_active_install_switch_invalidates_cached_check(status, monkeypatch):
    install = {'kind': 'managed', 'managed': True, 'active_commit': 'first'}
    monkeypatch.setattr(updater, '_installation_status', lambda: install)
    monkeypatch.setattr(updater.httpx, 'Client', lambda **kw: (_ for _ in ()).throw(httpx.ConnectError('offline')))
    assert updater.get_update_status(check=True)['status'] == 'unavailable'
    assert updater.get_update_status()['cached']
    install['active_commit'] = 'second'
    assert updater.get_update_status()['status'] == 'unchecked'
