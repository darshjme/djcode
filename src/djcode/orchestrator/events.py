"""Deprecated location. The event system now lives in ``djcode.core.events``.

Kept so existing imports and tests keep working unchanged. New code should import
from ``djcode.core`` (or ``djcode.core.events``) directly.

Two layers on purpose:

1. The star re-export catches everything, including names added to
   ``core.events`` later.
2. The explicit list below names every symbol an existing consumer actually
   imports from this module -- all 17 that ``orchestrator/engine.py:43-64``
   pulls in, the 2 that ``orchestrator/__init__.py`` re-exports, the 9 that
   ``tests/test_v4.py`` uses, and the 1 in ``tests/test_execution.py``.

Layer 2 exists because layer 1 is fragile in exactly one way: if anyone ever adds
an ``__all__`` to ``djcode/core/events.py``, the star silently narrows to it and
``engine.py``'s import fails at import time -- which takes the whole product down,
not one feature. ``core/events.py`` carries a comment forbidding that; this is the
belt to its braces.
"""

from djcode.core.events import *  # noqa: F401,F403
from djcode.core.events import (  # noqa: F401
    CoreEvent,
    EventBus,
    EventCallback,
    EventType,
    GateAction,
    GateSeverity,
    OrchestratorEvent,
    agent_complete_event,
    agent_error_event,
    agent_start_event,
    agent_token_event,
    agent_tool_event,
    blocking_gate_event,
    checkpoint_event,
    complete_event,
    context_conflict_event,
    context_inject_event,
    context_write_event,
    diff_event,
    error_event,
    orchestrator_complete_event,
    orchestrator_error_event,
    orchestrator_start_event,
    synthesis_complete_event,
    synthesis_start_event,
    thinking_event,
    token_event,
    tool_call_event,
    tool_result_event,
    wave_complete_event,
    wave_start_event,
)
