"""Update/onboarding checks: no model downloads, shell execution or live installs."""
import shlex
import subprocess
from types import SimpleNamespace

import click
import httpx
import pytest

from djcode import startup, updater
from djcode.installer import SoftwareInstaller


def test_managed_update_preserves_release_layout(tmp_path, monkeypatch):
    release = tmp_path / 'install root' / 'release.old'
    venv = release / 'venv'
    venv.mkdir(parents=True)
    script = release / 'source' / 'install.sh'
    script.parent.mkdir()
    script.write_text('# already installed trusted installer')
    binary = tmp_path / 'my bin' / 'djcode'
    monkeypatch.setattr(updater.sys, 'prefix', str(venv))
    monkeypatch.setattr(updater.shutil, 'which', lambda _: str(binary))
    parts = shlex.split(updater.get_update_command())
    assert parts == [f'DJCODE_INSTALL_DIR={release.parent}', f'DJCODE_BIN_DIR={binary.parent}', 'bash', str(script)]
    assert 'pip' not in parts


def test_source_update_targets_current_python_and_actual_repository(monkeypatch, tmp_path):
    monkeypatch.setattr(updater.sys, 'prefix', str(tmp_path))
    monkeypatch.setattr(updater.sys, 'executable', '/a path/venv/bin/python')
    monkeypatch.setattr(updater.shutil, 'which', lambda _: None)
    command = shlex.split(updater.get_update_command())
    assert command[:3] == ['/a path/venv/bin/python', '-m', 'pip']
    assert command[-1] == 'git+https://github.com/darshjme/djcode.git'


def test_release_lookup_and_message_use_real_repo(monkeypatch, tmp_path):
    monkeypatch.delenv("DJCODE_NO_UPDATE_CHECK", raising=False)
    urls = []
    def get(url, **kwargs):
        urls.append(url)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
            'tag_name': 'v99.0.0', 'name': 'Release', 'html_url': 'https://github.com/darshjme/djcode/releases/tag/v99.0.0',
        })
    monkeypatch.setattr(updater.httpx, 'get', get)
    monkeypatch.setattr(updater, 'CONFIG_DIR', tmp_path)
    monkeypatch.setattr(updater, 'UPDATE_CHECK_FILE', tmp_path / 'check.json')
    monkeypatch.setattr(updater, 'get_update_command', lambda: 'bash /release/source/install.sh')
    message = updater.get_update_message()
    assert urls == ['https://api.github.com/repos/darshjme/djcode/releases/latest']
    assert 'bash /release/source/install.sh' in message
    assert 'djcode-cli' not in message


def test_update_checks_can_be_disabled_without_network(monkeypatch):
    monkeypatch.setenv('DJCODE_NO_UPDATE_CHECK', '1')
    monkeypatch.setattr(updater.httpx, 'get', lambda *a, **kw: pytest.fail('network contacted'))
    assert updater.check_for_updates(force=True) is None


@pytest.mark.parametrize('tag', ['v1.0.0-rc1', 'garbage', '', '1.2'])
def test_nonstable_versions_not_advertised(tag):
    with pytest.raises(ValueError):
        updater._parse_version(tag)


def test_installer_rejects_package_option_injection(monkeypatch):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('process executed'))
    inst = SoftwareInstaller()
    assert not inst.install('--target /tmp/unwanted foo', confirm=False)
    assert not inst.install('foo; touch /tmp/unwanted', confirm=False)


def test_unknown_manager_does_not_execute_suggestion(monkeypatch):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('process executed'))
    inst = SoftwareInstaller()
    inst._detected_manager = 'unknown'
    assert not inst.install('example', confirm=False)


def test_debian_fd_alias(monkeypatch):
    monkeypatch.setattr('djcode.installer.shutil.which', lambda name: '/usr/bin/fdfind' if name == 'fdfind' else None)
    assert SoftwareInstaller().is_installed('fd')


def wizard(monkeypatch, answers, saved, discovery):
    """Drive startup.setup with scripted answers and a mocked discovery response.

    Any download, install or shell execution fails the test outright.
    """
    replies = iter(answers)
    endpoints = []

    def question(*args, **kwargs):
        return SimpleNamespace(ask=lambda: next(replies))

    for name in ['select', 'text', 'password', 'confirm', 'autocomplete']:
        monkeypatch.setattr(startup.questionary, name, question)
    monkeypatch.setattr(startup, 'load_config', dict)
    monkeypatch.setattr(startup, 'save_config', saved.append)
    monkeypatch.setattr(startup.httpx, 'post', lambda *a, **kw: pytest.fail('download/inference requested'))
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('process executed'))
    for variable in ('DJCODE_BASE_URL', 'DJCODE_API_KEY', 'OPENAI_API_KEY', 'FEATHERLESS_API_KEY', 'COLI_API_KEY'):
        monkeypatch.delenv(variable, raising=False)

    def discover(endpoint, headers):
        endpoints.append(endpoint)
        status, payload = discovery
        return httpx.Response(status, json=payload, request=httpx.Request('GET', endpoint))

    monkeypatch.setattr(startup, 'discover', discover)
    return endpoints


def test_cancelled_setup_does_not_create_configuration(monkeypatch):
    saved = []
    wizard(monkeypatch, [None], saved, (200, {}))
    with pytest.raises(KeyboardInterrupt):
        startup.setup({})
    assert saved == []


def test_featherless_setup_uses_explicit_model_no_download(monkeypatch):
    saved = []
    endpoints = wizard(monkeypatch, ['featherless', 'api_key', 'fl-key', 'account/model'], saved,
                       (200, {'data': [{'id': 'account/model'}]}))
    result = startup.setup({})
    assert result['provider'] == 'featherless'
    assert result['model'] == 'account/model'
    assert result['featherless_url'] == 'https://api.featherless.ai/v1'
    assert endpoints == ['https://api.featherless.ai/v1/models'] * 2
    assert len(saved) == 1


def test_custom_setup_captures_endpoint(monkeypatch):
    saved = []
    endpoints = wizard(monkeypatch, ['custom', 'https://inference.example/v1/', 'api_key', 'key', 'my-model'],
                       saved, (200, {'data': [{'id': 'my-model'}]}))
    result = startup.setup({})
    assert result['custom_url'] == 'https://inference.example/v1'
    assert result['model'] == 'my-model'
    assert endpoints == ['https://inference.example/v1/models'] * 2


def test_ollama_setup_selects_an_already_installed_model(monkeypatch):
    saved = []
    endpoints = wizard(monkeypatch, ['ollama', 'http://localhost:11434', 'already-installed-model'], saved,
                       (200, {'models': [{'name': 'already-installed-model'}]}))
    result = startup.setup({})
    assert result['model'] == 'already-installed-model'
    assert endpoints == ['http://localhost:11434/api/tags'] * 2
    assert len(saved) == 1


def test_empty_ollama_refuses_instead_of_downloading(monkeypatch):
    saved = []
    wizard(monkeypatch, ['ollama', 'http://localhost:11434', 'not-installed'], saved, (200, {'models': []}))
    with pytest.raises(click.ClickException, match='available at this provider'):
        startup.setup({})
    assert saved == []
