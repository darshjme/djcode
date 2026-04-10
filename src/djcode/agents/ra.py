"""Research Assistant Framework — Pre-execution intelligence for PhD agents.

Every PhD agent gets a Research Assistant that runs BEFORE the agent starts.
The RA:
  1. Pre-fetches relevant code context (grep, glob, file_read)
  2. Searches the ContextBus for related prior agent work
  3. Builds a structured briefing document for the PhD agent
  4. Operates in READ-ONLY mode: file_read, grep, glob, git (log/diff only)

The RA output is injected into the PhD agent's system prompt so it starts
with full context instead of burning tool rounds on reconnaissance.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from djcode.agents.registry import AgentRole, AgentSpec
from djcode.orchestrator.context_bus import ContextBus
from djcode.tools import dispatch_tool

logger = logging.getLogger(__name__)

__all__ = [
    "RABriefing",
    "CodeSnippet",
    "ResearchAssistant",
]


# -- RA read-only tool set ---------------------------------------------------

_RA_TOOLS: frozenset[str] = frozenset({"file_read", "grep", "glob", "git"})

# Git subcommands the RA is allowed to run (read-only operations)
_GIT_READONLY_COMMANDS: frozenset[str] = frozenset({
    "log", "diff", "status", "show", "blame", "shortlog", "branch",
    "tag", "remote", "stash list",
})


# -- Data types ---------------------------------------------------------------

@dataclass(frozen=True)
class CodeSnippet:
    """A code snippet found during codebase search."""

    file_path: str
    line_start: int
    line_end: int
    content: str
    relevance: str  # why this snippet is relevant

    def __str__(self) -> str:
        return f"--- {self.file_path}:{self.line_start}-{self.line_end} ({self.relevance}) ---\n{self.content}"


@dataclass(frozen=True)
class RABriefing:
    """Structured briefing document produced by the Research Assistant."""

    agent_role: AgentRole
    task_summary: str
    codebase_snippets: list[CodeSnippet]
    bus_context: str           # summary from ContextBus
    directory_structure: str   # relevant directory listing
    git_context: str           # recent commits / diffs
    search_duration_ms: float
    timestamp: float

    @property
    def is_empty(self) -> bool:
        return (
            not self.codebase_snippets
            and not self.bus_context
            and not self.directory_structure
            and not self.git_context
        )

    def to_prompt_injection(self) -> str:
        """Format the briefing as a system prompt section for the PhD agent."""
        if self.is_empty:
            return ""

        sections: list[str] = []
        sections.append("## Research Assistant Briefing")
        sections.append(f"**Task:** {self.task_summary}")
        sections.append(f"**Research time:** {self.search_duration_ms:.0f}ms\n")

        if self.directory_structure:
            sections.append("### Relevant Files")
            sections.append(f"```\n{self.directory_structure}\n```\n")

        if self.git_context:
            sections.append("### Recent Git Activity")
            sections.append(f"```\n{self.git_context}\n```\n")

        if self.codebase_snippets:
            sections.append("### Code Context")
            for snippet in self.codebase_snippets[:10]:  # cap at 10 to avoid prompt bloat
                sections.append(str(snippet))
            sections.append("")

        if self.bus_context:
            sections.append("### Prior Agent Work")
            sections.append(self.bus_context)

        return "\n".join(sections)

    @property
    def snippet_count(self) -> int:
        return len(self.codebase_snippets)


# -- Research Assistant -------------------------------------------------------

class ResearchAssistant:
    """Read-only research assistant that gathers context before a PhD agent executes.

    The RA performs lightweight, targeted searches based on the task description
    and the agent's specialty. It never modifies files or runs destructive commands.

    Usage:
        ra = ResearchAssistant(cwd="/path/to/project")
        briefing = await ra.brief(task="fix the auth middleware", agent_spec=coder_spec)
        # briefing.to_prompt_injection() -> inject into PhD agent system prompt
    """

    def __init__(
        self,
        cwd: str | None = None,
        context_bus: ContextBus | None = None,
        timeout_s: float = 15.0,
    ) -> None:
        self.cwd = cwd or os.getcwd()
        self.bus = context_bus
        self.timeout_s = timeout_s

    # -- Main entry point ------------------------------------------------------

    async def brief(self, task: str, agent_spec: AgentSpec) -> RABriefing:
        """Build a complete briefing document for the given task and agent.

        Runs all research phases concurrently within a timeout.
        """
        start = time.monotonic()

        # Extract search terms from the task
        keywords = self._extract_keywords(task)
        file_patterns = self._extract_file_patterns(task)

        # Run all research phases concurrently
        try:
            results = await asyncio.wait_for(
                asyncio.gather(
                    self._search_codebase(keywords, file_patterns),
                    self._gather_directory_context(file_patterns),
                    self._gather_git_context(),
                    self._gather_bus_context(task, agent_spec.role),
                    return_exceptions=True,
                ),
                timeout=self.timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "RA briefing timed out after %.1fs for %s",
                self.timeout_s,
                agent_spec.name,
            )
            results = [[], "", "", ""]

        # Unpack results (replace exceptions with empty defaults)
        snippets = results[0] if isinstance(results[0], list) else []
        directory = results[1] if isinstance(results[1], str) else ""
        git_ctx = results[2] if isinstance(results[2], str) else ""
        bus_ctx = results[3] if isinstance(results[3], str) else ""

        elapsed_ms = (time.monotonic() - start) * 1000

        briefing = RABriefing(
            agent_role=agent_spec.role,
            task_summary=task[:500],
            codebase_snippets=snippets,
            bus_context=bus_ctx,
            directory_structure=directory,
            git_context=git_ctx,
            search_duration_ms=elapsed_ms,
            timestamp=time.time(),
        )

        logger.info(
            "RA briefing for %s: %d snippets, %.0fms",
            agent_spec.name,
            briefing.snippet_count,
            elapsed_ms,
        )
        return briefing

    # -- Codebase search -------------------------------------------------------

    async def search_codebase(self, query: str) -> list[CodeSnippet]:
        """Public API: search the codebase for a given query string."""
        keywords = self._extract_keywords(query)
        return await self._search_codebase(keywords, [])

    async def _search_codebase(
        self,
        keywords: list[str],
        file_patterns: list[str],
    ) -> list[CodeSnippet]:
        """Search codebase using grep for each keyword, return relevant snippets."""
        snippets: list[CodeSnippet] = []
        seen_files: set[str] = set()

        # Search for each keyword via grep
        search_tasks = []
        for kw in keywords[:5]:  # limit to 5 keywords
            search_tasks.append(self._grep_keyword(kw))

        # Search for file patterns via glob
        for pattern in file_patterns[:3]:
            search_tasks.append(self._glob_pattern(pattern))

        results = await asyncio.gather(*search_tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, list):
                for snippet in result:
                    if snippet.file_path not in seen_files:
                        snippets.append(snippet)
                        seen_files.add(snippet.file_path)

        # Sort by relevance (prefer shorter paths = closer to project root)
        snippets.sort(key=lambda s: len(s.file_path))
        return snippets[:15]  # cap total snippets

    async def _grep_keyword(self, keyword: str) -> list[CodeSnippet]:
        """Grep for a keyword and return matching code snippets."""
        snippets: list[CodeSnippet] = []
        try:
            result = await dispatch_tool("grep", {
                "pattern": keyword,
                "path": self.cwd,
                "include": "*.py",
            })
            if not result or result.startswith("Error"):
                return snippets

            # Parse grep output lines: "file:line:content"
            for line in result.split("\n")[:20]:  # limit parsing
                match = re.match(r'^(.+?):(\d+):(.*)$', line)
                if match:
                    fpath, lineno, content = match.groups()
                    snippets.append(CodeSnippet(
                        file_path=fpath,
                        line_start=int(lineno),
                        line_end=int(lineno),
                        content=content.strip(),
                        relevance=f"matches '{keyword}'",
                    ))
        except Exception as e:
            logger.debug("Grep failed for keyword '%s': %s", keyword, e)

        return snippets

    async def _glob_pattern(self, pattern: str) -> list[CodeSnippet]:
        """Find files matching a glob pattern and read their first few lines."""
        snippets: list[CodeSnippet] = []
        try:
            result = await dispatch_tool("glob", {
                "pattern": pattern,
                "path": self.cwd,
            })
            if not result or result.startswith("Error"):
                return snippets

            files = [f.strip() for f in result.split("\n") if f.strip()][:5]
            for fpath in files:
                try:
                    content = await dispatch_tool("file_read", {
                        "path": fpath,
                        "limit": 30,
                    })
                    if content and not content.startswith("Error"):
                        snippets.append(CodeSnippet(
                            file_path=fpath,
                            line_start=1,
                            line_end=30,
                            content=content[:1000],
                            relevance=f"matches pattern '{pattern}'",
                        ))
                except Exception:
                    continue
        except Exception as e:
            logger.debug("Glob failed for pattern '%s': %s", pattern, e)

        return snippets

    # -- Directory context -----------------------------------------------------

    async def _gather_directory_context(self, file_patterns: list[str]) -> str:
        """Get relevant directory structure for context."""
        try:
            result = await dispatch_tool("glob", {
                "pattern": "**/*.py",
                "path": self.cwd,
            })
            if not result or result.startswith("Error"):
                return ""

            files = [f.strip() for f in result.split("\n") if f.strip()]
            # Trim to manageable size
            if len(files) > 50:
                files = files[:50]
                files.append(f"... and {len(files) - 50} more files")

            return "\n".join(files)
        except Exception as e:
            logger.debug("Directory context failed: %s", e)
            return ""

    # -- Git context -----------------------------------------------------------

    async def _gather_git_context(self) -> str:
        """Get recent git log and any uncommitted changes."""
        parts: list[str] = []

        try:
            # Recent commits
            log_result = await self._safe_git("log --oneline -10")
            if log_result:
                parts.append("Recent commits:")
                parts.append(log_result)

            # Uncommitted changes
            diff_result = await self._safe_git("diff --stat")
            if diff_result:
                parts.append("\nUncommitted changes:")
                parts.append(diff_result)

        except Exception as e:
            logger.debug("Git context failed: %s", e)

        return "\n".join(parts)

    async def _safe_git(self, subcommand: str) -> str:
        """Execute a read-only git command safely."""
        # Validate it's a read-only operation
        cmd_prefix = subcommand.split()[0] if subcommand.strip() else ""
        if cmd_prefix not in _GIT_READONLY_COMMANDS:
            logger.warning("RA blocked non-readonly git command: %s", subcommand)
            return ""

        try:
            result = await dispatch_tool("git", {"subcommand": subcommand})
            if result and not result.startswith("Error"):
                return result[:2000]  # cap output size
            return ""
        except Exception:
            return ""

    # -- Context bus -----------------------------------------------------------

    async def _gather_bus_context(self, task: str, role: AgentRole) -> str:
        """Summarize relevant entries from the ContextBus."""
        if not self.bus or len(self.bus) == 0:
            return ""

        # Get all bus entries and filter for relevance
        entries = self.bus.read_all()
        if not entries:
            return ""

        # Build a focused summary
        parts: list[str] = []
        for entry in entries:
            # Skip own prior entries
            if entry.role == role.value:
                continue
            parts.append(
                f"[{entry.agent} ({entry.role})] {entry.key}:\n"
                f"{entry.content[:500]}"
            )

        return "\n\n".join(parts[:5])  # cap at 5 entries

    # -- Keyword extraction ----------------------------------------------------

    @staticmethod
    def _extract_keywords(task: str) -> list[str]:
        """Extract meaningful search keywords from the task description.

        Filters out common English stop words and very short tokens.
        """
        stop_words = {
            "the", "a", "an", "is", "are", "was", "were", "be", "been",
            "being", "have", "has", "had", "do", "does", "did", "will",
            "would", "could", "should", "may", "might", "shall", "can",
            "to", "of", "in", "for", "on", "with", "at", "by", "from",
            "as", "into", "through", "during", "before", "after", "above",
            "below", "between", "under", "again", "further", "then", "once",
            "here", "there", "when", "where", "why", "how", "all", "each",
            "every", "both", "few", "more", "most", "other", "some", "such",
            "no", "nor", "not", "only", "own", "same", "so", "than", "too",
            "very", "just", "because", "but", "and", "or", "if", "while",
            "it", "its", "this", "that", "these", "those", "i", "me", "my",
            "we", "our", "you", "your", "he", "him", "his", "she", "her",
            "they", "them", "their", "what", "which", "who", "whom",
            "fix", "add", "create", "make", "build", "implement", "update",
            "change", "modify", "write", "read", "get", "set", "use",
            "file", "code", "function", "class", "method", "module",
            "please", "need", "want",
        }

        # Split on non-word characters and filter
        words = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', task.lower())
        keywords = []
        seen: set[str] = set()
        for word in words:
            if word not in stop_words and len(word) > 2 and word not in seen:
                keywords.append(word)
                seen.add(word)

        return keywords

    @staticmethod
    def _extract_file_patterns(task: str) -> list[str]:
        """Extract file path patterns from the task description.

        Looks for explicit paths (src/foo/bar.py), dotted module names
        (foo.bar.baz), and filename references (config.yaml).
        """
        patterns: list[str] = []

        # Explicit paths
        path_matches = re.findall(r'[\w./]+\.(?:py|ts|js|rs|go|yaml|yml|json|toml)', task)
        for p in path_matches:
            if "/" in p:
                patterns.append(p)
            else:
                patterns.append(f"**/{p}")

        # Dotted module names (e.g., djcode.agents.registry -> src/djcode/agents/registry.py)
        module_matches = re.findall(r'(?:[\w]+\.){2,}[\w]+', task)
        for m in module_matches:
            if not m.endswith(('.py', '.ts', '.js')):
                path = m.replace(".", "/") + ".py"
                patterns.append(f"**/{path}")

        return patterns[:5]  # cap at 5 patterns
