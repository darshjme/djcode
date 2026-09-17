"""One palette, one set of glyphs, one console factory. Everything else imports this.

Three problems this module exists to solve, all of them visible today.

**`/set theme=light` did nothing.** `config.py` has carried a `theme` key set to
`"auto"` since W1-9 and nothing ever read it (GAP B20). :func:`detect` reads it
now, along with ``DJCODE_THEME`` and ``COLORFGBG``.

**There were thirteen golds.** ``#FFD700`` in seven modules, ``#C79B7A`` in six,
plus literals inline -- and ``frontends/repl/render.py`` managed to define
``#C79B7A`` at module level while printing ``#FFD700`` forty lines earlier. A
semantic name (``dj.accent``) cannot drift the way a hex literal does.

**A cp1252 console cannot print ``⏺``.** :func:`glyphs` falls back to ASCII for
the whole surface rather than each renderer inventing its own fallback.

Two constraints that decide the shape of this module, both measured rather than
assumed (`DESIGN-CLI.md` §9.1, audit 2 §2.5-2.6):

* Rich's auto-downgrade is not a degraded palette. With rich 14.3.3 a console
  built with ``color_system="standard"`` still emits truecolor SGR, and
  ``Style.render`` maps ``#C79B7A`` to plain white at STANDARD depth. So the
  low-colour path must be an explicitly different palette, chosen here -- never
  Rich quietly approximating the dark one.
* On Windows, Rich's console feature probe is memoised process-wide and latches
  ``legacy_windows=True`` before prompt_toolkit enables virtual-terminal
  processing. :func:`make_console` therefore states ``legacy_windows`` and
  ``color_system`` rather than letting Rich decide after the fact.

Every palette is FOREGROUND-ONLY apart from the two diff backgrounds and the
menu. That is what makes a wrong light/dark guess cost a little contrast instead
of rendering invisible text on a background the user cannot see.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from rich.console import Console
from rich.theme import Theme

from djcode import tui_theme

#: The three palettes, by name. Values are Rich style strings.
PALETTES: dict[str, dict[str, str]] = {
    "dark": {
        "dj.accent": tui_theme.GOLD,
        "dj.text": "default",
        "dj.user": f"bold {tui_theme.TEXT_STRONG}",
        "dj.dim": tui_theme.TEXT_DIM,
        "dj.ok": tui_theme.SUCCESS,
        "dj.warn": tui_theme.WARNING,
        "dj.err": tui_theme.ERROR,
        "dj.info": tui_theme.INFO,
        "dj.think": f"italic {tui_theme.THINKING}",
        # DEVIATION from DESIGN-CLI §9.1, which says the dark column is the
        # existing tui_theme constants. tui_theme.PLAN_MODE (#9C27B0) measures
        # 2.79:1 on BG_PRIMARY -- below the 4.5:1 floor the same section asks
        # for, and PLAN is a badge the user has to be able to read at a
        # glance. Lightened along the same hue to 4.69:1. tui_theme keeps its
        # value; the Textual TUI paints it on a different background.
        "dj.plan": "#BE5BD0",
        "dj.add": "#A2BA9A on #1F2A1F",
        "dj.del": "#FF6B6B on #2A1F1F",
        # 3.38:1 on BG_PRIMARY. Line numbers are read, so they are held to
        # the 3:1 non-text floor rather than being free to fade out; the
        # design's #55514A measured 2.23:1.
        "dj.gutter": "#726C62",
        "dj.menu": f"{tui_theme.TEXT_STRONG} on {tui_theme.BG_INPUT}",
    },
    "light": {
        "dj.accent": "#8A5A2B",
        "dj.text": "default",
        "dj.user": "bold #1B1B1B",
        "dj.dim": "#6B6862",
        "dj.ok": "#3E7A3A",
        "dj.warn": "#B25E00",
        "dj.err": "#B3001B",
        "dj.info": "#0B5FB3",
        "dj.think": "italic #007A8A",
        "dj.plan": "#6A1B9A",
        "dj.add": "#1B5E20 on #E8F5E9",
        "dj.del": "#B71C1C on #FDECEA",
        "dj.gutter": "#8F8A82",  # 3.43:1 on white; the design's #A09C95 was 2.73:1
        "dj.menu": "#1B1B1B on #ECE9E4",
    },
    "ansi16": {
        "dj.accent": "yellow",
        "dj.text": "default",
        "dj.user": "bold",
        "dj.dim": "bright_black",
        "dj.ok": "green",
        "dj.warn": "yellow",
        "dj.err": "red",
        "dj.info": "blue",
        "dj.think": "italic cyan",
        "dj.plan": "magenta",
        "dj.add": "green",
        "dj.del": "red",
        "dj.gutter": "bright_black",
        "dj.menu": "black on white",
    },
}

CODE_THEME = {"dark": "monokai", "light": "default", "ansi16": "ansi_dark"}

#: Glyphs, by meaning. `DESIGN-CLI.md` §9.3.
UNICODE_GLYPHS: dict[str, str] = {
    "bullet": "⏺",  # tool / bullet
    "child": "↳",
    "think": "◌",
    "ok": "✓",
    "fail": "✗",
    "blocked": "⛔",
    "prompt": "❯",
    "plan_prompt": "⏸",
    "meter_full": "▰",
    "meter_empty": "▱",
    "resume": "↺",
    "attach": "⊕",
    "running": "◐",
    "up": "↑",
    "down": "↓",
    "sep": "·",
    "ellipsis": "…",
}

ASCII_GLYPHS: dict[str, str] = {
    "bullet": "*",
    "child": "->",
    "think": "~",
    "ok": "ok",
    "fail": "x",
    "blocked": "!!",
    "prompt": ">",
    "plan_prompt": "||",
    "meter_full": "#",
    "meter_empty": "-",
    "resume": "<-",
    "attach": "+",
    "running": "o",
    "up": "^",
    "down": "v",
    "sep": ".",
    "ellipsis": "...",
}

#: questionary needs its own style list. One definition, not the three copies
#: that were in repl.py, startup.py and auth.py.
_Q_STYLE_BY_PALETTE = {
    "dark": tui_theme.GOLD,
    "light": "#8A5A2B",
    "ansi16": "yellow",
}


@dataclass(frozen=True, slots=True)
class Palette:
    """A resolved look: which palette, its Rich theme, its glyphs, its lexer theme."""

    name: str
    theme: Theme
    glyphs: dict[str, str]
    code_theme: str
    no_color: bool = False

    def style(self, key: str) -> str:
        """The raw style string behind a semantic name, for non-Rich consumers."""
        return PALETTES[self.name][key]

    def glyph(self, key: str) -> str:
        return self.glyphs.get(key, "")


def _config_theme() -> str:
    try:
        from djcode.config import load_config

        value = load_config().get("theme", "auto")
    except Exception:  # pragma: no cover - a missing config must not break colour
        return "auto"
    return value.strip().lower() if isinstance(value, str) else "auto"


def _colorfgbg_is_light() -> bool | None:
    """xterm, rxvt, konsole and mintty publish the background colour here.

    Windows Terminal does not set it at all, which is why this is step 3 of 4
    and not step 1.
    """
    raw = os.environ.get("COLORFGBG", "")
    if not raw:
        return None
    last = raw.split(";")[-1].strip()
    if last in {"7", "15"}:
        return True
    if last.isdigit():
        return False
    return None


def detect(console: Console | None = None) -> str:
    """Which palette to use: ``dark`` | ``light`` | ``ansi16``.

    Order is deliberate -- an explicit instruction always beats a guess:

    1. ``DJCODE_THEME``
    2. the ``theme`` config key (this is GAP B20; ``/set theme=light`` works now)
    3. ``COLORFGBG``
    4. dark
    """
    env = os.environ.get("DJCODE_THEME", "").strip().lower()
    if env in PALETTES:
        return env

    configured = _config_theme()
    if configured in PALETTES:
        return configured

    if low_color(console):
        return "ansi16"

    light = _colorfgbg_is_light()
    if light is True:
        return "light"
    return "dark"


def low_color(console: Console | None = None) -> bool:
    """True when this terminal cannot render the truecolor palettes usefully.

    Not a Rich question. Rich will happily emit ``38;2;…`` into a console that
    shows it as white, so the check is on the reported colour system and the
    NO_COLOR convention, and the answer selects a DIFFERENT palette rather than
    asking Rich to approximate this one.
    """
    if os.environ.get("NO_COLOR"):
        return True
    probe = console or Console(stderr=True)
    return probe.color_system in (None, "standard", "windows")


def glyphs(console: Console | None = None) -> dict[str, str]:
    """Unicode where the terminal can encode it, ASCII where it cannot."""
    if console is None:
        encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
        legacy = False
    else:
        encoding = (getattr(console.file, "encoding", "") or "").lower()
        legacy = bool(getattr(console, "legacy_windows", False))
        if console.is_jupyter:
            return dict(UNICODE_GLYPHS)
    if legacy:
        return dict(ASCII_GLYPHS)
    if "utf" in encoding.replace("-", ""):
        return dict(UNICODE_GLYPHS)
    return dict(ASCII_GLYPHS)


def ensure_utf8_stdout(console: Console | None = None) -> None:
    """Widen the process's streams to UTF-8 before anything prints.

    A piped or redirected stdout on Windows is cp1252 and ``⏺`` raises
    ``UnicodeEncodeError`` out of it -- as does an em-dash inside a file the
    diff renderer is showing. W7 proved the crash and fixed it for diffs; this
    is the same call, made once for the whole surface. A stream that cannot be
    widened gets ``errors="replace"``, so the worst case is a question mark
    rather than a traceback out of the middle of a half-printed screen.
    """
    streams = [sys.stdout, sys.stderr]
    if console is not None:
        streams.append(console.file)
    for stream in streams:
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, LookupError):
            try:
                reconfigure(errors="replace")
            except Exception:  # pragma: no cover - closed or exotic stream
                pass


def build(name: str | None = None, *, console: Console | None = None) -> Palette:
    """Resolve a :class:`Palette`. ``name`` overrides detection."""
    resolved = name if name in PALETTES else detect(console)
    return Palette(
        name=resolved,
        theme=Theme(PALETTES[resolved], inherit=True),
        glyphs=glyphs(console),
        code_theme=CODE_THEME[resolved],
        no_color=bool(os.environ.get("NO_COLOR")),
    )


def make_console(
    *, stderr: bool = False, palette: Palette | None = None, **kwargs
) -> tuple[Console, Palette]:
    """A console that states what it is instead of letting Rich guess.

    ``legacy_windows=False`` is explicit because Rich's Windows feature probe is
    memoised process-wide and latches legacy 16-colour BEFORE prompt_toolkit
    turns on virtual-terminal processing -- leaving the whole REPL in 16 colours
    on a plain conhost for the life of the process.
    """
    palette = palette or build()
    if "color_system" not in kwargs:
        kwargs["color_system"] = "standard" if palette.name == "ansi16" else "truecolor"
    if os.name == "nt" and "legacy_windows" not in kwargs:
        kwargs["legacy_windows"] = False
    # Rich's automatic highlighter repaints every number, path and URL inside a
    # plain string. In a dim tool trailer that turns `654 passed . 41ms` into
    # four colours, and it coloured the `/` in `rm -rf /` magenta on the one
    # card where the command must read exactly as typed. Styling here is always
    # deliberate, never inferred from the text.
    kwargs.setdefault("highlight", False)
    console = Console(
        stderr=stderr,
        theme=palette.theme,
        no_color=palette.no_color,
        **kwargs,
    )
    return console, palette


def questionary_style(palette: Palette | None = None):
    """The one questionary style. There were three copies of this."""
    from questionary import Style

    palette = palette or build()
    accent = _Q_STYLE_BY_PALETTE[palette.name]
    return Style(
        [
            ("qmark", f"fg:{accent} bold"),
            ("question", "bold"),
            ("answer", f"fg:{accent} bold"),
            ("pointer", f"fg:{accent} bold"),
            ("highlighted", f"fg:{accent} bold"),
            ("selected", f"fg:{accent}"),
            ("instruction", ""),
            ("text", ""),
        ]
    )


def meter(fraction: float, width: int = 10, glyph_map: dict[str, str] | None = None) -> str:
    """A context-utilisation bar. Clamped, so a bad ratio cannot draw off-screen."""
    glyph_map = glyph_map or UNICODE_GLYPHS
    fraction = 0.0 if fraction != fraction else max(0.0, min(1.0, fraction))  # NaN -> 0
    filled = int(round(fraction * width))
    return glyph_map["meter_full"] * filled + glyph_map["meter_empty"] * (width - filled)


__all__ = [
    "ASCII_GLYPHS",
    "CODE_THEME",
    "PALETTES",
    "UNICODE_GLYPHS",
    "Palette",
    "build",
    "detect",
    "ensure_utf8_stdout",
    "glyphs",
    "low_color",
    "make_console",
    "meter",
    "questionary_style",
]
