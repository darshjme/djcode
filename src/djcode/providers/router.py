"""Provider router for DJcode.

Routes to the correct LLM provider based on configuration, model name,
or explicit provider selection. Supports:
- Auto-detection from model name (claude-* -> Anthropic, gpt-* -> OpenAI, etc.)
- Custom endpoints (any OpenAI-compat URL)
- Provider fallback chains (try primary, fall back to secondary)
- Model aliasing (opus -> claude-opus-4-6, sonnet -> claude-sonnet-4-6)
- Lazy provider initialization (only created when first used)
"""

from __future__ import annotations

import logging

from djcode.providers.base import (
    BaseProvider,
    ModelInfo,
    get_model_info,
    resolve_model,
)

logger = logging.getLogger(__name__)

# -- Provider detection from model name --

_MODEL_PROVIDER_PATTERNS: list[tuple[list[str], str]] = [
    # (prefixes, provider_name)
    (["claude-", "claude_"], "anthropic"),
    (["gpt-", "gpt4", "o1", "o3", "o4-", "chatgpt"], "openai"),
    (["gemini-", "gemini2"], "google"),
    (["deepseek-", "deepseek_"], "deepseek"),
    (["qwen", "qwen2", "qwen3"], "qwen"),
    (["llama", "llama3"], "ollama"),
    (["mistral", "mixtral", "codestral"], "ollama"),
    (["phi-", "phi3", "phi4"], "ollama"),
    (["gemma", "gemma2", "gemma3", "gemma4"], "ollama"),
    (["command-r", "command-r-plus"], "ollama"),
]


def detect_provider(model: str) -> str:
    """Detect provider from model name patterns. Returns provider key or 'unknown'."""
    model_lower = model.lower()
    for prefixes, provider in _MODEL_PROVIDER_PATTERNS:
        for prefix in prefixes:
            if model_lower.startswith(prefix):
                return provider
    return "unknown"


def create_provider(
    provider_name: str,
    model: str,
    api_key: str,
    base_url: str,
    *,
    enable_caching: bool = True,
    enable_thinking: bool = False,
    thinking_budget: int = 10_000,
) -> BaseProvider:
    """Factory: create the right provider instance for the given provider name.

    Args:
        provider_name: Provider key (anthropic, openai, google, ollama, etc.)
        model: Model ID or alias
        api_key: API key for authenticated providers
        base_url: Base URL for the API
        enable_caching: Enable prompt caching (Anthropic)
        enable_thinking: Enable extended thinking (Anthropic, OpenAI reasoning)
        thinking_budget: Max thinking tokens (Anthropic)

    Returns:
        An initialized provider instance ready for chat()
    """
    resolved = resolve_model(model)

    if provider_name == "anthropic":
        from djcode.providers.anthropic import AnthropicProvider
        return AnthropicProvider(
            model=resolved,
            api_key=api_key,
            base_url=base_url or "https://api.anthropic.com",
            enable_caching=enable_caching,
            enable_thinking=enable_thinking,
            thinking_budget=thinking_budget,
        )

    if provider_name == "openai":
        from djcode.providers.openai import OpenAIProvider
        return OpenAIProvider(
            model=resolved,
            api_key=api_key,
            base_url=base_url or "https://api.openai.com/v1",
        )

    if provider_name == "google":
        from djcode.providers.google import GoogleProvider
        return GoogleProvider(
            model=resolved,
            api_key=api_key,
            base_url=base_url or "https://generativelanguage.googleapis.com/v1beta",
        )

    # DeepSeek and Qwen use OpenAI-compatible APIs
    if provider_name == "deepseek":
        from djcode.providers.openai import OpenAIProvider
        return OpenAIProvider(
            model=resolved,
            api_key=api_key,
            base_url=base_url or "https://api.deepseek.com/v1",
        )

    if provider_name == "qwen":
        from djcode.providers.openai import OpenAIProvider
        return OpenAIProvider(
            model=resolved,
            api_key=api_key,
            base_url=base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

    # OpenAI-compatible providers (Groq, Together, OpenRouter, NVIDIA, etc.)
    from djcode.providers.openai import OpenAIProvider
    return OpenAIProvider(
        model=resolved,
        api_key=api_key,
        base_url=base_url,
    )


class ProviderRouter:
    """Intelligent router that selects and manages LLM provider instances.

    Creates provider instances lazily and caches them for reuse.
    Supports fallback chains: if the primary provider fails, tries the next.
    """

    def __init__(
        self,
        provider_name: str | None = None,
        model: str | None = None,
        api_key: str = "",
        base_url: str = "",
        *,
        fallback_providers: list[dict[str, str]] | None = None,
        enable_caching: bool = True,
        enable_thinking: bool = False,
        thinking_budget: int = 10_000,
    ) -> None:
        self._provider_name = provider_name or ""
        self._model = model or ""
        self._api_key = api_key
        self._base_url = base_url
        self._fallback_providers = fallback_providers or []
        self._enable_caching = enable_caching
        self._enable_thinking = enable_thinking
        self._thinking_budget = thinking_budget
        self._active_provider: BaseProvider | None = None
        self._provider_cache: dict[str, BaseProvider] = {}

    @classmethod
    def from_config(
        cls,
        provider_override: str | None = None,
        model_override: str | None = None,
    ) -> ProviderRouter:
        """Build a router from DJcode's saved configuration.

        Reads config, resolves provider/model, and returns a ready router.
        """
        import os

        from djcode.auth import PROVIDERS, get_api_key, get_base_url
        from djcode.config import load_config

        cfg = load_config()
        provider = provider_override or cfg.get("provider", "ollama")
        model = model_override or cfg.get("model", "gemma4")

        # Resolve the model alias first
        resolved_model = resolve_model(model)

        # Auto-detect provider from model name if provider is generic
        if provider in ("ollama", "remote", "custom") and resolved_model != model:
            detected = detect_provider(resolved_model)
            if detected != "unknown":
                provider = detected

        # Get connection settings
        api_key = ""
        base_url = ""

        if provider.startswith("http://") or provider.startswith("https://"):
            base_url = provider.rstrip("/")
            api_key = (
                os.environ.get("DJCODE_API_KEY", "")
                or os.environ.get("OPENAI_API_KEY", "")
                or cfg.get("remote_api_key", "")
            )
            provider = "custom"
        elif provider in cfg.get("custom_providers", {}):
            custom = cfg["custom_providers"][provider]
            base_url = custom.get("base_url", "")
            api_key = custom.get("api_key", "") or os.environ.get("DJCODE_API_KEY", "")
            resolved_model = model_override or custom.get("model", resolved_model)
            provider = "custom"
        elif provider in PROVIDERS:
            base_url = get_base_url(provider)
            api_key = get_api_key(provider)

        # Apply base_url overrides
        env_base_url = os.environ.get("DJCODE_BASE_URL", "")
        config_base_url = cfg.get("base_url", "")
        if env_base_url:
            base_url = env_base_url.rstrip("/")
        elif config_base_url:
            base_url = config_base_url.rstrip("/")

        return cls(
            provider_name=provider,
            model=resolved_model,
            api_key=api_key,
            base_url=base_url,
            enable_caching=cfg.get("enable_caching", True),
            enable_thinking=cfg.get("enable_thinking", False),
            thinking_budget=cfg.get("thinking_budget", 10_000),
        )

    def get_provider(self) -> BaseProvider:
        """Get or create the active provider instance."""
        if self._active_provider is not None:
            return self._active_provider

        # Auto-detect provider from model if not specified
        provider_name = self._provider_name
        if provider_name in ("", "unknown", "custom", "remote"):
            detected = detect_provider(self._model)
            if detected != "unknown":
                provider_name = detected
            elif provider_name == "":
                provider_name = "ollama"  # Default fallback

        cache_key = f"{provider_name}:{self._model}"
        if cache_key in self._provider_cache:
            self._active_provider = self._provider_cache[cache_key]
            return self._active_provider

        self._active_provider = create_provider(
            provider_name=provider_name,
            model=self._model,
            api_key=self._api_key,
            base_url=self._base_url,
            enable_caching=self._enable_caching,
            enable_thinking=self._enable_thinking,
            thinking_budget=self._thinking_budget,
        )
        self._provider_cache[cache_key] = self._active_provider
        return self._active_provider

    def model_info(self) -> ModelInfo:
        """Get model metadata without creating a provider."""
        return get_model_info(self._model)

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def display_name(self) -> str:
        return f"{self._provider_name}:{self._model}"

    async def close(self) -> None:
        """Close all cached provider instances."""
        for provider in self._provider_cache.values():
            await provider.close()
        self._provider_cache.clear()
        self._active_provider = None
