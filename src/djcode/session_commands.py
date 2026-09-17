"""Shared terminal commands for session and capability controls."""

from __future__ import annotations

import json
from pathlib import Path

from djcode.capabilities import capability_context
from djcode.config import load_config, save_config

from djcode.studio import COMMANDS as STUDIO_COMMANDS

NAMES = STUDIO_COMMANDS | {
    "/features",
    "/project",
    "/fleet",
    "/schedule",
    "/workflow",
    "/skills",
    "/skill",
    "/jobs",
    "/browser",
    "/computer",
    "/compact",
    "/session",
    "/fork",
    "/new",
}


async def handle(operator, command, argument=""):
    if command in STUDIO_COMMANDS:
        from djcode.studio import handle as studio_handle
        return await studio_handle(operator, command, argument)
    if command == "/features":
        from djcode.workspace import feature_help
        return feature_help()
    if command == "/project":
        import asyncio
        from djcode.workspace import project_context
        return await asyncio.to_thread(project_context)
    if command == "/fleet":
        from djcode.workspace import fleet_command
        return await fleet_command(argument)
    if operator is None:
        return "Connect a provider first with /connect"
    if command == "/workflow":
        if argument in {"daf", "native"}:
            operator.workflow.mode = argument
            config = load_config()
            config["workflow_engine"] = argument
            save_config(config)
        elif argument and argument != "status":
            return "Usage: /workflow [status|daf|native]"
        return (
            f"Engine: {operator.workflow.mode}. DAF mode uses the Rust GraphExecutor "
            f"and DDAL transport. Last run: {operator.workflow.last_run or 'none'}"
        )
    if command in {"/session", "/fork", "/new"}:
        db = getattr(operator, "session_db", None)
        current = getattr(operator, "session_id", None)
        if command == "/session":
            return (
                f"Session: {current or 'not recorded'}\nWorkspace: {Path.cwd()}\n"
                f"Messages: {len(operator.messages)}"
            )
        if db and current:
            db.save_conversation(current, operator.messages)
            db.end_session(current)
        if command == "/new":
            operator.reset()
        if db:
            operator.session_id = db.create_session(
                operator.provider.config.model, operator.provider.config.name, cwd=str(Path.cwd())
            )
            db.save_conversation(operator.session_id, operator.messages)
        label = "Forked" if command == "/fork" else "New"
        return f"{label} session: {getattr(operator, 'session_id', None) or 'in memory'}"
    if command == "/compact":
        manager = operator.context_manager
        manager.replace_messages(operator.messages)
        await manager.auto_compress()
        operator.messages = manager.get_messages()
        if operator.on_checkpoint:
            operator.on_checkpoint(operator.messages)
        return "Context compacted; saved session checkpoint updated"
    if command in {"/skills", "/skill"}:
        result = await operator.capabilities.skill("load" if argument else "list", argument)
        if argument:
            from djcode.provider import Message

            operator.messages.append(
                Message(role="user", content="Apply this selected skill to the session:\n" + result)
            )
        return result
    mapping = {
        "/schedule": "schedule",
        "/jobs": "process",
        "/browser": "browser",
        "/computer": "computer",
    }
    name = mapping[command]
    if argument.startswith("{"):
        args = json.loads(argument)
    else:
        parts = argument.split(maxsplit=1)
        args = {
            "action": parts[0]
            if parts
            else (
                "list"
                if name in {"process", "schedule"}
                else "tabs"
                if name == "browser"
                else "size"
            )
        }
        if len(parts) > 1:
            args["job_id" if name == "process" else "tab"] = parts[1]
    if not isinstance(args, dict):
        raise ValueError("Command arguments must be a JSON object")
    if not await operator._approve_tool(name, args):
        return "Tool execution denied"
    from djcode.tools import dispatch_tool

    with capability_context(operator.capabilities):
        return await operator.workflow.one(name, args, dispatch_tool)
