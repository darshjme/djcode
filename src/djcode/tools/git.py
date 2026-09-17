"""Git tool — run git subcommands."""

from __future__ import annotations

import shlex

# Commands that are safe to run without confirmation
SAFE_SUBCOMMANDS = {
    "status",
    "diff",
    "log",
    "show",
    "branch",
    "remote",
    "tag",
    "stash list",
    "blame",
    "shortlog",
    "reflog",
}

# Commands that modify state but are generally safe
MODIFY_SUBCOMMANDS = {
    "add",
    "commit",
    "stash",
    "stash pop",
    "stash drop",
    "checkout",
    "switch",
    "restore",
    "merge",
    "rebase",
    "pull",
    "fetch",
    "push",
    "cherry-pick",
}

# Dangerous commands that need extra care
DANGEROUS_PATTERNS = {"reset --hard", "push --force", "push -f", "clean -f", "branch -D"}


async def execute_git(subcommand: str) -> str:
    """Execute a git subcommand and return the output."""
    # Check for dangerous patterns
    for dangerous in DANGEROUS_PATTERNS:
        if dangerous in subcommand:
            # W6: this used to end with "Use bash tool directly if you really
            # need this" -- a refusal that told the model how to route around
            # itself, into a tool that had no screening at all. The permission
            # engine now gates both paths, so the escape hatch is gone and the
            # text is a plain refusal. It also starts with "Error" now: the old
            # "Warning:" prefix made `workflow._result_ok` read a refusal as a
            # success and run every dependent node.
            return (
                f"Error: '{subcommand}' is a destructive git operation and was not run. "
                "Ask the user to perform it, or choose a non-destructive alternative."
            )

    from djcode.tools.bash import run_process

    try:
        arguments = shlex.split(subcommand)
        if not arguments or arguments[0].startswith("-"):
            return "Error: a git subcommand is required (global options are not accepted)"
        # No output_limit= here on purpose. It used to be 30_000 -- a second,
        # lower ceiling than bash's, so a real `git diff` or `git log` was cut
        # at 30 k with no way to recover the rest. The chokepoint's spill file
        # (W3-2) is the one policy now; run_process keeps only its memory bound.
        return await run_process("git", "--no-pager", *arguments, timeout=30)
    except Exception as exc:
        return f"Error: {exc}"
