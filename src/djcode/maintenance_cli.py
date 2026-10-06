"""Explicit account, model and maintenance commands without starting inference."""

from __future__ import annotations

import asyncio
import json
import sys

import click


def emit(value, json_output=False):
    if json_output:
        click.echo(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, list):
        for item in value:
            click.echo(
                f"{item['provider']}: {item.get('status', 'unknown')}"
                f" · {item.get('source', 'none')} · {item.get('method', 'unknown')}"
            )
    else:
        click.echo(value.get("message") or value.get("summary") or value.get("status", "unknown"))
    if isinstance(value, dict) and value.get("ok") is False:
        click.get_current_context().exit(1)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def auth():
    """Manage DJcode provider credentials without starting a model."""


@auth.command("list")
@click.option("--json", "json_output", is_flag=True)
def auth_list(json_output):
    from djcode.auth_management import list_auth

    emit(list_auth(), json_output)


@auth.command("status")
@click.argument("provider", required=False)
@click.option("--json", "json_output", is_flag=True)
def auth_status(provider, json_output):
    from djcode.auth_management import AuthManagementError, auth_status as status

    try:
        result = status(provider)
    except AuthManagementError as error:
        emit({"ok": False, "status": "failed", "message": str(error)}, json_output)
        return
    emit(result, json_output)


@auth.command("login")
@click.argument("provider", required=False)
@click.option("--provider", "provider_option", help="Provider ID, alternative to the argument.")
@click.option("--method", type=click.Choice(["api_key", "browser", "account", "local"]))
@click.option("--key-stdin", is_flag=True, help="Read an API key from stdin instead of a hidden prompt.")
@click.option("--json", "json_output", is_flag=True)
def auth_login(provider, provider_option, method, key_stdin, json_output):
    from djcode.account_auth import AccountAuthError
    from djcode.auth_management import (
        auth_status as status,
        login_account,
        login_api_key,
        login_openrouter,
        select_auth_method,
    )
    from djcode.config import load_config

    if provider and provider_option and provider != provider_option:
        raise click.UsageError("Choose the provider argument or --provider, with the same ID.")
    provider = provider_option or provider
    if not provider:
        provider = click.prompt("Provider", default=load_config().get("provider", "ollama"))
    try:
        current = status(provider)
    except AccountAuthError as error:
        emit({"ok": False, "status": "failed", "message": str(error)}, json_output)
        return
    methods = current["methods"]
    available = [item["id"] for item in methods if item["available"]]
    if method is None:
        method = "api_key" if key_stdin else "local" if current.get("method") == "local" else "api_key"
        if not key_stdin and len(available) > 1:
            method = click.prompt("Login method", type=click.Choice(available), default=method)
    if key_stdin and method != "api_key":
        raise click.UsageError("--key-stdin requires --method api_key.")
    try:
        if method == "api_key":
            if key_stdin:
                key = sys.stdin.readline(65537)
                if len(key) > 65536:
                    raise click.ClickException("API key input exceeds the size limit.")
                key = key.strip()
            else:
                key = click.prompt("API key", hide_input=True, show_default=False)
            result = login_api_key(provider, key)
        elif method == "browser":
            if provider != "openrouter":
                raise AccountAuthError("Browser sign-in is available for OpenRouter.")
            from djcode.openrouter_auth import begin

            verifier, url = begin()
            click.echo(f"Open {url}", err=True)
            click.launch(url)
            code = click.prompt("One-time authorization code", hide_input=True)
            result = asyncio.run(login_openrouter(provider, code, verifier))
        elif method == "account":
            result = asyncio.run(login_account(provider, on_status=lambda text: click.echo(text, err=True)))
        else:
            result = select_auth_method(provider, method)
    except (AccountAuthError, ValueError) as error:
        emit({"ok": False, "status": "failed", "message": str(error)}, json_output)
        return
    emit(result, json_output)


@auth.command("logout")
@click.argument("provider")
@click.option("--json", "json_output", is_flag=True)
def auth_logout(provider, json_output):
    from djcode.auth_management import AuthManagementError, logout

    try:
        result = logout(provider)
    except AuthManagementError as error:
        emit({"ok": False, "status": "failed", "message": str(error)}, json_output)
        return
    emit(result, json_output)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("provider", required=False)
@click.option("--select", "reference", help="Validate and save PROVIDER/MODEL as the default.")
@click.option("--json", "json_output", is_flag=True)
def models(provider, reference, json_output):
    """Discover provider models or select a verified provider/model pair."""
    from djcode.config import load_config_for_write, save_config
    from djcode.model_selection import ModelSelectionError, model_catalog, select_model

    if reference:
        try:
            candidate = select_model(load_config_for_write(), reference, provider=provider)
            save_config(candidate)
        except (ModelSelectionError, OSError, ValueError) as error:
            emit({"ok": False, "status": "failed", "message": str(error)}, json_output)
            return
        emit({"ok": True, "provider": candidate["provider"], "model": candidate["model"],
              "message": f"Default model: {candidate['provider']}/{candidate['model']}"}, json_output)
        return
    catalog = model_catalog(provider=provider)
    catalog = {**catalog, "ok": catalog["status"] in {"ready", "available"}}
    if json_output:
        emit(catalog, True)
    else:
        click.echo(f"{catalog['provider']} · {catalog['status']} · {catalog['source']}")
        click.echo(catalog["message"])
        for item in catalog["models"]:
            click.echo(f"  {catalog['provider']}/{item['id']}")
        if not catalog["ok"]:
            click.get_current_context().exit(1)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--status", "show_status", is_flag=True, help="Show installation and update mode without network access.")
@click.option("--check", "check_update", is_flag=True, help="Check for updates without installing.")
@click.option("--refresh", is_flag=True, help="Skip the check cache; requires --check.")
@click.option("--rollback", is_flag=True, help="Restore the previous managed build.")
@click.option("--mode", type=click.Choice(["auto", "manual", "disabled"]))
@click.option("--json", "json_output", is_flag=True)
def update(show_status, check_update, refresh, rollback, mode, json_output):
    """Install a verified managed update, or inspect and control updates."""
    if sum([show_status, check_update, rollback, bool(mode)]) > 1:
        raise click.UsageError("Choose one of --status, --check, --rollback or --mode.")
    if refresh and not check_update:
        raise click.UsageError("--refresh requires --check.")
    if mode:
        from djcode.config import set_value

        try:
            set_value("update_mode", mode)
        except (ValueError, OSError):
            emit({"ok": False, "status": "failed", "message": "Unable to change update mode. Repair DJcode settings and check file permissions."}, json_output)
            return
    if show_status or check_update or mode:
        from djcode.updater import get_update_status

        result = get_update_status(check=check_update, force=refresh)
        emit(result, json_output)
        if not json_output:
            installation = result["installation"]
            click.echo(f"Installation: {installation['kind']} · mode: {result['update_mode']}")
            if installation.get("update_command"):
                click.echo(f"Update command: {installation['update_command']}")
        return
    from djcode.managed_update import perform_update, rollback as restore_previous

    emit(restore_previous() if rollback else perform_update(force=True), json_output)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("path", required=False, type=click.Path(path_type=str))
@click.option("--json", "json_output", is_flag=True)
def lint(path, json_output):
    """Report fatal Python lint diagnostics for PATH or the installation."""
    from djcode.maintenance import run_lint

    result = run_lint(path)
    if not json_output:
        for item in result.get("diagnostics", []):
            row = item.get("location", {}).get("row", "?")
            column = item.get("location", {}).get("column", "?")
            click.echo(f"{item.get('filename', path)}:{row}:{column}: {item.get('code')} {item.get('message')}")
        if result.get("detail"):
            click.echo(result["detail"])
    emit(result, json_output)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--quick", is_flag=True, help="Skip persistent memory and compiled DAF/DDAL probes.")
def doctor(quick):
    """Report installation, updater and optional runtime health as JSON."""
    from djcode.studio import doctor as run_doctor

    emit(run_doctor(deep=not quick), True)


commands = {"auth": auth, "models": models, "update": update, "lint": lint, "doctor": doctor}
