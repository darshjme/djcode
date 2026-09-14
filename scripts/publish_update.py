"""Build and verify an isolated local checkout; optionally publish immutable updates."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

REPOSITORY = "darshjme/djcode"


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def api(path):
    return json.loads(command("gh", "api", path))


def validate_run(run):
    if (run.get("head_repository", {}).get("full_name") != REPOSITORY
            or run.get("head_branch") != "main" or run.get("event") != "push"
            or run.get("status") != "completed" or run.get("conclusion") != "success"
            or run.get("path", "").split("@")[0] != ".github/workflows/ci.yml"
            or not re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", ""))):
        raise ValueError("Refusing artifacts from an untrusted or unsuccessful CI run")


def manifest_for(wheel, commit, run_id=None):
    match = re.fullmatch(r"djcode-(\d+\.\d+\.\d+)-py3-none-any\.whl", wheel.name)
    if not match:
        raise ValueError("Unexpected wheel name")
    manifest = {"schema": 1 if run_id is not None else 2, "repository": REPOSITORY, "branch": "main", "commit": commit,
            "version": match[1],
            "wheel_url": (f"https://github.com/{REPOSITORY}/releases/download/"
                          f"build-{commit[:12]}/{wheel.name}"),
            "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}
    if run_id is not None:
        manifest["run_id"] = run_id
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true", help="Publish only after local validation passes")
    args = parser.parse_args(argv)
    repository = Path(__file__).resolve().parents[1]
    commit = command("git", "-C", str(repository), "rev-parse", "HEAD")
    if command("git", "-C", str(repository), "status", "--porcelain"):
        raise ValueError("Commit the reviewed changes before building a release")
    if args.publish and api(f"repos/{REPOSITORY}/git/ref/heads/main")["object"]["sha"] != commit:
        raise ValueError("Only the current canonical main commit can be published")
    with tempfile.TemporaryDirectory(prefix="djcode-publish-") as temporary:
        source = Path(temporary) / "source"
        command("git", "clone", "--quiet", "--shared", str(repository), str(source))
        command("git", "-C", str(source), "checkout", "--quiet", "--detach", commit)
        env = {**os.environ, "DJCODE_CONFIG_DIR": str(Path(temporary) / "config"),
               "DJCODE_NO_UPDATE_CHECK": "1", "DJCODE_SKIP_STARTUP_CHECK": "1"}
        for check in (["uv", "run", "--frozen", "--with", "pytest", "--with", "pytest-asyncio", "python", "-m", "pytest", "-q"],
                      ["uv", "run", "--frozen", "python", "-m", "unittest", "discover", "-s", "scripts", "-p", "test_*.py"],
                      ["uv", "build"]):
            subprocess.run(check, cwd=source, env=env, check=True)
        dist = source / "dist"
        wheels, sdists = list(dist.glob("*.whl")), list(dist.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise ValueError("Build must produce exactly one wheel and source distribution")
        wheel, sdist = wheels[0], sdists[0]
        manifest = manifest_for(wheel, commit)
        if sdist.name != f"djcode-{manifest['version']}.tar.gz":
            raise ValueError("Source distribution and wheel versions differ")
        # Install the built artifact in a separate environment before distribution.
        acceptance = Path(temporary) / "acceptance"
        command("uv", "venv", str(acceptance))
        python = acceptance / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        command("uv", "pip", "install", "--python", str(python), str(wheel))
        subprocess.run([str(python), "-m", "djcode", "--version"], cwd=temporary, env=env, check=True)
        if not args.publish:
            print(json.dumps({"status": "verified", "manifest": manifest}, indent=2))
            return
        tag = f"build-{commit[:12]}"
        existing = subprocess.run(["gh", "release", "view", tag, "--repo", REPOSITORY],
                                  capture_output=True, check=False)
        if existing.returncode:
            command("gh", "release", "create", tag, str(wheel), str(sdist),
                    "--repo", REPOSITORY, "--target", commit, "--prerelease",
                    "--title", f"Verified build {commit[:12]}",
                    "--notes", f"Locally tested main build. Commit: {commit}.")
        else:
            # Immutable build assets are never overwritten, including reruns of the same commit.
            immutable = dist / "published"
            immutable.mkdir()
            command("gh", "release", "download", tag, "--repo", REPOSITORY,
                    "--dir", str(immutable), "--pattern", wheel.name, "--pattern", sdist.name)
            for artifact in (wheel, sdist):
                if (immutable / artifact.name).read_bytes() != artifact.read_bytes():
                    raise ValueError("Existing immutable release differs; refusing overwrite")
        # Recheck main immediately before promoting the rolling pointer.
        head = api(f"repos/{REPOSITORY}/git/ref/heads/main")["object"]["sha"]
        if head != commit:
            print("Immutable build retained; newer main revision exists, rolling update skipped.")
            return
        manifest_path = dist / "update.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        rolling = subprocess.run(["gh", "release", "view", "updates-main", "--repo", REPOSITORY],
                                 capture_output=True, check=False)
        if rolling.returncode:
            command("gh", "release", "create", "updates-main", str(manifest_path),
                    "--repo", REPOSITORY, "--target", commit, "--prerelease",
                    "--title", "Verified main updates", "--notes",
                    "Rolling locally verified build pointer; immutable assets use build tags.")
        else:
            command("gh", "api", "--method", "PATCH",
                    f"repos/{REPOSITORY}/git/refs/tags/updates-main",
                    "-f", f"sha={commit}", "-F", "force=true")
            command("gh", "release", "upload", "updates-main", str(manifest_path),
                    "--repo", REPOSITORY, "--clobber")
        published = json.loads(command("gh", "release", "download", "updates-main", "--repo",
                                       REPOSITORY, "--pattern", "update.json", "--output", "-"))
        if published != manifest:
            raise ValueError("Published rolling manifest verification failed")
        print(f"Published locally verified build {commit[:12]}.")


if __name__ == "__main__":
    main()
