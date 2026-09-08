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

    from djcode.tools.bash import run_process
    try:
        arguments = shlex.split(subcommand)
        if not arguments or arguments[0].startswith("-"):
            return "Error: a git subcommand is required (global options are not accepted)"
        return await run_process("git", "--no-pager", *arguments, timeout=30, output_limit=30_000)
    except Exception as exc:
        return f"Error: {exc}"
