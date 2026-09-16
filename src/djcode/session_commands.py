"""Shared terminal commands for session and capability controls."""

from __future__ import annotations

import json
from pathlib import Path

from djcode.capabilities import capability_context
from djcode.config import load_config, save_config

NAMES = {
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
        if command == "/fork":
            # A fork branches; it does not terminate the parent. The copy and
            # the lineage columns are one atomic SessionDB call now, and the
            # parent keeps its end_time NULL.
            if db and current:
                db.append_messages(current, operator.messages)
                forked = db.fork_session(current)
                if forked:
                    operator.session_id = forked
            return f"Forked session: {getattr(operator, 'session_id', None) or 'in memory'}"
        if db and current:
            db.append_messages(current, operator.messages)
            db.end_session(current)
        operator.reset()
        if db:
            operator.session_id = db.create_session(
                operator.provider.config.model, operator.provider.config.name, cwd=str(Path.cwd())
            )
            db.append_messages(operator.session_id, operator.messages)
        return f"New session: {getattr(operator, 'session_id', None) or 'in memory'}"
    if command == "/compact":
        manager = operator.context_manager
        db = getattr(operator, "session_db", None)
        current = getattr(operator, "session_id", None)
        # Flush the tail BEFORE compressing: the entries about to leave the
        # model's view have to be on disk for the compaction marker to point at
        # a real row, and this is the only chance to persist them.
        if db and current:
            db.append_messages(current, operator.messages)
        manager.replace_messages(operator.messages)
        result = await manager.auto_compress()
        operator.messages = manager.get_messages()
        # /compact must NOT fall through to the generic on_checkpoint hook: that
        # hook appends, and appending the compacted view on top of the full
        # history would show the model the summary and the originals both.
        if db and current:
            db.record_compaction(
                current,
                summary=getattr(result, "summary_text", "") or "",
                kept_messages=operator.messages,
                strategy=getattr(getattr(result, "strategy_used", None), "value", ""),
                messages_removed=getattr(result, "messages_removed", 0),
            )
        return "Context compacted; the full transcript is preserved in the session log"
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
        # str(): this returns into app.py's chat pane, which renders text.
        return str(await operator.workflow.one(name, args, dispatch_tool))
