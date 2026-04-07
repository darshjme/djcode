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

    def update(
        self,
        *,
        model: str | None = None,
        provider: str | None = None,
        token_count: int | None = None,
        auto_accept: bool | None = None,
        uncensored: bool | None = None,
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

    def render(self) -> HTML:
        """Render the bottom toolbar as prompt_toolkit HTML.

        This is called by prompt_toolkit on every keypress to keep the
        toolbar up to date. It must return an HTML fragment.
        """
        emoji = self.buddy.emoji
        name = self.buddy.name
        mood = self.buddy.mood
        # Mood indicators
        mood_icons = {
            "idle": "",
            "thinking": " \u23f3",
            "success": " \u2713",
            "error": " \u2717",
        }
        mood_suffix = mood_icons.get(mood, "")
        cwd = _shorten_cwd()
        tokens = _format_tokens(self.token_count)

        model_display = self.model or "no model"
        if self.uncensored:
            model_display += " \U0001f513"  # unlocked padlock

        auto_str = "on" if self.auto_accept else "off"

        return HTML(
            f'<b><style fg="{GOLD}">{emoji} {name}{mood_suffix}</style></b>'
            f' <style fg="#666666">\u2502</style> '
            f'<style fg="#FFFFFF">{model_display}</style>'
            f' <style fg="#666666">\u2502</style> '
            f'<style fg="#888888">{self.provider}</style>'
            f' <style fg="#666666">\u2502</style> '
            f'<style fg="#888888">{tokens} tokens</style>'
            f' <style fg="#666666">\u2502</style> '
            f'<style fg="#888888">{cwd}</style>'
            f' <style fg="#666666">\u2502</style> '
            f'<style fg="#888888">auto: {auto_str}</style>'
            f' <style fg="#666666">\u2502</style> '
            f'<style fg="#666666">/help</style>'
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
