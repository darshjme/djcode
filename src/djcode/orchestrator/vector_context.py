"""Persistent context retrieval with an offline lexical default.

An explicitly supplied embedding function can enable Chroma cosine search.
No default embedding model is constructed or downloaded. Backend labels travel
with retrieved context so keyword matches are never presented as semantic ones.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from djcode.config import CONFIG_DIR
from djcode.orchestrator.context_bus import ContextBus

VECTOR_DIR = CONFIG_DIR / "vectors"
logger = logging.getLogger(__name__)


class VectorContextStore:
    """SQLite lexical context with optional explicitly supplied Chroma embeddings."""

    def __init__(
        self, provider: Any | None = None, *,
        embedding_function: Callable[[str], list[float]] | None = None,
    ) -> None:
        # Provider is retained for constructor compatibility; it is never called
        # implicitly. Callers must explicitly supply their embedding function.
        self._provider = provider
        self._embedding_function = embedding_function
        self._collection = None
        self._client = None
        self._initialized = False
        self._db_path = VECTOR_DIR / "context.sqlite3"

    def initialize(self) -> bool:
        """Open lexical storage; optionally enable supplied-embedding Chroma."""
        try:
            VECTOR_DIR.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS snippets (
                    id TEXT PRIMARY KEY, text TEXT NOT NULL, metadata TEXT NOT NULL
                )""")
            self._initialized = True
        except (sqlite3.Error, OSError):
            logger.warning("Context storage could not be initialized")
            return False
        if self._embedding_function is not None:
            try:
                import chromadb
                from chromadb.config import Settings
                self._client = chromadb.PersistentClient(
                    path=str(VECTOR_DIR), settings=Settings(anonymized_telemetry=False),
                )
                self._collection = self._client.get_or_create_collection(
                    name="djcode_context", metadata={"hnsw:space": "cosine"},
                    embedding_function=None,
                )
            except Exception:
                logger.debug("Optional Chroma unavailable; using lexical context")
        return True

    @property
    def is_ready(self) -> bool:
        return self._initialized

    @property
    def backend(self) -> str:
        return "semantic (supplied embeddings)" if self._collection is not None else "lexical (SQLite)"

    def store(
        self, text: str, metadata: dict[str, str] | None = None,
        category: str = "conversation",
    ) -> None:
        """Persist text offline; index semantically only with explicit embeddings."""
        if not self.is_ready or not text.strip():
            return
        doc_id = hashlib.sha256(text.encode()).hexdigest()[:16]
        meta = {**(metadata or {}), "category": category, "timestamp": str(time.time())}
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("INSERT OR REPLACE INTO snippets VALUES (?, ?, ?)",
                             (doc_id, text, json.dumps(meta)))
        except sqlite3.Error:
            logger.warning("Context snippet could not be saved")
            return
        if self._collection is not None and self._embedding_function is not None:
            try:
                embedding = self._embedding_function(text)
                if embedding:
                    self._collection.upsert(documents=[text], metadatas=[meta],
                                            ids=[doc_id], embeddings=[embedding])
            except Exception:
                logger.debug("Context embedding failed; lexical copy retained")

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
        self, query: str, n_results: int = 5, category: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return ranked context with its actual retrieval backend and distance."""
        if not self.is_ready or n_results <= 0:
            return []
        if self._collection is not None and self._embedding_function is not None:
            try:
                embedding = self._embedding_function(query)
                results = self._collection.query(
                    query_embeddings=[embedding], n_results=n_results,
                    where={"category": category} if category else None,
                )
                docs = results.get("documents", [[]])[0]
                metadata = results.get("metadatas", [[]])[0]
                distances = results.get("distances", [[]])[0]
                if docs:
                    return [{"text": text, "metadata": metadata[i],
                             "distance": distances[i], "backend": "semantic"}
                            for i, text in enumerate(docs)]
            except Exception:
                logger.debug("Semantic query unavailable; using lexical context")
        terms = set(re.findall(r"\w+", query.casefold()))
        if not terms:
            return []
        try:
            with sqlite3.connect(self._db_path) as conn:
                rows = conn.execute("SELECT text, metadata FROM snippets").fetchall()
            matches = []
            for text, raw_meta in rows:
                meta = json.loads(raw_meta)
                if category and meta.get("category") != category:
                    continue
                words = set(re.findall(r"\w+", text.casefold()))
                score = len(terms & words) / len(terms)
                if score:
                    matches.append({"text": text, "metadata": meta,
                                    "distance": 1.0 - score, "backend": "lexical"})
            return sorted(matches, key=lambda doc: (doc["distance"], doc["text"]))[:n_results]
        except (sqlite3.Error, ValueError):
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
                    agent="ContextStore",
                    role="memory",
                    key="retrieved_context_" + hashlib.sha256(doc["text"].encode()).hexdigest()[:12],
                    content=doc["text"],
                    source=doc["backend"],
                    distance=doc["distance"],
                )
                injected += 1

        return injected

    def count(self) -> int:
        """Number of documents in the store."""
        if not self.is_ready:
            return 0
        try:
            with sqlite3.connect(self._db_path) as conn:
                return conn.execute("SELECT COUNT(*) FROM snippets").fetchone()[0]
        except sqlite3.Error:
            return 0
