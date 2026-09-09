"""Standalone stdlib bootstrap for verified DJcode releases; embedded in install.sh."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPOSITORY = "darshjme/djcode"
MANIFEST_URL = f"https://github.com/{REPOSITORY}/releases/download/updates-main/update.json"


def download(url, maximum, timeout=30):
    deadline = time.monotonic() + timeout
    request = urllib.request.Request(url, headers={"User-Agent": "DJcode-installer/1"})
    data = bytearray()
    with urllib.request.urlopen(request, timeout=min(timeout, 2)) as response:
        while chunk := response.read1(65536):
            if time.monotonic() > deadline or len(data) + len(chunk) > maximum:
                raise ValueError("Download exceeded its size/time limit")
            data.extend(chunk)
    return bytes(data)


def metadata(url):
    value = json.loads(download(url, 2 * 1024 * 1024, 8))
    if not isinstance(value, dict):
        raise ValueError("Expected an object in release metadata")
    return value


def validate_manifest(data):
    if not isinstance(data, dict):
        raise ValueError("Invalid release manifest")
    if (type(data.get("schema")) is not int or data["schema"] != 1
            or data.get("repository") != REPOSITORY
            or data.get("branch") != "main"):
        raise ValueError("Unexpected release repository/schema/branch")
    commit, version, digest = (data.get(key) for key in ("commit", "version", "sha256"))
    for value, pattern in ((commit, r"[0-9a-f]{40}"), (version, r"\d+\.\d+\.\d+"),
                           (digest, r"[0-9a-f]{64}")):
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise ValueError("Invalid release commit/version/checksum")
    expected = (f"https://github.com/{REPOSITORY}/releases/download/"
                f"build-{commit[:12]}/djcode-{version}-py3-none-any.whl")
    if data.get("wheel_url") != expected or type(data.get("run_id")) is not int:
        raise ValueError("Invalid immutable wheel or CI run identity")
    if data["run_id"] <= 0:
        raise ValueError("Invalid CI run identity")
    return data


def verify_run(manifest, run):
    if (not isinstance(run, dict) or run.get("head_sha") != manifest["commit"]
            or run.get("head_branch") != "main" or run.get("event") != "push"
            or run.get("conclusion") != "success" or run.get("status") != "completed"
            or not isinstance(run.get("path"), str)
            or run["path"].split("@")[0] != ".github/workflows/ci.yml"
            or not isinstance(run.get("head_repository"), dict)
            or run["head_repository"].get("full_name") != REPOSITORY):
        raise ValueError("Release does not have a successful canonical main CI run")


def run(command, *, env=None, timeout=180):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env)
    if result.returncode:
        # Do not echo package-manager URLs, custom remotes, or credential-bearing output.
        raise RuntimeError(f"Installation check failed: {Path(command[0]).name}")
    return result.stdout.strip()


def atomic_link(target, link):
    if link.exists() and not link.is_symlink():
        raise ValueError(f"Refusing to replace unrelated file: {link}")
    temporary = link.with_name(f".{link.name}.tmp-{os.getpid()}")
    try:
        temporary.symlink_to(target)
        os.replace(temporary, link)
    finally:
        temporary.unlink(missing_ok=True)


def owned_release(path, prefix):
    return path.parent == prefix and path.name.startswith("release.") and path.is_dir()


def preflight(prefix, bin_dir):
    """Validate every destination before staging or changing any live entrypoint."""
    old = None
    for name in ("current", "previous"):
        link = prefix / name
        if link.is_symlink():
            if not owned_release(link.resolve(), prefix):
                raise ValueError(f"Refusing unrelated {name} symlink")
            if name == "current":
                old = link.resolve()
        elif link.exists():
            raise ValueError(f"Refusing unrelated {name} file")
    for name in ("djcode", "djcode-colibri"):
        link = bin_dir / name
        if link.is_symlink():
            target = link.resolve()
            release = target.parent.parent.parent
            if target.parts[-3:] != ("venv", "bin", name) or not owned_release(release, prefix):
                raise ValueError(f"Refusing unrelated {name} symlink")
            if name == "djcode" and old is None:
                old = release  # Preserve a legacy release without adopting its trust.
        elif link.exists():
            raise ValueError(f"Refusing unrelated {name} executable")
    return old


def install(prefix, bin_dir, *, source=None, ref="main"):
    prefix, bin_dir = prefix.expanduser().resolve(), bin_dir.expanduser().resolve()
    prefix.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)
    with (prefix / ".update.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        old = preflight(prefix, bin_dir)
        manifest = None
        if source is None:
            manifest = validate_manifest(metadata(MANIFEST_URL))
            verify_run(manifest, metadata(
                f"https://api.github.com/repos/{REPOSITORY}/actions/runs/{manifest['run_id']}"))
        release = Path(tempfile.mkdtemp(prefix="release.", dir=prefix))
        activated = False
        original_links = {link: os.readlink(link) if link.is_symlink() else None
                          for link in [prefix / "current", prefix / "previous",
                                       bin_dir / "djcode", bin_dir / "djcode-colibri"]}
        try:
            if manifest:
                package = release / f"djcode-{manifest['version']}-py3-none-any.whl"
                content = download(manifest["wheel_url"], 8 * 1024 * 1024, 120)
                if hashlib.sha256(content).hexdigest() != manifest["sha256"]:
                    raise ValueError("Wheel checksum mismatch")
                package.write_bytes(content)
            else:
                package = release / "source"
                run(["git", "clone", "--quiet", "--depth", "1", "--branch", ref,
                     "--", source, str(package)])
            venv = release / "venv"
            uv = shutil.which("uv")
            if uv:
                run([uv, "venv", "--no-python-downloads", "--python", sys.executable, str(venv)])
                run([uv, "pip", "install", "--python", str(venv / "bin/python"), str(package)])
            else:
                run([sys.executable, "-m", "venv", str(venv)])
                run([str(venv / "bin/python"), "-m", "pip", "install", str(package)])
            env = {**os.environ, "DJCODE_NO_UPDATE_CHECK": "1", "DJCODE_SKIP_STARTUP_CHECK": "1"}
            version = run([str(venv / "bin/djcode"), "--version"], env=env, timeout=30)
            if manifest and version != f"djcode, version {manifest['version']}":
                raise ValueError("Installed version does not match release manifest")
            run([str(venv / "bin/djcode"), "--check"], env=env, timeout=60)
            for entry in ("djcode", "djcode-colibri"):
                if not (venv / "bin" / entry).is_file():
                    raise ValueError(f"Release is missing {entry}")
            receipt = {"schema": 1, "prefix": str(prefix), "bin_dir": str(bin_dir),
                       "repository": REPOSITORY if manifest else "unmanaged",
                       "managed": manifest is not None}
            if manifest:
                receipt.update({key: manifest[key] for key in ("commit", "version", "run_id")})
            (release / ".djcode-install.json").write_text(json.dumps(receipt, indent=2))
            if old:
                atomic_link(old, prefix / "previous")
            atomic_link(release, prefix / "current")
            for entry in ("djcode", "djcode-colibri"):
                atomic_link(prefix / "current/venv/bin" / entry, bin_dir / entry)
            run([str(bin_dir / "djcode"), "--version"], env=env, timeout=30)
            activated = True
            return version, release
        except BaseException:
            # Restore all pre-existing pointers if activation or its smoke test fails.
            for link, target in original_links.items():
                if target is None:
                    if link.is_symlink():
                        link.unlink()
                else:
                    atomic_link(Path(target), link)
            raise
        finally:
            if not activated:
                shutil.rmtree(release)


def main():
    if sys.version_info < (3, 12):  # noqa: UP036 -- standalone bootstrap may use older Python
        raise RuntimeError("Python 3.12+ is required; no Python or model downloads are automatic")
    source_override = os.environ.get("DJCODE_REPO_URL")
    ref_override = os.environ.get("DJCODE_REF")
    # Any explicit source/ref override is deliberately unmanaged, even canonical/main.
    source = (source_override or f"https://github.com/{REPOSITORY}.git") if (
        source_override or ref_override) else None
    version, _ = install(
        Path(os.environ.get("DJCODE_INSTALL_DIR", "~/.local/share/djcode")),
        Path(os.environ.get("DJCODE_BIN_DIR", "~/.local/bin")),
        source=source, ref=ref_override or "main")
    print(f"Installed {version}. No models were downloaded.")
    print("Managed canonical updates enabled." if source is None else
          "Custom source/ref installation: automatic canonical updates disabled.")
    print("Run djcode, or use DJCODE_BIN_DIR/djcode if that directory is not on PATH.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"DJcode installation failed ({type(error).__name__}); "
              "existing installation retained.",
              file=sys.stderr)
        sys.exit(1)
