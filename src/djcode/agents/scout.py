"""Scout agent — lightweight read-only agent for reconnaissance.

Used for exploring codebases, searching files, reading docs.
Cannot modify anything — only reads and reports.
"""

from __future__ import annotations

from djcode.provider import Provider


class Scout:
    """Read-only reconnaissance agent."""

    def __init__(self, provider: Provider) -> None:
        self.provider = provider

    async def investigate(self, task: str) -> str:
        """Run the specialist with bounded, read-only tool execution."""
        from djcode.agents.registry import AgentRole, get_agent
        from djcode.orchestrator.context_bus import ContextBus
        from djcode.orchestrator.engine import AgentRunner

        runner = AgentRunner(
            self.provider, get_agent(AgentRole.SCOUT), ContextBus(), auto_accept=False
        )
        return await runner.run(task)
