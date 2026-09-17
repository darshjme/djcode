"""File write tool — create or overwrite files.

W7-2 converts this to a `ToolOutcome` so the user can see what was written.

The blueprint says both file tools "already read the pre-image for their own
logic" and should just return it. That is false here: before W7 this module was
seventeen lines that never read anything. So the read is NOT added here -- W5's
`CheckpointStore._before_file` already took the pre-image two frames up the
stack, before the handler ran, and stored the bytes in its blob table keyed by
`pre_sha`. Adding a second read to hand back the same bytes would be exactly the
duplication the blueprint was trying to remove. What this tool does instead is
read the *one* thing the store does not hold: the file as it stood, so the diff
can be built from the same text that is about to be overwritten.

`ok` is authoritative the moment a handler returns a `ToolOutcome` -- see the
long warning in `file_edit.py`. Two paths, both explicit.

One measured Windows fact this module is the source of: `Path.write_text` opens
with `newline=None`, so every `\\n` in `content` becomes `\\r\\n` on disk (a
208-byte LF notebook came back 222 bytes -- `core/checkpoints.py:542`). The
diff is therefore built from `str` on both sides, never from the argument
against the disk bytes, which would report every line in the file as changed.
That behaviour is left as it is: changing this tool's newline policy is not W7's
to change, and it is reported rather than smuggled in.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from djcode.core.outcome import ToolOutcome


async def execute_file_write(path: str, content: str) -> ToolOutcome:
    """Write content to a file, creating parent directories as needed."""
    # Deferred imports: djcode.tools is inside djcode.core's import closure
    # (core -> provider -> capabilities -> tools), so importing either of these
    # at module level would close the cycle at interpreter start-up.
    from djcode.core.diff import diff_text, looks_binary
    from djcode.core.outcome import ToolOutcome as _Outcome

    try:
        p = Path(path).expanduser().resolve()
        before: str | None = None
        if p.is_file():
            try:
                raw = p.read_bytes()
                before = None if looks_binary(raw) else raw.decode("utf-8")
            except OSError:
                before = None
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        lines = content.count("\n") + 1
        diff = diff_text(str(p), before, content)
        return _Outcome(
            content=f"Wrote {lines} lines to {p}",
            ok=True,
            details={
                "diff": diff.as_dict(),
                "diff_stat": {
                    "path": diff.path,
                    "added": diff.added,
                    "removed": diff.removed,
                    "hunks": len(diff.hunks),
                },
            },
        )
    except Exception as e:
        return _Outcome(content=f"Error writing {path}: {e}", ok=False, details={})
