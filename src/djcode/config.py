"""Configuration management for DJcode.

Reads/writes ~/.djcode/config.json with sensible defaults.
Zero telemetry. Everything stays local.
"""

from __future__ import annotations

import json
import os
import tempfile
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
    # Must stay identical to auth.PROVIDERS["mlx"]["base_url"]. config.py cannot
    # import auth (auth imports config), so tests/test_config_defaults.py pins them.
    "mlx_url": "http://localhost:8899",
    "remote_url": "",
    "remote_api_key": "",
    "embedding_model": "nomic-embed-text",
    "temperature": 0.7,
    "max_tokens": 8192,
    "bypass_rlhf": False,
    "telemetry": False,
    "theme": "auto",  # auto | dark | light | ansi16; resolved by theme.detect()
    "workflow_engine": "daf",
    # W6 (P0-2). `permission_mode` is the single source of truth:
    # manual | accept-edits | auto | bypass. The two booleans below it are the
    # legacy spelling, kept READABLE for one release (`Operator._initial_mode`
    # falls back to them) but no longer written by `--auto-accept` or Ctrl+T --
    # a one-shot flag must not become a permanent setting.
    "permission_mode": "manual",
    "auto_approve_tools": False,
    "auto_accept": False,
    "base_url": "",
    "custom_providers": {},
    # W5 (P0-1) checkpoints. `checkpoint_budget_mb` is the key the blueprint
    # names; the other four exist because the honest answer to "can bash be
    # undone here?" depends on the size of the tree, and a user with a
    # 100k-file repo needs to be able to move the line rather than be told no.
    "checkpoint_budget_mb": 256,
    "checkpoint_max_file_mb": 8,
    "checkpoint_bash": True,
    "checkpoint_walk_budget_ms": 400,
    "checkpoint_baseline_budget_ms": 1500,
    "checkpoint_max_entries": 20000,
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


def get(key: str, default: Any = None) -> Any:
    """Get a single config value."""
    config = load_config()
    return config.get(key, default)


def set_value(key: str, value: Any) -> None:
    """Set a single config value and persist."""
    config = load_config()
    config[key] = value
    save_config(config)
