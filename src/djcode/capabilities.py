"""A session's skills, MCP, processes, computer and dynamic DAF workflows."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from djcode.computer import Computer
from djcode.extensions import ExtensionManager
from djcode.skills import SkillManager

_current = ContextVar("capabilities", default=None)


@contextmanager
def capability_context(runtime):
    token = _current.set(runtime)
    try:
        yield
    finally:
        _current.reset(token)


class Capabilities:
    def __init__(self, operator):
        self.operator = operator
        self.extensions = ExtensionManager()
        self.skills = SkillManager()
        self.computer = Computer()
        self.jobs = {}

    async def process(self, action, command="", job_id="", delay=0):
        if action == "list":
            return json.dumps([self._job_info(ident) for ident in self.jobs])
        if action in {"start", "schedule"}:
            if not command.strip() or not 0 <= delay <= 86400:
                raise ValueError("A command and delay of 0..86400 seconds are required")
            if sum(not j["task"].done() for j in self.jobs.values()) >= 8:
                raise ValueError("Eight active jobs maximum")
            ident = uuid.uuid4().hex[:8]
            job = {"command": command, "output": "", "process": None, "state": "scheduled"}

            async def run():
                try:
                    await asyncio.sleep(delay)
                    proc = await asyncio.create_subprocess_shell(
                        command,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        start_new_session=os.name != "nt",
                    )
                    job.update(process=proc, state="running")
                    while data := await proc.stdout.read(4096):
                        job["output"] = (job["output"] + data.decode(errors="replace"))[-32000:]
                    await proc.wait()
                    job.update(
                        state="completed" if proc.returncode == 0 else "failed",
                        exit_code=proc.returncode,
                    )
                except asyncio.CancelledError:
                    job["state"] = "cancelled"
                    raise
                except Exception as error:
                    job.update(state="failed", output=str(error))
                finally:
                    proc = job.get("process")
                    if proc and (os.name != "nt" or proc.returncode is None):
                        if os.name != "nt":
                            try:
                                os.killpg(proc.pid, signal.SIGTERM)
                            except ProcessLookupError:
                                pass
                        else:
                            proc.terminate()
                        try:
                            await asyncio.wait_for(proc.wait(), 2)
                        except TimeoutError:
                            if os.name != "nt":
                                os.killpg(proc.pid, signal.SIGKILL)
                            else:
                                proc.kill()
                            await proc.wait()
                        if os.name != "nt":
                            try:
                                os.killpg(proc.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass

            job["task"] = asyncio.create_task(run())
            self.jobs[ident] = job
            return json.dumps(self._job_info(ident))
        if job_id not in self.jobs:
            raise ValueError("Unknown job ID")
        if action == "stop":
            self.jobs[job_id]["task"].cancel()
            await asyncio.gather(self.jobs[job_id]["task"], return_exceptions=True)
        elif action != "read":
            raise ValueError("Unknown process action")
        return json.dumps(self._job_info(job_id))

    def _job_info(self, ident):
        return {
            "id": ident,
            **{k: v for k, v in self.jobs[ident].items() if k not in {"task", "process"}},
        }

    async def skill(self, action="list", name=""):
        self.skills._invalidate_cache()
        if action == "list":
            return json.dumps(
                [{"name": s.name, "description": s.description} for s in self.skills.list_skills()]
            )
        if action == "load":
            skill = self.skills.get_skill(name)
            if not skill:
                raise ValueError("Unknown skill")
            return f"Skill {skill.name}\n{skill.instructions}\n{skill.example}"
        raise ValueError("Use skill list/load")

    async def mcp(self, action="list", extension="", tool="", arguments=None):
        if action == "list":
            return json.dumps(self.extensions.get_status())
        if action == "tools":
            conn = await self.extensions._ensure_connection(extension)
            return json.dumps(await conn.list_tools())
        if action == "call":
            result = await self.extensions.call_tool(extension, tool, arguments or {})
            conn = self.extensions._connections.get(extension)
            if conn:
                self.computer.images.extend(conn.image_paths)
                conn.image_paths.clear()
            return result
        raise ValueError("Use mcp list/tools/call")

    async def workflow(self, nodes, concurrency=1):
        from djcode.tools import dispatch_tool
        from djcode.workflow import WorkflowEngine

        if any(node.get("name") == "workflow" for node in nodes):
            raise ValueError("Nested workflows are not supported")

        async def approved(name, arguments):
            if not await self.operator._approve_tool(name, arguments):
                return "Error: User denied tool execution"
            return await dispatch_tool(name, arguments)

        engine = WorkflowEngine(self.operator.workflow.event_callback, mode="daf")
        return json.dumps(await engine.execute(nodes, approved, concurrency))

    async def close(self):
        for job in self.jobs.values():
            job["task"].cancel()
        await asyncio.gather(*(j["task"] for j in self.jobs.values()), return_exceptions=True)
        await self.computer.close()
        await self.extensions.shutdown()


async def dispatch_capability(name, **arguments):
    runtime = _current.get()
    if runtime is None:
        raise RuntimeError("This tool requires an active DJcode session")
    handlers = {
        "process": runtime.process,
        "skill": runtime.skill,
        "mcp": runtime.mcp,
        "workflow": runtime.workflow,
        "browser": runtime.computer.browser_action,
        "computer": runtime.computer.desktop_action,
    }
    return await handlers[name](**arguments)


def tool(name, description, properties, required=()):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required)},
        },
    }


def string(description=""):
    return {"type": "string", "description": description}


CAPABILITY_TOOLS = [
    tool(
        "schedule",
        (
            "Create/list/cancel durable shell command schedules. Requires a "
            "separately launched djcode --scheduler worker on the workspace "
            "host. Creation authorizes future execution of the exact command. "
            "Failed or interrupted runs are not retried automatically."
        ),
        {
            "action": {"type": "string", "enum": ["create", "list", "cancel"]},
            "command": string(),
            "delay": {"type": "number"},
            "interval": {"type": "number"},
            "cwd": string(),
            "schedule_id": string(),
        },
        ["action"],
    ),
    tool(
        "skill",
        (
            "Discover and load user skills from SKILL.md files. Load "
            "instructions before applying a skill."
        ),
        {"action": {"enum": ["list", "load"], "type": "string"}, "name": string()},
        ["action"],
    ),
    tool(
        "mcp",
        (
            "Discover configured MCP servers, list a server's tool schemas, or"
            " execute a tool (including installed messaging and application "
            "connectors)."
        ),
        {
            "action": {"enum": ["list", "tools", "call"], "type": "string"},
            "extension": string(),
            "tool": string(),
            "arguments": {"type": "object"},
        },
        ["action"],
    ),
    tool(
        "process",
        (
            "Start/read/stop background shell jobs, or schedule a delayed "
            "command while this session is running. Jobs end when DJcode "
            "exits; not persistent cron."
        ),
        {
            "action": {"enum": ["start", "schedule", "list", "read", "stop"], "type": "string"},
            "command": string(),
            "job_id": string(),
            "delay": {"type": "number", "minimum": 0, "maximum": 86400},
        },
        ["action"],
    ),
    tool(
        "browser",
        (
            "Control a session-owned Chromium browser. Open URL, inspect "
            "accessibility snapshot, act using selectors, or take a screenshot"
            " for vision. Use snapshots to choose selectors, verify actions "
            "afterward."
        ),
        {
            "action": {
                "type": "string",
                "enum": [
                    "open",
                    "tabs",
                    "snapshot",
                    "click",
                    "fill",
                    "press",
                    "navigate",
                    "back",
                    "screenshot",
                    "close",
                ],
            },
            "tab": string(),
            "url": string(),
            "selector": string(),
            "text": string(),
            "key": string(),
            "headless": {"type": "boolean"},
        },
        ["action"],
    ),
    tool(
        "computer",
        (
            "Control the desktop through screenshots, pointer and keyboard. "
            "Requires OS permissions and a vision model for visual targeting. "
            "Inspect screen before acting and verify afterward."
        ),
        {
            "action": {
                "type": "string",
                "enum": ["screenshot", "size", "click", "move", "type", "key", "scroll"],
            },
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "text": string(),
            "keys": {"type": "array", "items": {"type": "string"}},
            "amount": {"type": "integer"},
        },
        ["action"],
    ),
    tool(
        "workflow",
        (
            "Execute an explicit dependency graph of tools using DAF and DDAL."
            " Parallelize only independent work. Each node gets approval; "
            "failures block dependents, no automatic retries. Arguments are "
            "literal, not templates."
        ),
        {
            "nodes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": string(),
                        "name": string(),
                        "arguments": {"type": "object"},
                        "dependencies": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["id", "name", "arguments"],
                },
            },
            "concurrency": {"type": "integer", "minimum": 1, "maximum": 4},
        },
        ["nodes"],
    ),
]
