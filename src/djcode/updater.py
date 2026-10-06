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


def perform_update(force: bool = False) -> dict:
    """Install a verified canonical build only into a managed installation."""
    from djcode.managed_update import perform_update as managed_update
    return managed_update(force=force)


def _installation_status() -> dict[str, Any]:
    """Describe the running install without invoking package managers."""
    from djcode.managed_update import installation, read_receipt
    managed = installation()
    if managed:
        prefix, active = managed
        try:
            active = read_receipt((prefix / "current").resolve(), prefix)
        except (OSError, ValueError, TypeError):
            # A running process may outlive a damaged pointer; report its receipt.
            pass
        rollback_available = False
        try:
            previous = prefix / "previous"
            if previous.is_symlink():
                read_receipt(previous.resolve(), prefix)
                rollback_available = True
        except (OSError, ValueError):
            pass
        return {"kind": "managed", "managed": True, "update_command": "djcode update",
                "rollback_available": rollback_available, "active_commit": active.get("commit"),
                "active_version": active.get("version")}
    kind = "pip"
    try:
        from importlib.metadata import distribution
        direct = json.loads(distribution("djcode").read_text("direct_url.json") or "{}")
        if direct.get("dir_info", {}).get("editable"):
            kind = "source"
    except Exception:
        # Installation metadata is optional; never inspect embedded URL credentials.
        pass
    if kind != "source" and "uv" in Path(sys.prefix).parts:
        kind = "uv"
    command = get_update_command()
    if kind == "source":
        command = "git pull --ff-only && uv sync --locked (review source changes first)"
    elif kind == "uv" and "tools" in Path(sys.prefix).parts:
        command = "uv tool upgrade djcode"
    return {"kind": kind, "managed": False, "update_command": command,
            "rollback_available": False}


def get_update_status(*, check: bool = False, force: bool = False) -> dict[str, Any]:
    """Non-installing status; an explicit check uses bounded canonical metadata.

    Offline status never opens a network client. Manual mode permits explicit
    checks; disabled mode and DJCODE_NO_UPDATE_CHECK block them, including force.
    Error caches are truthful (unavailable, never current), with no raw exceptions.
    """
    from djcode.config import load_config
    from djcode.managed_update import verified_manifest, fetch_json
    mode = load_config().get("update_mode", "auto")
    if mode not in {"auto", "manual", "disabled"}:
        mode = "manual"
    install = _installation_status()
    enabled = mode != "disabled" and os.environ.get("DJCODE_NO_UPDATE_CHECK", "").lower() not in {"1", "true", "yes"}
    base = {"ok": True, "status": "unchecked", "message": "No update check performed.",
            "current_version": __version__, "latest_version": None, "update_available": None,
            "installation": install, "update_mode": mode, "check_enabled": enabled,
            "automatic_install": enabled and mode == "auto" and install["managed"],
            "cached": False, "checked_at": None}
    if not enabled:
        return {**base, "status": "disabled", "message": "Update checks and installation are disabled."}
    cache_path = UPDATE_CHECK_FILE.with_name("update_status.json")
    cache = {}
    try:
        if cache_path.stat().st_size <= 64 * 1024:
            cache = json.loads(cache_path.read_text())
            age = (datetime.now() - datetime.fromisoformat(cache["checked_at"])).total_seconds()
            if (not isinstance(cache, dict) or not 0 <= age < CHECK_INTERVAL_HOURS * 3600
                    or cache.get("current_version") != __version__
                    or cache.get("installation_kind") != install["kind"]
                    or cache.get("active_commit") != install.get("active_commit")
                    or type(cache.get("ok")) is not bool
                    or not isinstance(cache.get("message"), str)
                    or (cache.get("update_available") is not None and type(cache.get("update_available")) is not bool)
                    or cache.get("status") not in {"current", "available", "unavailable"}
                    or not all(k in cache for k in ("ok", "message", "latest_version", "update_available"))):
                cache = {}
    except (OSError, ValueError, KeyError, TypeError):
        cache = {}
    if cache and (not check or not force):
        return {**base, **{k: cache[k] for k in ("ok", "status", "message", "latest_version", "update_available", "checked_at")}, "cached": True}
    if not check:
        return base
    checked = datetime.now().isoformat()
    try:
        with httpx.Client(timeout=httpx.Timeout(3, read=2), follow_redirects=True) as client:
            if install["managed"]:
                release = verified_manifest(client)
                latest = release["version"]
                from djcode.managed_update import installation, read_receipt
                prefix, _ = installation()
                active = read_receipt((prefix / "current").resolve(), prefix)
                available = release["commit"] != active.get("commit")
            else:
                release = fetch_json(client, GITHUB_API)
                if release.get("draft") or release.get("prerelease"):
                    raise ValueError("No stable release")
                latest = release.get("tag_name", "").removeprefix("v")
                available = _parse_version(latest) > _parse_version(__version__)
        answer = {**base, "status": "available" if available else "current", "latest_version": latest,
                  "update_available": available, "checked_at": checked,
                  "message": "Update available; no installation performed." if available else "Installation is current."}
    except (OSError, ValueError, TypeError, KeyError, httpx.HTTPError):
        answer = {**base, "ok": False, "status": "unavailable", "checked_at": checked,
                  "message": "Update check unavailable. Check network access and canonical release metadata, then retry with --check."}
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        import tempfile
        fd, temporary = tempfile.mkstemp(prefix=".update-status-", dir=cache_path.parent)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump({k: answer[k] for k in ("ok", "status", "message", "current_version", "latest_version", "update_available", "checked_at")} | {"installation_kind": install["kind"], "active_commit": install.get("active_commit")}, output)
            os.replace(temporary, cache_path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    except OSError:
        pass
    return answer
