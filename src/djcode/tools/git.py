"""Git tool — run git subcommands."""

from __future__ import annotations

import asyncio
import shlex


# Commands that are safe to run without confirmation
SAFE_SUBCOMMANDS = {
    "status", "diff", "log", "show", "branch", "remote", "tag",
    "stash list", "blame", "shortlog", "reflog",
}

# Commands that modify state but are generally safe
MODIFY_SUBCOMMANDS = {
    "add", "commit", "stash", "stash pop", "stash drop",
    "checkout", "switch", "restore", "merge", "rebase",
    "pull", "fetch", "push", "cherry-pick",
}

# Dangerous commands that need extra care
DANGEROUS_PATTERNS = {"reset --hard", "push --force", "push -f", "clean -f", "branch -D"}


async def execute_git(subcommand: str) -> str:
    """Execute a git subcommand and return the output."""
    # Check for dangerous patterns
    for dangerous in DANGEROUS_PATTERNS:
        if dangerous in subcommand:
            return (
                f"Warning: '{subcommand}' is a destructive operation. "
                "Use bash tool directly if you really need this."
            )

    cmd = f"git {subcommand}"
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        output_parts = []
        if stdout:
            output_parts.append(stdout.decode("utf-8", errors="replace"))
        if stderr and proc.returncode != 0:
            output_parts.append(stderr.decode("utf-8", errors="replace"))

        result = "\n".join(output_parts).strip()

        if proc.returncode != 0:
            result = f"[git exit code {proc.returncode}]\n{result}"

        # Cap output
        if len(result) > 30_000:
            result = result[:30_000] + "\n... (output truncated)"

        return result or "(no output)"

    except asyncio.TimeoutError:
        return "Git command timed out after 30s"
    except Exception as e:
        return f"Error: {e}"
