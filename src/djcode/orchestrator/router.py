"""Semantic Router — sentence-transformer based agent dispatch.

Uses a small embedding model to classify user intent via cosine similarity
against pre-computed agent description embeddings. Falls back to regex
classification if embeddings aren't available.

This makes agent routing model-agnostic — the same routing works whether
you're using gemma4, qwen3, dolphin3, or a remote API.
"""

from __future__ import annotations

import math
from typing import Any

from djcode.agents.registry import AgentRole, get_agents_for_intent, get_agent

# Backwards-compatible aliases
get_agent_for_intent = get_agents_for_intent
get_spec = get_agent


# ── Pre-computed agent task descriptions for semantic matching ──────────────
# Each agent has a set of canonical task descriptions that represent
# what it handles. The router embeds the user's input and finds the
# closest match via cosine similarity.

AGENT_EXEMPLARS: dict[AgentRole, list[str]] = {
    AgentRole.DEBUGGER: [
        "fix the bug",
        "debug this error",
        "why is this crashing",
        "investigate the failure",
        "trace the exception",
        "find the root cause",
        "this test is failing",
        "segfault in the handler",
        "null pointer exception",
        "the function returns wrong results",
    ],
    AgentRole.CODER: [
        "write a function",
        "create a new endpoint",
        "implement the feature",
        "add a handler for",
        "build a component",
        "write the code for",
        "create a class that",
        "generate boilerplate",
        "scaffold the project",
        "make a CLI command",
    ],
    AgentRole.TESTER: [
        "write tests for",
        "add unit tests",
        "test coverage",
        "create test cases",
        "write a pytest fixture",
        "add integration tests",
        "verify the behavior",
        "check edge cases",
        "write assertions for",
        "run the test suite",
    ],
    AgentRole.REFACTORER: [
        "refactor this code",
        "clean up the module",
        "rename the function",
        "extract a method",
        "simplify this logic",
        "reduce duplication",
        "restructure the classes",
        "split this file",
        "improve code quality",
        "make this more readable",
    ],
    AgentRole.ARCHITECT: [
        "design the architecture",
        "plan the implementation",
        "create a design document",
        "what's the best approach",
        "propose a solution",
        "architect the system",
        "draw the data flow",
        "design the API contract",
        "create an ADR",
        "plan the migration",
    ],
    AgentRole.REVIEWER: [
        "review this code",
        "check for security issues",
        "audit the implementation",
        "find potential bugs",
        "review the pull request",
        "check code quality",
        "evaluate performance",
        "look for vulnerabilities",
        "assess the code style",
        "review for best practices",
    ],
    AgentRole.SCOUT: [
        "explore the codebase",
        "what does this function do",
        "explain this code",
        "how does this work",
        "find where this is defined",
        "show me the file structure",
        "what framework is this using",
        "walk me through",
        "where is the config",
        "understand the architecture",
    ],
    AgentRole.DEVOPS: [
        "deploy to production",
        "set up docker",
        "configure CI/CD",
        "create a dockerfile",
        "set up github actions",
        "configure kubernetes",
        "write a deploy script",
        "set up monitoring",
        "configure nginx",
        "manage secrets",
    ],
    AgentRole.DOCS: [
        "write documentation",
        "update the README",
        "generate API docs",
        "write a tutorial",
        "create a changelog",
        "document the function",
        "add docstrings",
        "write usage examples",
        "create a contributing guide",
        "generate release notes",
    ],
    AgentRole.ORCHESTRATOR: [
        "do everything",
        "build the full feature",
        "complete this project",
        "handle the entire task",
        "orchestrate the work",
        "manage the implementation",
        "coordinate the agents",
    ],
}


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class SemanticRouter:
    """Routes tasks to agents using embedding-based semantic matching.

    Uses Ollama's embedding endpoint with a small model (nomic-embed-text)
    to compute similarity between the user's input and pre-defined agent
    task exemplars.

    Falls back to regex-based routing if embeddings aren't available.
    """

    def __init__(self, provider: Any | None = None) -> None:
        self._provider = provider
        self._exemplar_embeddings: dict[AgentRole, list[list[float]]] = {}
        self._initialized = False

    async def initialize(self) -> bool:
        """Pre-compute exemplar embeddings. Returns True if successful."""
        if not self._provider:
            return False

        try:
            for role, exemplars in AGENT_EXEMPLARS.items():
                embeddings: list[list[float]] = []
                for text in exemplars[:5]:  # Top 5 per agent to limit startup cost
                    emb = await self._provider.embed(text)
                    if emb:
                        embeddings.append(emb)
                if embeddings:
                    self._exemplar_embeddings[role] = embeddings

            self._initialized = bool(self._exemplar_embeddings)
            return self._initialized
        except Exception:
            self._initialized = False
            return False

    async def route(self, task: str) -> list[AgentRole]:
        """Route a task to the best agent(s) using semantic similarity.

        Falls back to regex-based routing if embeddings aren't available.
        """
        if not self._initialized or not self._provider:
            # Fallback to regex router
            from djcode.prompt_enhancer import detect_intent
            intent = detect_intent(task)
            return get_agent_for_intent(intent)

        try:
            task_embedding = await self._provider.embed(task)
            if not task_embedding:
                from djcode.prompt_enhancer import detect_intent
                return get_agent_for_intent(detect_intent(task))
        except Exception:
            from djcode.prompt_enhancer import detect_intent
            return get_agent_for_intent(detect_intent(task))

        # Score each agent by max similarity to their exemplars
        scores: list[tuple[AgentRole, float]] = []
        for role, exemplar_embs in self._exemplar_embeddings.items():
            max_sim = max(
                _cosine_similarity(task_embedding, emb)
                for emb in exemplar_embs
            )
            scores.append((role, max_sim))

        # Sort by score descending
        scores.sort(key=lambda x: x[1], reverse=True)

        # Return top agent, or top 2 if close scores
        if len(scores) >= 2 and scores[0][1] - scores[1][1] < 0.05:
            return [scores[0][0], scores[1][0]]
        return [scores[0][0]] if scores else [AgentRole.CODER]

    @property
    def is_semantic(self) -> bool:
        """Whether semantic routing is active (vs regex fallback)."""
        return self._initialized
