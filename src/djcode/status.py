"""Status bar for DJcode.

Provides both a fixed bottom toolbar for prompt_toolkit and a Rich fallback.
The fixed toolbar stays pinned at the terminal bottom at all times.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from prompt_toolkit.formatted_text import HTML

if TYPE_CHECKING:
    from djcode.buddy import Buddy

GOLD = "#FFD700"


def _shorten_cwd() -> str:
    """Get a shortened display of the current working directory."""
    cwd = os.getcwd()
    home = os.path.expanduser("~")
    if cwd.startswith(home):
        return "~" + cwd[len(home):]
    return cwd


def _format_tokens(count: int) -> str:
    """Format token count for display."""
    if count >= 1000:
        return f"{count / 1000:.1f}K"
    return str(count)


class StatusBar:
    """Manages the fixed bottom toolbar state for prompt_toolkit.

    Usage with PromptSession:
        status = StatusBar(buddy)
        session = PromptSession(bottom_toolbar=status.render)
    """

    def __init__(self, buddy: Buddy) -> None:
        self.buddy = buddy
        self.model: str = ""
        self.provider: str = ""
        self.token_count: int = 0
        self.auto_accept: bool = False
        self.uncensored: bool = False
        self.mode: str = "ACT"

    def update(
        self,
        *,
        model: str | None = None,
        provider: str | None = None,
        token_count: int | None = None,
        auto_accept: bool | None = None,
        uncensored: bool | None = None,
        mode: str | None = None,
    ) -> None:
        """Update status bar values."""
        if model is not None:
            self.model = model
        if provider is not None:
            self.provider = provider
        if token_count is not None:
            self.token_count = token_count
        if auto_accept is not None:
            self.auto_accept = auto_accept
        if uncensored is not None:
            self.uncensored = uncensored
        if mode is not None:
            self.mode = mode

    def render(self) -> HTML:
        """Render the bottom toolbar as prompt_toolkit HTML.

        Clean single line: ⏺ Mitra · ACT · gemma4 · ollama · ↓ 2.4k tokens · ~/project · Ctrl+? help
        """
        name = self.buddy.name
        cwd = _shorten_cwd()
        tokens = _format_tokens(self.token_count)

        model_display = self.model or "no model"

        # Mode indicator: PLAN highlighted magenta, ACT normal
        mode = self.mode
        if mode == "PLAN":
            mode_segment = f'<style fg="#FF00FF"><b>PLAN</b></style>'
        else:
            mode_segment = f'<style fg="#00FF00">ACT</style>'

        sep = ' <style fg="#444444">\u00b7</style> '

        return HTML(
            f'<style fg="{GOLD}">\u23fa</style> '
            f'<b><style fg="{GOLD}">{name}</style></b>'
            f'{sep}'
            f'{mode_segment}'
            f'{sep}'
            f'<style fg="#AAAAAA">{model_display}</style>'
            f'{sep}'
            f'<style fg="#666666">{self.provider}</style>'
            f'{sep}'
            f'<style fg="#666666">\u2193 {tokens} tokens</style>'
            f'{sep}'
            f'<style fg="#555555">{cwd}</style>'
            f'{sep}'
            f'<style fg="#444444">Ctrl+? help</style>'
        )


def render_status_bar(
    model: str,
    provider: str,
    token_count: int = 0,
    auto_accept: bool = False,
) -> None:
    """Legacy Rich-based inline status bar (kept for fallback/oneshot mode)."""
    from rich.console import Console
    from rich.text import Text

    console = Console()
    cwd = _shorten_cwd()
    tokens_str = _format_tokens(token_count)

    parts = [model, provider, f"{tokens_str} tokens", cwd]
    if auto_accept:
        parts.append("auto-accept: ON")
    parts.append("/help")

    bar_text = " | ".join(parts)
    console.print()
    console.print(Text(f"--- {bar_text} ---", style=f"dim {GOLD}"))
