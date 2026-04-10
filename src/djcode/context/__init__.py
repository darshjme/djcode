"""Context Window Management Engine for DJcode.

Provides intelligent 1M context window management with:
- Model-aware context limits and token counting
- Smart message prioritization and compression
- Multiple compression strategies (trim, selective, summary, hybrid)
- Extensible model registry with fuzzy matching

Works with ANY LLM -- from 4K Ollama models to 1M Claude Opus.
"""

from djcode.context.manager import ContextStats, ContextWindowManager
from djcode.context.compressor import CompressionStrategy, ConversationCompressor
from djcode.context.models import (
    MODEL_REGISTRY,
    ModelInfo,
    get_context_size,
    get_model_info,
    register_model,
    supports_tools,
    supports_vision,
)

__all__ = [
    "ContextWindowManager",
    "ContextStats",
    "ConversationCompressor",
    "CompressionStrategy",
    "ModelInfo",
    "MODEL_REGISTRY",
    "get_model_info",
    "get_context_size",
    "supports_tools",
    "supports_vision",
    "register_model",
]
