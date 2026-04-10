"""DJcode multi-LLM provider system.

Unified interface for Anthropic, OpenAI, Google Gemini, and OpenAI-compatible
providers. Each provider implements the LLMProvider protocol and streams
ProviderChunks with full token usage tracking.

Usage:
    from djcode.providers import ProviderRouter, ProviderChunk

    router = ProviderRouter.from_config()
    provider = router.get_provider()
    async for chunk in provider.chat(messages, tools=tools):
        print(chunk.content, end="")
"""

from djcode.providers.base import (
    KNOWN_MODELS,
    MODEL_ALIASES,
    MODEL_PRICING,
    BaseProvider,
    FinishReason,
    LLMProvider,
    ModelInfo,
    ProviderChunk,
    TokenUsage,
    ToolCall,
    get_model_info,
    resolve_model,
)
from djcode.providers.router import (
    ProviderRouter,
    create_provider,
    detect_provider,
)

__all__ = [
    # Protocol and base
    "LLMProvider",
    "BaseProvider",
    # Data types
    "ProviderChunk",
    "TokenUsage",
    "ToolCall",
    "ModelInfo",
    "FinishReason",
    # Registry
    "MODEL_ALIASES",
    "MODEL_PRICING",
    "KNOWN_MODELS",
    # Functions
    "get_model_info",
    "resolve_model",
    "detect_provider",
    "create_provider",
    # Router
    "ProviderRouter",
]
