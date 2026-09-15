"""DJcode - Local-first AI coding CLI by DarshJ.AI."""

import sys

__version__ = "4.3.0"
__author__ = "DarshJ"


def _use_utf8_streams() -> None:
    """Guarantee stdout/stderr can encode everything DJcode prints.

    DJcode's output is full of non-ASCII: agent icons, the U+276F prompt glyph,
    box drawing, arrows. On Windows sys.stdout falls back to the ANSI code page
    (cp1252 here) whenever it is not attached to a console -- a pipe, a shell
    redirect, or the Textual app's stdout capture. Writing one emoji to such a
    stream raises UnicodeEncodeError, which is how `/scout` inside the TUI died
    with "Scout error: 'charmap' codec can't encode character '🔍'"
    and never reached the provider at all.

    errors="replace" is deliberate: a glyph a legacy console genuinely cannot
    render degrades to "?" instead of destroying the command that printed it.

    sys.__stdout__/__stderr__ are included because Textual's driver and print
    capture write through the original interpreter streams, not through
    whatever sys.stdout currently points at; fixing only sys.stdout leaves the
    full-screen TUI broken.

    This is a no-op wherever the stream is already UTF-8, which is every POSIX
    process and every real Windows console, so it only fires on the exact
    configuration that was broken.
    """
    for stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
        reconfigure = getattr(stream, "reconfigure", None)
        encoding = (getattr(stream, "encoding", "") or "").lower()
        if reconfigure is None or encoding.startswith("utf"):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):  # detached or non-reconfigurable stream
            pass


_use_utf8_streams()
