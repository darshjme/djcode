"""Public maintenance commands must not launch inference or mutate on checks."""

import json

from click.testing import CliRunner

from djcode.cli import main


def test_public_command_help_does_not_start_runtime(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Help must not start a runtime")

    monkeypatch.setattr("djcode.startup.prepare", forbidden)
    for args in ([], ["auth"], ["auth", "login"], ["models"], ["update"], ["lint"], ["doctor"]):
        result = CliRunner().invoke(main, [*args, "--help"])
        assert result.exit_code == 0, result.output
        assert "Usage:" in result.output


def test_quoted_prompt_and_reserved_word_escape_remain_one_shot(monkeypatch):
    seen = []
    monkeypatch.setattr("djcode.startup.prepare", lambda *args, **kwargs: ("ollama", "fixture"))
    monkeypatch.setattr("djcode.updater.perform_update", lambda **kwargs: {"status": "disabled"})

    async def run(prompt, **kwargs):
        seen.append(prompt)

    monkeypatch.setattr("djcode.repl.run_oneshot", run)
    runner = CliRunner()
    assert runner.invoke(main, ["write a function"]).exit_code == 0
    assert runner.invoke(main, ["--", "auth"]).exit_code == 0
    assert seen == ["write a function", "auth"]


def test_update_check_json_is_read_only_and_failure_is_nonzero(monkeypatch):
    calls = []

    def status(**kwargs):
        calls.append(kwargs)
        return {"ok": False, "status": "unavailable", "message": "Network unavailable"}

    def forbidden(**kwargs):
        raise AssertionError("An update check must not install")

    monkeypatch.setattr("djcode.updater.get_update_status", status)
    monkeypatch.setattr("djcode.managed_update.perform_update", forbidden)
    result = CliRunner().invoke(main, ["update", "--check", "--refresh", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["status"] == "unavailable"
    assert calls == [{"check": True, "force": True}]


def test_update_status_offline_and_mutually_exclusive_actions(monkeypatch):
    calls = []

    def status(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "status": "idle", "message": "No network request"}

    monkeypatch.setattr("djcode.updater.get_update_status", status)
    runner = CliRunner()
    result = runner.invoke(main, ["update", "--status", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output)["status"] == "idle"
    assert calls == [{"check": False, "force": False}]
    for options in (["--status", "--check"], ["--refresh"], ["--check", "--rollback"]):
        assert runner.invoke(main, ["update", *options]).exit_code == 2
    assert len(calls) == 1


def test_real_lint_failure_has_json_location_and_nonzero_exit(tmp_path):
    path = tmp_path / "broken.py"
    path.write_text("if True\n    pass\n")
    result = CliRunner().invoke(main, ["lint", str(path), "--json"])
    assert result.exit_code == 1, result.output
    report = json.loads(result.output)
    assert report["ok"] is False
    assert report["diagnostics"]
    assert report["diagnostics"][0]["location"]["row"] == 1
    assert path.read_text() == "if True\n    pass\n"


def test_auth_stdin_login_status_and_scoped_logout(monkeypatch, tmp_path):
    from djcode import config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config.save_config({**config.DEFAULT_CONFIG, "anthropic_api_key": "other-provider-fixture"})
    runner = CliRunner()
    key = "private-fixture-key"
    result = runner.invoke(main, ["auth", "login", "openai", "--key-stdin", "--json"], input=key + "\n")
    assert result.exit_code == 0, result.output
    assert key not in result.output
    assert config.load_config()["openai_api_key"] == key
    result = runner.invoke(main, ["auth", "status", "openai", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["source"] == "stored"
    assert key not in result.output
    result = runner.invoke(main, ["auth", "logout", "openai", "--json"])
    assert result.exit_code == 0, result.output
    assert not config.load_config().get("openai_api_key")
    assert config.load_config()["anthropic_api_key"] == "other-provider-fixture"


def test_failed_model_selection_does_not_save(monkeypatch, tmp_path):
    from djcode import config
    from djcode.model_selection import ModelSelectionError

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    config.save_config({**config.DEFAULT_CONFIG, "model": "existing-fixture"})
    before = config.CONFIG_FILE.read_bytes()

    def reject(*args, **kwargs):
        raise ModelSelectionError("Model is unavailable")

    monkeypatch.setattr("djcode.model_selection.select_model", reject)
    result = CliRunner().invoke(main, ["models", "--select", "openai/missing", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["ok"] is False
    assert config.CONFIG_FILE.read_bytes() == before


def test_unknown_auth_provider_has_structured_error_without_mutation(monkeypatch, tmp_path):
    from djcode import config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    config.save_config(config.DEFAULT_CONFIG)
    before = config.CONFIG_FILE.read_bytes()
    for verb in ("status", "login", "logout"):
        result = CliRunner().invoke(main, ["auth", verb, "missing-provider", "--json"])
        assert result.exit_code == 1, result.output
        assert json.loads(result.output)["ok"] is False
        assert "Traceback" not in result.output
        assert config.CONFIG_FILE.read_bytes() == before


def test_local_auth_login_uses_offered_local_method(monkeypatch, tmp_path):
    from djcode import config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    result = CliRunner().invoke(main, ["auth", "login", "ollama", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["source"] == "local"
