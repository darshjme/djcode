"""CoreEvent -> one JSON object per line. The wire format for CI, scripts and a GUI.

P0-9 / W10-1. This module is the *only* place a ``CoreEvent`` becomes text, and
it is deliberately a front-end rather than a core concern: core emits typed
objects, and how they are framed for a pipe is a rendering decision exactly like
``frontends/repl/render.py`` drawing a tool card.

THE ENVELOPE
------------
Every line is a complete JSON object with a fixed five-key envelope::

    {"v": 1, "seq": 7, "ts": 1758.., "type": "tool_result",
     "agent": {"name": "", "role": ""}, "data": {...}}

* ``v``     -- schema version. Bumped only for a breaking change to the envelope.
* ``seq``   -- 0-based, monotonic within one stream. The one thing the event
  itself does not carry, and the only way a consumer can tell "the producer
  emitted nothing" from "I dropped a line".
* ``ts``    -- ``CoreEvent.timestamp``, unix seconds as a float.
* ``type``  -- ``CoreEvent.event_type.value``. Every member of ``EventType``,
  the orchestration family included; nothing is filtered here.
* ``agent`` -- ``agent_name`` / ``agent_role``, always present (empty strings for
  a single-agent turn), so a consumer never has to branch on absence.
* ``data``  -- ``CoreEvent.data``, coerced to JSON, keys and values intact.

WHAT COERCION MEANS, AND WHAT IT REFUSES TO DO
----------------------------------------------
``CoreEvent.data`` is not JSON today. Four session events deliberately carry the
*live object* alongside its flattened summary so an in-process renderer can use
the real thing: ``diff_event`` puts a ``FileDiff`` on ``data["diff"]``,
``checkpoint_event`` a checkpoint on ``data["checkpoint"]``, and the two
permission events a ``ToolRequest`` / ``Decision``.

The rule here: **coerce, never drop.** ``_jsonable`` walks the value and uses, in
order, a purpose-built ``as_dict()``, then dataclass fields, then ``__slots__``
attributes, then ``str()``. So a checkpoint and a permission request survive to
the wire as objects rather than as ``"<Checkpoint object at 0x...>"``.

The single exception is ``data["diff"]``, which IS dropped -- because
``diff_event`` already puts ``FileDiff.as_dict()`` on ``data["summary"]``, so
keeping both would send an identical payload twice on the largest event in the
stream. ``core/diff.py``'s module docstring is explicit that file bodies
(``before_text`` / ``after_text``) are "dropped at the serialisation boundary";
this is that boundary.

``ToolOutcome.details`` is NOT touched. It arrives on ``tool_result_event`` as a
plain dict, it is already required to be JSON-serialisable (``outcome.py``), and
it goes out key for key -- ``ok_source`` included, which is the one field that
decides whether ``ok`` means anything at all.

NON-FINITE FLOATS
-----------------
``json.dumps`` emits bare ``NaN`` / ``Infinity`` by default, which is not JSON and
which ``JSON.parse``, ``jq`` and Go's ``encoding/json`` all reject. Every dump
here passes ``allow_nan=False`` and non-finite floats are coerced to their string
form first, so a line is never silently unparseable at the far end.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from djcode.core.events import CoreEvent, EventType

#: Envelope version. Bump only when the five top-level keys change meaning.
SCHEMA_VERSION = 1

#: How deep ``_jsonable`` will walk before it gives up and stringifies.
MAX_DEPTH = 12

#: ``(event type, key)`` pairs whose value is a live object that is ALREADY on
#: the same event in serialised form. Only one exists; see the module docstring.
REDUNDANT_LIVE_KEYS: frozenset[tuple[str, str]] = frozenset(
    {
        (EventType.DIFF.value, "diff"),
    }
)


def _jsonable(value: Any, *, depth: int = 0) -> Any:
    """Coerce anything to something ``json.dumps(..., allow_nan=False)`` accepts."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        # -inf/inf/nan would come out as bare tokens no strict parser accepts.
        return value if math.isfinite(value) else str(value)
    if isinstance(value, enum.Enum):
        return _jsonable(value.value, depth=depth + 1)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes | bytearray):
        return bytes(value).decode("utf-8", "replace")
    if depth >= MAX_DEPTH:
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v, depth=depth + 1) for k, v in value.items()}
    if isinstance(value, set | frozenset):
        return [_jsonable(v, depth=depth + 1) for v in sorted(value, key=repr)]
    if isinstance(value, Sequence):
        return [_jsonable(v, depth=depth + 1) for v in value]

    # A type that knows its own bounded serialisation always wins: FileDiff,
    # Hunk, DiffLine and RestoreReport each have one, and each of them omits
    # something (file bodies) that must not reach the wire.
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        try:
            return _jsonable(as_dict(), depth=depth + 1)
        except Exception:  # pragma: no cover - a broken as_dict is not fatal
            return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _jsonable(getattr(value, f.name, None), depth=depth + 1)
            for f in dataclasses.fields(value)
        }
    slots = getattr(type(value), "__slots__", None)
    if slots:
        names = [slots] if isinstance(slots, str) else list(slots)
        return {
            name: _jsonable(getattr(value, name, None), depth=depth + 1)
            for name in names
            if not name.startswith("_")
        }
    return str(value)


def event_to_dict(event: CoreEvent, *, seq: int = 0) -> dict[str, Any]:
    """One event as the envelope described in the module docstring."""
    kind = (
        event.event_type.value
        if isinstance(event.event_type, EventType)
        else str(event.event_type)
    )
    data = {
        key: _jsonable(value)
        for key, value in (event.data or {}).items()
        if (kind, key) not in REDUNDANT_LIVE_KEYS
    }
    return {
        "v": SCHEMA_VERSION,
        "seq": int(seq),
        "ts": float(event.timestamp),
        "type": kind,
        "agent": {"name": event.agent_name or "", "role": event.agent_role or ""},
        "data": data,
    }


def event_to_line(event: CoreEvent, *, seq: int = 0) -> str:
    """One event as one line of JSONL -- no trailing newline, never an embedded one.

    ``ensure_ascii=True`` on purpose: a token stream carries arbitrary Unicode and
    the consumer's pipe is frequently cp1252 on this platform. Escaping at the
    producer is the only place that can be got right once.
    """
    return json.dumps(
        event_to_dict(event, seq=seq),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=False,
    )


def dict_to_event(payload: Mapping[str, Any]) -> CoreEvent:
    """Rebuild a ``CoreEvent`` from an envelope. The round trip a GUI backend does.

    Lossy by construction for the live objects the wire cannot carry -- a restored
    ``DIFF`` event has ``data["summary"]`` but no ``data["diff"]``. Everything the
    envelope states is restored exactly, ``event_type`` included, and an unknown
    ``type`` raises rather than being guessed at.
    """
    agent = payload.get("agent") or {}
    return CoreEvent(
        event_type=EventType(str(payload["type"])),
        agent_name=str(agent.get("name", "") or ""),
        agent_role=str(agent.get("role", "") or ""),
        data=dict(payload.get("data") or {}),
        timestamp=float(payload.get("ts", 0.0) or 0.0),
    )


class EventWriter:
    """Writes events to a text stream as JSONL, numbering them as it goes.

    Holds the sequence counter so callers cannot forget it, and flushes every
    line: a CI consumer reading the pipe live must see a tool call the moment it
    happens, not when an 8 KiB block buffer happens to fill.
    """

    __slots__ = ("_seq", "_stream")

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._seq = 0

    @property
    def count(self) -> int:
        """How many events have been written."""
        return self._seq

    def write(self, event: CoreEvent) -> None:
        line = event_to_line(event, seq=self._seq)
        self._seq += 1
        self._stream.write(line + "\n")
        flush = getattr(self._stream, "flush", None)
        if callable(flush):
            flush()


__all__ = [
    "MAX_DEPTH",
    "REDUNDANT_LIVE_KEYS",
    "SCHEMA_VERSION",
    "EventWriter",
    "dict_to_event",
    "event_to_dict",
    "event_to_line",
]
