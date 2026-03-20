"""File write tool — create or overwrite files."""

from __future__ import annotations

from pathlib import Path


async def execute_file_write(path: str, content: str) -> str:
    """Write content to a file, creating parent directories as needed."""
    try:
        p = Path(path).expanduser().resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        lines = content.count("\n") + 1
        return f"Wrote {lines} lines to {p}"
    except Exception as e:
        return f"Error writing {path}: {e}"
