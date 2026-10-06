"""Explicit, shared maintenance operations for both interactive interfaces."""
from __future__ import annotations


def run_operation(command: str, argument: str = "") -> list[str]:
    """Return plain text; status never installs an update or exposes credentials."""
    argument = argument.strip()
    if command == "/auth":
        from djcode.auth_management import auth_status, list_auth
        parts = argument.split()
        if not parts or parts == ["list"]:
            results = list_auth()
        elif parts[0] == "status" and len(parts) <= 2:
            results = [auth_status(parts[1] if len(parts) == 2 else None)]
        else:
            return ["Usage: /auth [list | status [PROVIDER]]. Use /connect to sign in."]
        lines = []
        for item in results:
            # Explicit allowlist: never render raw records, tokens or endpoints.
            lines.append(f"{item.get('provider', 'unknown')}: {item.get('status', 'unknown')} · {item.get('method', 'unknown')} · {item.get('source', 'none')}")
            if item.get("message"):
                lines.append(str(item["message"]))
        return lines or ["No configured authentication. Use /connect."]
    if command == "/lint":
        from djcode.maintenance import run_lint
        result = run_lint(argument or None)
        lines = [result["summary"]]
        for item in result.get("diagnostics", [])[:30]:
            row = item.get("location", {})
            lines.append(f"{item.get('filename', '?')}:{row.get('row', '?')}:{row.get('column', '?')}: {item.get('code', '?')} {item.get('message', '')}")
        if result.get("detail"):
            lines.append(result["detail"])
        return lines
    if command == "/check":
        from djcode.maintenance import run_checks
        result = run_checks()
        return [result["summary"], *[f"{item['name']}: {item['status']} · {item['detail']}" for item in result.get("checks", [])]]
    if command == "/update":
        from djcode.updater import get_update_status, perform_update
        if argument in {"", "status", "check"}:
            result = get_update_status(check=argument == "check")
            lines = [result["message"]]
            install = result.get("installation", {})
            lines.append(f"Installation: {install.get('kind', 'unknown')} · current {result.get('current_version', 'unknown')}")
            if result.get("latest_version"):
                lines.append(f"Latest reported version: {result['latest_version']}")
            if result.get("update_available"):
                lines.append("Use /update install to explicitly install, or the documented installation command.")
            return lines
        if argument == "install":
            result = perform_update(force=True)
            return [result["message"], *(["Restart DJcode after your current work to use the update."] if result.get("updated") else [])]
        return ["Usage: /update [status | check | install]. Status does not install."]
    raise ValueError("Unsupported terminal operation")
