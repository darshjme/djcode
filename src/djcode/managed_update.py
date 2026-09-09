"""Install CI-validated canonical builds without touching developer checkouts."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

import httpx

REPOSITORY = "darshjme/djcode"
MANIFEST_URL = f"https://github.com/{REPOSITORY}/releases/download/updates-main/update.json"


def result(status: str, message: str, *, ok=True, updated=False, **extra):
    return {"ok": ok, "status": status, "message": message, "updated": updated, **extra}


def read_receipt(release: Path, prefix: Path) -> dict:
    if release.parent != prefix or not release.name.startswith("release."):
        raise ValueError("Release is outside the managed directory")
    info = json.loads((release / ".djcode-install.json").read_text())
    if (not isinstance(info, dict) or info.get("repository") != REPOSITORY
            or not isinstance(info.get("prefix"), str) or Path(info["prefix"]).resolve() != prefix):
        raise ValueError("Invalid managed installation receipt")
    return info


def installation() -> tuple[Path, dict] | None:
    release = Path(sys.prefix).resolve().parent
    receipt = release / ".djcode-install.json"
    try:
        info = json.loads(receipt.read_text())
        prefix = Path(info["prefix"]).resolve()
        if Path(sys.prefix).name != "venv":
            return None
        return prefix, read_receipt(release, prefix)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def validate_manifest(data: dict) -> dict:
    if not isinstance(data, dict) or type(data.get("schema")) is not int or data.get("schema") != 1 or data.get("repository") != REPOSITORY or data.get("branch") != "main":
        raise ValueError("Unexpected update manifest repository/schema/branch")
    commit, version = data.get("commit", ""), data.get("version", "")
    if (not isinstance(commit, str) or not isinstance(version, str)
            or not re.fullmatch(r"[0-9a-f]{40}", commit) or not re.fullmatch(r"\d+\.\d+\.\d+", version)):
        raise ValueError("Invalid update revision/version")
    expected = f"https://github.com/{REPOSITORY}/releases/download/build-{commit[:12]}/djcode-{version}-py3-none-any.whl"
    if (data.get("wheel_url") != expected or not isinstance(data.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", data["sha256"])):
        raise ValueError("Update artifact is not an immutable canonical wheel")
    if type(data.get("run_id")) is not int or data["run_id"] <= 0:
        raise ValueError("Missing CI run identity")
    return data


def fetch_json(client: httpx.Client, url: str) -> dict:
    deadline = time.monotonic() + 8
    body = bytearray()
    with client.stream("GET", url) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            if time.monotonic() > deadline or len(body) + len(chunk) > 2 * 1024 * 1024:
                raise ValueError("Update metadata exceeded time/size limits")
            body.extend(chunk)
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("Update metadata must be an object")
    return data


def verified_manifest(client: httpx.Client) -> dict:
    data = validate_manifest(fetch_json(client, MANIFEST_URL))
    run = fetch_json(client, f"https://api.github.com/repos/{REPOSITORY}/actions/runs/{data['run_id']}")
    if (not isinstance(run.get("path"), str) or not isinstance(run.get("head_repository"), dict)
            or run.get("head_sha") != data["commit"] or run.get("head_branch") != "main"
            or run.get("event") != "push" or run.get("conclusion") != "success"
            or run.get("status") != "completed"
            or run.get("path", "").split("@")[0] != ".github/workflows/ci.yml"
            or run.get("head_repository", {}).get("full_name") != REPOSITORY):
        raise ValueError("Canonical main CI has not completed successfully for this artifact")
    return data


def atomic_link(target: Path, link: Path) -> None:
    if link.exists() and not link.is_symlink():
        raise ValueError(f"Refusing to replace non-symlink: {link}")
    temporary = link.with_name(link.name + f".tmp-{os.getpid()}")
    try:
        temporary.symlink_to(target, target_is_directory=True)
        os.replace(temporary, link)
    finally:
        temporary.unlink(missing_ok=True)


def run(command: list[str], *, timeout=180, env=None) -> str:
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env)
    if completed.returncode:
        # Package commands can emit URLs containing credentials. Do not include
        # captured output in the user-visible error or the persistent receipt.
        raise RuntimeError(f"Staged validation/install failed ({Path(command[0]).name}, exit {completed.returncode})")
    return completed.stdout.strip()


def stage_build(prefix: Path, info: dict, manifest: dict, client: httpx.Client) -> Path:
    release = Path(tempfile.mkdtemp(prefix=f"release.{manifest['commit'][:12]}.", dir=prefix))
    deadline = time.monotonic() + 240
    def bounded_run(command, *, limit=180, env=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Update staging deadline exceeded")
        return run(command, timeout=min(limit, remaining), env=env)
    try:
        wheel = release / f"djcode-{manifest['version']}-py3-none-any.whl"
        digest = hashlib.sha256()
        size = 0
        with client.stream("GET", manifest["wheel_url"]) as response, wheel.open("wb") as output:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 8 * 1024 * 1024 or time.monotonic() > deadline:
                    raise ValueError("Update wheel exceeds the download limit")
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != manifest["sha256"]:
            raise ValueError("Update wheel checksum mismatch")
        venv = release / "venv"
        uv = shutil.which("uv")
        if uv:
            bounded_run([uv, "venv", "--no-python-downloads", "--python", sys.executable, str(venv)])
            bounded_run([uv, "pip", "install", "--python", str(venv / "bin/python"), str(wheel)])
        else:
            bounded_run([sys.executable, "-m", "venv", str(venv)])
            bounded_run([str(venv / "bin/python"), "-m", "pip", "install", str(wheel)])
        env = {**os.environ, "DJCODE_NO_UPDATE_CHECK": "1", "DJCODE_SKIP_STARTUP_CHECK": "1"}
        version = bounded_run([str(venv / "bin/djcode"), "--version"], limit=30, env=env)
        if version != f"djcode, version {manifest['version']}":
            raise ValueError("Staged package version differs from its manifest")
        bounded_run([str(venv / "bin/djcode"), "--check"], limit=60, env=env)
        receipt = {**info, "commit": manifest["commit"], "version": manifest["version"], "run_id": manifest["run_id"]}
        (release / ".djcode-install.json").write_text(json.dumps(receipt, indent=2))
        return release
    except BaseException:
        shutil.rmtree(release)  # Only this newly created, never activated build.
        raise


def perform_update(force=False) -> dict:
    from djcode.config import load_config
    mode = load_config().get("update_mode", "auto")
    if os.environ.get("DJCODE_NO_UPDATE_CHECK", "").lower() in {"1", "true", "yes"} or mode == "disabled":
        return result("disabled", "Updates disabled.")
    if mode == "manual" and not force:
        return result("manual", "Manual updates enabled; run djcode --update when ready.")
    managed = installation()
    if not managed or os.name != "posix":
        return result("manual_required", "Developer/unmanaged installation preserved. Use the managed installer for automatic updates.")
    prefix, info = managed
    lock = None
    try:
        import fcntl
        lock = (prefix / ".update.lock").open("a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return result("busy", "Another DJcode update is already running.")
        with httpx.Client(timeout=httpx.Timeout(3, read=2), follow_redirects=True) as client:
            manifest = verified_manifest(client)
            current = prefix / "current"
            if not current.is_symlink():
                return result("manual_required", "This older install needs one managed-installer update before automatic switching is available.")
            old = current.resolve()
            current_info = read_receipt(old, prefix)
            if manifest["commit"] == current_info.get("commit"):
                return result("current", f"DJcode {current_info.get('version', '')} is current ({manifest['commit'][:8]}).")
            release = stage_build(prefix, current_info, manifest, client)
            atomic_link(old, prefix / "previous")
            try:
                atomic_link(release, current)
                env = {**os.environ, "DJCODE_NO_UPDATE_CHECK": "1", "DJCODE_SKIP_STARTUP_CHECK": "1"}
                run([str(current / "venv/bin/djcode"), "--version"], timeout=30, env=env)
            except BaseException:
                atomic_link(old, current)
                raise
        return result("updated", f"Installed DJcode {manifest['version']} ({manifest['commit'][:8]}). Previous build retained.",
                      updated=True, version=manifest["version"], commit=manifest["commit"], entrypoint=str(current / "venv/bin/djcode"))
    except (OSError, ValueError, RuntimeError, httpx.HTTPError, subprocess.TimeoutExpired) as error:
        return result("unavailable", f"Update unavailable ({type(error).__name__}); current installation retained.", ok=False)
    finally:
        if lock:
            lock.close()


def rollback() -> dict:
    managed = installation()
    if not managed or os.name != "posix":
        return result("manual_required", "Rollback is available only for managed installations.", ok=False)
    prefix, _ = managed
    lock = None
    try:
        import fcntl
        lock = (prefix / ".update.lock").open("a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return result("busy", "Another update or rollback is running.", ok=False)
        previous, current = prefix / "previous", prefix / "current"
        if not previous.is_symlink() or not current.is_symlink():
            return result("unavailable", "No previous managed build is available.", ok=False)
        target, old = previous.resolve(), current.resolve()
        for candidate in (target, old):
            read_receipt(candidate, prefix)
        from djcode.config import set_value
        set_value("update_mode", "manual")
        atomic_link(target, current)
        try:
            atomic_link(old, previous)
        except BaseException:
            atomic_link(old, current)
            raise
        return result("rolled_back", "Previous build restored. Updates set to manual to prevent immediate reinstallation.", updated=True)
    except (OSError, ValueError) as error:
        return result("unavailable", f"Rollback failed ({type(error).__name__}).", ok=False)
    finally:
        if lock:
            lock.close()
