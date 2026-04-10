"""Agent Executor — Runs a single PhD agent with full tool-calling loop.

The AgentExecutor is the core runtime for a single specialist agent.
It orchestrates:
  1. Research Assistant (pre-fetch context before the agent starts)
  2. LLM conversation loop with streaming token emission
  3. Tool calling with permission gates and policy enforcement
  4. State machine transitions + event emission for TUI
  5. Quality gate extraction (confidence_score from response)
  6. Result writing to ContextBus

This replaces the simpler AgentRunner in engine.py with a production-grade
executor that supports state tracking, RA briefings, and event streaming.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from djcode.agents.ra import RABriefing, ResearchAssistant
from djcode.agents.registry import AgentRole, AgentSpec, BLOCKING_AGENTS
from djcode.agents.state import (
    AgentEvent,
    AgentEventType,
    AgentState,
    AgentStateMachine,
    EventCallback,
)
from djcode.orchestrator.context_bus import ContextBus
from djcode.provider import Message, Provider
from djcode.tools import dispatch_tool

logger = logging.getLogger(__name__)

__all__ = [
    "AgentResult",
    "AgentExecutor",
]


# -- Result type ---------------------------------------------------------------

@dataclass
class AgentResult:
    """Complete result from an agent execution."""

    agent_role: AgentRole
    agent_name: str
    response: str
    confidence_score: float
    tokens_used: int
    tools_called: int
    duration_s: float
    ra_briefing: RABriefing | None
    state: AgentState
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.state == AgentState.DONE and self.error is None

    @property
    def is_blocking_critical(self) -> bool:
        """True if this agent is a blocking agent AND flagged critical findings."""
        return self.agent_role in BLOCKING_AGENTS and self.has_critical_findings

    @property
    def has_critical_findings(self) -> bool:
        upper = self.response.upper()
        return "CRITICAL" in upper and any(
            kw in upper for kw in ["BLOCK", "HALT", "REJECT", "FAIL", "VULNERABILITY"]
        )

    def summary_line(self) -> str:
        """One-line summary for logging."""
        status = "OK" if self.succeeded else f"FAIL({self.error or 'unknown'})"
        return (
            f"[{self.agent_name}] {status} | "
            f"conf={self.confidence_score:.2f} "
            f"tokens={self.tokens_used} "
            f"tools={self.tools_called} "
            f"time={self.duration_s:.1f}s"
        )


# -- Executor ------------------------------------------------------------------

class AgentExecutor:
    """Production-grade executor for a single PhD agent.

    Lifecycle:
      1. assign(task) -> RA.brief() -> build system prompt
      2. LLM conversation loop (streaming + tool calls)
      3. Extract confidence_score from response
      4. Write result to ContextBus
      5. Return AgentResult

    Usage:
        executor = AgentExecutor(spec, provider, bus)
        result = await executor.execute("implement user auth")

        # Or for streaming:
        async for event in executor.execute_streaming("implement user auth"):
            if event.event_type == AgentEventType.TOKEN:
                print(event.data["token"], end="")
    """

    def __init__(
        self,
        spec: AgentSpec,
        provider: Provider,
        bus: ContextBus,
        *,
        enable_ra: bool = True,
        ra_timeout_s: float = 15.0,
        execution_timeout_s: float = 300.0,
    ) -> None:
        self.spec = spec
        self.provider = provider
        self.bus = bus
        self.enable_ra = enable_ra
        self.ra_timeout_s = ra_timeout_s
        self.execution_timeout_s = execution_timeout_s

        self._sm = AgentStateMachine(spec=spec)
        self._ra = ResearchAssistant(context_bus=bus, timeout_s=ra_timeout_s) if enable_ra else None

    # -- Event registration ----------------------------------------------------

    def on_event(self, callback: EventCallback) -> None:
        """Register a callback for all state machine events."""
        self._sm.on_event(callback)

    @property
    def state_machine(self) -> AgentStateMachine:
        """Access the underlying state machine for inspection."""
        return self._sm

    # -- Main execution --------------------------------------------------------

    async def execute(self, task: str) -> AgentResult:
        """Execute the agent on a task. Returns a complete AgentResult.

        Non-streaming: collects all tokens internally.
        """
        full_response = ""
        async for event in self.execute_streaming(task):
            if event.event_type == AgentEventType.TOKEN:
                full_response += event.data.get("token", "")

        return AgentResult(
            agent_role=self.spec.role,
            agent_name=self.spec.name,
            response=full_response or self._sm.error_message,
            confidence_score=self._sm.confidence_score,
            tokens_used=self._sm.tokens_used,
            tools_called=self._sm.tool_count,
            duration_s=self._sm.duration_s,
            ra_briefing=None if not self._ra else self._get_last_briefing(),
            state=self._sm.state,
            error=self._sm.error_message or None,
        )

    async def execute_streaming(self, task: str) -> AsyncIterator[AgentEvent]:
        """Execute the agent with full event streaming.

        Yields AgentEvent instances for: state changes, tokens, tool calls,
        RA briefings, quality scores, completion, and errors.
        """
        try:
            # Phase 1: Assign task
            await self._sm.assign(task)
            yield self._make_event(AgentEventType.STATE_CHANGE, state="assigned")

            # Phase 2: Research (if RA enabled)
            briefing: RABriefing | None = None
            if self._ra:
                await self._sm.start_research()
                yield self._make_event(AgentEventType.STATE_CHANGE, state="researching")

                briefing = await self._ra.brief(task, self.spec)
                await self._sm.record_ra_briefing(briefing.to_prompt_injection())
                self._last_briefing = briefing

                yield self._make_event(
                    AgentEventType.RA_BRIEFING,
                    snippet_count=briefing.snippet_count,
                    duration_ms=briefing.search_duration_ms,
                )

            # Phase 3: Execute LLM loop
            await self._sm.start_execution()
            yield self._make_event(AgentEventType.STATE_CHANGE, state="executing")

            system_prompt = self._build_system_prompt(briefing)
            messages: list[Message] = [
                Message(role="system", content=system_prompt),
                Message(role="user", content=task),
            ]

            full_response = ""
            async for event in self._run_llm_loop(messages):
                if event.event_type == AgentEventType.TOKEN:
                    full_response += event.data.get("token", "")
                yield event

            # Phase 4: Extract confidence and review
            confidence = self._extract_confidence(full_response)
            await self._sm.record_quality_score(confidence)
            yield self._make_event(AgentEventType.QUALITY_SCORE, score=confidence)

            # Phase 5: Write to ContextBus
            self.bus.write(
                agent=self.spec.name,
                role=self.spec.role.value,
                key="result",
                content=full_response,
                confidence=confidence,
            )

            # Phase 6: Complete
            await self._sm.complete(confidence)
            yield self._make_event(
                AgentEventType.COMPLETE,
                confidence=confidence,
                duration_s=self._sm.duration_s,
                tokens=self._sm.tokens_used,
                tools=self._sm.tool_count,
            )

        except asyncio.TimeoutError:
            error_msg = f"Execution timed out after {self.execution_timeout_s}s"
            await self._sm.fail(error_msg)
            yield self._make_event(AgentEventType.ERROR, error=error_msg)

        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"
            logger.exception("Agent %s failed", self.spec.name)
            await self._sm.fail(error_msg)
            yield self._make_event(AgentEventType.ERROR, error=error_msg)

    # -- LLM conversation loop -------------------------------------------------

    async def _run_llm_loop(self, messages: list[Message]) -> AsyncIterator[AgentEvent]:
        """Run the multi-round LLM conversation with tool calling."""
        for round_num in range(self.spec.max_tool_rounds):
            chunk_response = ""
            tool_calls: list[dict[str, Any]] = []

            # Stream from provider
            async for token, tcs in self._stream_response(messages):
                if token:
                    chunk_response += token
                    await self._sm.record_token(token)
                    yield self._make_event(AgentEventType.TOKEN, token=token)
                if tcs:
                    tool_calls = tcs

            # No tool calls = final response
            if not tool_calls:
                break

            # Append assistant message with tool calls
            messages.append(Message(
                role="assistant",
                content=chunk_response,
                tool_calls=tool_calls,
            ))

            # Execute each tool call
            for tc in tool_calls:
                func = tc.get("function", {})
                tool_name = func.get("name", "unknown")
                args_raw = func.get("arguments", "{}")

                # Parse arguments
                if isinstance(args_raw, str):
                    try:
                        args = json.loads(args_raw)
                    except json.JSONDecodeError:
                        args = {"command": args_raw} if tool_name == "bash" else {}
                else:
                    args = args_raw

                # Enforce tool policy
                result = await self._execute_tool_gated(tool_name, args)

                # Record tool call
                await self._sm.record_tool_call(
                    tool_name=tool_name,
                    arguments=args,
                    result=result,
                    duration_ms=0.0,  # timing handled inside _execute_tool_gated
                )
                yield self._make_event(
                    AgentEventType.TOOL_CALL,
                    tool=tool_name,
                    args=args,
                    round=round_num,
                )

                # Append tool result to conversation
                messages.append(Message(
                    role="tool",
                    content=result,
                    tool_call_id=tc.get("id", f"call_{tool_name}_{round_num}"),
                    name=tool_name,
                ))

    async def _stream_response(
        self,
        messages: list[Message],
    ) -> AsyncIterator[tuple[str, list[dict[str, Any]] | None]]:
        """Stream tokens from the provider, accumulating tool calls.

        Yields (token, None) for content tokens.
        Yields ("", tool_calls) when tool calls are complete.
        """
        tool_calls_acc: dict[int, dict[str, Any]] = {}

        async for chunk in self.provider.chat(messages, stream=True):
            choices = chunk.get("choices", [])
            if not choices:
                # Ollama format
                msg = chunk.get("message", {})
                content = msg.get("content", "")
                if content:
                    yield content, None
                if "tool_calls" in msg:
                    yield "", msg["tool_calls"]
                if chunk.get("done", False):
                    break
                continue

            choice = choices[0]
            delta = choice.get("delta", {})

            # Content token
            content = delta.get("content", "")
            if content:
                yield content, None

            # Accumulate tool calls
            if "tool_calls" in delta:
                for tc_delta in delta["tool_calls"]:
                    idx = tc_delta.get("index", 0)
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {
                            "id": tc_delta.get("id", ""),
                            "function": {"name": "", "arguments": ""},
                        }
                    if "function" in tc_delta:
                        fn = tc_delta["function"]
                        if "name" in fn and fn["name"]:
                            tool_calls_acc[idx]["function"]["name"] = fn["name"]
                        if "arguments" in fn:
                            tool_calls_acc[idx]["function"]["arguments"] += fn["arguments"]

            # Check finish reason
            finish = choice.get("finish_reason", "")
            if finish == "tool_calls":
                yield "", list(tool_calls_acc.values())
                break
            elif finish == "stop":
                break

    # -- Tool policy enforcement -----------------------------------------------

    async def _execute_tool_gated(self, tool_name: str, args: dict[str, Any]) -> str:
        """Execute a tool call after enforcing the agent's tool policy."""
        # Check if tool is in the denied list
        if tool_name in self.spec.tools_denied:
            return f"Error: Tool '{tool_name}' is explicitly denied for agent {self.spec.name}."

        # Check if tool is in the allowed list
        if tool_name not in self.spec.tools_allowed:
            return (
                f"Error: Tool '{tool_name}' is not in the allowed tool set for agent {self.spec.name}. "
                f"Allowed: {', '.join(sorted(self.spec.tools_allowed))}"
            )

        # Enforce read-only mode
        if self.spec.read_only and tool_name in ("file_write", "file_edit", "bash"):
            return f"Error: Agent {self.spec.name} is in read-only mode. Cannot use '{tool_name}'."

        # Enforce read-only git for RA and read-only agents
        if self.spec.read_only and tool_name == "git":
            subcommand = args.get("subcommand", "")
            cmd_prefix = subcommand.split()[0] if subcommand.strip() else ""
            write_commands = {"add", "commit", "push", "merge", "rebase", "reset", "checkout", "cherry-pick"}
            if cmd_prefix in write_commands:
                return f"Error: Agent {self.spec.name} cannot run write git commands in read-only mode."

        # Execute the tool
        start = time.monotonic()
        try:
            result = await asyncio.wait_for(
                dispatch_tool(tool_name, args),
                timeout=120.0,
            )
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.debug(
                "%s: tool %s completed in %.0fms",
                self.spec.name, tool_name, elapsed_ms,
            )
            return result
        except asyncio.TimeoutError:
            return f"Error: Tool '{tool_name}' timed out after 120s."
        except Exception as e:
            return f"Error executing '{tool_name}': {type(e).__name__}: {e}"

    # -- System prompt construction --------------------------------------------

    def _build_system_prompt(self, briefing: RABriefing | None = None) -> str:
        """Build the full system prompt with RA briefing, bus context, and policy."""
        import os

        parts: list[str] = [self.spec.system_prompt]

        # RA briefing injection
        if briefing and not briefing.is_empty:
            parts.append(briefing.to_prompt_injection())

        # Context bus prior work
        bus_summary = self.bus.summary()
        if bus_summary:
            parts.append(f"\n## Prior Agent Work (Context Bus)\n{bus_summary}")

        # Environment
        parts.append(f"\n## Environment\nWorking directory: {os.getcwd()}")

        # Tool policy
        if self.spec.read_only:
            parts.append("\nIMPORTANT: You are in READ-ONLY mode. Do NOT modify any files.")

        # Quality gate requirement
        parts.append(
            "\n## Quality Gate\n"
            "At the END of your response, include a confidence assessment:\n"
            "CONFIDENCE: X.XX (0.00 to 1.00)\n"
            "This reflects how confident you are in the correctness and completeness of your work."
        )

        return "\n\n".join(parts)

    # -- Confidence extraction -------------------------------------------------

    @staticmethod
    def _extract_confidence(response: str) -> float:
        """Extract the confidence score from the agent's response.

        Looks for patterns like:
          CONFIDENCE: 0.92
          confidence_score: 0.85
          Confidence: 95%
        """
        if not response:
            return 0.0

        patterns = [
            r'(?i)confidence[:\s]+(\d+\.?\d*)\s*%',        # "Confidence: 92%"
            r'(?i)confidence[_\s]*(?:score)?[:\s]+(\d\.\d+)', # "CONFIDENCE: 0.92"
            r'(?i)confidence[_\s]*(?:score)?[:\s]+(\d+)',     # "confidence: 9"
        ]

        for pattern in patterns:
            match = re.search(pattern, response)
            if match:
                value = float(match.group(1))
                # Normalize percentage to 0-1 range
                if value > 1.0:
                    value = value / 100.0
                return max(0.0, min(1.0, value))

        # Default confidence if not explicitly stated
        return 0.5

    # -- Helpers ---------------------------------------------------------------

    def _make_event(self, event_type: AgentEventType, **data: Any) -> AgentEvent:
        """Create an AgentEvent from the current state."""
        return AgentEvent(
            event_type=event_type,
            agent_role=self.spec.role,
            agent_name=self.spec.name,
            timestamp=time.time(),
            data=data,
        )

    _last_briefing: RABriefing | None = None

    def _get_last_briefing(self) -> RABriefing | None:
        return getattr(self, "_last_briefing", None)
