import json
from pathlib import Path
import subprocess

from click.testing import CliRunner
import pytest

from djcode import colibri


@pytest.fixture
def runtime(tmp_path):
    model = tmp_path / "model space; no shell"
    model.mkdir()
    (model / "config.json").write_text("{}")
    launcher = tmp_path / "coli"
    launcher.write_text("""import json,sys,os
if sys.argv[1] == 'plan':
 print(json.dumps({'model': {'family_id': 'glm'}, 'received': sys.argv[2:]}))
else:
 print(json.dumps({'schema_version':1,'checks':[{'id':'model.family','status':'pass','details':{'descriptor':{'capabilities':{'tools':os.environ.get('FIXTURE_TOOLS') != 'no'}}}}]}))
 sys.exit(int(os.environ.get('FIXTURE_DOCTOR_EXIT','0')))
""")
    return ["--launcher", str(launcher), "--model-dir", str(model), "--ram-gb", "12"]


def test_plan_uses_real_subprocess_and_preserves_path(runtime):
    result = CliRunner().invoke(colibri.main, ["plan", *runtime])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["inference_tested"] is False
    assert data["plan"]["received"][1] == runtime[3]
    assert "--json" in data["plan"]["received"]


def test_dry_run_inspects_without_starting_server(runtime, monkeypatch):
    def unexpected(*args):
        pytest.fail("Dry run must not start the server")
    monkeypatch.setattr(colibri.os, "execvpe", unexpected)
    result = CliRunner().invoke(colibri.main, ["serve", *runtime, "--dry-run"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["server_started"] is False and data["ready_for_coding"] is True
    argv = data["argv"]
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--max-queue") + 1] == "1"
    assert argv[argv.index("--policy") + 1] == "quality"
    assert not any("convert" == arg or "download" == arg for arg in argv)


@pytest.mark.parametrize("env", [{"FIXTURE_TOOLS": "no"}, {"FIXTURE_DOCTOR_EXIT": "2"}])
def test_preflight_rejects_unsupported_or_unready_model(runtime, monkeypatch, env):
    monkeypatch.setattr(colibri.os, "execvpe", lambda *args: pytest.fail("Rejected model started"))
    result = CliRunner().invoke(colibri.main, ["serve", *runtime], env=env)
    assert result.exit_code != 0
    assert "native tool support" in result.output or "preflight failed" in result.output


def test_foreground_exec_forwards_budgets_without_shell(runtime, monkeypatch):
    launched = []
    monkeypatch.setattr(colibri.os, "execvpe", lambda executable, argv, env: launched.append(argv))
    result = CliRunner().invoke(colibri.main, ["serve", *runtime, "--gpu", "0", "--vram-gb", "10", "--context", "4096"])
    assert result.exit_code == 0, result.output
    assert len(launched) == 1
    assert launched[0][launched[0].index("--vram") + 1] == "10.0"
    assert launched[0][launched[0].index("--ctx") + 1] == "4096"
    assert "DJCODE_COLIBRI_CONTEXT=4096" in result.output


@pytest.mark.parametrize("options", [["--gpu", "0"], ["--vram-gb", "5"], ["--gpu", "0; echo bad"]])
def test_gpu_requires_explicit_consistent_budget(runtime, options):
    result = CliRunner().invoke(colibri.main, ["plan", *runtime, *options])
    assert result.exit_code != 0


def test_planning_timeout_does_not_start_inference(runtime, monkeypatch):
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 30
        raise subprocess.TimeoutExpired(args[0], 30)
    monkeypatch.setattr(colibri.subprocess, "run", timeout)
    result = CliRunner().invoke(colibri.main, ["serve", *runtime])
    assert result.exit_code != 0
    assert "no inference server was started" in result.output


def test_missing_installation_does_not_download(monkeypatch):
    monkeypatch.delenv("DJCODE_COLIBRI_LAUNCHER", raising=False)
    monkeypatch.setattr(colibri.shutil, "which", lambda name: None)
    with pytest.raises(Exception, match="no download was started"):
        colibri.launcher_command(None)
