"""DJcode Orchestrator — Multi-agent task execution engine.

Dispatches tasks to specialist agents based on intent classification.
Supports single-agent and multi-agent parallel workflows.
"""

from djcode.orchestrator.context_bus import ContextBus
from djcode.orchestrator.engine import Orchestrator

__all__ = ["Orchestrator", "ContextBus"]
