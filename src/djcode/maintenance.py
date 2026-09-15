"""Bounded installation checks used by the CLI, TUI and staged updater."""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path


def run_checks() -> dict:
    package = Path(__file__).resolve().parent
    checks = []
    try:
        files = list(package.rglob("*.py"))
        for path in files:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        checks.append({"name": "Python syntax", "status": "passed", "detail": f"{len(files)} modules"})
    except (SyntaxError, OSError, UnicodeError) as error:
        checks.append({"name": "Python syntax", "status": "failed", "detail": str(error)})
    try:
        from djcode.agents.registry import AGENT_SPECS
        from djcode.auth import PROVIDERS
        from djcode.provider import TOOL_DEFINITIONS
        names = [tool["function"]["name"] for tool in TOOL_DEFINITIONS]
        assert len(names) == len(set(names)), "Duplicate tool definitions"
        assert PROVIDERS and AGENT_SPECS and names, "Empty runtime registry"
        json.dumps(TOOL_DEFINITIONS)
        checks.append({"name": "Runtime registries", "status": "passed", "detail": f"{len(PROVIDERS)} providers, {len(names)} tools"})
    except Exception as error:
        checks.append({"name": "Runtime registries", "status": "failed", "detail": str(error)})
    try:
        result = subprocess.run([sys.executable, "-m", "ruff", "check", "--no-cache", "--select", "E9,F63,F7,F82",
                                 "--output-format", "concise", str(package)], capture_output=True, text=True, timeout=30)
        checks.append({"name": "Fatal lint", "status": "passed" if result.returncode == 0 else "failed",
                       "detail": (result.stdout or result.stderr)[-3000:].strip()})
    except (OSError, subprocess.TimeoutExpired) as error:
        checks.append({"name": "Fatal lint", "status": "failed", "detail": str(error)})
    ok = all(check["status"] == "passed" for check in checks)
    return {"ok": ok, "summary": "Installation checks passed." if ok else "Installation checks failed.", "checks": checks}
