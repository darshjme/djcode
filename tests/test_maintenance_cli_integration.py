"""Cross-surface maintenance failures must remain machine-readable and nonmutating."""
import json

from click.testing import CliRunner

from djcode.cli import main


def test_unknown_model_catalog_fails_without_probe_or_save(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Unknown provider must not probe or save')
    monkeypatch.setattr('djcode.model_selection.probe', forbidden)
    monkeypatch.setattr('djcode.config.save_config', forbidden)
    monkeypatch.setattr('djcode.model_selection.load_config', lambda: {'provider': 'ollama', 'custom_providers': {}})
    result = CliRunner().invoke(main, ['models', 'unknown-fixture', '--json'])
    assert result.exit_code != 0, result.output
    report = json.loads(result.output)
    assert report['error'] == 'provider_unknown'


def test_missing_lint_path_has_machine_report(tmp_path):
    result = CliRunner().invoke(main, ['lint', str(tmp_path / 'absent.py'), '--json'])
    assert result.exit_code != 0
    report = json.loads(result.output)
    assert report['ok'] is False
    assert report['status'] == 'unavailable'


def test_update_disabled_status_reads_only(monkeypatch, tmp_path):
    from djcode import config, updater
    monkeypatch.setattr(config, 'CONFIG_DIR', tmp_path)
    monkeypatch.setattr(config, 'CONFIG_FILE', tmp_path / 'config.json')
    config.save_config({**config.DEFAULT_CONFIG, 'update_mode': 'disabled'})
    before = config.CONFIG_FILE.read_bytes()
    monkeypatch.setattr(updater, 'UPDATE_CHECK_FILE', tmp_path / 'check.json')
    def forbidden(*args, **kwargs):
        raise AssertionError('Read-only status must not install or contact network')
    monkeypatch.setattr(updater.httpx, 'Client', forbidden)
    monkeypatch.setattr('djcode.managed_update.perform_update', forbidden)
    result = CliRunner().invoke(main, ['update', '--status', '--json'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['status'] == 'disabled'
    assert config.CONFIG_FILE.read_bytes() == before
    assert list(tmp_path.iterdir()) == [config.CONFIG_FILE]


def test_update_mode_rejects_malformed_existing_config(monkeypatch, tmp_path):
    from djcode import config
    monkeypatch.setattr(config, 'CONFIG_DIR', tmp_path)
    monkeypatch.setattr(config, 'CONFIG_FILE', tmp_path / 'config.json')
    config.CONFIG_FILE.write_text('{malformed-fixture')
    before = config.CONFIG_FILE.read_bytes()
    result = CliRunner().invoke(main, ['update', '--mode', 'disabled', '--json'])
    assert result.exit_code != 0, result.output
    assert json.loads(result.output)['ok'] is False
    assert config.CONFIG_FILE.read_bytes() == before


def test_model_selection_rejects_malformed_config_before_probe(monkeypatch, tmp_path):
    from djcode import config
    monkeypatch.setattr(config, 'CONFIG_DIR', tmp_path)
    monkeypatch.setattr(config, 'CONFIG_FILE', tmp_path / 'config.json')
    config.CONFIG_FILE.write_text('{malformed-fixture')
    before = config.CONFIG_FILE.read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError('Invalid settings must be rejected before model probing')
    monkeypatch.setattr('djcode.model_selection.select_model', forbidden)
    result = CliRunner().invoke(main, ['models', '--select', 'ollama/fixture', '--json'])
    assert result.exit_code != 0, result.output
    assert json.loads(result.output)['ok'] is False
    assert config.CONFIG_FILE.read_bytes() == before
