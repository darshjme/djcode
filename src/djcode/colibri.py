"""Opt-in bridge to an existing Colibri installation; never fetches weights."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import click
import httpx

UPSTREAM = "https://github.com/JustVugg/colibri"
REVIEWED_COMMIT = "fd93c41aa6ae2c7d1cc1a1e2d6b79dbe6d341708"
DEFAULT_URL = "http://127.0.0.1:8000/v1"


def launcher_command(launcher: str | None) -> list[str]:
    selected = launcher or os.environ.get("DJCODE_COLIBRI_LAUNCHER") or shutil.which("coli")
    if not selected:
        raise click.ClickException(f"Colibri is not installed. Obtain it separately from {UPSTREAM}; no download was started.")
    path = Path(selected).expanduser().resolve()
    if not path.is_file():
        raise click.ClickException(f"Colibri launcher not found: {path}")
    # Upstream c/coli is a Python script, including on Windows. Native launchers
    # remain directly executable. No shell interpolation or setup script runs.
    if path.suffix == ".py" or path.name == "coli":
        return [sys.executable, str(path)]
    return [str(path)]


def runtime_args(model_dir: Path, ram_gb: int, vram_gb: float, context: int, gpu: str) -> list[str]:
    if not math.isfinite(vram_gb):
        raise click.BadParameter("The VRAM budget must be finite", param_hint="--vram-gb")
    if not re.fullmatch(r"none|\d+(?:,\d+)*", gpu):
        raise click.BadParameter("Use 'none' or device IDs such as 0 or 0,1", param_hint="--gpu")
    if gpu == "none" and vram_gb:
        raise click.BadParameter("A VRAM budget requires explicit --gpu device IDs", param_hint="--vram-gb")
    if gpu != "none" and not vram_gb:
        raise click.BadParameter("Set an explicit --vram-gb budget for GPU use", param_hint="--vram-gb")
    if not (model_dir / "config.json").is_file():
        raise click.ClickException("The existing model directory must contain config.json. No conversion or download was started.")
    return ["--model", str(model_dir.resolve()), "--ram", str(ram_gb),
            "--vram", str(vram_gb), "--ctx", str(context), "--gpu", gpu,
            "--policy", "quality", "--no-tune-profile"]


def run_inspection(command: list[str], args: list[str], operation: str = "plan") -> tuple[dict, int]:
    try:
        result = subprocess.run(command + [operation, *args, "--json"],
                                capture_output=True, text=True, timeout=30, check=False)
    except subprocess.TimeoutExpired as error:
        raise click.ClickException("Colibri planning exceeded 30 seconds; no inference server was started.") from error
    except OSError as error:
        raise click.ClickException(f"Cannot run the existing Colibri launcher: {error}") from error
    if result.returncode and operation == "plan":
        raise click.ClickException(f"Colibri plan failed ({result.returncode}): {(result.stderr or result.stdout)[-2000:]}")
    try:
        plan = json.loads(result.stdout)
        if not isinstance(plan, dict) or (operation == "plan" and not isinstance(plan.get("model"), dict)):
            raise ValueError("Missing model resource information")
        if operation == "doctor" and (plan.get("schema_version") != 1 or not isinstance(plan.get("checks"), list)
                                       or not all(isinstance(item, dict) for item in plan["checks"])):
            raise ValueError("Unsupported doctor report")
        return plan, result.returncode
    except (ValueError, TypeError) as error:
        raise click.ClickException("Colibri returned an incompatible plan; check its version before serving.") from error


def common_options(function):
    for decorator in reversed([
        click.option("--launcher", type=click.Path(exists=True, dir_okay=False), help="Existing coli Python launcher or native executable."),
        click.option("--model-dir", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path)),
        click.option("--ram-gb", required=True, type=click.IntRange(1, 65536), help="Explicit Colibri RAM budget; leave room for the OS."),
        click.option("--vram-gb", default=0.0, type=click.FloatRange(0, 65536), show_default=True),
        click.option("--context", default=8192, type=click.IntRange(1024, 131072), show_default=True),
        click.option("--gpu", default="none", show_default=True, help="none or explicit device IDs (e.g. 0,1)."),
    ]):
        function = decorator(function)
    return function


@click.group()
def main():
    """Plan or serve existing Colibri models with explicit memory budgets.

    Colibri streams supported MoE experts from disk. This trades RAM for disk
    capacity and I/O latency; it does not make every model fit or run quickly.
    No engine, model weights or optional dependencies are downloaded here.
    """


@main.command()
@common_options
def plan(launcher, model_dir, ram_gb, vram_gb, context, gpu):
    """Inspect Colibri's actual resource plan without loading an inference engine."""
    result, _ = run_inspection(launcher_command(launcher), runtime_args(model_dir, ram_gb, vram_gb, context, gpu))
    click.echo(json.dumps({"planner": UPSTREAM, "reviewed_commit": REVIEWED_COMMIT,
                           "inference_tested": False, "plan": result}, indent=2))


@main.command()
@common_options
@click.option("--port", default=8000, type=click.IntRange(1024, 65535), show_default=True)
@click.option("--max-tokens", default=256, type=click.IntRange(1, 8192), show_default=True)
@click.option("--model-id", default="djcode-colibri", show_default=True)
@click.option("--dry-run", is_flag=True, help="Validate the plan and print argv without loading weights.")
def serve(launcher, model_dir, ram_gb, vram_gb, context, gpu, port, max_tokens, model_id, dry_run):
    """Explicitly start a foreground, loopback-only Colibri server.

    Ctrl-C stops this server through Colibri's own shutdown handler. DJcode
    connects separately, so its tool permissions and cancellation still apply.
    """
    if max_tokens >= context:
        raise click.BadParameter("Output budget must be smaller than --context", param_hint="--max-tokens")
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,128}", model_id):
        raise click.BadParameter("Use 1–128 letters, digits, dots, underscores, slashes or hyphens", param_hint="--model-id")
    command = launcher_command(launcher)
    args = runtime_args(model_dir, ram_gb, vram_gb, context, gpu)
    resource_plan, _ = run_inspection(command, args)
    doctor, doctor_exit = run_inspection(command, args, "doctor")
    descriptor = next((entry.get("details", {}).get("descriptor", {})
                       for entry in doctor.get("checks", [])
                       if entry.get("id") == "model.family" and entry.get("status") == "pass"), {})
    supports_tools = descriptor.get("capabilities", {}).get("tools") is True
    ready = doctor_exit == 0 and supports_tools
    argv = command + ["serve", *args, "--auto-tier", "--host", "127.0.0.1",
                      "--port", str(port), "--model-id", model_id,
                      "--ngen", str(max_tokens), "--max-queue", "1", "--kv-slots", "1"]
    if dry_run:
        click.echo(json.dumps({"argv": argv, "plan": resource_plan, "doctor": doctor,
                               "ready_for_coding": ready, "server_started": False}, indent=2))
        return
    if not supports_tools:
        raise click.ClickException("Colibri doctor does not advertise native tool support for this family. DJcode coding mode requires it; use upstream coli chat for chat-only models.")
    if doctor_exit:
        failures = [item.get("summary", item.get("id", "check")) for item in doctor.get("checks", []) if item.get("status") == "fail"]
        raise click.ClickException("Colibri preflight failed; no server started: " + "; ".join(failures[:5]))
    click.echo(f"Starting Colibri on 127.0.0.1:{port}. RAM budget is an engine setting, not an OS memory limit.", err=True)
    click.echo(f"Connect: DJCODE_BASE_URL=http://127.0.0.1:{port}/v1 DJCODE_COLIBRI_CONTEXT={context} DJCODE_COLIBRI_MAX_TOKENS={max_tokens} djcode --provider colibri --model {model_id}", err=True)
    # Replace only this explicitly invoked helper, not the DJcode client or any
    # existing server. Colibri remains the owner of the engine and its cleanup.
    try:
        os.execvpe(argv[0], argv, os.environ.copy())
    except OSError as error:
        raise click.ClickException(f"Cannot start Colibri: {error}") from error


@main.command()
@click.option("--url", default=DEFAULT_URL, show_default=True)
def check(url):
    """List served model IDs without inference, downloads or config changes.

    Successful discovery does not prove tool support or model quality. Some
    Colibri families cannot accept tools; their HTTP error is not hidden.
    """
    if not url.startswith(("http://", "https://")):
        raise click.BadParameter("Expected an HTTP(S) API base URL", param_hint="--url")
    base = url.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    key = os.environ.get("COLI_API_KEY", "")
    try:
        with httpx.Client(timeout=5) as client:
            response = client.get(base + "/models", headers={"Authorization": f"Bearer {key}"} if key else {})
            response.raise_for_status()
            models = [item["id"] for item in response.json()["data"] if isinstance(item.get("id"), str)]
        if not models:
            raise ValueError("No model IDs returned")
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise click.ClickException(f"Colibri model discovery failed: {type(error).__name__}") from error
    click.echo(json.dumps({"url": base, "models": models, "tool_support": "not advertised by upstream discovery",
                           "inference_tested": False}, indent=2))


if __name__ == "__main__":
    main()
