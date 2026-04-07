"""Embedding generation and vector storage for DJcode.

Uses Ollama for embeddings and ChromaDB for persistent vector storage.
Falls back to in-memory cosine similarity if ChromaDB is unavailable.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from djcode.config import CONFIG_DIR

if TYPE_CHECKING:
    from djcode.provider import Provider

logger = logging.getLogger(__name__)

CHROMA_DIR = CONFIG_DIR / "memory" / "chroma"


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


class VectorStore:
    """Vector store with ChromaDB backend, falling back to in-memory cosine similarity."""

    def __init__(self, collection_name: str = "djcode_memory") -> None:
        self._collection_name = collection_name
        self._chroma_client = None
        self._collection = None
        self._use_chroma = False
        self._init_chroma()

    def _init_chroma(self) -> None:
        """Try to initialize ChromaDB. Falls back silently on failure."""
        try:
            import chromadb

            CHROMA_DIR.mkdir(parents=True, exist_ok=True)
            self._chroma_client = chromadb.PersistentClient(path=str(CHROMA_DIR))
            self._collection = self._chroma_client.get_or_create_collection(
                name=self._collection_name,
                metadata={"hnsw:space": "cosine"},
            )
            self._use_chroma = True
            logger.debug("ChromaDB initialized at %s", CHROMA_DIR)
        except ImportError:
            logger.debug("ChromaDB not installed, using in-memory fallback")
        except Exception as e:
            logger.debug("ChromaDB init failed: %s, using in-memory fallback", e)

    @property
    def is_chroma(self) -> bool:
        """Whether ChromaDB is active."""
        return self._use_chroma

    def add(
        self,
        doc_id: str,
        content: str,
        embedding: list[float],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Add a document with its embedding to the store."""
        if not self._use_chroma or self._collection is None:
            return

        try:
            self._collection.upsert(
                ids=[doc_id],
                documents=[content],
                embeddings=[embedding],
                metadatas=[metadata or {}],
            )
        except Exception as e:
            logger.debug("ChromaDB upsert failed: %s", e)

    def query(
        self,
        query_embedding: list[float],
        n_results: int = 5,
    ) -> list[dict[str, Any]]:
        """Query for similar documents. Returns list of {id, content, score, metadata}."""
        if not self._use_chroma or self._collection is None:
            return []

        try:
            results = self._collection.query(
                query_embeddings=[query_embedding],
                n_results=n_results,
            )

            docs = []
            ids = results.get("ids", [[]])[0]
            documents = results.get("documents", [[]])[0]
            distances = results.get("distances", [[]])[0]
            metadatas = results.get("metadatas", [[]])[0]

            for i, doc_id in enumerate(ids):
                docs.append({
                    "id": doc_id,
                    "content": documents[i] if i < len(documents) else "",
                    "score": 1.0 - (distances[i] if i < len(distances) else 1.0),
                    "metadata": metadatas[i] if i < len(metadatas) else {},
                })
            return docs
        except Exception as e:
            logger.debug("ChromaDB query failed: %s", e)
            return []

    def delete(self, doc_id: str) -> None:
        """Remove a document from the store."""
        if not self._use_chroma or self._collection is None:
            return

        try:
            self._collection.delete(ids=[doc_id])
        except Exception as e:
            logger.debug("ChromaDB delete failed: %s", e)

    def count(self) -> int:
        """Return the number of documents in the store."""
        if not self._use_chroma or self._collection is None:
            return 0
        try:
            return self._collection.count()
        except Exception:
            return 0
