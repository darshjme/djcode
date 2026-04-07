"""Orchestrator Engine — Multi-agent task decomposition and execution.

The orchestrator:
1. Classifies intent from user input
2. Selects the right agent(s) from the registry
3. Injects agent-specific system prompts + context bus data
4. Runs agents (single or multi-agent pipeline)
5. Collects results on the context bus
6. Synthesizes final output

For complex tasks, runs a pipeline:
  SCOUT (recon) → ARCHITECT (plan) → CODER (implement) → TESTER (verify) → REVIEWER (check)
"""

from __future__ import annotations

import sys
import time
from typing import Any, AsyncIterator

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from djcode.agents.registry import (
    AGENT_SPECS,
    AgentRole,
    AgentSpec,
    get_agent_for_intent,
    get_spec,
    list_agents,
)
from djcode.orchestrator.context_bus import ContextBus
from djcode.prompt_enhancer import detect_intent
from djcode.provider import Message, Provider
from djcode.tools import dispatch_tool

console = Console()
GOLD = "#FFD700"


class AgentRunner:
    """Runs a single specialist agent with its dedicated system prompt and tool policy."""

    def __init__(
        self,
        provider: Provider,
        spec: AgentSpec,
        context_bus: ContextBus,
        auto_accept: bool = True,
    ) -> None:
        self.provider = provider
        self.spec = spec
        self.bus = context_bus
        self.auto_accept = auto_accept

    def _build_system_prompt(self) -> str:
        """Build the agent's system prompt with context bus injection."""
        prompt = self.spec.system_prompt

        # Inject context bus summary if there's prior work
        bus_summary = self.bus.summary()
        if bus_summary:
            prompt += f"\n\n## Prior Agent Work (Context Bus)\n{bus_summary}"

        # Inject working directory
        import os
        prompt += f"\n\n## Environment\nWorking directory: {os.getcwd()}"

        # Tool access note
        if self.spec.read_only:
            prompt += "\n\nIMPORTANT: You are in READ-ONLY mode. Do NOT modify any files."

        return prompt

    async def run(self, task: str) -> str:
        """Run the agent on a task. Returns the full response."""
        messages: list[Message] = [
            Message(role="system", content=self._build_system_prompt()),
            Message(role="user", content=task),
        ]

        full_response = ""

        for _round in range(self.spec.max_tool_rounds):
            chunk_response = ""
            tool_calls: list[dict[str, Any]] = []

            # Stream from provider
            if self.provider.is_ollama:
                async for chunk in self.provider.chat_ollama(messages, stream=True):
                    msg = chunk.get("message", {})
                    content = msg.get("content", "")
                    if content:
                        chunk_response += content
                    if "tool_calls" in msg:
                        tool_calls.extend(msg["tool_calls"])
                    if chunk.get("done", False):
                        if "tool_calls" in msg:
                            tool_calls.extend(msg.get("tool_calls", []))
                        break
            else:
                tool_calls_acc: dict[int, dict] = {}
                async for chunk in self.provider.chat_openai_compat(messages, stream=True):
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        chunk_response += content
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
                                if "name" in fn:
                                    tool_calls_acc[idx]["function"]["name"] = fn["name"]
                                if "arguments" in fn:
                                    tool_calls_acc[idx]["function"]["arguments"] += fn["arguments"]
                    finish = choices[0].get("finish_reason", "")
                    if finish == "tool_calls":
                        tool_calls = list(tool_calls_acc.values())
                    elif finish == "stop":
                        break

            full_response += chunk_response

            if not tool_calls:
                break

            # Execute tool calls (respecting agent's tool policy)
            messages.append(Message(role="assistant", content=chunk_response, tool_calls=tool_calls))

            import json
            for tc in tool_calls:
                func = tc.get("function", {})
                name = func.get("name", "unknown")
                args_raw = func.get("arguments", "{}")

                # Enforce tool policy
                if name not in self.spec.tools_allowed:
                    result = f"Error: Agent {self.spec.name} is not allowed to use tool '{name}'"
                elif self.spec.read_only and name in ("file_write", "file_edit", "bash"):
                    result = f"Error: Agent {self.spec.name} is in read-only mode"
                else:
                    if isinstance(args_raw, str):
                        try:
                            args = json.loads(args_raw)
                        except json.JSONDecodeError:
                            args = {"command": args_raw} if name == "bash" else {}
                    else:
                        args = args_raw

                    # Display tool use
                    console.print(f"    [dim]{self.spec.name} \u2192 {name}[/]")
                    result = await dispatch_tool(name, args)

                messages.append(Message(
                    role="tool",
                    content=result,
                    tool_call_id=tc.get("id", f"call_{name}"),
                    name=name,
                ))

        # Write result to context bus
        self.bus.write(
            agent=self.spec.name,
            role=self.spec.role.value,
            key="result",
            content=full_response,
        )

        return full_response

    async def run_streaming(self, task: str) -> AsyncIterator[str]:
        """Run the agent and stream tokens as they arrive."""
        messages: list[Message] = [
            Message(role="system", content=self._build_system_prompt()),
            Message(role="user", content=task),
        ]

        for _round in range(self.spec.max_tool_rounds):
            chunk_response = ""
            tool_calls: list[dict[str, Any]] = []

            if self.provider.is_ollama:
                async for chunk in self.provider.chat_ollama(messages, stream=True):
                    msg = chunk.get("message", {})
                    content = msg.get("content", "")
                    if content:
                        chunk_response += content
                        yield content
                    if "tool_calls" in msg:
                        tool_calls.extend(msg["tool_calls"])
                    if chunk.get("done", False):
                        break
            else:
                tool_calls_acc: dict[int, dict] = {}
                async for chunk in self.provider.chat_openai_compat(messages, stream=True):
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        chunk_response += content
                        yield content
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
                                if "name" in fn:
                                    tool_calls_acc[idx]["function"]["name"] = fn["name"]
                                if "arguments" in fn:
                                    tool_calls_acc[idx]["function"]["arguments"] += fn["arguments"]
                    finish = choices[0].get("finish_reason", "")
                    if finish == "tool_calls":
                        tool_calls = list(tool_calls_acc.values())
                    elif finish == "stop":
                        break

            if not tool_calls:
                self.bus.write(
                    agent=self.spec.name,
                    role=self.spec.role.value,
                    key="result",
                    content=chunk_response,
                )
                break

            # Handle tool calls
            messages.append(Message(role="assistant", content=chunk_response, tool_calls=tool_calls))

            import json
            for tc in tool_calls:
                func = tc.get("function", {})
                name = func.get("name", "unknown")
                args_raw = func.get("arguments", "{}")

                if name not in self.spec.tools_allowed:
                    result = f"Error: {self.spec.name} cannot use '{name}'"
                elif self.spec.read_only and name in ("file_write", "file_edit", "bash"):
                    result = f"Error: {self.spec.name} is read-only"
                else:
                    if isinstance(args_raw, str):
                        try:
                            args = json.loads(args_raw)
                        except json.JSONDecodeError:
                            args = {"command": args_raw} if name == "bash" else {}
                    else:
                        args = args_raw

                    console.print(f"    [dim]{self.spec.name} \u2192 {name}[/]")
                    result = await dispatch_tool(name, args)

                messages.append(Message(
                    role="tool",
                    content=result,
                    tool_call_id=tc.get("id", f"call_{name}"),
                    name=name,
                ))


class Orchestrator:
    """Multi-agent orchestrator that dispatches tasks to specialist agents.

    Usage:
        orch = Orchestrator(provider)
        result = await orch.run_single_agent(AgentRole.DEBUGGER, "fix the crash")
        async for token in orch.execute("build a REST API with auth"):
            print(token, end="")
    """

    def __init__(self, provider: Provider, auto_accept: bool = True) -> None:
        self.provider = provider
        self.auto_accept = auto_accept
        self.bus = ContextBus()

        # Semantic router (embedding-based agent dispatch)
        from djcode.orchestrator.router import SemanticRouter
        self.router = SemanticRouter(provider)
        self._router_initialized = False

        # Vector context store (long-term memory retrieval)
        from djcode.orchestrator.vector_context import VectorContextStore
        self.vector_store = VectorContextStore(provider)
        self.vector_store.initialize()  # Non-blocking, fails gracefully

    def _print_agent_header(self, spec: AgentSpec) -> None:
        """Print a compact header when an agent starts working."""
        icon = {
            "orchestrator": "\U0001f3af",
            "coder": "\U0001f4bb",
            "debugger": "\U0001f50e",
            "architect": "\U0001f4d0",
            "reviewer": "\u2705",
            "tester": "\U0001f9ea",
            "scout": "\U0001f50d",
            "devops": "\U0001f680",
            "docs": "\U0001f4dd",
            "refactorer": "\U0001f504",
        }.get(spec.role.value, "\u26a1")

        console.print(
            f"\n  [{GOLD}]{icon} {spec.name}[/] [dim]({spec.title})[/]"
        )
        console.print(f"  [dim]{'─' * 50}[/]")

    async def run_single_agent(self, role: AgentRole, task: str) -> str:
        """Run a single specialist agent on a task. Returns full response."""
        spec = get_spec(role)
        self.bus.clear()
        self.bus.set_task(task, role.value)

        self._print_agent_header(spec)

        runner = AgentRunner(self.provider, spec, self.bus, self.auto_accept)
        result = await runner.run(task)
        return result

    async def run_single_agent_streaming(
        self, role: AgentRole, task: str
    ) -> AsyncIterator[str]:
        """Run a single specialist agent with streaming output."""
        spec = get_spec(role)
        self.bus.clear()
        self.bus.set_task(task, role.value)

        self._print_agent_header(spec)

        runner = AgentRunner(self.provider, spec, self.bus, self.auto_accept)
        async for token in runner.run_streaming(task):
            yield token

    async def execute(self, task: str) -> AsyncIterator[str]:
        """Full orchestration — classify intent, pick agents, execute pipeline.

        1. Route via semantic embeddings (or regex fallback)
        2. Inject relevant vector context from past sessions
        3. Dispatch to single or multi-agent pipeline
        4. Store results back to vector store for future retrieval
        """
        # Initialize semantic router on first use (lazy, non-blocking)
        if not self._router_initialized:
            self._router_initialized = await self.router.initialize()

        # Route: semantic if available, regex fallback
        if self.router.is_semantic:
            roles = await self.router.route(task)
            route_method = "semantic"
        else:
            from djcode.prompt_enhancer import detect_intent as _detect
            intent = _detect(task)
            roles = get_agent_for_intent(intent)
            route_method = "regex"

        intent = detect_intent(task)
        self.bus.clear()
        self.bus.set_task(task, intent)

        # Inject relevant past context from vector store
        n_injected = self.vector_store.inject_context(self.bus, task, n_results=3)

        console.print(
            f"\n  [\u2728 {GOLD}]Orchestrator[/] "
            f"[dim]intent={intent}, agents={[get_spec(r).name for r in roles]}, "
            f"router={route_method}"
            + (f", context={n_injected} docs" if n_injected else "")
            + "[/]"
        )

        if len(roles) == 1:
            # Single agent — stream directly
            async for token in self.run_single_agent_streaming(roles[0], task):
                yield token
        else:
            # Multi-agent pipeline — run sequentially, each builds on prior context
            for i, role in enumerate(roles):
                spec = get_spec(role)
                self._print_agent_header(spec)

                # Build task with context from prior agents
                agent_task = task
                if self.bus:
                    agent_task = (
                        f"{task}\n\n"
                        f"## Context from prior agents\n"
                        f"{self.bus.summary()}"
                    )

                runner = AgentRunner(self.provider, spec, self.bus, self.auto_accept)

                # Stream the last agent, collect others
                if i == len(roles) - 1:
                    async for token in runner.run_streaming(agent_task):
                        yield token
                else:
                    result = await runner.run(agent_task)
                    # Show a brief summary of intermediate agent work
                    lines = result.strip().split("\n")
                    preview = lines[0][:100] if lines else "(no output)"
                    console.print(f"    [dim]{preview}[/]")

        # Store agent results in vector store for future context
        for entry in self.bus.read_all():
            if entry.role != "memory":  # Don't re-store retrieved context
                self.vector_store.store_agent_result(
                    agent_name=entry.agent,
                    role=entry.role,
                    task=task,
                    result=entry.content,
                )

        # Final summary
        stored = self.vector_store.count()
        console.print(
            f"\n  [dim]Orchestration complete. "
            f"{len(self.bus)} entries on context bus"
            + (f", {stored} in vector memory" if stored else "")
            + ".[/]\n"
        )

    def render_roster(self) -> None:
        """Display the agent roster — all 10 agents with status."""
        console.print(f"\n  [bold {GOLD}]Agent Roster[/]\n")

        for spec in list_agents():
            icon = {
                "orchestrator": "\U0001f3af",
                "coder": "\U0001f4bb",
                "debugger": "\U0001f50e",
                "architect": "\U0001f4d0",
                "reviewer": "\u2705",
                "tester": "\U0001f9ea",
                "scout": "\U0001f50d",
                "devops": "\U0001f680",
                "docs": "\U0001f4dd",
                "refactorer": "\U0001f504",
            }.get(spec.role.value, "\u26a1")

            mode = "[dim red]read-only[/]" if spec.read_only else "[dim green]full[/]"
            tools = len(spec.tools_allowed)
            temp = f"t={spec.temperature}"

            console.print(
                f"  {icon} [bold white]{spec.name:<14}[/] "
                f"[dim]{spec.title:<28}[/] "
                f"{tools} tools  {mode}  [dim]{temp}[/]"
            )

        console.print(f"\n  [dim]Use /orchestra <task> for multi-agent execution[/]")
        console.print(f"  [dim]Use /review, /debug, /test, /refactor, /devops, /docs for single-agent[/]\n")
