"""Jupyter notebook tool — read and edit .ipynb files.

Parses the JSON structure of Jupyter notebooks to display cells
with their outputs, and allows editing cell content by index.

No external dependencies — uses stdlib json only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


async def execute_notebook_read(
    path: str,
    cell_index: int | None = None,
    cell_type: str | None = None,
    max_output_chars: int = 5000,
) -> str:
    """Read a Jupyter notebook and display cells with outputs.

    Args:
        path: Absolute path to the .ipynb file.
        cell_index: If specified, show only this cell (0-based index).
        cell_type: Filter by cell type: 'code', 'markdown', or 'raw'.
        max_output_chars: Maximum characters per cell output (default 5000).

    Returns:
        Formatted display of notebook cells with their content and outputs.
    """
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return f"Error: File not found: {path}"
        if not p.is_file():
            return f"Error: Not a file: {path}"
        if p.suffix.lower() != ".ipynb":
            return f"Error: Not a Jupyter notebook (expected .ipynb): {path}"

        text = p.read_text(encoding="utf-8")
        try:
            nb = json.loads(text)
        except json.JSONDecodeError as e:
            return f"Error: Invalid notebook JSON in {path}: {e}"

        # Validate notebook structure
        if not isinstance(nb, dict) or "cells" not in nb:
            return f"Error: Invalid notebook structure in {path} (missing 'cells' key)"

        cells = nb.get("cells", [])
        if not cells:
            return f"Notebook {p.name}: empty (0 cells)"

        # Get notebook metadata
        metadata = nb.get("metadata", {})
        kernel = metadata.get("kernelspec", {})
        kernel_name = kernel.get("display_name", kernel.get("name", "unknown"))
        nb_format = f"{nb.get('nbformat', '?')}.{nb.get('nbformat_minor', '?')}"

        # Filter by type
        valid_types = {"code", "markdown", "raw"}
        if cell_type:
            cell_type = cell_type.lower().strip()
            if cell_type not in valid_types:
                return f"Error: Invalid cell_type '{cell_type}'. Must be one of: {', '.join(sorted(valid_types))}"

        # Build output
        lines: list[str] = [
            f"Notebook: {p.name}",
            f"Kernel: {kernel_name} | Format: {nb_format} | Cells: {len(cells)}",
            "",
        ]

        # Cell type summary
        type_counts: dict[str, int] = {}
        for c in cells:
            ct = c.get("cell_type", "unknown")
            type_counts[ct] = type_counts.get(ct, 0) + 1
        counts_str = ", ".join(f"{k}: {v}" for k, v in sorted(type_counts.items()))
        lines.append(f"Cell types: {counts_str}")
        lines.append("")

        # Display cells
        indices = range(len(cells))
        if cell_index is not None:
            if cell_index < 0 or cell_index >= len(cells):
                return f"Error: Cell index {cell_index} out of range (0-{len(cells) - 1})"
            indices = [cell_index]

        for idx in indices:
            cell = cells[idx]
            ct = cell.get("cell_type", "unknown")

            if cell_type and ct != cell_type:
                continue

            lines.append(_format_cell(idx, cell, max_output_chars))
            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        return f"Error reading notebook {path}: {e}"


async def execute_notebook_edit(
    path: str,
    cell_index: int,
    new_source: str | None = None,
    cell_type: str | None = None,
    insert_before: bool = False,
    delete: bool = False,
) -> str:
    """Edit a Jupyter notebook cell.

    Args:
        path: Absolute path to the .ipynb file.
        cell_index: Index of the cell to edit (0-based).
        new_source: New cell content (replaces existing source).
        cell_type: Change cell type to 'code', 'markdown', or 'raw'.
        insert_before: If True, insert a new cell before cell_index instead of editing.
        delete: If True, delete the cell at cell_index.

    Returns:
        Confirmation message with the edited cell details.
    """
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return f"Error: File not found: {path}"
        if p.suffix.lower() != ".ipynb":
            return f"Error: Not a Jupyter notebook (expected .ipynb): {path}"

        text = p.read_text(encoding="utf-8")
        try:
            nb = json.loads(text)
        except json.JSONDecodeError as e:
            return f"Error: Invalid notebook JSON: {e}"

        if "cells" not in nb:
            return "Error: Invalid notebook structure (missing 'cells' key)"

        cells = nb["cells"]

        # Validate cell_index
        if delete:
            if cell_index < 0 or cell_index >= len(cells):
                return f"Error: Cell index {cell_index} out of range (0-{len(cells) - 1})"

            removed = cells.pop(cell_index)
            _write_notebook(p, nb)
            return (
                f"Deleted cell {cell_index} ({removed.get('cell_type', 'unknown')}) "
                f"from {p.name}. Now {len(cells)} cells."
            )

        if insert_before:
            if cell_index < 0 or cell_index > len(cells):
                return f"Error: Insert index {cell_index} out of range (0-{len(cells)})"

            new_type = (cell_type or "code").lower().strip()
            if new_type not in {"code", "markdown", "raw"}:
                return f"Error: Invalid cell_type '{new_type}'"

            new_cell = _make_cell(new_type, new_source or "")
            cells.insert(cell_index, new_cell)
            _write_notebook(p, nb)
            return (
                f"Inserted new {new_type} cell at index {cell_index} "
                f"in {p.name}. Now {len(cells)} cells."
            )

        # Edit existing cell
        if cell_index < 0 or cell_index >= len(cells):
            return f"Error: Cell index {cell_index} out of range (0-{len(cells) - 1})"

        if new_source is None and cell_type is None:
            return "Error: Provide new_source and/or cell_type to edit"

        cell = cells[cell_index]
        changes: list[str] = []

        if new_source is not None:
            # Notebook stores source as list of lines
            if isinstance(new_source, str):
                cell["source"] = _string_to_source_lines(new_source)
            changes.append("source updated")

            # Clear outputs when code changes
            if cell.get("cell_type") == "code":
                cell["outputs"] = []
                cell["execution_count"] = None
                changes.append("outputs cleared")

        if cell_type is not None:
            new_type = cell_type.lower().strip()
            if new_type not in {"code", "markdown", "raw"}:
                return f"Error: Invalid cell_type '{new_type}'"

            old_type = cell.get("cell_type", "unknown")
            cell["cell_type"] = new_type

            # Adjust cell structure for type change
            if new_type == "code" and "outputs" not in cell:
                cell["outputs"] = []
                cell["execution_count"] = None
            elif new_type != "code":
                cell.pop("outputs", None)
                cell.pop("execution_count", None)

            changes.append(f"type: {old_type} -> {new_type}")

        _write_notebook(p, nb)

        source_preview = _get_source_text(cell)
        if len(source_preview) > 200:
            source_preview = source_preview[:197] + "..."

        return (
            f"Edited cell {cell_index} in {p.name}:\n"
            f"  Changes: {', '.join(changes)}\n"
            f"  Type: {cell.get('cell_type', 'unknown')}\n"
            f"  Content preview: {source_preview}"
        )

    except Exception as e:
        return f"Error editing notebook {path}: {e}"


def _format_cell(idx: int, cell: dict[str, Any], max_output_chars: int) -> str:
    """Format a single cell for display."""
    ct = cell.get("cell_type", "unknown")
    exec_count = cell.get("execution_count")

    # Header
    header_parts = [f"--- Cell {idx}"]
    header_parts.append(f"[{ct}]")
    if exec_count is not None:
        header_parts.append(f"In [{exec_count}]")
    header = " ".join(header_parts) + " ---"

    lines = [header]

    # Source content
    source = _get_source_text(cell)
    if source:
        # Number source lines for code cells
        if ct == "code":
            src_lines = source.split("\n")
            for i, line in enumerate(src_lines, 1):
                lines.append(f"  {i:>4} | {line}")
        else:
            for line in source.split("\n"):
                lines.append(f"  {line}")
    else:
        lines.append("  (empty cell)")

    # Outputs (code cells only)
    outputs = cell.get("outputs", [])
    if outputs:
        lines.append("")
        lines.append("  Output:")
        total_chars = 0

        for out in outputs:
            output_type = out.get("output_type", "unknown")

            if output_type == "stream":
                text = "".join(out.get("text", []))
                stream_name = out.get("name", "stdout")
                text = _truncate(text, max_output_chars - total_chars)
                total_chars += len(text)
                lines.append(f"  [{stream_name}] {text}")

            elif output_type == "execute_result":
                data = out.get("data", {})
                text = _extract_output_text(data)
                text = _truncate(text, max_output_chars - total_chars)
                total_chars += len(text)
                lines.append(f"  => {text}")

            elif output_type == "error":
                ename = out.get("ename", "Error")
                evalue = out.get("evalue", "")
                traceback_lines = out.get("traceback", [])
                error_text = f"{ename}: {evalue}"
                if traceback_lines:
                    # Strip ANSI codes from traceback
                    import re
                    tb = "\n".join(traceback_lines)
                    tb = re.sub(r"\x1b\[[0-9;]*m", "", tb)
                    error_text = _truncate(tb, max_output_chars - total_chars)
                total_chars += len(error_text)
                lines.append(f"  [ERROR] {error_text}")

            elif output_type == "display_data":
                data = out.get("data", {})
                if "text/plain" in data:
                    text = "".join(data["text/plain"])
                    text = _truncate(text, max_output_chars - total_chars)
                    total_chars += len(text)
                    lines.append(f"  [display] {text}")
                if "image/png" in data:
                    lines.append("  [display] <image/png embedded>")
                if "text/html" in data:
                    lines.append("  [display] <text/html embedded>")

            if total_chars >= max_output_chars:
                lines.append("  ... (output truncated)")
                break

    return "\n".join(lines)


def _get_source_text(cell: dict[str, Any]) -> str:
    """Extract source text from a cell, handling both string and list formats."""
    source = cell.get("source", "")
    if isinstance(source, list):
        return "".join(source)
    return str(source)


def _extract_output_text(data: dict[str, Any]) -> str:
    """Extract text representation from output data dict."""
    # Prefer text/plain, then text/html (stripped), then first available
    if "text/plain" in data:
        text = data["text/plain"]
        if isinstance(text, list):
            return "".join(text)
        return str(text)

    if "text/html" in data:
        import re
        html = data["text/html"]
        if isinstance(html, list):
            html = "".join(html)
        # Strip HTML tags for text display
        return re.sub(r"<[^>]+>", "", str(html)).strip()

    # Fall back to listing available MIME types
    if data:
        types = ", ".join(data.keys())
        return f"<{types}>"

    return "(no text output)"


def _truncate(text: str, max_chars: int) -> str:
    """Truncate text to max_chars."""
    if max_chars <= 0:
        return "..."
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


def _string_to_source_lines(text: str) -> list[str]:
    """Convert a string to notebook source format (list of lines with newlines)."""
    lines = text.split("\n")
    result: list[str] = []
    for i, line in enumerate(lines):
        if i < len(lines) - 1:
            result.append(line + "\n")
        else:
            # Last line: only add if non-empty
            if line:
                result.append(line)
    return result


def _make_cell(cell_type: str, source: str) -> dict[str, Any]:
    """Create a new notebook cell dict."""
    cell: dict[str, Any] = {
        "cell_type": cell_type,
        "metadata": {},
        "source": _string_to_source_lines(source),
    }
    if cell_type == "code":
        cell["outputs"] = []
        cell["execution_count"] = None
    return cell


def _write_notebook(path: Path, nb: dict[str, Any]) -> None:
    """Write notebook back to disk with proper formatting."""
    text = json.dumps(nb, indent=1, ensure_ascii=False)
    # Notebooks typically use single-space indent
    path.write_text(text + "\n", encoding="utf-8")
