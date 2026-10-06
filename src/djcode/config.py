"""Configuration management for DJcode.

Reads/writes ~/.djcode/config.json with sensible defaults.
Zero telemetry. Everything stays local.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(os.environ.get("DJCODE_CONFIG_DIR", str(Path.home() / ".djcode"))).expanduser()
CONFIG_FILE = CONFIG_DIR / "config.json"
MEMORY_DIR = CONFIG_DIR / "memory"
HISTORY_FILE = CONFIG_DIR / "history.txt"

DEFAULT_CONFIG: dict[str, Any] = {
    "provider": "ollama",
    "model": "gemma4",
    "ollama_url": "http://localhost:11434",
    "mlx_url": "http://localhost:8080",
    "remote_url": "",
    "remote_api_key": "",
    "embedding_model": "nomic-embed-text",
    "temperature": 0.7,
    "max_tokens": 8192,
    "bypass_rlhf": False,
    "telemetry": False,
    "theme": "dark",
    "workflow_engine": "daf",
    "auto_approve_tools": False,
    "auto_accept": False,
    "base_url": "",
    "custom_providers": {},
}


def ensure_dirs() -> None:
    """Create config and memory directories if they don't exist."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    """Load config from disk, merging with defaults."""
    ensure_dirs()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                user_config = json.load(f)
            if not isinstance(user_config, dict):
                logging.getLogger(__name__).warning(
                    "Configuration at %s must be a JSON object; using defaults without changing the file",
                    CONFIG_FILE,
                )
                return deepcopy(DEFAULT_CONFIG)
            merged = {**deepcopy(DEFAULT_CONFIG), **user_config}
            return merged
        except (json.JSONDecodeError, OSError):
            logging.getLogger(__name__).warning(
                "Cannot read configuration at %s; using defaults without changing the file",
                CONFIG_FILE,
            )
    return deepcopy(DEFAULT_CONFIG)


def save_config(config: dict[str, Any]) -> None:
    """Persist config to disk."""
    ensure_dirs()
    fd, temporary = tempfile.mkstemp(prefix=".config-", dir=CONFIG_DIR)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, CONFIG_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_config_for_write() -> dict[str, Any]:
    """Read mutation input without replacing damaged settings with defaults."""
    try:
        raw = CONFIG_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return deepcopy(DEFAULT_CONFIG)
    except (OSError, UnicodeError):
        raise ValueError("Cannot read DJcode settings. Repair the configuration before changing settings.") from None
    try:
        value = json.loads(raw)
    except ValueError:
        raise ValueError("DJcode settings contain invalid JSON. Repair the configuration before changing settings.") from None
    if not isinstance(value, dict) or not isinstance(value.get("custom_providers", {}), dict):
        raise ValueError("DJcode settings require a JSON object and valid custom providers. Repair the configuration first.")
    return {**deepcopy(DEFAULT_CONFIG), **value}


def get(key: str, default: Any = None) -> Any:
    """Get a single config value."""
    config = load_config()
    return config.get(key, default)


def set_value(key: str, value: Any) -> None:
    """Set a single config value and persist."""
    config = load_config_for_write()
    config[key] = value
    save_config(config)
