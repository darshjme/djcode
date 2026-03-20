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

    async def investigate(self, query: str) -> str:
        """Run a read-only investigation and return findings."""
        self.messages.append(Message(role="user", content=query))

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
