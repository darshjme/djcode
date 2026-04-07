"""Vector Context — feeds larger context to agents via semantic search.

Uses ChromaDB (already a dependency) to:
1. Store past conversation snippets as embeddings
2. Retrieve relevant context for new tasks via cosine similarity
3. Inject retrieved context into agents via the ContextBus

This gives agents access to a much larger effective context than the
model's native context window, and makes behavior consistent across
different model backends (Ollama, MLX, API).
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from djcode.config import CONFIG_DIR
from djcode.orchestrator.context_bus import ContextBus

VECTOR_DIR = CONFIG_DIR / "vectors"


class VectorContextStore:
    """ChromaDB-backed vector store for long-term context retrieval.

    Stores conversation snippets, code patterns, and agent results.
    Retrieves semantically relevant context for new tasks.
    """

    def __init__(self, provider: Any | None = None) -> None:
        self._provider = provider
        self._collection = None
        self._client = None
        self._initialized = False

    def initialize(self) -> bool:
        """Initialize ChromaDB with persistent storage."""
        try:
            import chromadb

            VECTOR_DIR.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(VECTOR_DIR))
            self._collection = self._client.get_or_create_collection(
                name="djcode_context",
                metadata={"hnsw:space": "cosine"},
            )
            self._initialized = True
            return True
        except Exception:
            self._initialized = False
            return False

    @property
    def is_ready(self) -> bool:
        return self._initialized and self._collection is not None

    def store(
        self,
        text: str,
        metadata: dict[str, str] | None = None,
        category: str = "conversation",
    ) -> None:
        """Store a text snippet with embeddings for future retrieval."""
        if not self.is_ready:
            return

        # Generate a stable ID from content hash
        doc_id = hashlib.sha256(text.encode()).hexdigest()[:16]
        meta = {
            "category": category,
            "timestamp": str(time.time()),
            **(metadata or {}),
        }

        try:
            self._collection.upsert(
                documents=[text],
                metadatas=[meta],
                ids=[doc_id],
            )
        except Exception:
            pass  # Non-critical — don't break the flow

    def store_exchange(
        self,
        user_input: str,
        response: str,
        model: str = "",
        agent: str = "",
    ) -> None:
        """Store a user-assistant exchange for future context retrieval."""
        if not self.is_ready or not response.strip():
            return

        # Store the combined exchange as one document
        combined = f"User: {user_input}\nAssistant: {response[:500]}"
        self.store(
            combined,
            metadata={
                "model": model,
                "agent": agent,
                "user_input": user_input[:200],
            },
            category="exchange",
        )

    def store_agent_result(
        self,
        agent_name: str,
        role: str,
        task: str,
        result: str,
    ) -> None:
        """Store an agent's work result for cross-session context."""
        if not self.is_ready or not result.strip():
            return

        combined = f"Agent: {agent_name} ({role})\nTask: {task}\nResult: {result[:800]}"
        self.store(
            combined,
            metadata={
                "agent": agent_name,
                "role": role,
                "task": task[:200],
            },
            category="agent_result",
        )

    def retrieve(
        self,
        query: str,
        n_results: int = 5,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve semantically relevant context for a query.

        Returns list of {text, metadata, distance} dicts.
        """
        if not self.is_ready:
            return []

        try:
            where = {"category": category} if category else None
            results = self._collection.query(
                query_texts=[query],
                n_results=n_results,
                where=where,
            )

            docs: list[dict[str, Any]] = []
            if results and results["documents"]:
                for i, doc in enumerate(results["documents"][0]):
                    meta = results["metadatas"][0][i] if results["metadatas"] else {}
                    dist = results["distances"][0][i] if results["distances"] else 1.0
                    docs.append({
                        "text": doc,
                        "metadata": meta,
                        "distance": dist,
                    })
            return docs
        except Exception:
            return []

    def inject_context(self, bus: ContextBus, task: str, n_results: int = 3) -> int:
        """Retrieve relevant context and inject it into the ContextBus.

        Returns the number of context entries injected.
        """
        if not self.is_ready:
            return 0

        docs = self.retrieve(task, n_results=n_results)
        injected = 0
        for doc in docs:
            # Only inject if reasonably relevant (distance < 0.7)
            if doc["distance"] < 0.7:
                bus.write(
                    agent="VectorStore",
                    role="memory",
                    key="retrieved_context",
                    content=doc["text"],
                    source="chromadb",
                    distance=doc["distance"],
                )
                injected += 1

        return injected

    def count(self) -> int:
        """Number of documents in the store."""
        if not self.is_ready:
            return 0
        try:
            return self._collection.count()
        except Exception:
            return 0
