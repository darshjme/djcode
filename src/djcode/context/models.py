"""Model Registry — maps model names/patterns to capabilities.

Extensible registry of LLM models with context sizes, feature support,
provider info, and cost data. Supports fuzzy matching so "opus" finds
"claude-opus-4-6" and "gpt4o" finds "gpt-4o".

Users can register custom models at runtime via register_model().
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Any


@dataclass(frozen=True)
class ModelInfo:
    """Capability profile for a single LLM model."""

    name: str
    max_context: int
    supports_tools: bool = True
    supports_vision: bool = False
    supports_thinking: bool = False
    provider: str = "unknown"
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0
    aliases: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Built-in model registry
# ---------------------------------------------------------------------------

_BUILTIN_MODELS: list[ModelInfo] = [
    # -- Anthropic --
    ModelInfo(
        name="claude-opus-4-6",
        max_context=1_000_000,
        supports_tools=True,
        supports_vision=True,
        supports_thinking=True,
        provider="anthropic",
        cost_per_1k_input=0.015,
        cost_per_1k_output=0.075,
        aliases=("opus", "opus-4", "claude-opus", "opus-4-6"),
    ),
    ModelInfo(
        name="claude-sonnet-4-6",
        max_context=1_000_000,
        supports_tools=True,
        supports_vision=True,
        supports_thinking=True,
        provider="anthropic",
        cost_per_1k_input=0.003,
        cost_per_1k_output=0.015,
        aliases=("sonnet", "sonnet-4", "claude-sonnet", "sonnet-4-6"),
    ),
    ModelInfo(
        name="claude-3.5-sonnet",
        max_context=200_000,
        supports_tools=True,
        supports_vision=True,
        supports_thinking=False,
        provider="anthropic",
        cost_per_1k_input=0.003,
        cost_per_1k_output=0.015,
        aliases=("sonnet-3.5", "claude-3.5"),
    ),
    ModelInfo(
        name="claude-3.5-haiku",
        max_context=200_000,
        supports_tools=True,
        supports_vision=True,
        supports_thinking=False,
        provider="anthropic",
        cost_per_1k_input=0.0008,
        cost_per_1k_output=0.004,
        aliases=("haiku", "haiku-3.5", "claude-haiku"),
    ),
    # -- OpenAI --
    ModelInfo(
        name="gpt-4o",
        max_context=128_000,
        supports_tools=True,
        supports_vision=True,
        supports_thinking=False,
        provider="openai",
        cost_per_1k_input=0.0025,
        cost_per_1k_output=0.01,
        aliases=("gpt4o", "4o"),
    ),
    ModelInfo(
        name="gpt-4o-mini",
        max_context=128_000,
        supports_tools=True,
        supports_vision=True,
        supports_thinking=False,
        provider="openai",
        cost_per_1k_input=0.00015,
        cost_per_1k_output=0.0006,
        aliases=("gpt4o-mini", "4o-mini"),
    ),
    ModelInfo(
        name="gpt-4-turbo",
        max_context=128_000,
        supports_tools=True,
        supports_vision=True,
        supports_thinking=False,
        provider="openai",
        cost_per_1k_input=0.01,
        cost_per_1k_output=0.03,
        aliases=("gpt4-turbo", "gpt-4t"),
    ),
    ModelInfo(
        name="o3",
        max_context=200_000,
        supports_tools=True,
        supports_vision=True,
        supports_thinking=True,
        provider="openai",
        cost_per_1k_input=0.01,
        cost_per_1k_output=0.04,
        aliases=("o3-full",),
    ),
    ModelInfo(
        name="o3-mini",
        max_context=200_000,
        supports_tools=True,
        supports_vision=False,
        supports_thinking=True,
        provider="openai",
        cost_per_1k_input=0.0011,
        cost_per_1k_output=0.0044,
        aliases=("o3m",),
    ),
    ModelInfo(
        name="o4-mini",
        max_context=200_000,
        supports_tools=True,
        supports_vision=True,
        supports_thinking=True,
        provider="openai",
        cost_per_1k_input=0.0011,
        cost_per_1k_output=0.0044,
        aliases=("o4m",),
    ),
    # -- Google --
    ModelInfo(
        name="gemini-2.5-pro",
        max_context=1_000_000,
        supports_tools=True,
        supports_vision=True,
        supports_thinking=True,
        provider="google",
        cost_per_1k_input=0.00125,
        cost_per_1k_output=0.01,
        aliases=("gemini-pro", "gemini-2.5", "gemini"),
    ),
    ModelInfo(
        name="gemini-2.5-flash",
        max_context=1_000_000,
        supports_tools=True,
        supports_vision=True,
        supports_thinking=True,
        provider="google",
        cost_per_1k_input=0.00015,
        cost_per_1k_output=0.0006,
        aliases=("gemini-flash", "flash"),
    ),
    # -- Meta (Llama) --
    ModelInfo(
        name="llama3.1",
        max_context=128_000,
        supports_tools=True,
        supports_vision=False,
        supports_thinking=False,
        provider="ollama",
        aliases=("llama-3.1", "llama3.1:8b", "llama3.1:70b"),
    ),
    ModelInfo(
        name="llama3.3",
        max_context=128_000,
        supports_tools=True,
        supports_vision=False,
        supports_thinking=False,
        provider="ollama",
        aliases=("llama-3.3", "llama3.3:70b"),
    ),
    ModelInfo(
        name="llama3",
        max_context=8_192,
        supports_tools=False,
        supports_vision=False,
        supports_thinking=False,
        provider="ollama",
        aliases=("llama-3", "llama3:8b"),
    ),
    ModelInfo(
        name="llama4",
        max_context=10_000_000,
        supports_tools=True,
        supports_vision=True,
        supports_thinking=False,
        provider="ollama",
        aliases=("llama-4", "llama4-maverick", "llama4-scout"),
    ),
    # -- Qwen --
    ModelInfo(
        name="qwen2.5-coder",
        max_context=32_768,
        supports_tools=True,
        supports_vision=False,
        supports_thinking=False,
        provider="ollama",
        aliases=("qwen-coder", "qwen2.5-coder:7b", "qwen2.5-coder:32b"),
    ),
    ModelInfo(
        name="qwen3",
        max_context=128_000,
        supports_tools=True,
        supports_vision=False,
        supports_thinking=True,
        provider="ollama",
        aliases=("qwen-3", "qwen3:32b", "qwen3:8b"),
    ),
    ModelInfo(
        name="qwq",
        max_context=128_000,
        supports_tools=True,
        supports_vision=False,
        supports_thinking=True,
        provider="ollama",
        aliases=("qwq:32b",),
    ),
    # -- DeepSeek --
    ModelInfo(
        name="deepseek-coder-v2",
        max_context=128_000,
        supports_tools=True,
        supports_vision=False,
        supports_thinking=False,
        provider="ollama",
        aliases=("deepseek-coder", "deepseek-v2"),
    ),
    ModelInfo(
        name="deepseek-r1",
        max_context=128_000,
        supports_tools=True,
        supports_vision=False,
        supports_thinking=True,
        provider="ollama",
        aliases=("deepseek-r1:32b", "deepseek-r1:70b"),
    ),
    # -- Mistral --
    ModelInfo(
        name="mistral",
        max_context=32_768,
        supports_tools=True,
        supports_vision=False,
        supports_thinking=False,
        provider="ollama",
        aliases=("mistral:7b", "mistral-7b"),
    ),
    ModelInfo(
        name="mixtral",
        max_context=32_768,
        supports_tools=True,
        supports_vision=False,
        supports_thinking=False,
        provider="ollama",
        aliases=("mixtral:8x7b", "mixtral-8x7b"),
    ),
    ModelInfo(
        name="codestral",
        max_context=32_768,
        supports_tools=True,
        supports_vision=False,
        supports_thinking=False,
        provider="mistral",
        cost_per_1k_input=0.0003,
        cost_per_1k_output=0.0009,
        aliases=("codestral-25.01",),
    ),
    # -- Google (Ollama) --
    ModelInfo(
        name="gemma4",
        max_context=32_768,
        supports_tools=True,
        supports_vision=True,
        supports_thinking=True,
        provider="ollama",
        aliases=("gemma-4", "gemma4:27b", "gemma4:12b"),
    ),
    ModelInfo(
        name="gemma3",
        max_context=32_768,
        supports_tools=True,
        supports_vision=True,
        supports_thinking=False,
        provider="ollama",
        aliases=("gemma-3", "gemma3:27b", "gemma3:12b"),
    ),
    # -- Microsoft --
    ModelInfo(
        name="phi4",
        max_context=16_384,
        supports_tools=True,
        supports_vision=False,
        supports_thinking=False,
        provider="ollama",
        aliases=("phi-4", "phi4:14b"),
    ),
    # -- Cohere --
    ModelInfo(
        name="command-r-plus",
        max_context=128_000,
        supports_tools=True,
        supports_vision=False,
        supports_thinking=False,
        provider="cohere",
        cost_per_1k_input=0.003,
        cost_per_1k_output=0.015,
        aliases=("command-r", "command-r+"),
    ),
    # -- xAI --
    ModelInfo(
        name="grok-3",
        max_context=131_072,
        supports_tools=True,
        supports_vision=True,
        supports_thinking=True,
        provider="xai",
        cost_per_1k_input=0.003,
        cost_per_1k_output=0.015,
        aliases=("grok", "grok3"),
    ),
    # -- NVIDIA --
    ModelInfo(
        name="nemotron-ultra",
        max_context=128_000,
        supports_tools=True,
        supports_vision=False,
        supports_thinking=True,
        provider="nvidia",
        aliases=("nemotron", "nemotron-ultra-253b"),
    ),
    # -- Small / legacy models --
    ModelInfo(
        name="tinyllama",
        max_context=2_048,
        supports_tools=False,
        supports_vision=False,
        supports_thinking=False,
        provider="ollama",
        aliases=("tinyllama:1.1b",),
    ),
    ModelInfo(
        name="dolphin3",
        max_context=4_096,
        supports_tools=False,
        supports_vision=False,
        supports_thinking=False,
        provider="ollama",
        aliases=("dolphin", "dolphin3:8b"),
    ),
    ModelInfo(
        name="starcoder2",
        max_context=16_384,
        supports_tools=False,
        supports_vision=False,
        supports_thinking=False,
        provider="ollama",
        aliases=("starcoder", "starcoder2:15b"),
    ),
]


# ---------------------------------------------------------------------------
# Mutable registry: built-in + user-added
# ---------------------------------------------------------------------------

MODEL_REGISTRY: dict[str, ModelInfo] = {}

# Index by name and aliases
for _model in _BUILTIN_MODELS:
    MODEL_REGISTRY[_model.name.lower()] = _model
    for _alias in _model.aliases:
        MODEL_REGISTRY[_alias.lower()] = _model


# ---------------------------------------------------------------------------
# Fuzzy matching engine
# ---------------------------------------------------------------------------

def _normalize(name: str) -> str:
    """Normalize a model name for matching: lowercase, strip whitespace."""
    return name.strip().lower()


def _fuzzy_match(query: str, candidates: dict[str, ModelInfo]) -> ModelInfo | None:
    """Multi-stage fuzzy match against the registry.

    Match pipeline:
    1. Exact key match
    2. Key starts with query (prefix)
    3. Query appears as substring in key
    4. Key appears as substring in query (e.g. query="claude-opus-4-6-20260410")
    5. difflib close match (Levenshtein-ish)
    """
    q = _normalize(query)

    # 1. Exact match
    if q in candidates:
        return candidates[q]

    # 2. Prefix match — query is a prefix of a registry key
    prefix_hits = [(k, v) for k, v in candidates.items() if k.startswith(q)]
    if len(prefix_hits) == 1:
        return prefix_hits[0][1]
    if prefix_hits:
        # Prefer shortest key (closest match)
        return min(prefix_hits, key=lambda kv: len(kv[0]))[1]

    # 3. Substring match — query appears inside a registry key
    sub_hits = [(k, v) for k, v in candidates.items() if q in k]
    if len(sub_hits) == 1:
        return sub_hits[0][1]
    if sub_hits:
        return min(sub_hits, key=lambda kv: len(kv[0]))[1]

    # 4. Reverse substring — registry key appears inside query
    # Handles versioned model names like "claude-opus-4-6-20260410"
    rev_hits = [(k, v) for k, v in candidates.items() if k in q]
    if rev_hits:
        # Prefer the longest matching key (most specific)
        return max(rev_hits, key=lambda kv: len(kv[0]))[1]

    # 5. difflib fuzzy match
    close = get_close_matches(q, list(candidates.keys()), n=1, cutoff=0.5)
    if close:
        return candidates[close[0]]

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_model_info(model: str) -> ModelInfo | None:
    """Look up full model info by name, alias, or fuzzy match.

    Returns None if no match found. Callers should fall back to defaults.

    Examples:
        get_model_info("opus")            -> claude-opus-4-6
        get_model_info("gpt4o")           -> gpt-4o
        get_model_info("qwen-coder")      -> qwen2.5-coder
        get_model_info("unknown-model")   -> None
    """
    return _fuzzy_match(model, MODEL_REGISTRY)


def get_context_size(model: str, default: int = 8_192) -> int:
    """Get the context window size for a model.

    Falls back to *default* if model is not in the registry.
    This is the primary function the ContextWindowManager uses.
    """
    info = get_model_info(model)
    return info.max_context if info else default


def supports_tools(model: str) -> bool:
    """Check if a model supports tool/function calling.

    Returns True by default for unknown models (optimistic).
    """
    info = get_model_info(model)
    return info.supports_tools if info else True


def supports_vision(model: str) -> bool:
    """Check if a model supports vision/image inputs.

    Returns False by default for unknown models (conservative).
    """
    info = get_model_info(model)
    return info.supports_vision if info else False


def supports_thinking(model: str) -> bool:
    """Check if a model supports extended thinking / chain-of-thought.

    Returns False by default for unknown models (conservative).
    """
    info = get_model_info(model)
    return info.supports_thinking if info else False


def register_model(
    name: str,
    max_context: int,
    *,
    supports_tools: bool = True,
    supports_vision: bool = False,
    supports_thinking: bool = False,
    provider: str = "custom",
    cost_per_1k_input: float = 0.0,
    cost_per_1k_output: float = 0.0,
    aliases: tuple[str, ...] | list[str] = (),
) -> ModelInfo:
    """Register a custom model at runtime.

    Overwrites existing entries if name/alias collides.
    Returns the created ModelInfo for reference.

    Example:
        register_model(
            "my-finetune",
            max_context=16_384,
            provider="ollama",
            aliases=("finetune", "ft-v1"),
        )
    """
    info = ModelInfo(
        name=name,
        max_context=max_context,
        supports_tools=supports_tools,
        supports_vision=supports_vision,
        supports_thinking=supports_thinking,
        provider=provider,
        cost_per_1k_input=cost_per_1k_input,
        cost_per_1k_output=cost_per_1k_output,
        aliases=tuple(aliases),
    )

    MODEL_REGISTRY[name.lower()] = info
    for alias in aliases:
        MODEL_REGISTRY[alias.lower()] = info

    return info


def list_models(provider: str | None = None) -> list[ModelInfo]:
    """List all unique registered models, optionally filtered by provider.

    Returns deduplicated list sorted by context size (largest first).
    """
    seen: set[str] = set()
    models: list[ModelInfo] = []

    for info in MODEL_REGISTRY.values():
        if info.name in seen:
            continue
        if provider and info.provider != provider:
            continue
        seen.add(info.name)
        models.append(info)

    models.sort(key=lambda m: m.max_context, reverse=True)
    return models


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> float | None:
    """Estimate the cost in USD for a given token usage.

    Returns None if model not found or has no cost data.
    """
    info = get_model_info(model)
    if not info:
        return None
    if info.cost_per_1k_input == 0.0 and info.cost_per_1k_output == 0.0:
        return None  # Local/free model

    input_cost = (input_tokens / 1000) * info.cost_per_1k_input
    output_cost = (output_tokens / 1000) * info.cost_per_1k_output
    return round(input_cost + output_cost, 6)
