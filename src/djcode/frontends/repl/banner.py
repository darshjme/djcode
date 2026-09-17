"""The first two lines of the session.

The old banner was a Rich `Panel` -- four lines of box drawing around three
facts -- printed before every REPL. `DESIGN-CLI.md` §3.2 replaces it with two
lines and no box, on the principle that the first thing on screen should be
information, not furniture.

Two facts it adds, both of which used to be invisible:

* **local or hosted.** Derived from ``PROVIDERS[name]["needs_key"]``, so a user
  who thinks they are talking to a local model and is in fact spending money on
  a hosted one finds out on line one rather than from a bill.
* **the recent-session offer.** A session started in this directory in the last
  day is announced with the one command that reopens it. Nothing else in the
  program ever told a user their previous conversation was still there.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.text import Text

__all__ = ["is_local_provider", "print_banner", "recent_session_line", "short_cwd"]

#: How recent a session has to be for the banner to offer it.
RECENT_WINDOW = timedelta(hours=24)


def short_cwd(cwd: str | None = None) -> str:
    path = Path(cwd) if cwd else Path.cwd()
    try:
        return "~/" + str(path.relative_to(Path.home())).replace("\\", "/")
    except ValueError:
        return str(path)


def is_local_provider(name: str) -> bool | None:
    """True for a runtime on this machine, False for a hosted one, None if unknown.

    None is a real answer and is rendered as nothing: claiming "local" about a
    provider the registry has never heard of would be the exact mistake this
    badge exists to prevent.
    """
    try:
        from djcode.auth import PROVIDERS
    except Exception:  # pragma: no cover - defensive
        return None
    entry = PROVIDERS.get(name)
    if not isinstance(entry, dict) or "needs_key" not in entry:
        return None
    return not entry["needs_key"]


def _ago(stamp: str | None) -> str:
    if not stamp:
        return ""
    try:
        moment = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return ""
    delta = datetime.now(moment.tzinfo) - moment
    seconds = int(delta.total_seconds())
    if seconds < 10:
        # Covers a clock that ran backwards across a DST boundary as well as a
        # session touched a moment ago; "-3s ago" helps nobody.
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def recent_session_line(db: Any = None, cwd: str | None = None) -> str | None:
    """`Last session here 14m ago · 12 msgs · "fix os.uname" · /resume`, or None."""
    from djcode.sessions import SessionDB

    db = db or SessionDB()
    cwd = cwd or os.getcwd()
    try:
        recent = db.sessions_for_cwd(cwd, limit=1)
    except Exception:  # pragma: no cover - a banner must never fail a launch
        return None
    if not recent:
        return None
    session = recent[0]
    stamp = session.last_interaction_at or session.updated_at or session.start
    try:
        moment = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if datetime.now(moment.tzinfo) - moment > RECENT_WINDOW:
        return None

    parts = [f"Last session here {_ago(stamp)}"]
    if session.messages_count:
        parts.append(f"{session.messages_count} msgs")
    summary = (session.summary or "").strip().replace("\n", " ")
    if summary:
        parts.append(f'"{summary[:48]}"')
    return " · ".join(parts)


def print_banner(
    provider: Any,
    *,
    auto_accept: bool = False,
    mode: str = "ACT",
    thinking: bool = True,
    console: Console | None = None,
    offer_resume: bool = True,
) -> None:
    """Two lines, no box."""
    from djcode import __version__
    from djcode.frontends.repl.render import ACCENT, glyph
    from djcode.frontends.repl.render import console as default_console

    console = console or default_console
    name = getattr(provider.config, "name", "")
    model = getattr(provider.config, "model", "")
    sep = glyph("sep")

    first = Text()
    first.append(f"DJcode {__version__}", style=f"bold {ACCENT}")
    first.append(f"   {model} {sep} {name}")
    local = is_local_provider(name)
    if local is not None:
        first.append(f" {sep} {'local' if local else 'hosted'}", style="dj.dim")
    first.append(f"   {short_cwd()}", style="dj.dim")

    second = Text(style="dj.dim")
    second.append(f"{'auto' if auto_accept else 'ask'} {sep} {mode}")
    second.append(f" {sep} thinking {'on' if thinking else 'off'}")
    second.append(f"   /help {sep} /shortcuts {sep} /undo {sep} /diff")

    console.print()
    console.print(first)
    console.print(second)

    if offer_resume:
        line = recent_session_line()
        if line:
            console.print(
                f"[dj.dim]{glyph('resume')} {line}[/]   "
                f"[{ACCENT}]--continue[/][dj.dim] or /resume to pick one[/]"
            )
    console.print()
