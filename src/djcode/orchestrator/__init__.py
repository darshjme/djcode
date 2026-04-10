"""DJcode Orchestrator — Multi-agent parallel execution engine.

Dispatches tasks to specialist agents based on intent classification.
Supports single-agent, parallel, pipeline, wave, and full army workflows.
"""

from djcode.orchestrator.context_bus import ContextBus
from djcode.orchestrator.engine import Orchestrator, ShadowOrchestrator
from djcode.orchestrator.events import EventBus, OrchestratorEvent
from djcode.orchestrator.router import SemanticRouter
from djcode.orchestrator.vector_context import VectorContextStore

__all__ = [
    "Orchestrator",
    "ShadowOrchestrator",
    "ContextBus",
    "EventBus",
    "OrchestratorEvent",
    "SemanticRouter",
    "VectorContextStore",
]
