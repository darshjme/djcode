"""DJcode tool system — local-first file, shell, and git operations."""

from __future__ import annotations

from typing import Any

from djcode.tools.bash import execute_bash
from djcode.tools.file_edit import execute_file_edit
from djcode.tools.file_read import execute_file_read
from djcode.tools.file_write import execute_file_write
from djcode.tools.git import execute_git
from djcode.tools.glob import execute_glob
from djcode.tools.grep import execute_grep
from djcode.tools.web_fetch import execute_web_fetch

# Central dispatch table
TOOL_DISPATCH: dict[str, Any] = {
    "bash": execute_bash,
    "file_read": execute_file_read,
    "file_write": execute_file_write,
    "file_edit": execute_file_edit,
    "grep": execute_grep,
    "glob": execute_glob,
    "git": execute_git,
    "web_fetch": execute_web_fetch,
}


async def dispatch_tool(name: str, arguments: dict[str, Any]) -> str:
    """Execute a tool by name with the given arguments. Returns result as string."""
    handler = TOOL_DISPATCH.get(name)
    if handler is None:
        return f"Error: Unknown tool '{name}'"
    try:
        result = await handler(**arguments)
        return str(result)
    except Exception as e:
        return f"Error executing {name}: {e}"
