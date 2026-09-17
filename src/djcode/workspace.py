"""Shared workspace discovery for the Textual app and line-oriented REPL."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

FEATURES = (
    ("Project studio", "/agent · /organisation · /flow; custom teams and dependency workflows"),
    ("Roadmap and evidence", "/roadmap · /timeline · /finish; recorded checks govern completion"),
    ("Doctor", "/doctor verifies memory restart and a real DAF/DDAL roundtrip"),
    ("Project tools", "/project · ask to read/edit files, run commands or inspect a diff"),
    ("Intent context", "Eight intent modes; files and git context enrich task prompts"),
    ("Specialists", "/scout · /architect · /build · /orchestra · /test · /agents"),
    ("Persistent memory", "/memory · /remember key=value · /recall key · /history · /resume ID"),
    (
        "Semantic retrieval",
        "Requires explicitly configured embeddings; lexical recall works offline",
    ),
    (
        "Local and hosted models",
        "/connect · /models · /provider; Ollama and MLX need a local server",
    ),
    ("Seven design packs", "/design lists bundled guidance; /design ID selects a reference"),
    ("Reviewable changes", "/project shows git changes; tool approval starts enabled"),
    ("DAF / DDAL", "/workflow; Rust graph execution and binary transport"),
    (
        "Vyasa fleet",
        '/fleet lists authenticated specialists; /fleet {"text":"…","employee":"…","session":"…"}',
    ),
    (
        "Local storage",
        "Sessions and facts stay local; hosted requests send selected context; no usage analytics",
    ),
)


def feature_help() -> str:
    return "DJcode / WORKSPACE\n\n" + "\n\n".join(
        f"{title}\n  {detail}" for title, detail in FEATURES
    )


def project_context() -> str:
    """Inspect actual git state without shell interpolation or provider access."""
    cwd = Path.cwd()
    lines = [f"PROJECT CONTEXT\n{cwd}"]
    for args in (("branch", "--show-current"), ("status", "--short"), ("diff", "--stat")):
        try:
            result = subprocess.run(
                ["git", "--no-optional-locks", *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            lines.append("Git inspection unavailable; check git installation or retry.")
            break
        if result.returncode:
            lines.append("No readable git repository in this workspace.")
            break
        output = result.stdout.strip()
        label = {"branch": "Branch", "status": "Changes", "diff": "Diff summary"}[args[0]]
        lines.append(
            f"{label}: {output[:12000] or ('detached HEAD' if args[0] == 'branch' else 'none')}"
        )
    return "\n\n".join(lines)


async def fleet_command(argument: str) -> str:
    """Explicit authenticated requests; no discovery or inference on UI mount."""
    from djcode.vyasa import request_fleet

    args = json.loads(argument) if argument.strip() else {}
    if not isinstance(args, dict) or set(args) - {"text", "employee", "session"}:
        raise ValueError(
            'Use /fleet or /fleet {"text":"request","employee":"name","session":"name"}'
        )
    if any(not isinstance(value, str) or not value.strip() for value in args.values()):
        raise ValueError("Fleet values must be non-empty strings")
    if args and "text" not in args:
        raise ValueError("A fleet chat requires text; use /fleet alone for the directory")
    response = await asyncio.to_thread(request_fleet, **args)
    return json.dumps(response, ensure_ascii=False, indent=2)
