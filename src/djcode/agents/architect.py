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

    async def plan(self, requirement: str) -> str:
        """Generate an implementation plan for the given requirement."""
        self.messages.append(Message(role="user", content=requirement))

        full_response = ""
        async for chunk in self.provider.chat(self.messages, stream=False):
            if self.provider.is_ollama:
                msg = chunk.get("message", {})
                full_response = msg.get("content", "")
            else:
                choices = chunk.get("choices", [])
                if choices:
                    full_response = choices[0].get("message", {}).get("content", "")

        self.messages.append(Message(role="assistant", content=full_response))
        return full_response
