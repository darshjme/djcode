"""Publish only artifacts from a successful canonical main CI run."""
from __future__ import annotations

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


def manifest_for(wheel, commit, run_id):
    match = re.fullmatch(r"djcode-(\d+\.\d+\.\d+)-py3-none-any\.whl", wheel.name)
    if not match:
        raise ValueError("Unexpected wheel name")
    return {"schema": 1, "repository": REPOSITORY, "branch": "main", "commit": commit,
            "version": match[1], "run_id": run_id,
            "wheel_url": (f"https://github.com/{REPOSITORY}/releases/download/"
                          f"build-{commit[:12]}/{wheel.name}"),
            "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}


def main():
    run_id = int(os.environ["CI_RUN_ID"])
    run = api(f"repos/{REPOSITORY}/actions/runs/{run_id}")
    validate_run(run)
    commit = run["head_sha"]
    # workflow_run executes trusted default-branch publisher code, never artifact code.
    with tempfile.TemporaryDirectory(prefix="djcode-publish-") as temporary:
        dist = Path(temporary)
        command("gh", "run", "download", str(run_id), "--repo", REPOSITORY,
                "--name", "python-dist", "--dir", str(dist))
        wheels, sdists = list(dist.glob("*.whl")), list(dist.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise ValueError("CI must produce exactly one wheel and source distribution")
        wheel, sdist = wheels[0], sdists[0]
        manifest = manifest_for(wheel, commit, run_id)
        if sdist.name != f"djcode-{manifest['version']}.tar.gz":
            raise ValueError("Source distribution and wheel versions differ")
        tag = f"build-{commit[:12]}"
        existing = subprocess.run(["gh", "release", "view", tag, "--repo", REPOSITORY],
                                  capture_output=True, check=False)
        if existing.returncode:
            command("gh", "release", "create", tag, str(wheel), str(sdist),
                    "--repo", REPOSITORY, "--target", commit, "--prerelease",
                    "--title", f"CI build {commit[:12]}",
                    "--notes", f"Validated main build. CI run: {run_id}. Commit: {commit}.")
        else:
            # Immutable build assets are never overwritten, including reruns of the same commit.
            immutable = dist / "published"
            immutable.mkdir()
            command("gh", "release", "download", tag, "--repo", REPOSITORY,
                    "--dir", str(immutable), "--pattern", wheel.name, "--pattern", sdist.name)
            for artifact in (wheel, sdist):
                if (immutable / artifact.name).read_bytes() != artifact.read_bytes():
                    raise ValueError("Existing immutable release differs; refusing overwrite")
        # All publisher runs share workflow concurrency; an obsolete completion cannot
        # replace the rolling manifest after a newer push or successful publication.
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
                    "Rolling verified CI build pointer; immutable assets use build tags.")
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
        print(f"Published verified build {commit[:12]} from CI run {run_id}.")


if __name__ == "__main__":
    main()
