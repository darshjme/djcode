"""Embedding generation via Ollama for semantic memory search."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from djcode.provider import Provider


async def embed_text(provider: Provider, text: str) -> list[float]:
    """Generate an embedding vector for the given text."""
    return await provider.embed(text)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
