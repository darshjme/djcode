"""Architect agent — high-level planning and design agent.

Analyzes requirements, designs architectures, creates implementation plans.
Thinks before acting — produces structured plans that the Operator executes.
"""

from __future__ import annotations

from djcode.provider import Provider


class Architect:
    """Planning and design agent."""

    def __init__(self, provider: Provider) -> None:
        self.provider = provider

    async def plan(self, task: str) -> str:
        """Run the specialist with bounded, read-only tool execution."""
        from djcode.agents.registry import AgentRole, get_agent
        from djcode.orchestrator.context_bus import ContextBus
        from djcode.orchestrator.engine import AgentRunner

        runner = AgentRunner(
            self.provider, get_agent(AgentRole.ARCHITECT), ContextBus(), auto_accept=False
        )
        return await runner.run(task)
