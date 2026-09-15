"""W1-9 regression pins for `djcode.config.DEFAULT_CONFIG`.

Closes B19 (three-way MLX URL conflict) and locks the `theme` default that
`DESIGN-CLI.md` 9.2 decision 8 requires, plus the W11 boundary: the layered
config work (P1-13) must ship *with* its machine-local denylist, so the three
secret-bearing keys have to still be plain members of DEFAULT_CONFIG today.
"""

from __future__ import annotations

import importlib
import json

import pytest

from djcode import config as config_module
from djcode.auth import PROVIDERS
from djcode.config import DEFAULT_CONFIG


def test_mlx_url_default_matches_auth_provider_registry() -> None:
    """B19: config.py and auth.py must agree on the MLX endpoint.

    config.py cannot import auth.py (auth.py imports config.py at module
    scope, so a back-import would be circular). This assertion is therefore
    the only mechanism that keeps the two literals from drifting apart again.
    """
    assert DEFAULT_CONFIG["mlx_url"] == PROVIDERS["mlx"]["base_url"]


def test_mlx_url_default_is_8899_not_8080() -> None:
    """The exact value B19 names, asserted literally so a joint edit still fails."""
    assert DEFAULT_CONFIG["mlx_url"] == "http://localhost:8899"


def test_theme_default_is_auto() -> None:
    """DESIGN-CLI 9.2 decision 8: `auto` is the new default, `dark` the fallback."""
    assert DEFAULT_CONFIG["theme"] == "auto"


@pytest.mark.parametrize("key", ["remote_url", "remote_api_key", "custom_providers"])
def test_w11_layering_keys_are_still_unlayered(key: str) -> None:
    """W1-9 explicitly leaves these alone; P1-13 moves them in W11 with the denylist."""
    assert key in DEFAULT_CONFIG


def test_defaults_are_not_shared_mutable_state() -> None:
    """load_config() must hand back a copy; DEFAULT_CONFIG is module-global."""
    first = config_module.load_config()
    first["model"] = "mutated-by-caller"
    assert DEFAULT_CONFIG["model"] != "mutated-by-caller"
    assert config_module.load_config()["model"] == DEFAULT_CONFIG["model"]


def test_saved_config_wins_over_new_defaults(tmp_path, monkeypatch) -> None:
    """Merge order is defaults-then-user, so an on-disk value survives a default change.

    This is the documented consequence of changing a default: an install that
    already persisted `mlx_url` keeps whatever it persisted. See the W1-9 note
    in the report -- `set_value()` writes the *merged* dict, so every install
    that ever ran setup has the old literals frozen on disk.
    """
    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_module, "CONFIG_FILE", tmp_path / "config.json")
    (tmp_path / "config.json").write_text(
        json.dumps({"mlx_url": "http://localhost:8080", "theme": "dark"}), encoding="utf-8"
    )
    loaded = config_module.load_config()
    assert loaded["mlx_url"] == "http://localhost:8080"
    assert loaded["theme"] == "dark"
    # Keys the user never wrote still come from the (new) defaults.
    assert loaded["provider"] == DEFAULT_CONFIG["provider"]


def test_fresh_install_gets_the_corrected_defaults(tmp_path, monkeypatch) -> None:
    """No config.json on disk -> the corrected values are what a new user gets."""
    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path / "fresh")
    monkeypatch.setattr(config_module, "MEMORY_DIR", tmp_path / "fresh" / "memory")
    monkeypatch.setattr(config_module, "CONFIG_FILE", tmp_path / "fresh" / "config.json")
    loaded = config_module.load_config()
    assert loaded["mlx_url"] == "http://localhost:8899"
    assert loaded["theme"] == "auto"


def test_roundtrip_through_disk_preserves_the_defaults(tmp_path, monkeypatch) -> None:
    """set_value() persists the merged dict; the corrected defaults must survive it."""
    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path / "rt")
    monkeypatch.setattr(config_module, "MEMORY_DIR", tmp_path / "rt" / "memory")
    monkeypatch.setattr(config_module, "CONFIG_FILE", tmp_path / "rt" / "config.json")
    config_module.set_value("provider", "mlx")
    written = json.loads((tmp_path / "rt" / "config.json").read_text(encoding="utf-8"))
    assert written["provider"] == "mlx"
    assert written["mlx_url"] == "http://localhost:8899"
    assert written["theme"] == "auto"
    assert not list((tmp_path / "rt").glob(".config-*")), "temp file left behind by save_config"


def test_module_reimport_is_stable() -> None:
    """Guards against a future import-time mutation of DEFAULT_CONFIG."""
    reloaded = importlib.reload(config_module)
    assert reloaded.DEFAULT_CONFIG["mlx_url"] == "http://localhost:8899"
    assert reloaded.DEFAULT_CONFIG["theme"] == "auto"
