"""Agent spawning tool — spawn sub-agents for specialized tasks.

Allows the AI to create and run specialist agents (debugger, tester,
reviewer, etc.) from the DJcode agent registry. Sub-agents run in
isolation with their own context and tool access policies.

Supports foreground (blocking) and background (async) execution.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from typing import Any

logger = logging.getLogger(__name__)

# Track background agents
_background_tasks: dict[str, dict[str, Any]] = {}


_parent_context: ContextVar[tuple[Any, bool, Any] | None] = ContextVar("agent_parent", default=None)
_spawn_depth: ContextVar[int] = ContextVar("agent_depth", default=0)

# Name of the subagent whose tool call is currently awaiting approval, so a UI
# that cannot take an extra argument can still title the prompt
# "Approve file_write - via Prometheus" (DESIGN-CLI.md 5.3).
_approval_agent: ContextVar[str | None] = ContextVar("agent_approval_agent", default=None)


def current_approval_agent() -> str | None:
    """The subagent this approval request belongs to, or None for the operator.

    Read this from an approval callback to label the prompt. It is only set
    while a child agent's request is being presented.
    """
    return _approval_agent.get()


@contextmanager
def agent_context(provider: Any, auto_accept: bool = False, approval_callback=None):
    """Publish the caller's provider and consent state to a spawned agent.

    The tuple is what a child *sees*, not what a child *gets*: SSOT P1-12 says
    consent does not descend, so `_spawn_foreground` re-derives the child's
    approval path from this tuple through `_child_approval`. `auto_accept` is
    kept in the tuple because the executor re-publishes its own state through
    this same context manager for its own nested dispatches.
    """
    token = _parent_context.set((provider, auto_accept, approval_callback))
    try:
        yield
    finally:
        _parent_context.reset(token)


def _child_approval(agent_name: str, auto_accept: bool, approval_callback):
    """Build the approval callback a spawned agent runs under.

    SSOT P1-12 / GAP B10: a subagent must not inherit the parent's
    auto-accept. The parent's callback is wrapped so every non-read tool call
    the child makes is presented, carrying the agent's name, no matter what
    mode the parent is in.

    The one case the blueprint does not cover is a parent with auto-accept and
    *no* callback at all -- headless `djcode --wave ... --yes`
    (`cli.py:349` builds an Orchestrator with `auto_accept` and no callback).
    There is no human on the other end of that process to present anything to,
    and the operator granted the whole run on the command line, so the child
    runs under an explicit standing grant that logs each tool it covers,
    rather than being silently denied every write.
    """
    if approval_callback is None:
        if not auto_accept:
            return None  # The executor denies and says why; nothing to wrap.

        async def standing_grant(name: str, arguments: dict) -> bool:
            logger.info(
                "Subagent %s: %s auto-approved by the session's standing grant "
                "(no approval channel is attached to this process)",
                agent_name,
                name,
            )
            return True

        return standing_grant

    passes_agent_name = _accepts_agent_name(approval_callback)

    async def presented(name: str, arguments: dict):
        # Wrappers nest (a subagent may spawn its own), and the outermost call
        # is the one closest to the tool. Whoever claims the name first is the
        # agent that actually asked; inner wrappers relay it unchanged.
        token = _approval_agent.set(agent_name) if _approval_agent.get() is None else None
        try:
            requester = _approval_agent.get() or agent_name
            if passes_agent_name:
                decision = approval_callback(name, arguments, agent_name=requester)
            else:
                decision = approval_callback(name, arguments)
            if inspect.isawaitable(decision):
                decision = await decision
            # Hand the front-end's answer back UNCHANGED. `bool(decision)`
            # collapsed a `Decision(DENY, comment="use uv, not pip")` to False,
            # so deny-with-a-note worked for the main agent and was silently
            # dropped for every subagent -- the model got the generic "User
            # denied tool execution" and had no idea what it was meant to do
            # instead. `Decision.__bool__` is fail-closed, so every caller that
            # still writes `if not await callback(...)` keeps its meaning.
            return decision
        finally:
            if token is not None:
                _approval_agent.reset(token)

    return presented


def _accepts_agent_name(callback) -> bool:
    """True when `callback` can take the agent name as a keyword argument."""
    try:
        parameters = inspect.signature(callback).parameters
    except (TypeError, ValueError):
        return False
    if "agent_name" in parameters:
        return parameters["agent_name"].kind is not inspect.Parameter.POSITIONAL_ONLY
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())


async def cancel_background_agents() -> None:
    tasks = [info.get("async_task") for info in _background_tasks.values()]
    tasks = [task for task in tasks if task and not task.done()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def execute_spawn_agent(
    role: str,
    task: str,
    background: bool = False,
    max_tool_rounds: int | None = None,
) -> str:
    """Spawn a specialist agent to handle a specific task.

    Args:
        role: Agent role to spawn. Available roles:
            - coder: Full-stack engineering (write code, fix bugs, build features)
            - debugger: Root cause analysis and bug fixing
            - tester: Write and run tests
            - reviewer: Code review (read-only)
            - architect: System design and planning (read-only)
            - scout: Codebase reconnaissance and exploration (read-only)
            - refactorer: Code cleanup and restructuring
            - devops: Docker, CI/CD, deployment
            - docs: Documentation writing
            - security_compliance: Security audit and compliance checking
            - data_scientist: Data analysis and ML
            - sre: Site reliability and monitoring
        task: Description of what the agent should do.
        background: If True, run agent in background and return immediately
            with a tracking ID. Use task_list to check status.
        max_tool_rounds: Override max tool execution rounds (default: agent's setting).

    Returns:
        Agent's response (foreground) or tracking ID (background).
    """
    if not role or not role.strip():
        return "Error: 'role' is required"
    if not task or not task.strip():
        return "Error: 'task' is required"

    role = role.lower().strip()
    if role == "status":
        return await execute_agent_status(task)
    if _spawn_depth.get() >= 3:
        return "Error: Subagent nesting limit reached (3)"
    if max_tool_rounds is not None and (
        type(max_tool_rounds) is not int or not 1 <= max_tool_rounds <= 100
    ):
        return "Error: max_tool_rounds must be an integer between 1 and 100"
    if background and sum(i["status"] == "running" for i in _background_tasks.values()) >= 8:
        return "Error: Background agent limit reached (8)"

    # Validate role exists in registry
    try:
        from djcode.agents.registry import AGENT_SPECS, AgentRole

        role_enum = _resolve_role(role)
        if role_enum is None:
            available = [r.value for r in AgentRole]
            return f"Error: Unknown role '{role}'. Available roles:\n" + "\n".join(
                f"  - {r}" for r in sorted(available)
            )

        spec = AGENT_SPECS.get(role_enum)
        if spec is None:
            return f"Error: No agent spec found for role '{role}'"

    except ImportError:
        return "Error: Agent registry not available. Check djcode.agents.registry module."

    if background:
        return await _spawn_background(spec, task, max_tool_rounds)
    else:
        return await _spawn_foreground(spec, task, max_tool_rounds)


async def _spawn_foreground(spec: Any, task: str, max_rounds: int | None) -> str:
    """Run an agent in the foreground and return its full response."""
    provider = None
    owns_provider = False
    depth_token = _spawn_depth.set(_spawn_depth.get() + 1)
    try:
        from djcode.orchestrator.context_bus import ContextBus
        from djcode.orchestrator.engine import AgentRunner
        from djcode.provider import Provider, ProviderConfig

        parent = _parent_context.get()
        if parent is None:
            provider = Provider(ProviderConfig.from_config())
            parent_auto_accept = False
            parent_callback = None
            owns_provider = True
        else:
            provider, parent_auto_accept, parent_callback = parent
        # SSOT P1-12: the child never inherits `auto_accept`. Every non-read
        # tool it calls goes through `_child_approval`, which presents the
        # request under this agent's name.
        approval_callback = _child_approval(spec.name, parent_auto_accept, parent_callback)
        if max_rounds is not None:
            spec = replace(spec, max_tool_rounds=max_rounds)
        bus = ContextBus()
        bus.set_task(task, spec.role.value)
        runner = AgentRunner(
            provider, spec, bus, auto_accept=False, approval_callback=approval_callback
        )
        start = time.monotonic()
        result = await runner.run(task)
        if result.lstrip().startswith("Error:") or "\nError: Agent " in result:
            return result.strip()
        return (
            f"Agent: {spec.name} ({spec.title}) | Time: {time.monotonic() - start:.1f}s\n" + result
        )
    except Exception as e:
        logger.error("Agent spawn failed: %s", e, exc_info=True)
        return f"Error spawning agent '{spec.name}': {e}"
    finally:
        _spawn_depth.reset(depth_token)
        if owns_provider and provider is not None:
            await provider.close()


async def _spawn_background(spec: Any, task: str, max_rounds: int | None) -> str:
    """Spawn an agent in the background and return a tracking ID."""
    agent_id = f"agent_{spec.role.value}_{uuid.uuid4().hex[:12]}"

    _background_tasks[agent_id] = {
        "id": agent_id,
        "role": spec.role.value,
        "agent_name": spec.name,
        "task": task[:200],
        "status": "running",
        "started_at": time.monotonic(),
        "result": None,
        "error": None,
    }

    async def _run() -> None:
        """Run the agent and store results."""
        try:
            result = await _spawn_foreground(spec, task, max_rounds)
            # W3 interim sniff: _spawn_foreground returns a plain str (it is the
            # agent's assembled response, not a dispatch result), so there is no
            # ToolOutcome.ok to consult here. Converting this to a structured
            # flag means giving _spawn_foreground an outcome of its own; until
            # then an honest prefix check beats a fabricated ok.
            _background_tasks[agent_id]["status"] = (
                "failed" if result.startswith("Error") else "completed"
            )
            if result.startswith("Error"):
                _background_tasks[agent_id]["error"] = result
            _background_tasks[agent_id]["result"] = result
        except asyncio.CancelledError:
            _background_tasks[agent_id]["status"] = "cancelled"
            raise
        except Exception as e:
            _background_tasks[agent_id]["status"] = "failed"
            _background_tasks[agent_id]["error"] = str(e)
        finally:
            elapsed = time.monotonic() - _background_tasks[agent_id]["started_at"]
            _background_tasks[agent_id]["elapsed"] = elapsed

    # Fire and forget
    _background_tasks[agent_id]["async_task"] = asyncio.create_task(_run())

    return (
        f"Agent spawned in background:\n"
        f"  ID: {agent_id}\n"
        f"  Agent: {spec.name} ({spec.title})\n"
        f"  Task: {task[:100]}{'...' if len(task) > 100 else ''}\n"
        f"\nUse spawn_agent with role='status' and task='{agent_id}' to check progress."
    )


async def execute_agent_status(agent_id: str = "") -> str:
    """Check status of background agents.

    Args:
        agent_id: Specific agent ID to check, or empty for all.

    Returns:
        Status information for background agent(s).
    """
    if not _background_tasks:
        return "No background agents running or completed."

    if agent_id and agent_id.strip():
        agent_id = agent_id.strip()
        info = _background_tasks.get(agent_id)
        if not info:
            available = list(_background_tasks.keys())
            return (
                f"Error: Agent '{agent_id}' not found.\n"
                f"Active agents: {', '.join(available) if available else 'none'}"
            )

        lines = [
            f"Agent: {info['agent_name']}",
            f"ID: {info['id']}",
            f"Role: {info['role']}",
            f"Status: {info['status']}",
            f"Task: {info['task']}",
        ]

        if "elapsed" in info:
            lines.append(f"Duration: {info['elapsed']:.1f}s")

        if info["status"] == "completed" and info["result"]:
            # W3-2: the 5 000-char head-only cut that used to be here is gone.
            # `agent_status` goes through dispatch_tool like every other tool,
            # so a long agent result is now bounded head+tail and spilled to a
            # file the model can read back -- which is what the removed comment
            # here promised W3 would do. The full text was never in danger of
            # being lost to memory: _background_tasks holds it either way.
            lines.append(f"\nResult:\n{info['result']}")
        elif info["status"] == "failed" and info["error"]:
            lines.append(f"\nError: {info['error']}")

        return "\n".join(lines)

    # List all background agents
    lines = [f"Background agents: {len(_background_tasks)}", ""]

    status_icons = {
        "running": "[~]",
        "completed": "[x]",
        "failed": "[!]",
    }

    for aid, info in _background_tasks.items():
        icon = status_icons.get(info["status"], "[ ]")
        elapsed = info.get("elapsed", time.monotonic() - info["started_at"])
        lines.append(
            f"  {icon} {aid}: {info['agent_name']} ({info['status']}, {elapsed:.1f}s) "
            f"— {info['task'][:60]}"
        )

    return "\n".join(lines)


def _resolve_role(role_str: str) -> Any | None:
    """Resolve a role string to an AgentRole enum member."""
    from djcode.agents.registry import AgentRole

    # Direct match
    for member in AgentRole:
        if member.value == role_str:
            return member

    # Common aliases
    aliases: dict[str, str] = {
        "code": "coder",
        "debug": "debugger",
        "test": "tester",
        "review": "reviewer",
        "arch": "architect",
        "refactor": "refactorer",
        "doc": "docs",
        "security": "security_compliance",
        "compliance": "security_compliance",
        "data": "data_scientist",
        "ml": "data_scientist",
        "ops": "devops",
        "infra": "devops",
        "reliability": "sre",
        "product": "product_strategist",
        "strategy": "product_strategist",
        "ux": "ux_workflow",
        "legal": "legal_intelligence",
        "risk": "risk_engine",
        "integrate": "integration",
    }

    resolved = aliases.get(role_str)
    if resolved:
        for member in AgentRole:
            if member.value == resolved:
                return member

    return None
