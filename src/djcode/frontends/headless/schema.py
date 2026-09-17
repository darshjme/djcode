"""The JSON Schema for the JSONL event union, printed by ``djcode --output-schema``.

Why a schema at all: the consumers of this stream are not people. They are CI
steps, shell scripts and the future GUI backend, and every one of them has to
decide what a line means before it has ever seen one. A schema is the difference
between "parse it and hope" and a generated client.

**The enum is derived, never typed.** ``_TYPES`` comes from ``EventType`` itself,
so a wave that adds an event type cannot ship a schema that does not know about
it -- which is precisely how the previous generation of this codebase ended up
with a ``/docs`` command claiming 22 agents and version 2.0.1.

Per-type ``data`` shapes are declared only for the **session** family (the twelve
events a turn emits, which is what a front-end renders). The orchestration family
keeps ``data`` open: those events are consumed by the orchestrator's own UI, their
payloads are still moving, and a wrong schema is worse than an honest permissive
one. ``additionalProperties`` stays ``true`` on every ``data`` object for the same
reason -- a new key must not invalidate an old consumer's documents.
"""

from __future__ import annotations

from typing import Any

from djcode.core.events import EventType
from djcode.frontends.headless.serialise import SCHEMA_VERSION

#: Every event type, straight off the enum. Sorted so the printed schema is
#: byte-stable across runs and a diff of two schema dumps means something.
_TYPES: list[str] = sorted(member.value for member in EventType)


def _obj(required: list[str], properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "required": required,
        "additionalProperties": True,
        "properties": properties,
    }


_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_INT = {"type": "integer"}
_NUM = {"type": "number"}
_ANY_OBJ = {"type": "object"}

#: The live-object fields. ``_jsonable`` turns a type it can walk (a dataclass,
#: a ``__slots__`` object, anything with ``as_dict``) into an object and an
#: opaque one into its ``str()``. Every type core actually produces on these
#: fields walks -- ``test_headless_json`` pins that separately -- so the schema
#: says "whatever the coercer made of it" rather than promising an object it
#: cannot guarantee for a third-party value that lands there later.
_COERCED = {"description": "The live object, coerced: an object, or its str() if opaque."}


#: ``data`` per session event, mirroring the factories in ``core/events.py``.
_DATA: dict[str, dict[str, Any]] = {
    EventType.TOKEN.value: _obj(["text"], {"text": _STR}),
    EventType.THINKING.value: _obj(["text"], {"text": _STR}),
    EventType.TOOL_CALL.value: _obj(
        ["name", "args", "call_id"],
        {"name": _STR, "args": _ANY_OBJ, "call_id": _STR},
    ),
    EventType.TOOL_RESULT.value: _obj(
        ["call_id", "name", "content", "ok", "details"],
        {
            "call_id": _STR,
            "name": _STR,
            "content": _STR,
            # Meaningful ONLY when details.ok_source is handler/raised/dispatch.
            # "unverified" means dispatch saw no failure signal, which is not
            # the same statement as "the tool succeeded" (W3-VERIFICATION.md).
            "ok": _BOOL,
            "details": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "ok_source": {
                        "type": "string",
                        "enum": ["handler", "raised", "dispatch", "unverified"],
                    }
                },
            },
            "spill_path": {"type": ["string", "null"]},
            "duration_ms": _INT,
        },
    ),
    EventType.DIFF.value: _obj(
        ["path", "summary"],
        {
            "path": _STR,
            # FileDiff.as_dict(): hunks only, capped, never file bodies.
            "summary": _ANY_OBJ,
        },
    ),
    EventType.CHECKPOINT.value: _obj(
        ["checkpoint_id"],
        {"checkpoint_id": _STR, "checkpoint": _COERCED},
    ),
    EventType.COMPLETE.value: _obj(
        ["response", "tool_rounds", "elapsed_s"],
        {"response": _STR, "tool_rounds": _INT, "elapsed_s": _NUM},
    ),
    EventType.ERROR.value: _obj(
        ["error", "kind", "recoverable"],
        {"error": _STR, "kind": _STR, "recoverable": _BOOL},
    ),
    EventType.PERMISSION_REQUEST.value: _obj(
        ["tool", "arguments"],
        {
            "tool": _STR,
            "arguments": _ANY_OBJ,
            "command": _STR,
            "path": _STR,
            "request": _COERCED,
        },
    ),
    EventType.PERMISSION_DECIDED.value: _obj(
        ["tool", "action", "allowed"],
        {
            "tool": _STR,
            "action": {"type": "string", "enum": ["allow", "deny", "always", "session"]},
            "allowed": _BOOL,
            "comment": _STR,
            "decision": _COERCED,
        },
    ),
    EventType.STEER.value: _obj(["text", "delivered"], {"text": _STR, "delivered": _BOOL}),
    EventType.QUEUE.value: _obj(
        ["text", "action", "depth"],
        {
            "text": _STR,
            "action": {"type": "string", "enum": ["queued", "delivered", "returned"]},
            "depth": _INT,
        },
    ),
}


def event_schema() -> dict[str, Any]:
    """The JSON Schema (draft 2020-12) for one line of the ``--json`` stream."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://darshj.ai/djcode/schema/core-event-v{SCHEMA_VERSION}.json",
        "title": "DJcode core event",
        "description": (
            "One line of the djcode --json stream: a single CoreEvent emitted by "
            "djcode.core.session.CoreSession. The stream is JSONL on stderr, one "
            "complete object per line; stdout carries the final assistant message "
            "and nothing else."
        ),
        "type": "object",
        "required": ["v", "seq", "ts", "type", "agent", "data"],
        "additionalProperties": False,
        "properties": {
            "v": {"const": SCHEMA_VERSION, "description": "Envelope version."},
            "seq": {
                "type": "integer",
                "minimum": 0,
                "description": "0-based, monotonic within one stream. Gaps mean lost lines.",
            },
            "ts": {"type": "number", "description": "Unix seconds, as time.time() produced it."},
            "type": {
                "type": "string",
                "enum": _TYPES,
                "description": "CoreEvent.event_type. Both event families share this enum.",
            },
            "agent": {
                "type": "object",
                "required": ["name", "role"],
                "additionalProperties": False,
                "properties": {"name": _STR, "role": _STR},
                "description": "Empty strings on a single-agent turn.",
            },
            "data": {"type": "object", "description": "CoreEvent.data, coerced to JSON."},
        },
        "allOf": [
            {
                "if": {"properties": {"type": {"const": kind}}, "required": ["type"]},
                "then": {"properties": {"data": shape}},
            }
            for kind, shape in _DATA.items()
        ],
    }


__all__ = ["event_schema"]
