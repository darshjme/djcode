"""Configuration management for DJcode.

Reads/writes ~/.djcode/config.json with sensible defaults.
Zero telemetry. Everything stays local.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONFIG_DIR = Path.home() / ".djcode"
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
    "auto_approve_tools": False,
    "auto_accept": False,
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
            merged = {**DEFAULT_CONFIG, **user_config}
            return merged
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_CONFIG)


def save_config(config: dict[str, Any]) -> None:
    """Persist config to disk."""
    ensure_dirs()
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get(key: str, default: Any = None) -> Any:
    """Get a single config value."""
    config = load_config()
    return config.get(key, default)


def set_value(key: str, value: Any) -> None:
    """Set a single config value and persist."""
    config = load_config()
    config[key] = value
    save_config(config)
