"""File edit tool — surgical string replacement in files."""

from __future__ import annotations

from pathlib import Path


async def execute_file_edit(path: str, old_string: str, new_string: str) -> str:
    """Replace old_string with new_string in a file. The old_string must be unique."""
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return f"Error: File not found: {path}"

        content = p.read_text(encoding="utf-8")

        count = content.count(old_string)
        if count == 0:
            return f"Error: old_string not found in {path}"
        if count > 1:
            return (
                f"Error: old_string found {count} times in {path}. "
                "Provide more context to make it unique."
            )

        new_content = content.replace(old_string, new_string, 1)
        p.write_text(new_content, encoding="utf-8")

        return f"Edited {p}: replaced 1 occurrence"

    except Exception as e:
        return f"Error editing {path}: {e}"
