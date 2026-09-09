"""Offline protocol/filesystem tests; no downloads, installs, or real GitHub mutations."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import managed_install as installer
import publish_update as publisher

COMMIT = "a" * 40
WHEEL = b"fixture wheel bytes, not executable"
MANIFEST = {"schema": 1, "repository": installer.REPOSITORY, "branch": "main",
            "commit": COMMIT, "version": "4.2.0", "run_id": 42,
            "wheel_url": f"https://github.com/{installer.REPOSITORY}/releases/download/"
                         f"build-{COMMIT[:12]}/djcode-4.2.0-py3-none-any.whl",
            "sha256": hashlib.sha256(WHEEL).hexdigest()}
RUN = {"head_repository": {"full_name": installer.REPOSITORY}, "head_branch": "main",
       "head_sha": COMMIT, "path": ".github/workflows/ci.yml", "event": "push",
       "conclusion": "success", "status": "completed"}


class ProtocolTests(unittest.TestCase):
    def test_manifest_and_run(self):
        installer.validate_manifest(MANIFEST)
        installer.verify_run(MANIFEST, RUN)
        publisher.validate_run(RUN)
        for key, value in [("schema", True), ("wheel_url", "https://evil.example/x.whl"),
                           ("commit", None),
                           ("version", [4]), ("run_id", True), ("sha256", "no"),
                           ("repository", "other/fork"), ("branch", "other")]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                installer.validate_manifest({**MANIFEST, key: value})
        for key, value in [("conclusion", "failure"), ("status", "in_progress"),
                           ("event", "pull_request"), ("head_sha", "b" * 40),
                           ("path", ".github/workflows/other.yml"),
                           ("head_repository", {"full_name": "other/fork"})]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                installer.verify_run(MANIFEST, {**RUN, key: value})

    def test_publisher_manifest_matches_installer(self):
        with tempfile.TemporaryDirectory() as temporary:
            wheel = Path(temporary) / "djcode-4.2.0-py3-none-any.whl"
            wheel.write_bytes(WHEEL)
            self.assertEqual(publisher.manifest_for(wheel, COMMIT, 42), MANIFEST)


class FilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve()
        self.prefix, self.bin_dir = root / "prefix", root / "bin"
        self.old = self.prefix / "release.old"
        self.old.mkdir(parents=True)
        self.bin_dir.mkdir()
        (self.old / "venv/bin").mkdir(parents=True)
        for entry in ("djcode", "djcode-colibri"):
            (self.old / "venv/bin" / entry).write_text("old")
            (self.bin_dir / entry).symlink_to(self.old / "venv/bin" / entry)
        (self.prefix / "current").symlink_to(self.old)
        self.version_fail = False
        self.check_fail = False

    def fake_run(self, command, **_kwargs):
        if "--version" in command:
            if self.version_fail and command[0] == str(self.bin_dir / "djcode"):
                raise RuntimeError("activation smoke failed")
            return "djcode, version 4.2.0"
        if "--check" in command:
            if self.check_fail:
                raise RuntimeError("staged check failed")
            return "checks passed"
        if "venv" in command:
            venv = Path(command[-1])
            (venv / "bin").mkdir(parents=True)
            for name in ("python", "djcode", "djcode-colibri"):
                (venv / "bin" / name).write_text("fixture")
        return ""

    def install(self, **kwargs):
        with patch.object(installer, "metadata", side_effect=[MANIFEST, RUN]), \
                patch.object(installer, "download", return_value=WHEEL), \
                patch.object(installer, "run", side_effect=self.fake_run), \
                patch.object(installer.shutil, "which", return_value=None):
            return installer.install(self.prefix, self.bin_dir, **kwargs)

    def assert_old_intact(self):
        self.assertEqual((self.prefix / "current").resolve(), self.old)
        self.assertEqual((self.bin_dir / "djcode").resolve(), self.old / "venv/bin/djcode")
        self.assertEqual(list(self.prefix.glob("release.*")), [self.old])

    def test_install_receipt_and_pointers(self):
        _, release = self.install()
        receipt = json.loads((release / ".djcode-install.json").read_text())
        self.assertEqual(receipt["repository"], installer.REPOSITORY)
        self.assertEqual(receipt["prefix"], str(self.prefix))
        self.assertEqual(receipt["commit"], COMMIT)
        self.assertEqual((self.prefix / "current").resolve(), release)
        self.assertEqual((self.prefix / "previous").resolve(), self.old)
        self.assertEqual((self.bin_dir / "djcode").readlink(),
                         self.prefix / "current/venv/bin/djcode")

    def test_custom_source_never_gets_canonical_receipt(self):
        _, release = self.install(source="https://example.org/custom.git", ref="main")
        receipt = json.loads((release / ".djcode-install.json").read_text())
        self.assertEqual(receipt["repository"], "unmanaged")
        self.assertFalse(receipt["managed"])
        self.assertNotIn("run_id", receipt)

    def test_hash_failure_preserves_old(self):
        with patch.object(installer, "metadata", side_effect=[MANIFEST, RUN]), \
                patch.object(installer, "download", return_value=b"tampered"):
            with self.assertRaises(ValueError):
                installer.install(self.prefix, self.bin_dir)
        self.assert_old_intact()

    def test_check_failure_preserves_old(self):
        self.check_fail = True
        with self.assertRaises(RuntimeError):
            self.install()
        self.assert_old_intact()

    def test_activation_failure_restores_all_pointers(self):
        self.version_fail = True
        with self.assertRaises(RuntimeError):
            self.install()
        self.assert_old_intact()
        self.assertFalse((self.prefix / "previous").exists())

    def test_unrelated_bin_symlink_rejected(self):
        (self.bin_dir / "djcode").unlink()
        (self.bin_dir / "djcode").symlink_to("/usr/bin/true")
        with self.assertRaises(ValueError):
            installer.preflight(self.prefix, self.bin_dir)
        self.assertEqual((self.bin_dir / "djcode").readlink(), Path("/usr/bin/true"))

    def test_unrelated_current_rejected(self):
        (self.prefix / "current").unlink()
        (self.prefix / "current").write_text("unrelated")
        with self.assertRaises(ValueError):
            installer.preflight(self.prefix, self.bin_dir)
        self.assertEqual((self.prefix / "current").read_text(), "unrelated")


class PublisherTests(unittest.TestCase):
    def exercise(self, *, stale=False, existing=False, different=False):
        calls = []
        def fake_api(path):
            if "/actions/runs/" in path:
                return RUN
            return {"object": {"sha": "b" * 40 if stale else COMMIT}}
        def fake_command(*args):
            calls.append(args)
            if args[:3] == ("gh", "run", "download"):
                dest = Path(args[args.index("--dir") + 1])
                (dest / "djcode-4.2.0-py3-none-any.whl").write_bytes(WHEEL)
                (dest / "djcode-4.2.0.tar.gz").write_bytes(b"sdist")
            if args[:3] == ("gh", "release", "download"):
                if "--output" in args:
                    return json.dumps(MANIFEST)
                dest = Path(args[args.index("--dir") + 1])
                (dest / "djcode-4.2.0-py3-none-any.whl").write_bytes(
                    b"changed" if different else WHEEL)
                (dest / "djcode-4.2.0.tar.gz").write_bytes(b"sdist")
            return ""
        with patch.dict(publisher.os.environ, {"CI_RUN_ID": "42"}), \
                patch.object(publisher, "api", side_effect=fake_api), \
                patch.object(publisher, "command", side_effect=fake_command), \
                patch.object(publisher.subprocess, "run") as status:
            status.return_value.returncode = 0 if existing else 1
            publisher.main()
        return calls

    def test_stale_main_never_updates_rolling(self):
        calls = self.exercise(stale=True)
        self.assertFalse(any("updates-main" in call for call in calls))
        self.assertTrue(any("build-" + COMMIT[:12] in call for call in calls))

    def test_existing_immutable_assets_cannot_be_replaced(self):
        with self.assertRaises(ValueError):
            self.exercise(existing=True, different=True)

    def test_verified_rolling_publication(self):
        calls = self.exercise()
        creates = [call for call in calls if call[:3] == ("gh", "release", "create")]
        self.assertEqual([call[3] for call in creates], ["build-" + COMMIT[:12], "updates-main"])


if __name__ == "__main__":
    unittest.main()
