"""Grep tool — search file contents with regex."""

from __future__ import annotations

import asyncio


async def execute_grep(
    pattern: str,
    path: str = ".",
    include: str | None = None,
) -> str:
    """Search for a regex pattern in files using ripgrep or grep."""
    # Prefer rg (ripgrep) if available, fall back to grep
    cmd_parts = ["rg", "--no-heading", "--line-number", "--color=never"]

    if include:
        cmd_parts.extend(["--glob", include])

    cmd_parts.extend(["--max-count=200", pattern, path])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd_parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except FileNotFoundError:
        # ripgrep not found, fall back to grep -rn
        cmd_parts = ["grep", "-rn", "--color=never"]
        if include:
            cmd_parts.extend(["--include", include])
        cmd_parts.extend([pattern, path])

        proc = await asyncio.create_subprocess_exec(
            *cmd_parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        return "Search timed out after 30s"

    result = stdout.decode("utf-8", errors="replace").strip()
    if not result:
        return "No matches found."

    # Cap output
    lines = result.splitlines()
    if len(lines) > 200:
        lines = lines[:200]
        lines.append(f"... ({len(lines)} matches shown, more truncated)")

    return "\n".join(lines)
