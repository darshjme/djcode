"""Error presentation for the REPL: what broke, what to do, and what it cost.

``errors.format_error`` prints an emoji, the message and a suggestion::

    🔌 Cannot reach the provider
      Start Ollama with `ollama serve`

Three problems with that. The emoji is a font lottery -- on a cp1252 console it
is a `?`, in a legacy Windows console it is a box, and `DESIGN-CLI.md` §9.4
rules emoji out of the surface entirely. The provider and model are missing, so
"cannot reach the provider" does not say WHICH. And nothing tells the user the
one thing they most want to know when something fails mid-turn: **whether
anything was written to disk before it broke.**

That last line is the point of this module. A failure after three `file_edit`
calls and a failure before any of them look identical today, and the difference
decides whether the user reaches for `/undo` or just retries.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console

__all__ = ["describe_disk_effect", "render_error"]


def describe_disk_effect(checkpoints: Any, session_id: str | None = None) -> str:
    """One clause about whether THIS turn touched the filesystem.

    Reads the checkpoint store W5 attached to the dispatch context -- the same
    store `/undo` reverts from -- so the sentence is derived from what was
    actually captured, not from a guess about what the model intended.

    The turn id is compared explicitly. `last_turn` returns the newest turn
    that touched disk, which for a turn that touched nothing is the PREVIOUS
    turn -- and reporting that one's files here would tell the user their
    failed request had changed things it never touched.
    """
    if checkpoints is None:
        return "Checkpoints are off, so what reached disk is not tracked."
    if not session_id:
        return ""
    try:
        turn = checkpoints.last_turn(session_id)
        current = getattr(checkpoints, "_turn_id", None)
    except Exception:  # pragma: no cover - never fail an error message
        return ""
    if not turn or (current and turn[0].turn_id != current):
        return "Nothing was written to disk this turn."
    paths = {
        change.path
        for checkpoint in turn
        for change in getattr(checkpoint, "files", []) or []
        if getattr(change, "path", None)
    }
    if not paths:
        return "Nothing was written to disk this turn."
    noun = "file" if len(paths) == 1 else "files"
    return f"{len(paths)} {noun} changed before this failed; /undo reverts the turn."


def render_error(
    error: BaseException,
    *,
    console: Console | None = None,
    provider: str = "",
    model: str = "",
    checkpoints: Any = None,
    session_id: str | None = None,
    verbose: bool = False,
) -> None:
    """Draw one error, in the one shape every error class uses.

        ✗ Provider error · ollama · gemma4
          connection refused at http://localhost:11434
          → start Ollama (ollama serve), or /connect to pick another provider
          Nothing was written to disk this turn.
    """
    from djcode.errors import classify_error
    from djcode.frontends.repl.render import console as default_console
    from djcode.frontends.repl.render import glyph

    console = console or default_console
    classified = classify_error(error)

    subject = f" {glyph('sep')} ".join(part for part in (provider, model) if part)
    heading = f"{classified.category.title()} error"
    if subject:
        heading += f" [dim]{glyph('sep')} {subject}[/]"
    console.print(f"\n[dj.err]{glyph('fail')} {heading}[/]")
    console.print(f"  {classified.message}")
    if classified.suggestion:
        console.print(f"  [dj.warn]→ {classified.suggestion}[/]")
    if verbose and classified.original:
        console.print(f"  [dim]{classified.original[:300]}[/]")
    if classified.recoverable and classified.fallback:
        console.print(
            f"  [dim]auto-recovery: {classified.fallback.replace('_', ' ')}[/]"
        )
    effect = describe_disk_effect(checkpoints, session_id)
    if effect:
        console.print(f"  [dim]{effect}[/]")
    console.print()
