"""Context Window Manager for DJcode.

Tracks and manages the conversation's token usage with model-aware limits.
This is the brain of the 1M context engine -- it knows how much space you
have, what's filling it, and when to trigger compression.

Features:
- Token counting per message (tiktoken for OpenAI, estimate for others)
- Model-aware context limits from the model registry
- Real-time usage tracking: current_tokens, max_tokens, utilization_pct
- Smart message prioritization: system > pinned > recent user > recent assistant > old
- Automatic compression at configurable utilization threshold (default 80%)
- Context injection with priority levels for files, memory, agent results
- Pinned messages that survive compression
- Full integration with the model registry and conversation compressor
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING, Any

from djcode.context.compressor import (
    CompressionResult,
    CompressionStrategy,
    ConversationCompressor,
    _count_tokens,
    _message_tokens,
    _total_tokens,
)
from djcode.context.models import get_context_size, get_model_info

if TYPE_CHECKING:
    from djcode.provider import Message, Provider


# ---------------------------------------------------------------------------
# Priority levels for injected context
# ---------------------------------------------------------------------------

class Priority(IntEnum):
    """Priority levels for context injection. Higher = more important."""

    LOW = 10        # Nice-to-have background info
    NORMAL = 50     # Standard context (memory recalls, file contents)
    HIGH = 80       # Important context (recent file edits, errors)
    CRITICAL = 100  # Must-have context (user instructions, project rules)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ContextStats:
    """Snapshot of context window utilization."""

    model: str
    max_context_tokens: int
    current_tokens: int
    message_count: int
    pinned_count: int
    injected_count: int
    injected_tokens: int
    utilization_pct: float
    remaining_tokens: int
    compression_triggered: bool
    compressions_performed: int
    last_compression_strategy: str | None
    last_compression_ratio: float

    def to_dict(self) -> dict[str, Any]:
        """Serialize for display or logging."""
        return {
            "model": self.model,
            "max_context": self.max_context_tokens,
            "current_tokens": self.current_tokens,
            "messages": self.message_count,
            "pinned": self.pinned_count,
            "injected": self.injected_count,
            "injected_tokens": self.injected_tokens,
            "utilization": f"{self.utilization_pct:.1f}%",
            "remaining": self.remaining_tokens,
            "compressions": self.compressions_performed,
            "last_strategy": self.last_compression_strategy,
            "last_ratio": f"{self.last_compression_ratio:.2f}",
        }


@dataclass
class InjectedContext:
    """A piece of context injected into the conversation."""

    content: str
    priority: int
    source: str  # Where this came from (e.g., "memory", "file", "agent")
    tokens: int
    injected_at: float = field(default_factory=time.time)
    ttl: float = 0.0  # Time-to-live in seconds. 0 = permanent until evicted.

    @property
    def is_expired(self) -> bool:
        if self.ttl <= 0:
            return False
        return (time.time() - self.injected_at) > self.ttl


# ---------------------------------------------------------------------------
# ContextWindowManager
# ---------------------------------------------------------------------------

class ContextWindowManager:
    """Manages the conversation's context window with model-aware limits.

    Usage:
        manager = ContextWindowManager("claude-opus-4-6")
        manager.add_message(system_prompt, pinned=True)
        manager.add_message(user_msg)
        manager.inject_context(file_contents, priority=Priority.HIGH)

        if manager.needs_compression():
            await manager.auto_compress()

        messages = manager.get_messages()  # optimized, ready for LLM
    """

    def __init__(
        self,
        model: str,
        max_context: int | None = None,
        compression_threshold: float = 0.80,
        provider: Provider | None = None,
    ) -> None:
        """Initialize the context window manager.

        Args:
            model: Model name (used for registry lookup).
            max_context: Override context size. If None, looked up from registry.
            compression_threshold: Utilization ratio (0.0-1.0) that triggers compression.
            provider: Optional LLM provider for summary-based compression.
        """
        self._model = model
        self._max_context = max_context or get_context_size(model, default=8_192)
        self._compression_threshold = compression_threshold
        self._provider = provider

        # Message storage
        self._messages: list[Message] = []
        self._pinned_indices: set[int] = set()  # Indices of pinned messages

        # Injected context
        self._injected: list[InjectedContext] = []

        # Compression state
        self._compressor = ConversationCompressor(provider=provider)
        self._compressions_performed: int = 0
        self._last_compression_strategy: str | None = None
        self._last_compression_ratio: float = 0.0

        # Token cache (invalidated on any mutation)
        self._cached_tokens: int | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def model(self) -> str:
        return self._model

    @property
    def max_context(self) -> int:
        return self._max_context

    @max_context.setter
    def max_context(self, value: int) -> None:
        self._max_context = value
        self._invalidate_cache()

    @property
    def current_tokens(self) -> int:
        """Total tokens across all messages + injected context."""
        if self._cached_tokens is None:
            msg_tokens = _total_tokens(self._messages)
            inj_tokens = sum(ic.tokens for ic in self._injected if not ic.is_expired)
            self._cached_tokens = msg_tokens + inj_tokens
        return self._cached_tokens

    @property
    def remaining_tokens(self) -> int:
        """How many tokens remain before hitting the context limit."""
        return max(0, self._max_context - self.current_tokens)

    @property
    def utilization(self) -> float:
        """Current context utilization as a ratio (0.0 to 1.0)."""
        if self._max_context <= 0:
            return 1.0
        return min(1.0, self.current_tokens / self._max_context)

    @property
    def utilization_pct(self) -> float:
        """Current context utilization as a percentage."""
        return self.utilization * 100

    @property
    def message_count(self) -> int:
        return len(self._messages)

    @property
    def stats(self) -> ContextStats:
        """Get a full snapshot of current context window state."""
        active_injected = [ic for ic in self._injected if not ic.is_expired]
        return ContextStats(
            model=self._model,
            max_context_tokens=self._max_context,
            current_tokens=self.current_tokens,
            message_count=len(self._messages),
            pinned_count=len(self._pinned_indices),
            injected_count=len(active_injected),
            injected_tokens=sum(ic.tokens for ic in active_injected),
            utilization_pct=self.utilization_pct,
            remaining_tokens=self.remaining_tokens,
            compression_triggered=self.needs_compression(),
            compressions_performed=self._compressions_performed,
            last_compression_strategy=self._last_compression_strategy,
            last_compression_ratio=self._last_compression_ratio,
        )

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def _invalidate_cache(self) -> None:
        self._cached_tokens = None

    # ------------------------------------------------------------------
    # Message management
    # ------------------------------------------------------------------

    def add_message(self, msg: Message, pinned: bool = False) -> None:
        """Add a message to the conversation.

        Args:
            msg: The message to add.
            pinned: If True, this message survives compression.
        """
        self._messages.append(msg)
        idx = len(self._messages) - 1

        if pinned:
            self._pinned_indices.add(idx)
            # Set a marker on the message object for the compressor
            msg._pinned = True  # type: ignore[attr-defined]

        self._invalidate_cache()

    def add_messages(self, msgs: list[Message]) -> None:
        """Add multiple messages at once."""
        for msg in msgs:
            self.add_message(msg)

    def get_messages(self) -> list[Message]:
        """Get the optimized message list for sending to the LLM.

        This builds the final message list by:
        1. Starting with all conversation messages
        2. Injecting context (sorted by priority) after system messages
        3. Evicting expired injected context
        4. Ensuring total stays within the context window

        The returned list is a new list -- the internal state is not modified.
        """
        from djcode.provider import Message as Msg

        # Clean up expired injections
        self._injected = [ic for ic in self._injected if not ic.is_expired]
        self._invalidate_cache()

        # Build base message list
        result = list(self._messages)

        # Build injected context messages (sorted by priority, highest first)
        active_injected = sorted(self._injected, key=lambda ic: ic.priority, reverse=True)

        if active_injected:
            # Combine injected context into a single system message
            # to avoid polluting the conversation with many small messages
            injection_parts: list[str] = []
            for ic in active_injected:
                injection_parts.append(f"[{ic.source}]\n{ic.content}")

            injection_text = "\n\n---\n\n".join(injection_parts)
            injection_msg = Msg(
                role="system",
                content=f"[Injected context]\n{injection_text}",
            )

            # Insert after system messages, before conversation
            insert_idx = 0
            for i, msg in enumerate(result):
                if msg.role == "system":
                    insert_idx = i + 1
                else:
                    break

            result.insert(insert_idx, injection_msg)

        return result

    def clear_messages(self) -> None:
        """Clear all messages and injected context."""
        self._messages.clear()
        self._pinned_indices.clear()
        self._injected.clear()
        self._invalidate_cache()

    def replace_messages(self, messages: list[Message]) -> None:
        """Replace the entire message list (used after compression)."""
        self._messages = list(messages)
        # Rebuild pinned indices
        self._pinned_indices.clear()
        for i, msg in enumerate(self._messages):
            if getattr(msg, "_pinned", False):
                self._pinned_indices.add(i)
        self._invalidate_cache()

    # ------------------------------------------------------------------
    # Context injection
    # ------------------------------------------------------------------

    def inject_context(
        self,
        content: str,
        priority: int = Priority.NORMAL,
        source: str = "context",
        ttl: float = 0.0,
    ) -> None:
        """Inject additional context into the conversation.

        Injected context appears as system messages when get_messages() is called.
        Higher priority context is placed first and survives eviction longer.

        Args:
            content: The text content to inject.
            priority: Priority level (use Priority enum or raw int).
            source: Label for where this came from.
            ttl: Time-to-live in seconds. 0 means permanent until explicitly cleared.
        """
        if not content or not content.strip():
            return

        tokens = _count_tokens(content)

        self._injected.append(InjectedContext(
            content=content.strip(),
            priority=priority,
            source=source,
            tokens=tokens,
            ttl=ttl,
        ))

        self._invalidate_cache()

    def clear_injected(self, source: str | None = None) -> int:
        """Clear injected context, optionally filtered by source.

        Returns the number of items cleared.
        """
        if source is None:
            count = len(self._injected)
            self._injected.clear()
        else:
            before = len(self._injected)
            self._injected = [ic for ic in self._injected if ic.source != source]
            count = before - len(self._injected)

        self._invalidate_cache()
        return count

    def evict_lowest_priority(self, tokens_to_free: int) -> int:
        """Evict injected context starting from lowest priority until enough tokens freed.

        Returns actual tokens freed.
        """
        if not self._injected:
            return 0

        # Sort by priority ascending (lowest first to evict)
        self._injected.sort(key=lambda ic: ic.priority)

        freed = 0
        to_remove: list[int] = []

        for i, ic in enumerate(self._injected):
            if freed >= tokens_to_free:
                break
            freed += ic.tokens
            to_remove.append(i)

        # Remove in reverse order to preserve indices
        for i in reversed(to_remove):
            self._injected.pop(i)

        self._invalidate_cache()
        return freed

    # ------------------------------------------------------------------
    # Compression
    # ------------------------------------------------------------------

    def needs_compression(self) -> bool:
        """Check if the context window utilization exceeds the threshold."""
        return self.utilization >= self._compression_threshold

    def _pick_strategy(self) -> CompressionStrategy:
        """Automatically choose the best compression strategy based on context.

        Logic:
        - First compression: SELECTIVE (gentle, keeps tool data)
        - Second compression: HYBRID if provider available, else TRIM
        - Third+ compression: TRIM (aggressive, just make space)
        """
        if self._compressions_performed == 0:
            return CompressionStrategy.SELECTIVE
        elif self._compressions_performed == 1:
            if self._provider is not None:
                return CompressionStrategy.HYBRID
            return CompressionStrategy.TRIM
        else:
            return CompressionStrategy.TRIM

    async def auto_compress(
        self,
        strategy: CompressionStrategy | str | None = None,
        target_utilization: float = 0.60,
    ) -> CompressionResult:
        """Automatically compress the conversation to free context space.

        Args:
            strategy: Override the auto-selected strategy. If None, picks based on history.
            target_utilization: Target utilization after compression (0.0-1.0).

        Returns:
            CompressionResult with details of what happened.
        """
        target_tokens = int(self._max_context * target_utilization)

        if strategy is None:
            strat = self._pick_strategy()
        elif isinstance(strategy, str):
            strat = CompressionStrategy(strategy.lower())
        else:
            strat = strategy

        # Run compression
        result = await self._compressor.compress(
            messages=self._messages,
            strategy=strat,
            target_tokens=target_tokens,
            keep_recent=max(5, len(self._messages) // 5),  # Keep at least 20%
        )

        # Apply the compressed messages
        self.replace_messages(result.messages)

        # Update compression tracking
        self._compressions_performed += 1
        self._last_compression_strategy = result.strategy_used.value
        self._last_compression_ratio = result.compression_ratio

        # If still over threshold after compression, evict low-priority injected context
        if self.needs_compression() and self._injected:
            excess = self.current_tokens - target_tokens
            self.evict_lowest_priority(excess)

        return result

    # ------------------------------------------------------------------
    # Token counting utilities
    # ------------------------------------------------------------------

    @staticmethod
    def count_tokens(text: str) -> int:
        """Count tokens in a text string.

        Uses tiktoken (cl100k_base encoding) if available, otherwise
        estimates at ~4 characters per token.
        """
        return _count_tokens(text)

    @staticmethod
    def count_message_tokens(msg: Message) -> int:
        """Count tokens in a single message including overhead."""
        return _message_tokens(msg)

    # ------------------------------------------------------------------
    # Model awareness
    # ------------------------------------------------------------------

    def switch_model(self, model: str, max_context: int | None = None) -> None:
        """Switch to a different model, adjusting context limits.

        If the new model has a smaller context window and current usage
        exceeds it, this is flagged in stats but NOT auto-compressed --
        the caller should check needs_compression() and act accordingly.
        """
        self._model = model
        self._max_context = max_context or get_context_size(model, default=8_192)
        self._invalidate_cache()

    def get_model_info(self) -> dict[str, Any]:
        """Get full model info from the registry."""
        info = get_model_info(self._model)
        if info is None:
            return {
                "name": self._model,
                "max_context": self._max_context,
                "provider": "unknown",
                "in_registry": False,
            }
        return {
            "name": info.name,
            "max_context": info.max_context,
            "provider": info.provider,
            "supports_tools": info.supports_tools,
            "supports_vision": info.supports_vision,
            "supports_thinking": info.supports_thinking,
            "cost_per_1k_input": info.cost_per_1k_input,
            "cost_per_1k_output": info.cost_per_1k_output,
            "in_registry": True,
        }

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Serialize the manager state for persistence or debugging.

        Does NOT include the provider (not serializable).
        """
        return {
            "model": self._model,
            "max_context": self._max_context,
            "compression_threshold": self._compression_threshold,
            "message_count": len(self._messages),
            "pinned_count": len(self._pinned_indices),
            "injected_count": len(self._injected),
            "compressions_performed": self._compressions_performed,
            "last_compression_strategy": self._last_compression_strategy,
            "last_compression_ratio": self._last_compression_ratio,
            "current_tokens": self.current_tokens,
            "utilization_pct": self.utilization_pct,
            "remaining_tokens": self.remaining_tokens,
        }

    def __repr__(self) -> str:
        return (
            f"ContextWindowManager("
            f"model={self._model!r}, "
            f"tokens={self.current_tokens}/{self._max_context}, "
            f"util={self.utilization_pct:.1f}%, "
            f"msgs={len(self._messages)}, "
            f"pinned={len(self._pinned_indices)}"
            f")"
        )
