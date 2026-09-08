"""3-tier memory manager for DJcode.

Tier 1: Session memory (in-process, conversation context)
Tier 2: Local persistent memory (~/.djcode/memory/*.json)
Tier 3: Vector search with explicitly supplied embeddings (optional ChromaDB)

Facts stay on disk locally. Embeddings are supplied by the caller; lexical search needs no model.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from functools import wraps
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from djcode.config import MEMORY_DIR
from djcode.memory.embedder import VectorStore, cosine_similarity

FACTS_FILE = MEMORY_DIR / "facts.json"
CONVERSATIONS_DIR = MEMORY_DIR / "conversations"


def _locked_facts(method):
    """Serialize read-modify-write transactions across concurrent CLI sessions."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        with open(MEMORY_DIR / "facts.lock", "a+") as lock:
            if os.name == "nt":
                import msvcrt
                lock.seek(0)
                if not lock.read(1):
                    lock.write("0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                self._load_facts()
                return method(self, *args, **kwargs)
            except Exception:
                self._load_facts()
                raise
            finally:
                if os.name == "nt":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return wrapped


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
        self._facts = {}
        if FACTS_FILE.exists():
            try:
                with open(FACTS_FILE) as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("Expected a facts object")
                for key, entry_data in data.items():
                    self._facts[key] = MemoryEntry.from_dict(entry_data)
            except (json.JSONDecodeError, TypeError, AttributeError, ValueError) as exc:
                raise ValueError(f"Cannot read memory file {FACTS_FILE}; repair or back it up before saving") from exc

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        """Replace atomically so interrupted writes leave the last durable version."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(data, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _save_facts(self) -> None:
        """Persist facts to disk atomically."""
        self._write_json(FACTS_FILE, {k: v.to_dict() for k, v in self._facts.items()})

    def search(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        """Lexical token-overlap retrieval, available offline without embeddings."""
        self._load_facts()
        terms = set(re.findall(r"\w+", query.casefold()))
        if not terms or top_k <= 0:
            return []
        scored = []
        for key, entry in self._facts.items():
            words = set(re.findall(r"\w+", " ".join([key, entry.content, *entry.tags]).casefold()))
            score = len(terms & words) / len(terms)
            if score:
                scored.append((key, score))
        return sorted(scored, key=lambda item: (-item[1], item[0]))[:top_k]

    @staticmethod
    def _conversation_path(session_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
            raise ValueError("Session ID must contain only letters, digits, underscores or hyphens")
        return CONVERSATIONS_DIR / f"{session_id}.json"

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

    @_locked_facts
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

        # Updating without an embedding must invalidate any old semantic value.
        if not embedding:
            self._vectors.delete(key)
        if embedding:
            self._vectors.add(
                doc_id=key,
                content=content,
                embedding=embedding,
                metadata={"tags": ",".join(tags or [])},
            )

    @_locked_facts
    def recall(self, key: str) -> str | None:
        """Recall a fact by exact key."""
        entry = self._facts.get(key)
        if entry:
            entry.access_count += 1
            self._save_facts()
            return entry.content
        return None

    @_locked_facts
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
        self._load_facts()
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
        self._load_facts()
        if not query_embedding or top_k <= 0:
            return []

        # Try ChromaDB first
        if self._vectors.is_chroma:
            results = self._vectors.query(query_embedding, n_results=top_k)
            if results:
                return [
                    (r["id"], r["score"])
                    for r in results
                    if r["score"] >= min_similarity and r["id"] in self._facts
                    and self._facts[r["id"]].embedding
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
        path = self._conversation_path(session_id)
        self._write_json(path, self._session)
        return path

    def load_conversation(self, session_id: str) -> bool:
        """Load a previous conversation."""
        path = self._conversation_path(session_id)
        if path.exists():
            try:
                with open(path) as f:
                    messages = json.load(f)
                if not isinstance(messages, list) or any(
                    not isinstance(m, dict) or not isinstance(m.get("role"), str)
                    or not isinstance(m.get("content"), str) for m in messages
                ):
                    return False
                self._session = messages
                return True
            except (json.JSONDecodeError, OSError):
                pass
        return False

    @property
    def stats(self) -> dict[str, int | str]:
        """Return memory statistics."""
        return {
            "session_messages": len(self._session),
            "persistent_facts": len(self._facts),
            "facts_with_embeddings": sum(1 for f in self._facts.values() if f.embedding),
            "vector_store_docs": self._vectors.count(),
            "vector_store_backend": "chromadb" if self._vectors.is_chroma else "stored-embedding cosine",
            "search_backend": "lexical (offline); semantic requires supplied embeddings",
        }
