"""Parallel Coordinator — Runs multiple PhD agents concurrently.

Execution patterns:
  1. run_parallel()  — All agents run independently via asyncio.gather
  2. run_pipeline()  — Sequential chain: output of A feeds into B
  3. run_waves()     — Wave-based: Wave 1 runs, results feed Wave 2, etc.

Features:
  - Error isolation: one agent failing does not kill others
  - Live status tracking via event callbacks
  - Result aggregation into coherent merged output
  - Blocking agent gates: if Kavach/Varuna/Mitra/Indra flag CRITICAL, halt
  - Configurable timeouts per agent and overall
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from djcode.agents.executor import AgentExecutor, AgentResult
from djcode.agents.registry import (
    BLOCKING_AGENTS,
    AgentRole,
    AgentSpec,
    AGENT_SPECS,
)
from djcode.agents.state import (
    AgentEvent,
    AgentEventType,
    AgentState,
    EventCallback,
)
from djcode.orchestrator.context_bus import ContextBus
from djcode.provider import Provider

logger = logging.getLogger(__name__)

__all__ = [
    "CoordinatorEvent",
    "CoordinatorResult",
    "ParallelCoordinator",
]


# -- Coordinator-level events --------------------------------------------------

class CoordinatorEventType(str, __import__("enum").Enum):
    """Events emitted by the coordinator itself."""

    WAVE_START     = "wave_start"
    WAVE_COMPLETE  = "wave_complete"
    AGENT_START    = "agent_start"
    AGENT_COMPLETE = "agent_complete"
    AGENT_ERROR    = "agent_error"
    HALT           = "halt"
    ALL_COMPLETE   = "all_complete"


@dataclass(frozen=True)
class CoordinatorEvent:
    """Event emitted by the ParallelCoordinator."""

    event_type: CoordinatorEventType
    timestamp: float
    data: dict[str, Any] = field(default_factory=dict)


CoordinatorCallback = asyncio.coroutines.iscoroutinefunction  # type alias placeholder
# Actual callback type:
CoordCallback = EventCallback  # reuse the same signature


# -- Result aggregation --------------------------------------------------------

@dataclass
class CoordinatorResult:
    """Aggregated result from a coordinator run."""

    results: list[AgentResult]
    halted: bool = False
    halt_reason: str = ""
    total_duration_s: float = 0.0
    total_tokens: int = 0
    total_tools: int = 0

    @property
    def succeeded(self) -> list[AgentResult]:
        return [r for r in self.results if r.succeeded]

    @property
    def failed(self) -> list[AgentResult]:
        return [r for r in self.results if not r.succeeded]

    @property
    def all_succeeded(self) -> bool:
        return all(r.succeeded for r in self.results) and not self.halted

    @property
    def has_blocking_critical(self) -> bool:
        return any(r.is_blocking_critical for r in self.results)

    def merged_response(self) -> str:
        """Merge all agent responses into a coherent combined output."""
        if not self.results:
            return ""

        if len(self.results) == 1:
            return self.results[0].response

        parts: list[str] = []
        for result in self.results:
            if result.succeeded and result.response.strip():
                header = f"## {result.agent_name} ({result.agent_role.value})"
                confidence = f"*Confidence: {result.confidence_score:.2f}*"
                parts.append(f"{header}\n{confidence}\n\n{result.response}")

        if self.halted:
            parts.append(f"\n---\n**HALTED:** {self.halt_reason}")

        return "\n\n---\n\n".join(parts)

    def summary_table(self) -> str:
        """Generate a compact summary table of all agent results."""
        lines: list[str] = [
            f"{'Agent':<16} {'Status':<8} {'Conf':>6} {'Tokens':>8} {'Tools':>6} {'Time':>8}",
            "-" * 60,
        ]
        for r in self.results:
            status = "OK" if r.succeeded else "FAIL"
            lines.append(
                f"{r.agent_name:<16} {status:<8} {r.confidence_score:>6.2f} "
                f"{r.tokens_used:>8} {r.tools_called:>6} {r.duration_s:>7.1f}s"
            )
        lines.append("-" * 60)
        lines.append(
            f"{'TOTAL':<16} {'HALT' if self.halted else 'OK':<8} "
            f"{'':>6} {self.total_tokens:>8} {self.total_tools:>6} "
            f"{self.total_duration_s:>7.1f}s"
        )
        return "\n".join(lines)


# -- Parallel Coordinator ------------------------------------------------------

class ParallelCoordinator:
    """Coordinates parallel and sequential execution of multiple PhD agents.

    Usage:
        coord = ParallelCoordinator(provider, bus)

        # Independent agents
        result = await coord.run_parallel([coder_spec, tester_spec], task)

        # Sequential pipeline
        result = await coord.run_pipeline([scout_spec, architect_spec, coder_spec], task)

        # Wave-based execution
        result = await coord.run_waves(
            [[security_spec, risk_spec], [coder_spec, tester_spec]],
            task,
        )
    """

    def __init__(
        self,
        provider: Provider,
        bus: ContextBus | None = None,
        *,
        enable_ra: bool = True,
        per_agent_timeout_s: float = 300.0,
        overall_timeout_s: float = 600.0,
        halt_on_blocking_critical: bool = True,
    ) -> None:
        self.provider = provider
        self.bus = bus or ContextBus()
        self.enable_ra = enable_ra
        self.per_agent_timeout_s = per_agent_timeout_s
        self.overall_timeout_s = overall_timeout_s
        self.halt_on_blocking_critical = halt_on_blocking_critical
        self._callbacks: list[CoordCallback] = []
        self._executors: dict[AgentRole, AgentExecutor] = {}

    # -- Event registration ----------------------------------------------------

    def on_event(self, callback: CoordCallback) -> None:
        """Register a callback for coordinator-level events."""
        self._callbacks.append(callback)

    def on_agent_event(self, callback: EventCallback) -> None:
        """Register a callback that receives ALL individual agent events."""
        self._agent_callback = callback

    async def _emit(self, event_type: CoordinatorEventType, **data: Any) -> None:
        """Emit a coordinator event to all registered callbacks."""
        event = AgentEvent(
            event_type=AgentEventType.STATE_CHANGE,
            agent_role=AgentRole.ORCHESTRATOR,
            agent_name="coordinator",
            timestamp=time.time(),
            data={"coordinator_event": event_type.value, **data},
        )
        for cb in self._callbacks:
            try:
                await cb(event)
            except Exception:
                logger.exception("Coordinator callback error")

    # -- Executor factory ------------------------------------------------------

    def _make_executor(self, spec: AgentSpec) -> AgentExecutor:
        """Create an AgentExecutor for a spec, attaching the shared agent event callback."""
        executor = AgentExecutor(
            spec=spec,
            provider=self.provider,
            bus=self.bus,
            enable_ra=self.enable_ra,
            ra_timeout_s=15.0,
            execution_timeout_s=self.per_agent_timeout_s,
        )
        # Attach global agent event callback if registered
        if hasattr(self, "_agent_callback"):
            executor.on_event(self._agent_callback)
        self._executors[spec.role] = executor
        return executor

    # -- Parallel execution (independent agents) --------------------------------

    async def run_parallel(
        self,
        specs: list[AgentSpec],
        task: str,
    ) -> CoordinatorResult:
        """Run all agents concurrently. Error in one does not affect others.

        If halt_on_blocking_critical is True and a blocking agent (Kavach, Varuna,
        Mitra, Indra) reports CRITICAL findings, remaining agents are cancelled.
        """
        start = time.monotonic()
        await self._emit(CoordinatorEventType.WAVE_START, agents=[s.name for s in specs])

        # Create tasks for each agent
        agent_tasks: dict[AgentRole, asyncio.Task[AgentResult]] = {}
        for spec in specs:
            executor = self._make_executor(spec)
            coro = self._run_single_with_timeout(executor, task, spec)
            agent_tasks[spec.role] = asyncio.create_task(coro, name=f"agent-{spec.name}")

        results: list[AgentResult] = []
        halted = False
        halt_reason = ""

        # Wait for all tasks, checking for blocking criticals
        done: set[asyncio.Task[AgentResult]] = set()
        pending = set(agent_tasks.values())

        while pending:
            newly_done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED,
            )
            for completed_task in newly_done:
                done.add(completed_task)
                try:
                    result = completed_task.result()
                except Exception as e:
                    # Task-level exception (shouldn't happen, executor catches internally)
                    role = self._role_for_task(completed_task, agent_tasks)
                    result = AgentResult(
                        agent_role=role,
                        agent_name=AGENT_SPECS[role].name if role in AGENT_SPECS else "unknown",
                        response="",
                        confidence_score=0.0,
                        tokens_used=0,
                        tools_called=0,
                        duration_s=0.0,
                        ra_briefing=None,
                        state=AgentState.ERROR,
                        error=f"{type(e).__name__}: {e}",
                    )

                results.append(result)
                await self._emit(
                    CoordinatorEventType.AGENT_COMPLETE,
                    agent=result.agent_name,
                    succeeded=result.succeeded,
                )

                # Check blocking critical gate
                if (
                    self.halt_on_blocking_critical
                    and result.is_blocking_critical
                    and pending
                ):
                    halt_reason = (
                        f"Blocking agent {result.agent_name} flagged CRITICAL findings. "
                        f"Cancelling {len(pending)} remaining agents."
                    )
                    logger.warning(halt_reason)
                    halted = True
                    for p in pending:
                        p.cancel()
                    # Collect cancelled tasks
                    for p in pending:
                        try:
                            await p
                        except (asyncio.CancelledError, Exception):
                            pass
                    pending = set()
                    await self._emit(CoordinatorEventType.HALT, reason=halt_reason)
                    break

        total_duration = time.monotonic() - start
        await self._emit(CoordinatorEventType.ALL_COMPLETE, duration_s=total_duration)

        return CoordinatorResult(
            results=results,
            halted=halted,
            halt_reason=halt_reason,
            total_duration_s=round(total_duration, 3),
            total_tokens=sum(r.tokens_used for r in results),
            total_tools=sum(r.tools_called for r in results),
        )

    # -- Sequential pipeline ---------------------------------------------------

    async def run_pipeline(
        self,
        specs: list[AgentSpec],
        task: str,
    ) -> CoordinatorResult:
        """Run agents sequentially. Output of agent A is fed into agent B's context.

        Each agent gets the original task PLUS all prior agent outputs from the
        ContextBus. If a blocking agent flags CRITICAL, the pipeline halts.
        """
        start = time.monotonic()
        results: list[AgentResult] = []
        halted = False
        halt_reason = ""

        for i, spec in enumerate(specs):
            await self._emit(
                CoordinatorEventType.AGENT_START,
                agent=spec.name,
                position=i + 1,
                total=len(specs),
            )

            executor = self._make_executor(spec)

            # Build enriched task with prior agent context
            enriched_task = task
            if self.bus and len(self.bus) > 0:
                enriched_task = (
                    f"{task}\n\n"
                    f"## Context from prior agents in pipeline\n"
                    f"{self.bus.summary()}"
                )

            result = await self._run_single_with_timeout(executor, enriched_task, spec)
            results.append(result)

            await self._emit(
                CoordinatorEventType.AGENT_COMPLETE,
                agent=result.agent_name,
                succeeded=result.succeeded,
                position=i + 1,
            )

            # Check blocking critical gate
            if self.halt_on_blocking_critical and result.is_blocking_critical:
                halt_reason = (
                    f"Pipeline halted at stage {i + 1}/{len(specs)}: "
                    f"{result.agent_name} flagged CRITICAL findings."
                )
                logger.warning(halt_reason)
                halted = True
                await self._emit(CoordinatorEventType.HALT, reason=halt_reason)
                break

            # If agent failed entirely, log but continue pipeline
            if not result.succeeded:
                logger.warning(
                    "Pipeline agent %s failed: %s (continuing pipeline)",
                    spec.name,
                    result.error,
                )

        total_duration = time.monotonic() - start
        await self._emit(CoordinatorEventType.ALL_COMPLETE, duration_s=total_duration)

        return CoordinatorResult(
            results=results,
            halted=halted,
            halt_reason=halt_reason,
            total_duration_s=round(total_duration, 3),
            total_tokens=sum(r.tokens_used for r in results),
            total_tools=sum(r.tools_called for r in results),
        )

    # -- Wave-based execution --------------------------------------------------

    async def run_waves(
        self,
        waves: list[list[AgentSpec]],
        task: str,
    ) -> CoordinatorResult:
        """Execute agents in waves. Wave N runs in parallel, then Wave N+1 gets results.

        This is the most powerful execution pattern:
          Wave 1: [Security, Risk]     — run in parallel
          Wave 2: [Coder, Tester]      — run in parallel, with Wave 1 results
          Wave 3: [Reviewer]           — runs with all prior results

        If a blocking agent in any wave flags CRITICAL, subsequent waves are skipped.
        """
        start = time.monotonic()
        all_results: list[AgentResult] = []
        halted = False
        halt_reason = ""

        for wave_idx, wave_specs in enumerate(waves):
            wave_num = wave_idx + 1
            agent_names = [s.name for s in wave_specs]
            logger.info("Wave %d: starting %s", wave_num, agent_names)
            await self._emit(
                CoordinatorEventType.WAVE_START,
                wave=wave_num,
                agents=agent_names,
            )

            # Run the wave (all agents in parallel)
            wave_result = await self.run_parallel(wave_specs, task)
            all_results.extend(wave_result.results)

            await self._emit(
                CoordinatorEventType.WAVE_COMPLETE,
                wave=wave_num,
                succeeded=wave_result.all_succeeded,
            )

            # Check for halt
            if wave_result.halted:
                halt_reason = (
                    f"Wave {wave_num} halted: {wave_result.halt_reason}. "
                    f"Skipping {len(waves) - wave_num} remaining waves."
                )
                halted = True
                await self._emit(CoordinatorEventType.HALT, reason=halt_reason)
                break

            # Check for blocking critical in completed wave
            if self.halt_on_blocking_critical and wave_result.has_blocking_critical:
                critical_agents = [
                    r.agent_name for r in wave_result.results if r.is_blocking_critical
                ]
                halt_reason = (
                    f"Wave {wave_num} blocking agents {critical_agents} flagged CRITICAL. "
                    f"Skipping remaining waves."
                )
                halted = True
                await self._emit(CoordinatorEventType.HALT, reason=halt_reason)
                break

        total_duration = time.monotonic() - start
        await self._emit(CoordinatorEventType.ALL_COMPLETE, duration_s=total_duration)

        return CoordinatorResult(
            results=all_results,
            halted=halted,
            halt_reason=halt_reason,
            total_duration_s=round(total_duration, 3),
            total_tokens=sum(r.tokens_used for r in all_results),
            total_tools=sum(r.tools_called for r in all_results),
        )

    # -- Streaming execution ---------------------------------------------------

    async def run_parallel_streaming(
        self,
        specs: list[AgentSpec],
        task: str,
    ) -> AsyncIterator[AgentEvent]:
        """Run agents in parallel with live event streaming.

        Yields AgentEvent from ALL agents interleaved as they occur.
        Useful for the TUI dashboard.
        """
        event_queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
        executors: list[AgentExecutor] = []

        for spec in specs:
            executor = self._make_executor(spec)
            executors.append(executor)

        async def _run_and_enqueue(executor: AgentExecutor, task: str) -> None:
            try:
                async for event in executor.execute_streaming(task):
                    await event_queue.put(event)
            except Exception as e:
                await event_queue.put(AgentEvent(
                    event_type=AgentEventType.ERROR,
                    agent_role=executor.spec.role,
                    agent_name=executor.spec.name,
                    timestamp=time.time(),
                    data={"error": str(e)},
                ))
            finally:
                await event_queue.put(None)  # sentinel for this agent

        # Start all agent tasks
        tasks = [
            asyncio.create_task(_run_and_enqueue(ex, task))
            for ex in executors
        ]

        # Yield events as they arrive until all agents complete
        completed_count = 0
        while completed_count < len(executors):
            event = await event_queue.get()
            if event is None:
                completed_count += 1
            else:
                yield event

        # Ensure all tasks are cleaned up
        for t in tasks:
            if not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    # -- Internal helpers -------------------------------------------------------

    async def _run_single_with_timeout(
        self,
        executor: AgentExecutor,
        task: str,
        spec: AgentSpec,
    ) -> AgentResult:
        """Run a single agent executor with a timeout wrapper."""
        try:
            return await asyncio.wait_for(
                executor.execute(task),
                timeout=self.per_agent_timeout_s,
            )
        except asyncio.TimeoutError:
            error_msg = f"Agent {spec.name} timed out after {self.per_agent_timeout_s}s"
            logger.error(error_msg)
            return AgentResult(
                agent_role=spec.role,
                agent_name=spec.name,
                response="",
                confidence_score=0.0,
                tokens_used=0,
                tools_called=0,
                duration_s=self.per_agent_timeout_s,
                ra_briefing=None,
                state=AgentState.ERROR,
                error=error_msg,
            )
        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"
            logger.exception("Agent %s execution failed", spec.name)
            return AgentResult(
                agent_role=spec.role,
                agent_name=spec.name,
                response="",
                confidence_score=0.0,
                tokens_used=0,
                tools_called=0,
                duration_s=0.0,
                ra_briefing=None,
                state=AgentState.ERROR,
                error=error_msg,
            )

    @staticmethod
    def _role_for_task(
        task: asyncio.Task[AgentResult],
        task_map: dict[AgentRole, asyncio.Task[AgentResult]],
    ) -> AgentRole:
        """Reverse-lookup the AgentRole for a completed asyncio.Task."""
        for role, t in task_map.items():
            if t is task:
                return role
        return AgentRole.CODER  # fallback

    @property
    def active_executors(self) -> dict[AgentRole, AgentExecutor]:
        """Access the executors created during the current run."""
        return dict(self._executors)

    def status_snapshot(self) -> list[dict[str, Any]]:
        """Get a snapshot of all active executor state machines."""
        return [
            ex.state_machine.snapshot()
            for ex in self._executors.values()
        ]
