"""Malformed configuration stays recoverable and does not mutate shared defaults."""
import json

import pytest

from djcode import config


@pytest.mark.parametrize("content", ["null", "[]", '"private-fixture-value"', "{broken"])
def test_bad_configuration_preserves_original_and_reports_path(tmp_path, monkeypatch, caplog, content):
    path = tmp_path / "config.json"
    path.write_text(content)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(config, "CONFIG_FILE", path)
    loaded = config.load_config()
    assert loaded == config.DEFAULT_CONFIG
    assert path.read_text() == content
    assert str(path) in caplog.text
    assert "private-fixture-value" not in caplog.text


def test_nested_defaults_are_independent_between_loads(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    first = config.load_config()
    first["custom_providers"]["fixture"] = {"url": "http://localhost"}
    assert config.load_config()["custom_providers"] == {}
    assert config.DEFAULT_CONFIG["custom_providers"] == {}


def test_valid_user_configuration_retains_unknown_extension_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    config.CONFIG_FILE.write_text(json.dumps({"provider": "remote", "extension_state": {"enabled": True}}))
    loaded = config.load_config()
    assert loaded["provider"] == "remote"
    assert loaded["extension_state"] == {"enabled": True}
