"""Base provider protocol and shared data types for DJcode LLM providers.

Defines the contract every provider must implement plus shared value objects
for streaming chunks, token usage, model metadata, and tool calling.
All providers communicate through these types — no provider-specific leakage.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

# -- Pricing per 1M tokens (USD) -- updated 2025-Q2 --

MODEL_PRICING: dict[str, tuple[float, float]] = {
    # Anthropic  (input, output)
    "claude-opus-4-6": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (0.80, 4.0),
    # OpenAI
    "gpt-4o": (2.50, 10.0),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "o1": (15.0, 60.0),
    "o3": (10.0, 40.0),
    "o3-mini": (1.10, 4.40),
    "o4-mini": (1.10, 4.40),
    # Google
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash": (0.15, 0.60),
    "gemini-2.0-flash": (0.10, 0.40),
    # DeepSeek
    "deepseek-r1": (0.55, 2.19),
    "deepseek-chat": (0.27, 1.10),
    # Qwen
    "qwen3-235b-a22b": (0.30, 1.20),
}


class FinishReason(StrEnum):
    """Why the model stopped generating."""
    STOP = "stop"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    ERROR = "error"


@dataclass(slots=True)
class TokenUsage:
    """Token accounting for a single request."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    thinking_tokens: int = 0
    total_cost: float = 0.0

    def calculate_cost(self, model: str) -> float:
        """Compute USD cost from token counts and model pricing."""
        key = _match_pricing_key(model)
        if not key:
            return 0.0
        inp_price, out_price = MODEL_PRICING[key]
        effective_input = self.input_tokens + self.cache_creation_tokens + self.cache_read_tokens
        cost = (effective_input / 1_000_000) * inp_price
        cost += (self.output_tokens / 1_000_000) * out_price
        self.total_cost = round(cost, 6)
        return self.total_cost


def _match_pricing_key(model: str) -> str | None:
    """Find the best matching pricing key for a model string."""
    model_lower = model.lower()
    for key in MODEL_PRICING:
        if key in model_lower:
            return key
    return None


@dataclass(slots=True)
class ToolCall:
    """A single tool invocation from the model."""
    id: str
    name: str
    arguments: str  # JSON string


@dataclass(slots=True)
class ProviderChunk:
    """One piece of a streaming response.

    Accumulate these to reconstruct the full assistant reply.
    """
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    thinking: str = ""
    usage: TokenUsage | None = None
    finish_reason: FinishReason | None = None


@dataclass(slots=True)
class ModelInfo:
    """Static metadata about a model."""
    id: str
    provider: str
    max_context: int = 128_000
    max_output: int = 8_192
    supports_tools: bool = True
    supports_vision: bool = False
    supports_thinking: bool = False
    supports_caching: bool = False
    supports_streaming: bool = True


# -- Model registry for known models --

KNOWN_MODELS: dict[str, ModelInfo] = {
    # Anthropic
    "claude-opus-4-6": ModelInfo(
        id="claude-opus-4-6", provider="anthropic",
        max_context=1_000_000, max_output=32_768,
        supports_tools=True, supports_vision=True,
        supports_thinking=True, supports_caching=True,
    ),
    "claude-sonnet-4-6": ModelInfo(
        id="claude-sonnet-4-6", provider="anthropic",
        max_context=1_000_000, max_output=16_384,
        supports_tools=True, supports_vision=True,
        supports_thinking=True, supports_caching=True,
    ),
    "claude-haiku-4-5": ModelInfo(
        id="claude-haiku-4-5", provider="anthropic",
        max_context=200_000, max_output=8_192,
        supports_tools=True, supports_vision=True,
        supports_thinking=False, supports_caching=True,
    ),
    # OpenAI
    "gpt-4o": ModelInfo(
        id="gpt-4o", provider="openai",
        max_context=128_000, max_output=16_384,
        supports_tools=True, supports_vision=True,
    ),
    "gpt-4o-mini": ModelInfo(
        id="gpt-4o-mini", provider="openai",
        max_context=128_000, max_output=16_384,
        supports_tools=True, supports_vision=True,
    ),
    "gpt-4.1": ModelInfo(
        id="gpt-4.1", provider="openai",
        max_context=1_000_000, max_output=32_768,
        supports_tools=True, supports_vision=True,
    ),
    "gpt-4.1-mini": ModelInfo(
        id="gpt-4.1-mini", provider="openai",
        max_context=1_000_000, max_output=32_768,
        supports_tools=True, supports_vision=True,
    ),
    "gpt-4.1-nano": ModelInfo(
        id="gpt-4.1-nano", provider="openai",
        max_context=1_000_000, max_output=32_768,
        supports_tools=True, supports_vision=True,
    ),
    "o1": ModelInfo(
        id="o1", provider="openai",
        max_context=200_000, max_output=100_000,
        supports_tools=True, supports_thinking=True,
    ),
    "o3": ModelInfo(
        id="o3", provider="openai",
        max_context=200_000, max_output=100_000,
        supports_tools=True, supports_thinking=True,
    ),
    "o3-mini": ModelInfo(
        id="o3-mini", provider="openai",
        max_context=200_000, max_output=100_000,
        supports_tools=True, supports_thinking=True,
    ),
    "o4-mini": ModelInfo(
        id="o4-mini", provider="openai",
        max_context=200_000, max_output=100_000,
        supports_tools=True, supports_thinking=True,
    ),
    # Google
    "gemini-2.5-pro": ModelInfo(
        id="gemini-2.5-pro-preview-03-25", provider="google",
        max_context=1_000_000, max_output=65_536,
        supports_tools=True, supports_vision=True,
        supports_thinking=True,
    ),
    "gemini-2.5-flash": ModelInfo(
        id="gemini-2.5-flash-preview-04-17", provider="google",
        max_context=1_000_000, max_output=65_536,
        supports_tools=True, supports_vision=True,
        supports_thinking=True,
    ),
    "gemini-2.0-flash": ModelInfo(
        id="gemini-2.0-flash", provider="google",
        max_context=1_000_000, max_output=8_192,
        supports_tools=True, supports_vision=True,
    ),
}


# -- Model alias map --

MODEL_ALIASES: dict[str, str] = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
    "gpt4o": "gpt-4o",
    "4o": "gpt-4o",
    "4o-mini": "gpt-4o-mini",
    "4.1": "gpt-4.1",
    "4.1-mini": "gpt-4.1-mini",
    "4.1-nano": "gpt-4.1-nano",
    "gemini": "gemini-2.5-pro",
    "gemini-pro": "gemini-2.5-pro",
    "gemini-flash": "gemini-2.5-flash",
    "deepseek": "deepseek-chat",
    "deepseek-r1": "deepseek-r1",
}


def resolve_model(name: str) -> str:
    """Resolve an alias or shorthand to a canonical model ID."""
    return MODEL_ALIASES.get(name.lower().strip(), name)


def get_model_info(model: str) -> ModelInfo:
    """Look up model info, falling back to sensible defaults for unknown models."""
    resolved = resolve_model(model)
    if resolved in KNOWN_MODELS:
        return KNOWN_MODELS[resolved]
    # Fuzzy substring match
    for key, info in KNOWN_MODELS.items():
        if key in resolved or resolved in key:
            return info
    # Unknown model — conservative defaults
    return ModelInfo(
        id=resolved,
        provider="unknown",
        max_context=128_000,
        max_output=8_192,
    )


@runtime_checkable
class LLMProvider(Protocol):
    """Contract every LLM provider must satisfy."""

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        stream: bool = True,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 8192,
    ) -> AsyncIterator[ProviderChunk]:
        """Stream a chat completion, yielding ProviderChunks."""
        ...

    async def count_tokens(self, text: str) -> int:
        """Estimate token count for text."""
        ...

    def model_info(self) -> ModelInfo:
        """Return static model metadata."""
        ...

    async def close(self) -> None:
        """Release resources (httpx client, etc.)."""
        ...


class BaseProvider:
    """Shared implementation bits inherited by concrete providers."""

    def __init__(self, model: str, api_key: str, base_url: str) -> None:
        import httpx
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=300.0)
        self._request_count = 0
        self._total_usage = TokenUsage()

    def model_info(self) -> ModelInfo:
        return get_model_info(self._model)

    async def count_tokens(self, text: str) -> int:
        """Rough estimate: 1 token per 4 characters. Subclasses may override."""
        return max(1, len(text) // 4)

    async def close(self) -> None:
        await self._client.aclose()

    def _track_usage(self, usage: TokenUsage) -> None:
        """Accumulate usage stats across requests."""
        self._request_count += 1
        self._total_usage.input_tokens += usage.input_tokens
        self._total_usage.output_tokens += usage.output_tokens
        self._total_usage.cache_creation_tokens += usage.cache_creation_tokens
        self._total_usage.cache_read_tokens += usage.cache_read_tokens
        self._total_usage.thinking_tokens += usage.thinking_tokens
        usage.calculate_cost(self._model)
        self._total_usage.total_cost += usage.total_cost

    @staticmethod
    async def _backoff_sleep(attempt: int) -> None:
        """Exponential backoff: 1s, 2s, 4s, 8s, 16s."""
        import asyncio
        delay = min(2 ** attempt, 16)
        await asyncio.sleep(delay)
