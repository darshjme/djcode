"""Glob tool — find files by pattern."""

from __future__ import annotations

from pathlib import Path


async def execute_glob(pattern: str, path: str = ".") -> str:
    """Find files matching a glob pattern, sorted by modification time."""
    try:
        base = Path(path).expanduser().resolve()
        if not base.exists():
            return f"Error: Path not found: {path}"

        matches = sorted(base.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)

        if not matches:
            return f"No files matching '{pattern}' in {base}"

        # Cap at 500 results
        results = []
        for i, m in enumerate(matches[:500]):
            try:
                results.append(str(m))
            except OSError:
                continue

        if len(matches) > 500:
            results.append(f"... ({len(matches)} total matches, showing first 500)")

        return "\n".join(results)

    except Exception as e:
        return f"Error: {e}"
