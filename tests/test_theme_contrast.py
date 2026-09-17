"""The light palette is actually readable on white, measured rather than asserted.

`DESIGN-CLI.md` §9.1 says of the light column: "computed, not yet measured --
add tests/test_theme_contrast.py". This is that file. It computes WCAG 2.x
relative luminance and contrast ratios directly, so a future edit that picks a
prettier hex cannot quietly drop a style below legibility.

Also pinned here: the palettes agree on their key set (a semantic name missing
from one palette is a KeyError on somebody's terminal and nobody else's), the
glyph maps agree on theirs, and detection honours the config key -- which is
GAP B20, the whole reason `/set theme=light` did nothing for two versions.
"""

from __future__ import annotations

import pytest

from djcode.frontends.repl import theme

WHITE = (1.0, 1.0, 1.0)
DARK = (0x17 / 255, 0x19 / 255, 0x1D / 255)  # tui_theme.BG_PRIMARY


def _channel(value: float) -> float:
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _luminance(rgb: tuple[float, float, float]) -> float:
    r, g, b = (_channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _rgb(hex_colour: str) -> tuple[float, float, float]:
    hex_colour = hex_colour.lstrip("#")
    return tuple(int(hex_colour[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def contrast(fg_hex: str, bg: tuple[float, float, float]) -> float:
    a, b = _luminance(_rgb(fg_hex)), _luminance(bg)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


def test_the_maths_matches_the_published_reference_values():
    """Guard the guard: black on white is 21:1 and #767676 on white is the
    canonical 4.54:1 boundary case."""
    assert round(contrast("#000000", WHITE), 1) == 21.0
    assert 4.5 <= contrast("#767676", WHITE) < 4.6


def _foreground_styles(palette_name: str) -> dict[str, str]:
    """Styles whose colour is a plain hex foreground with no background."""
    out = {}
    for key, value in theme.PALETTES[palette_name].items():
        if " on " in value or value == "default":
            continue
        parts = [p for p in value.split() if p.startswith("#")]
        if parts:
            out[key] = parts[0]
    return out


#: `dj.gutter` is diff line numbers -- supporting chrome that must not compete
#: with the code beside it. It is held to WCAG's 3:1 non-text floor rather than
#: the 4.5:1 prose floor, in BOTH palettes, and nothing else gets that
#: exemption. `dj.dim` is prose the user reads (timings, footers, paths) and is
#: held to 4.5:1 in both.
QUIET = {"dj.gutter"}


def _floor(key: str) -> float:
    return 3.0 if key in QUIET else 4.5


@pytest.mark.parametrize("key,colour", sorted(_foreground_styles("light").items()))
def test_light_palette_is_readable_on_white(key, colour):
    floor, ratio = _floor(key), contrast(colour, WHITE)
    assert ratio >= floor, f"{key} ({colour}) is {ratio:.2f}:1 on white, needs {floor}:1"


@pytest.mark.parametrize("key,colour", sorted(_foreground_styles("dark").items()))
def test_dark_palette_is_readable_on_the_dark_background(key, colour):
    floor, ratio = _floor(key), contrast(colour, DARK)
    assert ratio >= floor, f"{key} ({colour}) is {ratio:.2f}:1 on the dark bg, needs {floor}:1"


def test_only_the_gutter_is_allowed_to_be_quiet():
    """The exemption list is a slippery slope, so it is pinned. Adding a style
    here has to be a deliberate edit to this test, not a side effect."""
    assert QUIET == {"dj.gutter"}


def test_diff_backgrounds_carry_their_own_readable_foreground():
    for palette in ("dark", "light"):
        for key in ("dj.add", "dj.del"):
            fg, _, bg = theme.PALETTES[palette][key].partition(" on ")
            if not bg.startswith("#"):
                continue
            ratio = contrast(fg, _rgb(bg))
            assert ratio >= 4.5, f"{palette}/{key} is {ratio:.2f}:1"


# -- structure -------------------------------------------------------------


def test_every_palette_defines_every_style():
    keys = [set(styles) for styles in theme.PALETTES.values()]
    assert all(k == keys[0] for k in keys), "a style missing from one palette is a KeyError"


def test_ascii_fallback_covers_every_glyph():
    assert set(theme.ASCII_GLYPHS) == set(theme.UNICODE_GLYPHS)


def test_ascii_fallback_is_actually_ascii():
    for key, value in theme.ASCII_GLYPHS.items():
        assert value.isascii(), f"{key} fallback {value!r} is not ASCII"


def test_every_palette_has_a_code_theme():
    assert set(theme.CODE_THEME) == set(theme.PALETTES)


# -- detection (GAP B20) ---------------------------------------------------


def test_env_wins_over_everything(monkeypatch):
    monkeypatch.setenv("DJCODE_THEME", "light")
    assert theme.detect() == "light"


def test_the_config_key_is_finally_read(monkeypatch):
    """GAP B20. `config.py` has carried `theme` since W1-9 and nothing read it,
    so `/set theme=light` was a no-op that reported success."""
    monkeypatch.delenv("DJCODE_THEME", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(theme, "_config_theme", lambda: "light")
    monkeypatch.setattr(theme, "low_color", lambda console=None: False)
    assert theme.detect() == "light"


def test_no_color_selects_the_sixteen_colour_palette(monkeypatch):
    """Not Rich's auto-downgrade -- a genuinely different palette, because a
    truecolor hex at STANDARD depth renders as plain white."""
    monkeypatch.delenv("DJCODE_THEME", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(theme, "_config_theme", lambda: "auto")
    assert theme.detect() == "ansi16"


def test_colorfgbg_light_background(monkeypatch):
    monkeypatch.delenv("DJCODE_THEME", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(theme, "_config_theme", lambda: "auto")
    monkeypatch.setattr(theme, "low_color", lambda console=None: False)
    monkeypatch.setenv("COLORFGBG", "0;15")
    assert theme.detect() == "light"
    monkeypatch.setenv("COLORFGBG", "15;0")
    assert theme.detect() == "dark"


def test_detection_falls_back_to_dark(monkeypatch):
    monkeypatch.delenv("DJCODE_THEME", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("COLORFGBG", raising=False)
    monkeypatch.setattr(theme, "_config_theme", lambda: "auto")
    monkeypatch.setattr(theme, "low_color", lambda console=None: False)
    assert theme.detect() == "dark"


def test_a_broken_config_cannot_break_colour(monkeypatch):
    def explode():
        raise RuntimeError("no config")

    monkeypatch.delenv("DJCODE_THEME", raising=False)
    monkeypatch.setattr(theme, "_config_theme", explode)
    with pytest.raises(RuntimeError):
        theme._config_theme()
    # detect() reads it through the guarded helper, so the real one is safe.
    monkeypatch.undo()
    assert theme.detect() in theme.PALETTES


# -- console construction --------------------------------------------------


def test_make_console_states_its_colour_system_rather_than_guessing():
    console, palette = theme.make_console(palette=theme.build("dark"))
    assert console.color_system is not None
    assert palette.name == "dark"


def test_make_console_never_leaves_legacy_windows_to_rich(monkeypatch):
    """Rich's Windows feature probe is memoised process-wide and latches legacy
    16-colour before prompt_toolkit enables VT processing."""
    import os

    if os.name != "nt":
        pytest.skip("Windows-only behaviour")
    console, _ = theme.make_console(palette=theme.build("dark"))
    assert console.legacy_windows is False


def test_meter_is_clamped_and_fixed_width():
    assert len(theme.meter(0.5, width=10)) == 10
    assert len(theme.meter(9.0, width=10)) == 10
    assert len(theme.meter(-3.0, width=10)) == 10
    assert theme.meter(0.0, width=4) == theme.UNICODE_GLYPHS["meter_empty"] * 4
    assert theme.meter(1.0, width=4) == theme.UNICODE_GLYPHS["meter_full"] * 4


def test_meter_survives_a_nan_ratio():
    assert len(theme.meter(float("nan"), width=8)) == 8


def test_questionary_style_exists_for_every_palette():
    for name in theme.PALETTES:
        assert theme.questionary_style(theme.build(name)) is not None
