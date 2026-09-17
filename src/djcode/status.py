"""Status bar for DJcode.

Provides both a fixed bottom toolbar for prompt_toolkit and a Rich fallback.
The fixed toolbar stays pinned at the terminal bottom at all times.

Three changes in W9, all of them things a user can see:

* The hardcoded ``bg="#111111"`` is gone. It painted a near-black strip across
  the bottom of a light terminal and there was no way to turn it off.
* There is a real context meter. ``ContextStats.utilization_pct`` has existed
  since W1 and no surface read it, so the bar showed ``2.4k tokens`` with no
  idea whether that filled an 8k window or 2% of a 200k one.
* Segments are **dropped** from the end rather than truncated when the terminal
  is narrow, because a bottom toolbar that wraps pushes the prompt off-screen.
"""

from __future__ import annotations

import os
from html import escape

from prompt_toolkit.formatted_text import HTML

from djcode.frontends.repl import theme

_PALETTE = theme.build()
GOLD = _PALETTE.style("dj.accent")
_DIM = _PALETTE.style("dj.dim")
_GLYPHS = _PALETTE.glyphs


def _shorten_cwd() -> str:
    """Get a shortened display of the current working directory."""
    cwd = os.getcwd()
    home = os.path.expanduser("~")
    if cwd.startswith(home):
        return "~" + cwd[len(home) :]
    return cwd


def _format_tokens(count: int) -> str:
    """Format token count for display."""
    if count >= 1000:
        return f"{count / 1000:.1f}K"
    return str(count)


class StatusBar:
    """Manages the fixed bottom toolbar state for prompt_toolkit.

    Usage with PromptSession:
        status = StatusBar()
        session = PromptSession(bottom_toolbar=status.render)
    """

    def __init__(self) -> None:
        self.model: str = ""
        self.provider: str = ""
        self.token_count: int = 0
        # W9 / P1-6: `token_count` used to be len(text)//4 with nothing saying
        # so. It is now the provider's own input count when the provider
        # reported one, and this flag is what puts the `~` back on the display
        # when it did not. Defaulting to True means a caller that has not been
        # taught the difference still renders an honest estimate.
        self.tokens_estimated: bool = True
        self.auto_accept: bool = False
        self.uncensored: bool = False
        self.mode: str = "ACT"
        #: Context-window utilisation, 0.0-1.0, or None when unknown. `None` is
        #: not "0%" -- an empty meter and an unknown meter are different facts,
        #: and the bar shows nothing rather than claiming the window is empty.
        self.context_fraction: float | None = None

    def update(
        self,
        *,
        model: str | None = None,
        provider: str | None = None,
        token_count: int | None = None,
        tokens_estimated: bool | None = None,
        auto_accept: bool | None = None,
        uncensored: bool | None = None,
        mode: str | None = None,
        context_fraction: float | None = None,
    ) -> None:
        """Update status bar values."""
        if model is not None:
            self.model = model
        if provider is not None:
            self.provider = provider
        if token_count is not None:
            self.token_count = token_count
        if tokens_estimated is not None:
            self.tokens_estimated = tokens_estimated
        if auto_accept is not None:
            self.auto_accept = auto_accept
        if uncensored is not None:
            self.uncensored = uncensored
        if mode is not None:
            self.mode = mode
        if context_fraction is not None:
            self.context_fraction = context_fraction

    def context_segment(self) -> str:
        """``▰▰▰▱▱▱▱▱▱▱ 31%`` -- or nothing at all when nobody has told us."""
        if self.context_fraction is None:
            return ""
        fraction = max(0.0, min(1.0, self.context_fraction))
        bar = theme.meter(fraction, width=10, glyph_map=_GLYPHS)
        return f"{bar} {fraction * 100:.0f}%"

    def segments(self) -> list[tuple[str, str]]:
        """``(style, text)`` pairs, most important first.

        Ordered so `render` can drop from the END when the terminal is narrow:
        the model you are talking to and the mode you are in survive a
        60-column window; the "Tab complete" hint does not.
        """
        tokens = ("~" if self.tokens_estimated else "") + _format_tokens(self.token_count)
        items: list[tuple[str, str]] = [
            ("brand", "DJcode"),
            ("mode", self.mode),
            ("strong", self.model or "no model"),
            ("dim", self.provider),
            ("dim", f"{_GLYPHS['down']} {tokens} tokens"),
        ]
        meter = self.context_segment()
        if meter:
            items.append(("dim", meter))
        items.append(("dim", _shorten_cwd()))
        items.append(("strong", f"Approvals: {'auto' if self.auto_accept else 'ask'}"))
        items.append(("dim", f"/help {_GLYPHS['sep']} Tab complete"))
        return items

    def _styled(self, style: str, text: str) -> str:
        safe = escape(text)
        if style == "brand":
            return f'<b><style fg="{GOLD}">{safe}</style></b>'
        if style == "mode":
            colour = _PALETTE.style("dj.plan" if self.mode == "PLAN" else "dj.ok")
            body = f"<b>{safe}</b>" if self.mode == "PLAN" else safe
            return f'<style fg="{colour}">{body}</style>'
        if style == "strong":
            return f'<style fg="#AAAAAA">{safe}</style>'
        return f'<style fg="{_DIM}">{safe}</style>'

    def render(self, width: int | None = None) -> HTML:
        """The bottom toolbar, fitted to the terminal."""
        if width is None:
            try:
                width = os.get_terminal_size().columns
            except OSError:
                width = 200

        sep_glyph = _GLYPHS["sep"]
        bullet = _GLYPHS["bullet"]
        items = self.segments()
        while len(items) > 2:
            plain = f"{bullet} " + f" {sep_glyph} ".join(text for _, text in items)
            if len(plain) <= max(20, width - 1):
                break
            items.pop()

        sep = f' <style fg="{_DIM}">{escape(sep_glyph)}</style> '
        body = sep.join(self._styled(style, text) for style, text in items)
        return HTML(f'<style fg="{GOLD}">{escape(bullet)}</style> {body}')


def render_status_bar(
    model: str,
    provider: str,
    token_count: int = 0,
    auto_accept: bool = False,
) -> None:
    """Legacy Rich-based inline status bar (kept for fallback/oneshot mode)."""
    from rich.text import Text

    console, _ = theme.make_console()
    cwd = _shorten_cwd()
    tokens_str = _format_tokens(token_count)

    parts = [model, provider, f"{tokens_str} tokens", cwd]
    if auto_accept:
        parts.append("auto-accept: ON")
    parts.append("/help")

    bar_text = " | ".join(parts)
    console.print()
    console.print(Text(f"--- {bar_text} ---", style=f"dim {GOLD}"))
