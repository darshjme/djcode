"""``djcode.frontends.headless`` -- the machine-readable surface (P0-9, W10-1).

Three front-ends consume ``djcode.core`` as peers: ``frontends/repl`` draws for a
human, ``frontends/tui`` draws for a human in a full-screen frame, and this one
draws for a program. It is the smallest of the three by an order of magnitude,
which is the clearest evidence W2's extraction actually worked -- a complete new
surface is a serialiser, a schema and a hundred lines of driver, with no engine
code duplicated into it.

Public surface::

    run_headless(prompt, ...)   one turn: JSONL on stderr, the answer on stdout
    event_schema()              the JSON Schema for one line of that stream
    event_to_dict / _line       one CoreEvent -> envelope / one line of JSONL
    dict_to_event               the reverse, for a GUI backend reading the pipe
    EventWriter                 a stream plus the sequence counter

Nothing here is imported by ``djcode.core``; ``tests/test_headless_purity.py``
enforces that arrow in the one direction it is allowed to point.
"""

from __future__ import annotations

from djcode.frontends.headless.runner import NO_HUMAN, run_headless
from djcode.frontends.headless.schema import event_schema
from djcode.frontends.headless.serialise import (
    SCHEMA_VERSION,
    EventWriter,
    dict_to_event,
    event_to_dict,
    event_to_line,
)

__all__ = [
    "NO_HUMAN",
    "SCHEMA_VERSION",
    "EventWriter",
    "dict_to_event",
    "event_schema",
    "event_to_dict",
    "event_to_line",
    "run_headless",
]
