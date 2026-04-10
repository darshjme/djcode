"""Shadow Army Orchestrator — Multi-agent parallel execution engine.

The orchestrator v2:
1. Classifies task complexity (SIMPLE, MODERATE, COMPLEX, CRITICAL)
2. Selects execution strategy (SINGLE, PARALLEL, PIPELINE, WAVE, FULL_ARMY)
3. Runs blocking gate agents (Security, Risk, Legal, SRE) before execution
4. Executes agents via the selected strategy with context propagation
5. Synthesizes results with priority ordering (Security > Compliance > Correctness)
6. Emits events throughout for TUI dashboard rendering

Execution strategies:
  SINGLE   — one agent, direct execution
  PARALLEL — multiple independent agents, asyncio.gather()
  PIPELINE — sequential chain: Scout -> Architect -> Coder -> Tester -> Reviewer
  WAVE     — Wave 1 (recon) -> Wave 2 (plan) -> Wave 3 (execute) -> Wave 4 (verify)
  FULL_ARMY— all relevant agents, Vyasa coordinates

Backwards compatible: the old Orchestrator class is preserved as a thin wrapper.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
import time
from typing import Any, AsyncIterator

from rich.console import Console

from djcode.agents.registry import (
    AGENT_SPECS,
    BLOCKING_AGENTS,
    AgentRole,
    AgentSpec,
    AgentTier,
    ROLE_TIERS,
    get_agent,
    get_agents_by_tier,
    get_agents_for_intent,
)
from djcode.orchestrator.context_bus import ContextBus, EntryType, Priority
from djcode.orchestrator.events import (
    EventBus,
    EventType,
    GateAction,
    GateSeverity,
    OrchestratorEvent,
    agent_complete_event,
    agent_error_event,
    agent_start_event,
    agent_token_event,
    agent_tool_event,
    blocking_gate_event,
    context_inject_event,
    orchestrator_complete_event,
    orchestrator_error_event,
    orchestrator_start_event,
    synthesis_complete_event,
    synthesis_start_event,
    wave_complete_event,
    wave_start_event,
)
from djcode.prompt_enhancer import detect_intent
from djcode.provider import Message, Provider
from djcode.tools import dispatch_tool

logger = logging.getLogger(__name__)
console = Console()
GOLD = "#FFD700"


# -- Complexity & Strategy Enums -----------------------------------------------

class TaskComplexity(str, enum.Enum):
    """Task complexity classification."""
    SIMPLE   = "simple"      # 1 agent
    MODERATE = "moderate"    # 2-3 agents
    COMPLEX  = "complex"     # pipeline (5+ agents)
    CRITICAL = "critical"    # full army with blocking gates


class ExecutionStrategy(str, enum.Enum):
    """How to run the selected agents."""
    SINGLE     = "single"
    PARALLEL   = "parallel"
    PIPELINE   = "pipeline"
    WAVE       = "wave"
    FULL_ARMY  = "full_army"


# -- Complexity keywords -------------------------------------------------------

_CRITICAL_KEYWORDS = frozenset({
    "deploy", "production", "security", "audit", "compliance", "migration",
    "financial", "payment", "trading", "pci", "gdpr", "sox", "hipaa",
    "incident", "outage", "rollback",
})

_COMPLEX_KEYWORDS = frozenset({
    "build", "create", "implement", "design", "architect", "full",
    "complete", "entire", "system", "feature", "api", "database",
    "integrate", "pipeline", "microservice",
})

_MODERATE_KEYWORDS = frozenset({
    "refactor", "review", "test", "optimize", "fix", "debug",
    "update", "improve", "add", "extend",
})


# -- Wave Definitions ----------------------------------------------------------

WAVE_DEFINITIONS: dict[int, dict[str, Any]] = {
    1: {
        "name": "Recon",
        "roles": [AgentRole.SCOUT],
        "description": "Explore and map the current state",
    },
    2: {
        "name": "Plan",
        "roles": [AgentRole.ARCHITECT, AgentRole.PRODUCT_STRATEGIST],
        "description": "Design the approach and plan execution",
    },
    3: {
        "name": "Execute",
        "roles": [AgentRole.CODER, AgentRole.TESTER],
        "description": "Implement the solution and write tests",
    },
    4: {
        "name": "Verify",
        "roles": [AgentRole.REVIEWER, AgentRole.SECURITY_COMPLIANCE, AgentRole.SRE],
        "description": "Review, audit, and validate the work",
    },
}

# Pipeline order for PIPELINE strategy
PIPELINE_ORDER: list[AgentRole] = [
    AgentRole.SCOUT,
    AgentRole.ARCHITECT,
    AgentRole.CODER,
    AgentRole.TESTER,
    AgentRole.REVIEWER,
]


# ==============================================================================
#  AgentRunner — Runs a single specialist agent with tool-calling loop
# ==============================================================================

class AgentRunner:
    """Runs a single specialist agent with its dedicated system prompt and tool policy.

    Supports both blocking (run) and streaming (run_streaming) execution.
    Emits events to the EventBus for TUI rendering.
    """

    def __init__(
        self,
        provider: Provider,
        spec: AgentSpec,
        context_bus: ContextBus,
        event_bus: EventBus | None = None,
        auto_accept: bool = True,
    ) -> None:
        self.provider = provider
        self.spec = spec
        self.bus = context_bus
        self.event_bus = event_bus
        self.auto_accept = auto_accept

    def _build_system_prompt(self) -> str:
        """Build the agent's system prompt with context bus injection."""
        prompt = self.spec.system_prompt

        # Inject context from other agents (excluding own prior work)
        bus_context = self.bus.summary_for_agent(self.spec.name)
        if bus_context:
            prompt += f"\n\n{bus_context}"

        # Inject working directory
        prompt += f"\n\n## Environment\nWorking directory: {os.getcwd()}"

        # Tool access note
        if self.spec.read_only:
            prompt += "\n\nIMPORTANT: You are in READ-ONLY mode. Do NOT modify any files."

        return prompt

    async def _emit(self, event: OrchestratorEvent) -> None:
        """Emit an event if event bus is available."""
        if self.event_bus:
            await self.event_bus.emit(event)

    async def _execute_tool(
        self, name: str, args_raw: Any, messages: list[Message], tc: dict[str, Any],
    ) -> str:
        """Execute a single tool call with policy enforcement and event emission."""
        # Parse arguments
        if isinstance(args_raw, str):
            try:
                args = json.loads(args_raw)
            except json.JSONDecodeError:
                args = {"command": args_raw} if name == "bash" else {}
        else:
            args = args_raw

        # Enforce tool policy
        if name not in self.spec.tools_allowed:
            result = f"Error: Agent {self.spec.name} is not allowed to use tool '{name}'"
        elif self.spec.read_only and name in ("file_write", "file_edit", "bash"):
            result = f"Error: Agent {self.spec.name} is in read-only mode"
        else:
            start = time.time()
            console.print(f"    [dim]{self.spec.name} -> {name}[/]")
            result = await dispatch_tool(name, args)
            duration_ms = (time.time() - start) * 1000

            await self._emit(agent_tool_event(
                agent_name=self.spec.name,
                agent_role=self.spec.role.value,
                tool_name=name,
                tool_args=args,
                tool_result=result,
                duration_ms=duration_ms,
            ))

        messages.append(Message(
            role="tool",
            content=result,
            tool_call_id=tc.get("id", f"call_{name}"),
            name=name,
        ))
        return result

    async def _process_stream(
        self, messages: list[Message], stream: bool = True,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Process a single round of LLM streaming, returning content and tool calls."""
        chunk_response = ""
        tool_calls: list[dict[str, Any]] = []

        if self.provider.is_ollama:
            async for chunk in self.provider.chat_ollama(messages, stream=stream):
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
            async for chunk in self.provider.chat_openai_compat(messages, stream=stream):
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

        return chunk_response, tool_calls

    async def run(self, task: str) -> str:
        """Run the agent on a task. Returns the full response."""
        messages: list[Message] = [
            Message(role="system", content=self._build_system_prompt()),
            Message(role="user", content=task),
        ]

        await self._emit(agent_start_event(
            self.spec.name, self.spec.role.value, task,
        ))

        start_time = time.time()
        full_response = ""
        total_tokens = 0

        try:
            for _round in range(self.spec.max_tool_rounds):
                chunk_response, tool_calls = await self._process_stream(messages)
                full_response += chunk_response
                total_tokens += len(chunk_response.split())  # rough estimate

                if not tool_calls:
                    break

                messages.append(Message(
                    role="assistant", content=chunk_response, tool_calls=tool_calls,
                ))

                for tc in tool_calls:
                    func = tc.get("function", {})
                    name = func.get("name", "unknown")
                    args_raw = func.get("arguments", "{}")
                    await self._execute_tool(name, args_raw, messages, tc)

            # Write result to context bus
            self.bus.write(
                agent=self.spec.name,
                role=self.spec.role.value,
                key="result",
                content=full_response,
                entry_type=EntryType.RESULT,
            )

            elapsed = time.time() - start_time
            await self._emit(agent_complete_event(
                agent_name=self.spec.name,
                agent_role=self.spec.role.value,
                result_preview=full_response[:300],
                confidence=0.0,
                elapsed_s=elapsed,
                tokens=total_tokens,
            ))

        except Exception as exc:
            await self._emit(agent_error_event(
                self.spec.name, self.spec.role.value, str(exc),
            ))
            logger.exception("Agent %s failed", self.spec.name)
            full_response = f"Error: Agent {self.spec.name} failed: {exc}"

        return full_response

    async def run_streaming(self, task: str) -> AsyncIterator[str]:
        """Run the agent and stream tokens as they arrive."""
        messages: list[Message] = [
            Message(role="system", content=self._build_system_prompt()),
            Message(role="user", content=task),
        ]

        await self._emit(agent_start_event(
            self.spec.name, self.spec.role.value, task,
        ))

        start_time = time.time()

        try:
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
                            await self._emit(agent_token_event(
                                self.spec.name, self.spec.role.value, content,
                            ))
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
                            await self._emit(agent_token_event(
                                self.spec.name, self.spec.role.value, content,
                            ))
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
                        entry_type=EntryType.RESULT,
                    )
                    elapsed = time.time() - start_time
                    await self._emit(agent_complete_event(
                        self.spec.name, self.spec.role.value,
                        chunk_response[:300], 0.0, elapsed, 0,
                    ))
                    break

                # Handle tool calls
                messages.append(Message(
                    role="assistant", content=chunk_response, tool_calls=tool_calls,
                ))

                for tc in tool_calls:
                    func = tc.get("function", {})
                    name = func.get("name", "unknown")
                    args_raw = func.get("arguments", "{}")
                    await self._execute_tool(name, args_raw, messages, tc)

        except Exception as exc:
            await self._emit(agent_error_event(
                self.spec.name, self.spec.role.value, str(exc),
            ))
            logger.exception("Agent %s streaming failed", self.spec.name)
            yield f"\nError: Agent {self.spec.name} failed: {exc}"


# ==============================================================================
#  ShadowOrchestrator — The v2 Multi-Agent Parallel Engine
# ==============================================================================

class ShadowOrchestrator:
    """Multi-agent parallel orchestrator with wave-based execution.

    The Shadow Army engine. Classifies task complexity, selects execution
    strategy, runs blocking gates, executes agents in parallel/pipeline/wave,
    and synthesizes results.

    Usage:
        orch = ShadowOrchestrator(provider)
        async for event in orch.execute("build a REST API with auth"):
            handle_event(event)
    """

    def __init__(
        self,
        provider: Provider,
        auto_accept: bool = True,
    ) -> None:
        self.provider = provider
        self.auto_accept = auto_accept
        self.bus = ContextBus()
        self.event_bus = EventBus()

        # Semantic router (embedding-based agent dispatch)
        from djcode.orchestrator.router import SemanticRouter
        self.router = SemanticRouter(provider)
        self._router_initialized = False

        # Vector context store (long-term memory retrieval)
        from djcode.orchestrator.vector_context import VectorContextStore
        self.vector_store = VectorContextStore(provider)
        self.vector_store.initialize()

    # -- Complexity Classification ---------------------------------------------

    def classify_complexity(self, task: str) -> TaskComplexity:
        """Classify task complexity based on keywords and structure.

        Returns SIMPLE for single-concern tasks, up to CRITICAL for
        production/security/financial tasks that require full army.
        """
        task_lower = task.lower()
        words = set(task_lower.split())

        # Check for critical keywords first
        if words & _CRITICAL_KEYWORDS:
            return TaskComplexity.CRITICAL

        # Check for complex keywords
        complex_hits = words & _COMPLEX_KEYWORDS
        if len(complex_hits) >= 2 or any(kw in task_lower for kw in [
            "from scratch", "full stack", "end to end", "complete system",
        ]):
            return TaskComplexity.COMPLEX

        # Check for moderate keywords
        if words & _MODERATE_KEYWORDS:
            return TaskComplexity.MODERATE

        return TaskComplexity.SIMPLE

    def select_strategy(
        self, complexity: TaskComplexity, roles: list[AgentRole],
    ) -> ExecutionStrategy:
        """Select execution strategy based on complexity and routed agents.

        Strategy selection logic:
        - SIMPLE + 1 agent -> SINGLE
        - MODERATE + independent agents -> PARALLEL
        - COMPLEX -> PIPELINE (sequential handoff)
        - CRITICAL -> WAVE (multi-phase with blocking gates)
        """
        # CRITICAL always uses WAVE regardless of agent count
        if complexity == TaskComplexity.CRITICAL:
            return ExecutionStrategy.WAVE

        # Simple tasks or single agent -> direct execution
        if complexity == TaskComplexity.SIMPLE or len(roles) == 1:
            return ExecutionStrategy.SINGLE

        if complexity == TaskComplexity.COMPLEX:
            # Use pipeline if we have the canonical pipeline roles
            pipeline_roles = {r for r in roles if r in set(PIPELINE_ORDER)}
            if len(pipeline_roles) >= 3:
                return ExecutionStrategy.PIPELINE
            return ExecutionStrategy.WAVE

        # MODERATE — parallel if agents are independent, pipeline if sequential
        has_blocking = any(r in BLOCKING_AGENTS for r in roles)
        if has_blocking:
            return ExecutionStrategy.PIPELINE
        return ExecutionStrategy.PARALLEL

    # -- Agent Header Display --------------------------------------------------

    def _print_agent_header(self, spec: AgentSpec) -> None:
        """Print a compact header when an agent starts working."""
        icon = {
            "orchestrator": "\U0001f3af", "coder": "\U0001f4bb",
            "debugger": "\U0001f50e", "architect": "\U0001f4d0",
            "reviewer": "\u2705", "tester": "\U0001f9ea",
            "scout": "\U0001f50d", "devops": "\U0001f680",
            "docs": "\U0001f4dd", "refactorer": "\U0001f504",
            "product_strategist": "\U0001f4ca", "security_compliance": "\U0001f6e1\ufe0f",
            "data_scientist": "\U0001f9ec", "sre": "\U0001f6a8",
            "cost_optimizer": "\U0001f4b0", "integration": "\U0001f517",
            "ux_workflow": "\U0001f3a8", "legal_intelligence": "\u2696\ufe0f",
            "risk_engine": "\u26a0\ufe0f",
        }.get(spec.role.value, "\u26a1")

        console.print(
            f"\n  [{GOLD}]{icon} {spec.name}[/] [dim]({spec.title})[/]"
        )
        console.print(f"  [dim]{'---' * 17}[/]")

    # -- Runner Factory --------------------------------------------------------

    def _make_runner(self, spec: AgentSpec) -> AgentRunner:
        """Create an AgentRunner with full wiring."""
        return AgentRunner(
            provider=self.provider,
            spec=spec,
            context_bus=self.bus,
            event_bus=self.event_bus,
            auto_accept=self.auto_accept,
        )

    # -- Blocking Gate Check ---------------------------------------------------

    async def _run_blocking_gates(
        self, task: str, roles: list[AgentRole],
    ) -> list[OrchestratorEvent]:
        """Run blocking agents and check for HALT conditions.

        Blocking agents: Kavach (Security), Varuna (Risk), Mitra (Legal), Indra (SRE).
        Any CRITICAL finding from these agents halts the entire pipeline.

        Returns list of gate events for the caller to inspect.
        """
        blocking_roles = [r for r in roles if r in BLOCKING_AGENTS]
        if not blocking_roles:
            return []

        gate_events: list[OrchestratorEvent] = []

        # Run blocking agents in parallel — they're independent reviewers
        async def run_gate(role: AgentRole) -> OrchestratorEvent | None:
            spec = get_agent(role)
            self._print_agent_header(spec)
            runner = self._make_runner(spec)

            gate_task = (
                f"BLOCKING GATE CHECK: Review the following task for issues in your domain.\n"
                f"Task: {task}\n\n"
                f"Respond with:\n"
                f"- SEVERITY: INFO|WARNING|HIGH|CRITICAL\n"
                f"- ACTION: PASS|WARN|HALT\n"
                f"- FINDING: Brief description\n\n"
                f"If no issues, respond with SEVERITY: INFO, ACTION: PASS"
            )

            result = await runner.run(gate_task)
            result_upper = result.upper()

            # Parse gate response
            severity = GateSeverity.INFO
            action = GateAction.PASS

            if "CRITICAL" in result_upper:
                severity = GateSeverity.CRITICAL
                action = GateAction.HALT
            elif "HIGH" in result_upper:
                severity = GateSeverity.HIGH
                action = GateAction.WARN
            elif "WARNING" in result_upper:
                severity = GateSeverity.WARNING
                action = GateAction.WARN

            if "HALT" in result_upper:
                action = GateAction.HALT
            elif "ESCALATE" in result_upper:
                action = GateAction.ESCALATE

            event = blocking_gate_event(
                agent_name=spec.name,
                agent_role=spec.role.value,
                severity=severity,
                finding=result[:500],
                action=action,
            )
            await self.event_bus.emit(event)
            return event

        results = await asyncio.gather(
            *[run_gate(role) for role in blocking_roles],
            return_exceptions=True,
        )

        for r in results:
            if isinstance(r, OrchestratorEvent):
                gate_events.append(r)
            elif isinstance(r, Exception):
                logger.exception("Blocking gate failed: %s", r)

        return gate_events

    # -- Execution Strategies --------------------------------------------------

    async def execute_single(
        self, role: AgentRole, task: str,
    ) -> AsyncIterator[OrchestratorEvent]:
        """Execute a single agent with streaming. Yields events."""
        spec = get_agent(role)
        self._print_agent_header(spec)
        runner = self._make_runner(spec)

        async for token in runner.run_streaming(task):
            yield agent_token_event(spec.name, spec.role.value, token)

    async def execute_parallel(
        self, roles: list[AgentRole], task: str,
    ) -> AsyncIterator[OrchestratorEvent]:
        """Execute multiple independent agents in parallel via asyncio.gather.

        All agents run concurrently. Results are collected on the context bus.
        Only the final synthesis is streamed.
        """
        async def run_agent(role: AgentRole) -> str:
            spec = get_agent(role)
            self._print_agent_header(spec)
            runner = self._make_runner(spec)
            return await runner.run(task)

        yield wave_start_event(1, "Parallel Execution", [get_agent(r).name for r in roles])

        start = time.time()
        results = await asyncio.gather(
            *[run_agent(r) for r in roles],
            return_exceptions=True,
        )

        agent_results: dict[str, str] = {}
        for role, result in zip(roles, results):
            spec = get_agent(role)
            if isinstance(result, Exception):
                agent_results[spec.name] = f"Error: {result}"
                yield agent_error_event(spec.name, spec.role.value, str(result))
            else:
                agent_results[spec.name] = result
                yield agent_complete_event(
                    spec.name, spec.role.value, result[:300],
                    0.0, time.time() - start, 0,
                )

        yield wave_complete_event(1, "Parallel Execution", agent_results, time.time() - start)

    async def execute_pipeline(
        self, roles: list[AgentRole], task: str,
    ) -> AsyncIterator[OrchestratorEvent]:
        """Execute agents sequentially, each building on prior context.

        Each agent gets the full context bus with prior agents' results.
        The last agent in the pipeline streams its output.
        """
        for i, role in enumerate(roles):
            spec = get_agent(role)
            self._print_agent_header(spec)

            # Build task with accumulated context
            agent_task = task
            bus_summary = self.bus.summary_for_agent(spec.name)
            if bus_summary:
                agent_task = f"{task}\n\n{bus_summary}"

            runner = self._make_runner(spec)

            if i == len(roles) - 1:
                # Stream the last agent
                async for token in runner.run_streaming(agent_task):
                    yield agent_token_event(spec.name, spec.role.value, token)
            else:
                start = time.time()
                result = await runner.run(agent_task)
                elapsed = time.time() - start

                # Show brief preview of intermediate agent work
                preview = result.strip().split("\n")[0][:100] if result.strip() else "(no output)"
                console.print(f"    [dim]{preview}[/]")

                yield agent_complete_event(
                    spec.name, spec.role.value, result[:300],
                    0.0, elapsed, 0,
                )

    async def execute_wave(
        self, task: str, roles: list[AgentRole] | None = None,
    ) -> AsyncIterator[OrchestratorEvent]:
        """Execute agents in waves: Recon -> Plan -> Execute -> Verify.

        Each wave runs its agents in parallel. Wave N+1 agents have access
        to all Wave N results via the context bus. Blocking agents in the
        Verify wave can halt the pipeline.
        """
        for wave_num, wave_def in WAVE_DEFINITIONS.items():
            wave_name = wave_def["name"]
            wave_roles = wave_def["roles"]

            # Filter to roles that were actually selected (if roles specified)
            if roles is not None:
                wave_roles = [r for r in wave_roles if r in roles]
            if not wave_roles:
                continue

            agent_names = [get_agent(r).name for r in wave_roles]
            yield wave_start_event(wave_num, wave_name, agent_names)

            console.print(
                f"\n  [bold {GOLD}]Wave {wave_num}: {wave_name}[/] "
                f"[dim]({', '.join(agent_names)})[/]"
            )

            start = time.time()

            # Run wave agents in parallel
            async def run_wave_agent(role: AgentRole, wn: int) -> tuple[str, str]:
                spec = get_agent(role)
                self._print_agent_header(spec)

                agent_task = task
                bus_context = self.bus.summary_for_agent(spec.name)
                if bus_context:
                    agent_task = f"{task}\n\n{bus_context}"

                runner = self._make_runner(spec)
                result = await runner.run(agent_task)
                return spec.name, result

            results = await asyncio.gather(
                *[run_wave_agent(r, wave_num) for r in wave_roles],
                return_exceptions=True,
            )

            wave_results: dict[str, str] = {}
            for r in results:
                if isinstance(r, Exception):
                    logger.exception("Wave %d agent failed: %s", wave_num, r)
                else:
                    name, result = r
                    wave_results[name] = result
                    yield agent_complete_event(
                        name, "", result[:300], 0.0, time.time() - start, 0,
                    )

            elapsed = time.time() - start
            yield wave_complete_event(wave_num, wave_name, wave_results, elapsed)

            console.print(
                f"  [dim]Wave {wave_num} complete ({elapsed:.1f}s, "
                f"{len(wave_results)}/{len(wave_roles)} agents)[/]"
            )

    # -- Main Entry Point ------------------------------------------------------

    async def execute(self, task: str) -> AsyncIterator[OrchestratorEvent]:
        """Full orchestration — classify, route, gate, execute, synthesize.

        This is the main entry point. It:
        1. Initializes the semantic router (lazy)
        2. Routes the task to appropriate agents
        3. Classifies complexity and selects strategy
        4. Injects vector store context
        5. Runs blocking gates if needed
        6. Executes via selected strategy
        7. Stores results back to vector store
        8. Emits completion event
        """
        orchestration_start = time.time()

        # Initialize semantic router on first use
        if not self._router_initialized:
            self._router_initialized = await self.router.initialize()

        # Route to agents
        if self.router.is_semantic:
            roles = await self.router.route(task)
            route_method = "semantic"
        else:
            intent = detect_intent(task)
            roles = get_agents_for_intent(intent)
            route_method = "regex"

        intent = detect_intent(task)

        # Reset context bus for new orchestration
        self.bus.clear()
        self.bus.set_task(task, intent)

        # Inject vector store context
        n_injected = self.vector_store.inject_context(self.bus, task, n_results=3)
        if n_injected:
            yield context_inject_event("chromadb", n_injected)

        # Classify and strategize
        complexity = self.classify_complexity(task)
        strategy = self.select_strategy(complexity, roles)

        agent_names = [get_agent(r).name for r in roles]

        console.print(
            f"\n  [bold {GOLD}]Shadow Army Orchestrator[/] "
            f"[dim]intent={intent}, complexity={complexity.value}, "
            f"strategy={strategy.value}, agents={agent_names}, "
            f"router={route_method}"
            + (f", context={n_injected} docs" if n_injected else "")
            + "[/]"
        )

        yield orchestrator_start_event(task, strategy.value, agent_names, complexity.value)

        # Run blocking gates for CRITICAL tasks
        halted = False
        if complexity == TaskComplexity.CRITICAL:
            gate_events = await self._run_blocking_gates(task, roles)
            for ge in gate_events:
                yield ge
                if ge.data.get("action") == GateAction.HALT.value:
                    halted = True

        if halted:
            yield orchestrator_error_event(
                task, "Halted by blocking gate agent (CRITICAL finding)", agent_names,
            )
            console.print(
                f"\n  [bold red]HALTED[/] — Blocking agent issued CRITICAL halt. "
                f"Review findings above before proceeding."
            )
            return

        # Execute via selected strategy
        agents_completed: list[str] = []
        try:
            if strategy == ExecutionStrategy.SINGLE:
                async for event in self.execute_single(roles[0], task):
                    yield event
                agents_completed.append(get_agent(roles[0]).name)

            elif strategy == ExecutionStrategy.PARALLEL:
                async for event in self.execute_parallel(roles, task):
                    yield event
                    if event.event_type == EventType.AGENT_COMPLETE:
                        agents_completed.append(event.agent_name)

            elif strategy == ExecutionStrategy.PIPELINE:
                async for event in self.execute_pipeline(roles, task):
                    yield event
                    if event.event_type == EventType.AGENT_COMPLETE:
                        agents_completed.append(event.agent_name)

            elif strategy in (ExecutionStrategy.WAVE, ExecutionStrategy.FULL_ARMY):
                async for event in self.execute_wave(task, roles):
                    yield event
                    if event.event_type == EventType.AGENT_COMPLETE:
                        agents_completed.append(event.agent_name)

        except Exception as exc:
            logger.exception("Orchestration failed during %s", strategy.value)
            yield orchestrator_error_event(task, str(exc), agents_completed)
            return

        # Store agent results in vector store for future context
        for entry in self.bus.read_all():
            if entry.role != "memory":
                self.vector_store.store_agent_result(
                    agent_name=entry.agent,
                    role=entry.role,
                    task=task,
                    result=entry.content,
                )

        # Completion
        total_elapsed = time.time() - orchestration_start
        stored = self.vector_store.count()

        console.print(
            f"\n  [dim]Orchestration complete. Strategy={strategy.value}, "
            f"{len(self.bus)} entries on context bus, "
            f"{len(agents_completed)} agents completed"
            + (f", {stored} in vector memory" if stored else "")
            + f" ({total_elapsed:.1f}s).[/]\n"
        )

        yield orchestrator_complete_event(
            task=task,
            agents_used=agents_completed,
            total_tokens=0,
            total_duration_s=total_elapsed,
            strategy=strategy.value,
        )

    # -- Direct Agent Execution ------------------------------------------------

    async def run_single_agent(self, role: AgentRole, task: str) -> str:
        """Run a single specialist agent on a task. Returns full response."""
        spec = get_agent(role)
        self.bus.clear()
        self.bus.set_task(task, role.value)
        self._print_agent_header(spec)

        runner = self._make_runner(spec)
        return await runner.run(task)

    async def run_single_agent_streaming(
        self, role: AgentRole, task: str,
    ) -> AsyncIterator[str]:
        """Run a single specialist agent with streaming output."""
        spec = get_agent(role)
        self.bus.clear()
        self.bus.set_task(task, role.value)
        self._print_agent_header(spec)

        runner = self._make_runner(spec)
        async for token in runner.run_streaming(task):
            yield token

    # -- Roster Display --------------------------------------------------------

    def render_roster(self) -> None:
        """Display the full agent roster — all 18 agents with status."""
        console.print(f"\n  [bold {GOLD}]Shadow Army Roster[/]\n")

        tier_labels = {
            AgentTier.CONTROL: "TIER 4 -- CONTROL",
            AgentTier.ENTERPRISE: "TIER 3 -- ENTERPRISE INTELLIGENCE",
            AgentTier.ARCHITECTURE: "TIER 2 -- ARCHITECTURE",
            AgentTier.EXECUTION: "TIER 1 -- EXECUTION",
        }

        for tier in [AgentTier.CONTROL, AgentTier.ENTERPRISE,
                     AgentTier.ARCHITECTURE, AgentTier.EXECUTION]:
            agents = get_agents_by_tier(tier)
            console.print(f"\n  [bold dim]{tier_labels[tier]}[/]")

            for spec in agents:
                icon = {
                    "orchestrator": "\U0001f3af", "coder": "\U0001f4bb",
                    "debugger": "\U0001f50e", "architect": "\U0001f4d0",
                    "reviewer": "\u2705", "tester": "\U0001f9ea",
                    "scout": "\U0001f50d", "devops": "\U0001f680",
                    "docs": "\U0001f4dd", "refactorer": "\U0001f504",
                    "product_strategist": "\U0001f4ca",
                    "security_compliance": "\U0001f6e1\ufe0f",
                    "data_scientist": "\U0001f9ec", "sre": "\U0001f6a8",
                    "cost_optimizer": "\U0001f4b0", "integration": "\U0001f517",
                    "ux_workflow": "\U0001f3a8",
                    "legal_intelligence": "\u2696\ufe0f",
                    "risk_engine": "\u26a0\ufe0f",
                }.get(spec.role.value, "\u26a1")

                mode = "[dim red]read-only[/]" if spec.read_only else "[dim green]full[/]"
                blocking = " [bold red]BLOCKING[/]" if spec.role in BLOCKING_AGENTS else ""
                tools = len(spec.tools_allowed)

                console.print(
                    f"  {icon} [bold white]{spec.name:<16}[/] "
                    f"[dim]{spec.title:<38}[/] "
                    f"{tools} tools  {mode}  [dim]p={spec.priority}[/]{blocking}"
                )

        console.print(f"\n  [dim]Use /orchestra <task> for multi-agent execution[/]")
        console.print(f"  [dim]Use /review, /debug, /test, /refactor, /devops, /docs for single-agent[/]\n")


# ==============================================================================
#  Orchestrator — Backwards-compatible wrapper
# ==============================================================================

class Orchestrator:
    """Backwards-compatible orchestrator wrapper around ShadowOrchestrator.

    Preserves the original Orchestrator API so existing code (TUI, REPL)
    continues to work without modification.

    Usage:
        orch = Orchestrator(provider)
        result = await orch.run_single_agent(AgentRole.DEBUGGER, "fix the crash")
        async for token in orch.execute("build a REST API with auth"):
            print(token, end="")
    """

    def __init__(self, provider: Provider, auto_accept: bool = True) -> None:
        self._shadow = ShadowOrchestrator(provider, auto_accept)
        self.provider = provider
        self.auto_accept = auto_accept
        self.bus = self._shadow.bus
        self.router = self._shadow.router
        self.vector_store = self._shadow.vector_store
        self.event_bus = self._shadow.event_bus
        self._router_initialized = False

    def _print_agent_header(self, spec: AgentSpec) -> None:
        """Delegate to shadow orchestrator."""
        self._shadow._print_agent_header(spec)

    async def run_single_agent(self, role: AgentRole, task: str) -> str:
        """Run a single specialist agent on a task. Returns full response."""
        return await self._shadow.run_single_agent(role, task)

    async def run_single_agent_streaming(
        self, role: AgentRole, task: str,
    ) -> AsyncIterator[str]:
        """Run a single specialist agent with streaming output."""
        async for token in self._shadow.run_single_agent_streaming(role, task):
            yield token

    async def execute(self, task: str) -> AsyncIterator[str]:
        """Full orchestration — yields tokens (not events) for backwards compatibility.

        Internally uses the ShadowOrchestrator's event system but converts
        agent_token events back to plain string tokens for the old API.
        """
        async for event in self._shadow.execute(task):
            # Extract tokens from agent_token events for backwards compat
            if event.event_type == EventType.AGENT_TOKEN:
                token = event.data.get("token", "")
                if token:
                    yield token

    def render_roster(self) -> None:
        """Display the agent roster."""
        self._shadow.render_roster()
