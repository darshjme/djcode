"""The reported revision belongs to this process's installed release, without I/O."""
from click.testing import CliRunner
from djcode.cli import main
from djcode import managed_update


def test_revision_reports_installed_receipt_without_network(monkeypatch, tmp_path):
    commit = "b" * 40
    monkeypatch.setattr(managed_update, "installation", lambda: (tmp_path, {"commit": commit}))
    monkeypatch.setattr(managed_update, "perform_update", lambda **kwargs: (_ for _ in ()).throw(AssertionError("network forbidden")))
    result = CliRunner().invoke(main, ["--revision"])
    assert result.exit_code == 0
    assert commit in result.output


def test_unmanaged_revision_is_explicit(monkeypatch):
    monkeypatch.setattr(managed_update, "installation", lambda: None)
    result = CliRunner().invoke(main, ["--revision"])
    assert result.exit_code == 0
    assert "unmanaged installation" in result.output
