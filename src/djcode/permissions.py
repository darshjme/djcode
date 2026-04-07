"""Permission system for DJcode — access control and safety warnings.

Shows clear warnings about what DJcode can do in the current directory.
Requires explicit permission for sensitive operations.
Tracks granted permissions per session.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

GOLD = "#FFD700"

console = Console()


# ── Permission levels ──────────────────────────────────────────────────────

class PermissionLevel:
    """Permission levels for DJcode operations."""
    READ = "read"           # Read files, grep, glob
    WRITE = "write"         # Write/edit files
    EXECUTE = "execute"     # Run shell commands
    GIT = "git"             # Git operations (commit, push)
    SYSTEM = "system"       # System modifications (install packages)
    NETWORK = "network"     # Network requests (web_fetch, APIs)


# Operations and their required permission levels
OPERATION_PERMISSIONS: dict[str, str] = {
    "file_read": PermissionLevel.READ,
    "grep": PermissionLevel.READ,
    "glob": PermissionLevel.READ,
    "file_write": PermissionLevel.WRITE,
    "file_edit": PermissionLevel.WRITE,
    "bash": PermissionLevel.EXECUTE,
    "git": PermissionLevel.GIT,
    "web_fetch": PermissionLevel.NETWORK,
}

# Dangerous patterns in bash commands
DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    ("rm -rf", "Recursive force delete — could destroy files permanently"),
    ("rm -r /", "Attempting to delete root filesystem"),
    ("sudo ", "Elevated privileges — could modify system files"),
    ("chmod 777", "Making files world-writable — security risk"),
    ("dd if=", "Raw disk write — could destroy data"),
    ("> /dev/", "Writing to device files"),
    ("mkfs", "Formatting filesystem"),
    (":(){ :|:& };:", "Fork bomb — will crash the system"),
    ("curl | bash", "Piping remote code to shell — security risk"),
    ("wget | sh", "Piping remote code to shell — security risk"),
    ("npm install -g", "Global package install — modifies system"),
    ("pip install", "Python package install — modifies environment"),
    ("brew install", "Homebrew install — modifies system"),
    ("apt install", "Package install — modifies system"),
    ("systemctl", "System service management"),
    ("launchctl", "macOS service management"),
]


class PermissionManager:
    """Manages access permissions for the current session."""

    def __init__(self, auto_accept: bool = False) -> None:
        self.auto_accept = auto_accept
        self._granted: set[str] = set()  # Granted permission levels
        self._denied_ops: set[str] = set()  # Specifically denied operations
        self._cwd = os.getcwd()

    def show_startup_warning(self) -> None:
        """Display the startup permission warning about what DJcode can do."""
        cwd = os.getcwd()
        home = os.path.expanduser("~")
        cwd_display = "~" + cwd[len(home):] if cwd.startswith(home) else cwd

        # Check what's writable
        is_writable = os.access(cwd, os.W_OK)
        is_home = cwd == home
        is_system = cwd.startswith("/etc") or cwd.startswith("/usr") or cwd.startswith("/System")

        warning_level = "normal"
        if is_system:
            warning_level = "critical"
        elif is_home:
            warning_level = "elevated"

        # Build warning
        lines: list[str] = []

        if warning_level == "critical":
            lines.append(f"[bold red]WARNING: System directory[/]")
            lines.append(f"DJcode has access to [bold]{cwd_display}[/]")
            lines.append(f"Modifications here can break your system.")
            border = "red"
        elif warning_level == "elevated":
            lines.append(f"[bold yellow]NOTICE: Home directory[/]")
            lines.append(f"DJcode has access to [bold]{cwd_display}[/]")
            lines.append(f"Be careful with file operations.")
            border = "yellow"
        else:
            lines.append(f"[{GOLD}]Folder access: [bold]{cwd_display}[/]")
            if is_writable:
                lines.append(f"DJcode can [green]read[/], [yellow]write[/], and [red]execute[/] in this directory.")
            else:
                lines.append(f"DJcode can [green]read[/] this directory (write access denied).")
            border = GOLD

        lines.append("")
        lines.append("[dim]DJcode can:[/]")
        lines.append("  [green]\u2713[/] Read any file in this directory tree")
        if is_writable:
            lines.append("  [yellow]\u2713[/] Create, edit, and delete files")
        lines.append("  [red]\u2713[/] Execute shell commands")
        lines.append("  [yellow]\u2713[/] Run git operations")
        lines.append("  [dim]\u2713[/] Fetch URLs from the internet")
        lines.append("")

        if self.auto_accept:
            lines.append("[bold yellow]Auto-accept is ON[/] — tools execute without confirmation.")
        else:
            lines.append("[dim]Tool execution requires your approval. Use /auto to toggle.[/]")

        console.print(Panel(
            "\n".join(lines),
            title=f"[bold {GOLD}]DJcode Access[/]",
            border_style=border,
            padding=(0, 2),
        ))

    def check_dangerous_command(self, command: str) -> str | None:
        """Check if a bash command is dangerous. Returns warning message or None."""
        cmd_lower = command.lower().strip()
        for pattern, description in DANGEROUS_PATTERNS:
            if pattern in cmd_lower:
                return f"[bold red]DANGER:[/] {description}\n[dim]Command: {command[:100]}[/]"
        return None

    def grant(self, level: str) -> None:
        """Grant a permission level for this session."""
        self._granted.add(level)

    def is_granted(self, level: str) -> bool:
        """Check if a permission level has been granted."""
        if self.auto_accept:
            return True
        return level in self._granted

    def check_tool_permission(self, tool_name: str) -> bool:
        """Check if a tool is allowed. Returns True if allowed."""
        required = OPERATION_PERMISSIONS.get(tool_name)
        if not required:
            return True  # Unknown tool, allow by default
        if self.auto_accept:
            return True
        return required in self._granted


def format_access_request(tool_name: str, args: dict) -> str:
    """Format a tool access request for user display."""
    icon = {
        "file_read": "\U0001f4c4 Read",
        "file_write": "\u270f Write",
        "file_edit": "\u2702 Edit",
        "bash": "\u2699 Execute",
        "grep": "\U0001f50d Search",
        "glob": "\U0001f4c2 Find",
        "git": "\U0001f500 Git",
        "web_fetch": "\U0001f310 Fetch",
    }.get(tool_name, f"\u26a1 {tool_name}")

    if tool_name == "bash":
        cmd = args.get("command", "")[:80]
        return f"{icon}: [white]{cmd}[/]"
    elif tool_name in ("file_read", "file_write", "file_edit"):
        path = args.get("path", "")
        return f"{icon}: [white]{path}[/]"
    elif tool_name == "grep":
        return f"{icon}: [white]{args.get('pattern', '')}[/]"
    elif tool_name == "glob":
        return f"{icon}: [white]{args.get('pattern', '')}[/]"
    elif tool_name == "git":
        return f"{icon}: [white]{args.get('subcommand', '')}[/]"
    elif tool_name == "web_fetch":
        return f"{icon}: [white]{args.get('url', '')[:60]}[/]"
    return f"{icon}"
