"""File read tool — read files from the local filesystem."""

from __future__ import annotations

from pathlib import Path


async def execute_file_read(
    path: str,
    offset: int = 0,
    limit: int = 2000,
) -> str:
    """Read a file, returning numbered lines."""
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return f"Error: File not found: {path}"
        if not p.is_file():
            return f"Error: Not a file: {path}"

        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()

        start = max(0, offset)
        end = start + limit
        selected = lines[start:end]

        numbered = []
        for i, line in enumerate(selected, start=start + 1):
            numbered.append(f"{i:>6}\t{line}")

        return "\n".join(numbered) if numbered else "(empty file)"

    except Exception as e:
        return f"Error reading {path}: {e}"
