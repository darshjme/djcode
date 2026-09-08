"""Scout agent — lightweight read-only agent for reconnaissance.

Used for exploring codebases, searching files, reading docs.
Cannot modify anything — only reads and reports.
"""

from __future__ import annotations

from djcode.provider import Message, Provider
from djcode.prompt import SYSTEM_PROMPT

SCOUT_PROMPT = """\
You are DJcode Scout — a read-only reconnaissance agent. Your job is to explore, \
search, and report. You have access ONLY to read-only tools: file_read, grep, glob, \
and git (read-only subcommands only: status, diff, log, show, branch).

You MUST NOT modify any files or run destructive commands. Report your findings \
clearly and concisely.
"""


class Scout:
    """Read-only reconnaissance agent."""

    def __init__(self, provider: Provider) -> None:
        self.provider = provider
        self.messages: list[Message] = [
            Message(role="system", content=SCOUT_PROMPT)
        ]

    async def investigate(self, task: str) -> str:
        """Run the specialist with bounded, read-only tool execution."""
        from djcode.agents.registry import AgentRole, get_agent
        from djcode.orchestrator.context_bus import ContextBus
        from djcode.orchestrator.engine import AgentRunner

        runner = AgentRunner(self.provider, get_agent(AgentRole.SCOUT), ContextBus(), auto_accept=False)
        self.messages.append(Message(role="user", content=task))
        response = await runner.run(task)
        self.messages.append(Message(role="assistant", content=response))
        return response
