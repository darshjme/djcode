"""DJcode update checker — checks GitHub for new versions.

Checks cli.darshj.ai or GitHub releases for updates.
Shows changelog when a new version is available.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from djcode import __version__
from djcode.config import CONFIG_DIR

GITHUB_REPO = "darshjme/djcode"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
CHANGELOG_URL = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/CHANGELOG.md"
UPDATE_CHECK_FILE = CONFIG_DIR / "last_update_check.json"
CHECK_INTERVAL_HOURS = 24


def _load_last_check() -> dict[str, Any]:
    """Load last update check info."""
    try:
        if UPDATE_CHECK_FILE.exists():
            return json.loads(UPDATE_CHECK_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_last_check(data: dict[str, Any]) -> None:
    """Save update check info."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        UPDATE_CHECK_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


def _should_check() -> bool:
    """Check if enough time has passed since last check."""
    data = _load_last_check()
    last_check = data.get("last_check")
    if not last_check:
        return True
    try:
        last_dt = datetime.fromisoformat(last_check)
        return datetime.now() - last_dt > timedelta(hours=CHECK_INTERVAL_HOURS)
    except Exception:
        return True


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse version string like '0.1.0' or 'v1.2.3' into tuple."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", v.strip())
    if not match:
        raise ValueError("Expected a stable major.minor.patch release")
    return tuple(int(part) for part in match.groups())


def get_update_command() -> str:
    """Suggest an explicit update for the running installation; never execute it.

    Managed installs create a new release using the same installer and keep the
    current release for rollback. Source/venv installs update their own interpreter.
    """
    prefix = Path(sys.prefix).resolve()
    source_installer = prefix.parent / "source" / "install.sh"
    if prefix.name == "venv" and source_installer.is_file():
        binary = shutil.which("djcode")
        bin_dir = Path(binary).absolute().parent if binary else Path.home() / ".local" / "bin"
        values = [
            f"DJCODE_INSTALL_DIR={shlex.quote(str(prefix.parent.parent))}",
            f"DJCODE_BIN_DIR={shlex.quote(str(bin_dir))}",
            "bash", shlex.quote(str(source_installer)),
        ]
        return " ".join(values)
    python = sys.executable
    repository = f"git+https://github.com/{GITHUB_REPO}.git"
    if shutil.which("uv"):
        return shlex.join(["uv", "pip", "install", "--python", python, "--upgrade", repository])
    return shlex.join([python, "-m", "pip", "install", "--upgrade", repository])


def check_for_updates(force: bool = False) -> dict[str, Any] | None:
    """Check GitHub for a newer version. Returns update info or None.

    Returns:
        dict with keys: latest_version, current_version, update_available, changelog_url, download_url
        None if check was skipped or failed
    """
    if os.environ.get("DJCODE_NO_UPDATE_CHECK", "").lower() in {"1", "true", "yes"}:
        return None
    if not force and not _should_check():
        # Check cached result
        data = _load_last_check()
        if data.get("current_version") == __version__ and data.get("update_available"):
            return data
        return None

    try:
        resp = httpx.get(GITHUB_API, timeout=5.0, follow_redirects=True)
        resp.raise_for_status()
        release = resp.json()

        if release.get("draft") or release.get("prerelease"):
            return None
        latest_tag = release.get("tag_name", "")
        latest_version = latest_tag.lstrip("v")
        current_version = __version__

        update_available = _parse_version(latest_version) > _parse_version(current_version)

        result = {
            "last_check": datetime.now().isoformat(),
            "latest_version": latest_version,
            "current_version": current_version,
            "update_available": update_available,
            "release_name": release.get("name", ""),
            "release_body": release.get("body", "")[:500],  # first 500 chars of changelog
            "release_url": release.get("html_url", ""),
            "download_url": f"https://github.com/{GITHUB_REPO}",
        }

        _save_last_check(result)
        return result if update_available else None

    except Exception:
        # Save that we checked (so we don't retry immediately)
        _save_last_check({
            "last_check": datetime.now().isoformat(),
            "update_available": False,
        })
        return None


def get_update_message() -> str | None:
    """Get a formatted update message if an update is available."""
    info = check_for_updates()
    if not info or not info.get("update_available"):
        return None

    latest = info["latest_version"]
    current = info["current_version"]
    name = info.get("release_name", "")

    msg = f"[yellow]Update available:[/] v{current} → v{latest}"
    if name:
        msg += f" ({name})"
    from rich.markup import escape
    msg += f"\n[dim]Update explicitly: {escape(get_update_command())}[/]"
    msg += f"\n[dim]Changelog: {info.get('release_url', CHANGELOG_URL)}[/]"
    return msg


def format_changelog(body: str, max_lines: int = 20) -> str:
    """Format release body for terminal display."""
    lines = body.strip().split("\n")[:max_lines]
    return "\n".join(lines)
