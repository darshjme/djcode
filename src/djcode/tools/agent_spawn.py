"""Agent spawning tool — spawn sub-agents for specialized tasks.

Allows the AI to create and run specialist agents (debugger, tester,
reviewer, etc.) from the DJcode agent registry. Sub-agents run in
isolation with their own context and tool access policies.

Supports foreground (blocking) and background (async) execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Track background agents
_background_tasks: dict[str, dict[str, Any]] = {}


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

    # Validate role exists in registry
    try:
        from djcode.agents.registry import AgentRole, AGENT_SPECS

        role_enum = _resolve_role(role)
        if role_enum is None:
            available = [r.value for r in AgentRole]
            return (
                f"Error: Unknown role '{role}'. Available roles:\n"
                + "\n".join(f"  - {r}" for r in sorted(available))
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
    try:
        from djcode.provider import Provider, ProviderConfig
        from djcode.orchestrator.engine import AgentRunner
        from djcode.orchestrator.context_bus import ContextBus

        # Create a fresh provider instance for the sub-agent
        config = ProviderConfig.from_config()
        provider = Provider(config)

        # Override max tool rounds if specified
        if max_rounds is not None:
            # Create a modified spec (AgentSpec is frozen, so we rebuild it)
            from djcode.agents.registry import AgentSpec
            spec = AgentSpec(
                role=spec.role,
                name=spec.name,
                title=spec.title,
                system_prompt=spec.system_prompt,
                tools_allowed=spec.tools_allowed,
                tools_denied=spec.tools_denied,
                read_only=spec.read_only,
                max_tool_rounds=max_rounds,
                temperature=spec.temperature,
                priority=spec.priority,
                tier=spec.tier,
            )

        # Create an isolated context bus for this agent
        bus = ContextBus()
        bus.set_task(task, spec.role.value)

        runner = AgentRunner(provider, spec, bus, auto_accept=True)

        start = time.monotonic()
        result = await runner.run(task)
        elapsed = time.monotonic() - start

        await provider.close()

        # Format the response
        header = (
            f"Agent: {spec.name} ({spec.title})\n"
            f"Role: {spec.role.value} | Read-only: {spec.read_only} | "
            f"Tools: {len(spec.tools_allowed)} | Time: {elapsed:.1f}s\n"
            f"Task: {task[:100]}{'...' if len(task) > 100 else ''}\n"
            f"{'=' * 60}\n"
        )

        return header + result

    except ConnectionError as e:
        return f"Error: Agent '{spec.name}' failed — {e}"
    except Exception as e:
        logger.error("Agent spawn failed: %s", e, exc_info=True)
        return f"Error spawning agent '{spec.name}': {e}"


async def _spawn_background(spec: Any, task: str, max_rounds: int | None) -> str:
    """Spawn an agent in the background and return a tracking ID."""
    agent_id = f"agent_{spec.role.value}_{int(time.time() * 1000) % 1_000_000}"

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
            _background_tasks[agent_id]["status"] = "completed"
            _background_tasks[agent_id]["result"] = result
        except Exception as e:
            _background_tasks[agent_id]["status"] = "failed"
            _background_tasks[agent_id]["error"] = str(e)
        finally:
            elapsed = time.monotonic() - _background_tasks[agent_id]["started_at"]
            _background_tasks[agent_id]["elapsed"] = elapsed

    # Fire and forget
    asyncio.create_task(_run())

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
            result = info["result"]
            if len(result) > 5000:
                result = result[:5000] + "\n... (truncated, full result available)"
            lines.append(f"\nResult:\n{result}")
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
