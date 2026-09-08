"""Update/onboarding checks: no model downloads, shell execution or live installs."""
import shlex
import subprocess
from types import SimpleNamespace

import pytest

from djcode import onboarding, updater
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


def wizard(monkeypatch, choices, saved):
    answers = iter(choices)
    def question(*args, **kwargs):
        return SimpleNamespace(ask=lambda: next(answers))
    for name in ['select', 'text', 'password', 'confirm']:
        monkeypatch.setattr(onboarding.questionary, name, question)
    monkeypatch.setattr(onboarding, 'ensure_dirs', lambda: None)
    monkeypatch.setattr(onboarding, 'save_config', saved.append)
    monkeypatch.setattr(onboarding.httpx, 'post', lambda *a, **kw: pytest.fail('download/inference requested'))


def test_cancelled_onboarding_does_not_create_configuration(monkeypatch):
    saved = []
    wizard(monkeypatch, [None], saved)
    with pytest.raises(KeyboardInterrupt):
        onboarding.run_onboarding()
    assert saved == []


def test_featherless_onboarding_uses_explicit_model_no_download(monkeypatch):
    saved = []
    wizard(monkeypatch, ['featherless', '', 'account/model', False], saved)
    result = onboarding.run_onboarding()
    assert result['provider'] == 'featherless'
    assert result['model'] == 'account/model'
    assert result['featherless_url'] == 'https://api.featherless.ai/v1'
    assert len(saved) == 1


def test_custom_onboarding_captures_endpoint(monkeypatch):
    saved = []
    wizard(monkeypatch, ['custom', '', 'https://inference.example/v1', 'my-model', False], saved)
    result = onboarding.run_onboarding()
    assert result['base_url'] == 'https://inference.example/v1'
    assert result['model'] == 'my-model'


def test_empty_ollama_does_not_download_models(monkeypatch):
    saved = []
    wizard(monkeypatch, ['ollama', 'already-installed-model', False], saved)
    monkeypatch.setattr(onboarding, '_fetch_ollama_models', lambda url: [])
    result = onboarding.run_onboarding()
    assert result['model'] == 'already-installed-model'
