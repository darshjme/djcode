"""Status bar renderer for DJcode.

Shows model, provider, token estimate, working directory, and quick help.
"""

from __future__ import annotations

import os

from rich.console import Console
from rich.text import Text

GOLD = "#FFD700"

console = Console()


def render_status_bar(
    model: str,
    provider: str,
    token_count: int = 0,
    auto_accept: bool = False,
) -> None:
    """Print a styled status bar line."""
    cwd = os.getcwd()
    home = os.path.expanduser("~")
    if cwd.startswith(home):
        cwd = "~" + cwd[len(home):]

    # Format token count
    if token_count >= 1000:
        tokens_str = f"{token_count / 1000:.1f}K"
    else:
        tokens_str = str(token_count)

    parts = [
        model,
        provider,
        f"{tokens_str} tokens",
        cwd,
    ]
    if auto_accept:
        parts.append("auto-accept: ON")
    parts.append("/help")

    bar_text = " | ".join(parts)

    console.print()
    console.print(
        Text(f"--- {bar_text} ---", style=f"dim {GOLD}"),
    )
