"""Architect agent — high-level planning and design agent.

Analyzes requirements, designs architectures, creates implementation plans.
Thinks before acting — produces structured plans that the Operator executes.
"""

from __future__ import annotations

from djcode.provider import Message, Provider

ARCHITECT_PROMPT = """\
You are DJcode Architect — a senior software architect agent. Your job is to:

1. Analyze requirements and break them into implementation steps
2. Design clean, maintainable architectures
3. Identify risks, edge cases, and dependencies
4. Produce structured implementation plans

You think deeply before recommending action. Your output is always a structured plan \
with clear phases, dependencies, and acceptance criteria. You do NOT execute code — \
you plan it.

Format your plans as:
## Plan: <title>
### Phase 1: <name>
- Task: ...
- Files: ...
- Dependencies: ...
- Acceptance: ...
"""


class Architect:
    """Planning and design agent."""

    def __init__(self, provider: Provider) -> None:
        self.provider = provider
        self.messages: list[Message] = [
            Message(role="system", content=ARCHITECT_PROMPT)
        ]

    async def plan(self, task: str) -> str:
        """Run the specialist with bounded, read-only tool execution."""
        from djcode.agents.registry import AgentRole, get_agent
        from djcode.orchestrator.context_bus import ContextBus
        from djcode.orchestrator.engine import AgentRunner

        runner = AgentRunner(self.provider, get_agent(AgentRole.ARCHITECT), ContextBus(), auto_accept=False)
        self.messages.append(Message(role="user", content=task))
        response = await runner.run(task)
        self.messages.append(Message(role="assistant", content=response))
        return response
