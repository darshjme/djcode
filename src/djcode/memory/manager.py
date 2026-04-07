"""3-tier memory manager for DJcode.

Tier 1: Session memory (in-process, conversation context)
Tier 2: Local persistent memory (~/.djcode/memory/*.json)
Tier 3: Vector search via Ollama embeddings (optional, local Qdrant or flat file)

Everything stays local. Zero telemetry.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from djcode.config import MEMORY_DIR
from djcode.memory.embedder import VectorStore, cosine_similarity

FACTS_FILE = MEMORY_DIR / "facts.json"
CONVERSATIONS_DIR = MEMORY_DIR / "conversations"


@dataclass
class MemoryEntry:
    """A single memory entry."""

    key: str
    content: str
    tags: list[str] = field(default_factory=list)
    embedding: list[float] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    access_count: int = 0
    boost: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "content": self.content,
            "tags": self.tags,
            "embedding": self.embedding,
            "created_at": self.created_at,
            "access_count": self.access_count,
            "boost": self.boost,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MemoryEntry:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class MemoryManager:
    """Manages the 3-tier memory system."""

    def __init__(self) -> None:
        self._session: list[dict[str, str]] = []  # Tier 1: conversation messages
        self._facts: dict[str, MemoryEntry] = {}  # Tier 2: persistent facts
        self._vectors = VectorStore()  # Tier 3: ChromaDB vector store
        self._load_facts()

    def _load_facts(self) -> None:
        """Load persistent facts from disk."""
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        if FACTS_FILE.exists():
            try:
                with open(FACTS_FILE) as f:
                    data = json.load(f)
                for key, entry_data in data.items():
                    self._facts[key] = MemoryEntry.from_dict(entry_data)
            except (json.JSONDecodeError, OSError):
                pass

    def _save_facts(self) -> None:
        """Persist facts to disk."""
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        data = {k: v.to_dict() for k, v in self._facts.items()}
        with open(FACTS_FILE, "w") as f:
            json.dump(data, f, indent=2)

    # -- Tier 1: Session --

    def add_session_message(self, role: str, content: str) -> None:
        """Add a message to session memory."""
        self._session.append({"role": role, "content": content})

    def get_session_messages(self) -> list[dict[str, str]]:
        """Get all session messages."""
        return list(self._session)

    def clear_session(self) -> None:
        """Clear session memory."""
        self._session.clear()

    # -- Tier 2: Persistent facts --

    def remember(
        self,
        key: str,
        content: str,
        tags: list[str] | None = None,
        embedding: list[float] | None = None,
    ) -> None:
        """Store a persistent fact. Also indexes in ChromaDB if embedding provided."""
        self._facts[key] = MemoryEntry(
            key=key,
            content=content,
            tags=tags or [],
            embedding=embedding or [],
        )
        self._save_facts()

        # Also store in ChromaDB vector store
        if embedding:
            self._vectors.add(
                doc_id=key,
                content=content,
                embedding=embedding,
                metadata={"tags": ",".join(tags or [])},
            )

    def recall(self, key: str) -> str | None:
        """Recall a fact by exact key."""
        entry = self._facts.get(key)
        if entry:
            entry.access_count += 1
            self._save_facts()
            return entry.content
        return None

    def forget(self, key: str) -> bool:
        """Remove a fact from persistent storage and vector store."""
        if key in self._facts:
            del self._facts[key]
            self._save_facts()
            self._vectors.delete(key)
            return True
        return False

    def list_facts(self) -> list[str]:
        """List all fact keys."""
        return sorted(self._facts.keys())

    # -- Tier 3: Semantic search --

    def search_similar(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        min_similarity: float = 0.5,
    ) -> list[tuple[str, float]]:
        """Find facts most similar to the query embedding.

        Uses ChromaDB if available, falls back to in-memory cosine similarity.
        """
        if not query_embedding:
            return []

        # Try ChromaDB first
        if self._vectors.is_chroma:
            results = self._vectors.query(query_embedding, n_results=top_k)
            if results:
                return [
                    (r["id"], r["score"])
                    for r in results
                    if r["score"] >= min_similarity
                ]

        # Fallback: in-memory cosine similarity
        scored = []
        for key, entry in self._facts.items():
            if not entry.embedding:
                continue
            sim = cosine_similarity(query_embedding, entry.embedding)
            sim *= entry.boost  # Apply boost factor
            if sim >= min_similarity:
                scored.append((key, sim))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    # -- Conversation persistence --

    def save_conversation(self, session_id: str) -> Path:
        """Save the current session to a conversation file."""
        CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
        path = CONVERSATIONS_DIR / f"{session_id}.json"
        with open(path, "w") as f:
            json.dump(self._session, f, indent=2)
        return path

    def load_conversation(self, session_id: str) -> bool:
        """Load a previous conversation."""
        path = CONVERSATIONS_DIR / f"{session_id}.json"
        if path.exists():
            try:
                with open(path) as f:
                    self._session = json.load(f)
                return True
            except (json.JSONDecodeError, OSError):
                pass
        return False

    @property
    def stats(self) -> dict[str, int]:
        """Return memory statistics."""
        return {
            "session_messages": len(self._session),
            "persistent_facts": len(self._facts),
            "facts_with_embeddings": sum(1 for f in self._facts.values() if f.embedding),
            "vector_store_docs": self._vectors.count(),
            "vector_store_backend": "chromadb" if self._vectors.is_chroma else "in-memory",
        }
