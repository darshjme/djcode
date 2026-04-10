"""Smart Conversation Compression for DJcode.

Compresses old messages to free context space using multiple strategies:
- TRIM: Drop oldest messages, keep system + last N
- SELECTIVE: Keep messages with tool calls/results, drop plain chat
- SUMMARY: Replace old messages with an LLM-generated summary (requires provider)
- HYBRID: Summarize old chat, keep all tool interactions verbatim

TRIM and SELECTIVE work without any LLM call. SUMMARY and HYBRID optionally
use the LLM for higher-quality compression.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from djcode.provider import Message, Provider


class CompressionStrategy(str, Enum):
    """Available compression strategies, ordered by aggressiveness."""

    TRIM = "trim"
    SELECTIVE = "selective"
    SUMMARY = "summary"
    HYBRID = "hybrid"


@dataclass
class CompressionResult:
    """Result of a compression operation."""

    messages: list[Message]
    original_tokens: int
    compressed_tokens: int
    strategy_used: CompressionStrategy
    messages_removed: int
    summary_text: str = ""

    @property
    def compression_ratio(self) -> float:
        """How much smaller the result is (0.0 = no change, 1.0 = everything removed)."""
        if self.original_tokens == 0:
            return 0.0
        return 1.0 - (self.compressed_tokens / self.original_tokens)

    @property
    def tokens_freed(self) -> int:
        return self.original_tokens - self.compressed_tokens


# ---------------------------------------------------------------------------
# Token estimation (shared utility)
# ---------------------------------------------------------------------------

def _count_tokens(text: str) -> int:
    """Estimate token count. Uses tiktoken if available, else ~4 chars/token."""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except (ImportError, Exception):
        return max(1, len(text) // 4)


def _message_tokens(msg: Message) -> int:
    """Count tokens in a single message including role overhead."""
    tokens = _count_tokens(msg.content or "")
    # Role/name overhead: ~4 tokens for message framing
    tokens += 4
    # Tool calls add significant tokens
    if msg.tool_calls:
        import json
        for tc in msg.tool_calls:
            tokens += _count_tokens(json.dumps(tc, default=str))
    if msg.name:
        tokens += _count_tokens(msg.name)
    return tokens


def _total_tokens(messages: list[Message]) -> int:
    """Total tokens across all messages."""
    return sum(_message_tokens(m) for m in messages)


# ---------------------------------------------------------------------------
# Extractive summarizer (no LLM needed)
# ---------------------------------------------------------------------------

_ACTION_WORDS = frozenset({
    "created", "fixed", "added", "removed", "updated", "changed",
    "implemented", "built", "wrote", "deployed", "configured",
    "installed", "error", "bug", "issue", "working", "failed",
    "success", "complete", "moved", "deleted", "refactored",
    "tested", "merged", "committed", "resolved", "found",
    "modified", "set", "enabled", "disabled", "connected",
})

_IMPORTANCE_WORDS = frozenset({
    "important", "critical", "must", "should", "need", "required",
    "decision", "architecture", "design", "plan", "goal", "next",
    "todo", "blocker", "priority", "key", "note", "warning",
})


def extractive_summarize(messages: list[Message], max_sentences: int = 15) -> str:
    """Extract the most important sentences from messages without an LLM.

    Scoring factors:
    - Presence of action keywords (created, fixed, deployed, etc.)
    - Presence of importance keywords (critical, must, decision, etc.)
    - Recency (newer messages score higher)
    - Code references (backticks, file paths)
    - Sentence length (penalize too short/long)

    Returns a "Previously:" formatted block.
    """
    if not messages:
        return ""

    # Gather text from messages
    texts: list[str] = []
    for msg in messages:
        content = msg.content or ""
        if content.strip():
            texts.append(content.strip())

    if not texts:
        return ""

    # Split into sentences
    all_sentences: list[tuple[str, int]] = []  # (sentence, message_index)
    for idx, text in enumerate(texts):
        parts = re.split(r"(?<=[.!?])\s+", text)
        for part in parts:
            part = part.strip()
            if len(part) > 15:
                all_sentences.append((part, idx))

    if not all_sentences:
        # Fallback: use raw texts truncated
        lines = [f"- {t[:200]}" for t in texts[-max_sentences:]]
        return "Previously:\n" + "\n".join(lines)

    # Score each sentence
    total_msgs = len(texts)
    scored: list[tuple[float, str]] = []

    for sentence, msg_idx in all_sentences:
        words = set(sentence.lower().split())
        score = 0.0

        # Action keyword boost
        score += len(words & _ACTION_WORDS) * 2.0

        # Importance keyword boost
        score += len(words & _IMPORTANCE_WORDS) * 1.5

        # Recency boost (linear from 0 to 3)
        recency = (msg_idx + 1) / max(total_msgs, 1)
        score += recency * 3.0

        # Code reference boost
        if any(ch in sentence for ch in ("`", "```", "()", "->", "=>", "/")):
            score += 1.0

        # File path boost (looks like a path)
        if re.search(r"[\w/]+\.\w{1,5}", sentence):
            score += 0.5

        # Length penalty
        slen = len(sentence)
        if slen < 30:
            score *= 0.5
        elif slen > 500:
            score *= 0.7

        # Declarative sentence boost
        if re.match(r"^(I |We |The |This |That |It |Let's |Next )", sentence):
            score += 0.8

        scored.append((score, sentence))

    # Select top sentences, re-sort by original order
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:max_sentences]

    sentence_order = {s: i for i, (s, _) in enumerate(all_sentences)}
    top_sorted = sorted(
        [s for _, s in top],
        key=lambda s: sentence_order.get(s, 0),
    )

    lines = []
    for s in top_sorted:
        if len(s) > 250:
            s = s[:247] + "..."
        lines.append(f"- {s}")

    return "Previously:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# ConversationCompressor
# ---------------------------------------------------------------------------

class ConversationCompressor:
    """Compresses conversation history to reclaim context space.

    All strategies preserve:
    - System messages (always first)
    - Pinned messages (marked with _pinned attribute)
    - The most recent N messages

    The provider is only needed for SUMMARY and HYBRID strategies.
    TRIM and SELECTIVE are fully local.
    """

    def __init__(self, provider: Provider | None = None) -> None:
        self._provider = provider

    def _is_pinned(self, msg: Message) -> bool:
        """Check if a message is pinned (should never be compressed)."""
        return getattr(msg, "_pinned", False)

    def _is_tool_related(self, msg: Message) -> bool:
        """Check if a message involves tool use."""
        if msg.role == "tool":
            return True
        if msg.tool_calls:
            return True
        if msg.tool_call_id:
            return True
        return False

    def _split_messages(
        self,
        messages: list[Message],
        keep_recent: int,
    ) -> tuple[list[Message], list[Message], list[Message], list[Message]]:
        """Split messages into segments for compression.

        Returns: (system_msgs, pinned_msgs, compressible_msgs, recent_msgs)
        """
        system: list[Message] = []
        pinned: list[Message] = []
        rest: list[Message] = []

        for msg in messages:
            if msg.role == "system" and not rest:
                # System messages at the start
                system.append(msg)
            elif self._is_pinned(msg):
                pinned.append(msg)
            else:
                rest.append(msg)

        # Split rest into compressible + recent
        if len(rest) <= keep_recent:
            return system, pinned, [], rest

        compressible = rest[:-keep_recent]
        recent = rest[-keep_recent:]

        return system, pinned, compressible, recent

    # ------------------------------------------------------------------
    # Strategy: TRIM
    # ------------------------------------------------------------------

    def trim(
        self,
        messages: list[Message],
        target_tokens: int,
        keep_recent: int = 10,
    ) -> CompressionResult:
        """Drop oldest messages until under target_tokens.

        Keeps system prompt, pinned messages, and last keep_recent messages.
        Simplest and fastest strategy. No LLM needed.
        """
        original_tokens = _total_tokens(messages)

        if original_tokens <= target_tokens:
            return CompressionResult(
                messages=messages,
                original_tokens=original_tokens,
                compressed_tokens=original_tokens,
                strategy_used=CompressionStrategy.TRIM,
                messages_removed=0,
            )

        system, pinned, compressible, recent = self._split_messages(messages, keep_recent)

        # Drop from the front of compressible until we fit
        removed = 0
        while compressible and _total_tokens(system + pinned + compressible + recent) > target_tokens:
            compressible.pop(0)
            removed += 1

        result_msgs = system + pinned + compressible + recent
        compressed_tokens = _total_tokens(result_msgs)

        return CompressionResult(
            messages=result_msgs,
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            strategy_used=CompressionStrategy.TRIM,
            messages_removed=removed,
        )

    # ------------------------------------------------------------------
    # Strategy: SELECTIVE
    # ------------------------------------------------------------------

    def selective_trim(
        self,
        messages: list[Message],
        target_tokens: int,
        keep_recent: int = 10,
    ) -> CompressionResult:
        """Keep tool-call messages, drop plain chat from old messages.

        Preserves the tool interaction history (function calls + results)
        which tend to carry more information density than conversational chat.
        """
        original_tokens = _total_tokens(messages)

        if original_tokens <= target_tokens:
            return CompressionResult(
                messages=messages,
                original_tokens=original_tokens,
                compressed_tokens=original_tokens,
                strategy_used=CompressionStrategy.SELECTIVE,
                messages_removed=0,
            )

        system, pinned, compressible, recent = self._split_messages(messages, keep_recent)

        # Partition compressible into tool-related and chat
        tool_msgs: list[Message] = []
        chat_msgs: list[Message] = []
        for msg in compressible:
            if self._is_tool_related(msg):
                tool_msgs.append(msg)
            else:
                chat_msgs.append(msg)

        # First, drop all old chat
        kept = system + pinned + tool_msgs + recent
        removed = len(chat_msgs)

        # If still over budget, trim old tool messages from the front
        while tool_msgs and _total_tokens(system + pinned + tool_msgs + recent) > target_tokens:
            tool_msgs.pop(0)
            removed += 1

        result_msgs = system + pinned + tool_msgs + recent
        compressed_tokens = _total_tokens(result_msgs)

        return CompressionResult(
            messages=result_msgs,
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            strategy_used=CompressionStrategy.SELECTIVE,
            messages_removed=removed,
        )

    # ------------------------------------------------------------------
    # Strategy: SUMMARY (requires LLM)
    # ------------------------------------------------------------------

    async def summarize(
        self,
        messages: list[Message],
        max_summary_tokens: int = 500,
    ) -> str:
        """Generate an LLM-powered summary of messages.

        Falls back to extractive summarization if no provider is available.
        """
        if not messages:
            return ""

        # If no provider, fall back to extractive
        if self._provider is None:
            return extractive_summarize(messages, max_sentences=15)

        # Build a prompt for the LLM
        from djcode.provider import Message as Msg

        conversation_text = []
        for msg in messages:
            role = msg.role.upper()
            content = (msg.content or "")[:500]  # Cap per-message for the summary prompt
            conversation_text.append(f"[{role}]: {content}")

        prompt_text = (
            "Summarize this conversation concisely. Focus on:\n"
            "1. Key decisions made\n"
            "2. Files created or modified\n"
            "3. Errors encountered and how they were resolved\n"
            "4. Current state and next steps\n\n"
            "Conversation:\n" + "\n".join(conversation_text) + "\n\n"
            "Summary (bullet points):"
        )

        summary_msgs = [
            Msg(role="system", content="You are a conversation summarizer. Be concise and factual."),
            Msg(role="user", content=prompt_text),
        ]

        # Call the LLM (non-streaming for simplicity)
        collected = []
        try:
            async for chunk in self._provider.chat(summary_msgs, stream=True):
                # Handle both Ollama and OpenAI-compatible response formats
                if "message" in chunk:
                    # Ollama non-streaming
                    text = chunk.get("message", {}).get("content", "")
                    if text:
                        collected.append(text)
                elif "choices" in chunk:
                    # OpenAI-compatible streaming
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta", {})
                        text = delta.get("content", "")
                        if text:
                            collected.append(text)
        except Exception:
            # LLM failed — fall back to extractive
            return extractive_summarize(messages, max_sentences=15)

        summary = "".join(collected).strip()
        if not summary:
            return extractive_summarize(messages, max_sentences=15)

        # Truncate if the summary itself is too long
        summary_tokens = _count_tokens(summary)
        if summary_tokens > max_summary_tokens:
            # Rough truncation by character proportion
            ratio = max_summary_tokens / summary_tokens
            summary = summary[: int(len(summary) * ratio)]

        return summary

    async def summary_compress(
        self,
        messages: list[Message],
        target_tokens: int,
        keep_recent: int = 10,
    ) -> CompressionResult:
        """Replace old messages with an LLM-generated summary.

        Falls back to extractive summary if no LLM provider available.
        """
        from djcode.provider import Message as Msg

        original_tokens = _total_tokens(messages)

        if original_tokens <= target_tokens:
            return CompressionResult(
                messages=messages,
                original_tokens=original_tokens,
                compressed_tokens=original_tokens,
                strategy_used=CompressionStrategy.SUMMARY,
                messages_removed=0,
            )

        system, pinned, compressible, recent = self._split_messages(messages, keep_recent)

        if not compressible:
            return CompressionResult(
                messages=messages,
                original_tokens=original_tokens,
                compressed_tokens=original_tokens,
                strategy_used=CompressionStrategy.SUMMARY,
                messages_removed=0,
            )

        # Calculate how many tokens we can spend on the summary
        reserved = _total_tokens(system + pinned + recent)
        summary_budget = max(200, target_tokens - reserved - 100)

        summary_text = await self.summarize(compressible, max_summary_tokens=summary_budget)

        # Create a summary message
        summary_msg = Msg(
            role="system",
            content=f"[Conversation summary]\n{summary_text}",
        )

        result_msgs = system + [summary_msg] + pinned + recent
        compressed_tokens = _total_tokens(result_msgs)

        return CompressionResult(
            messages=result_msgs,
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            strategy_used=CompressionStrategy.SUMMARY,
            messages_removed=len(compressible),
            summary_text=summary_text,
        )

    # ------------------------------------------------------------------
    # Strategy: HYBRID
    # ------------------------------------------------------------------

    async def hybrid_compress(
        self,
        messages: list[Message],
        target_tokens: int,
        keep_recent: int = 10,
    ) -> CompressionResult:
        """Summarize old chat but keep all tool interactions verbatim.

        Best balance of compression and information retention.
        Tool calls/results carry structured data that doesn't summarize well,
        while conversational exchanges compress effectively.
        """
        from djcode.provider import Message as Msg

        original_tokens = _total_tokens(messages)

        if original_tokens <= target_tokens:
            return CompressionResult(
                messages=messages,
                original_tokens=original_tokens,
                compressed_tokens=original_tokens,
                strategy_used=CompressionStrategy.HYBRID,
                messages_removed=0,
            )

        system, pinned, compressible, recent = self._split_messages(messages, keep_recent)

        if not compressible:
            return CompressionResult(
                messages=messages,
                original_tokens=original_tokens,
                compressed_tokens=original_tokens,
                strategy_used=CompressionStrategy.HYBRID,
                messages_removed=0,
            )

        # Separate tool-related from plain chat in compressible
        tool_msgs: list[Message] = []
        chat_msgs: list[Message] = []
        for msg in compressible:
            if self._is_tool_related(msg):
                tool_msgs.append(msg)
            else:
                chat_msgs.append(msg)

        # Summarize only the chat messages
        reserved = _total_tokens(system + pinned + tool_msgs + recent)
        summary_budget = max(200, target_tokens - reserved - 100)

        if chat_msgs:
            summary_text = await self.summarize(chat_msgs, max_summary_tokens=summary_budget)
            summary_msg = Msg(
                role="system",
                content=f"[Conversation summary]\n{summary_text}",
            )
            result_msgs = system + [summary_msg] + pinned + tool_msgs + recent
        else:
            summary_text = ""
            result_msgs = system + pinned + tool_msgs + recent

        compressed_tokens = _total_tokens(result_msgs)

        # If still over budget, start trimming old tool messages
        removed_extra = 0
        while tool_msgs and _total_tokens(result_msgs) > target_tokens:
            tool_msgs.pop(0)
            removed_extra += 1
            if chat_msgs and summary_text:
                result_msgs = system + [summary_msg] + pinned + tool_msgs + recent
            else:
                result_msgs = system + pinned + tool_msgs + recent

        compressed_tokens = _total_tokens(result_msgs)

        return CompressionResult(
            messages=result_msgs,
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            strategy_used=CompressionStrategy.HYBRID,
            messages_removed=len(chat_msgs) + removed_extra,
            summary_text=summary_text,
        )

    # ------------------------------------------------------------------
    # Unified compress interface
    # ------------------------------------------------------------------

    async def compress(
        self,
        messages: list[Message],
        strategy: str | CompressionStrategy = CompressionStrategy.SELECTIVE,
        target_tokens: int = 0,
        keep_recent: int = 10,
    ) -> CompressionResult:
        """Compress messages using the specified strategy.

        Args:
            messages: Full message history to compress.
            strategy: Which compression strategy to use.
            target_tokens: Target token count after compression.
                          If 0, uses 70% of current token count.
            keep_recent: Number of recent messages to always keep.

        Returns:
            CompressionResult with the compressed message list and stats.
        """
        if isinstance(strategy, str):
            strategy = CompressionStrategy(strategy.lower())

        if target_tokens <= 0:
            target_tokens = int(_total_tokens(messages) * 0.7)

        if strategy == CompressionStrategy.TRIM:
            return self.trim(messages, target_tokens, keep_recent)
        elif strategy == CompressionStrategy.SELECTIVE:
            return self.selective_trim(messages, target_tokens, keep_recent)
        elif strategy == CompressionStrategy.SUMMARY:
            return await self.summary_compress(messages, target_tokens, keep_recent)
        elif strategy == CompressionStrategy.HYBRID:
            return await self.hybrid_compress(messages, target_tokens, keep_recent)
        else:
            # Unknown strategy — fall back to selective
            return self.selective_trim(messages, target_tokens, keep_recent)
