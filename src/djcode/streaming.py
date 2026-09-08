"""One streaming protocol adapter shared by the interactive and specialist loops."""
from __future__ import annotations

import json
from contextlib import aclosing
import uuid
from typing import Any, AsyncIterator

from djcode.provider import Message, Provider


async def stream_turn(provider: Provider, messages: list[Message]) -> AsyncIterator[tuple[str, list[dict[str, Any]]]]:
    """Yield text followed by complete, indexed tool calls; reject truncated streams."""
    calls: dict[int, dict[str, Any]] = {}
    ended = False
    async with aclosing(provider.chat(messages, stream=True)) as response:
        async for chunk in response:
            if chunk.get("error"):
                raise ConnectionError(str(chunk["error"]))
            if "message" in chunk:  # Ollama NDJSON
                msg = chunk["message"]
                if msg.get("content"):
                    yield msg["content"], []
                for tc in msg.get("tool_calls", []):
                    calls[len(calls)] = tc
                if chunk.get("done"):
                    if chunk.get("done_reason") == "length":
                        raise RuntimeError("Model output limit reached before completion.")
                    ended = True
                    continue
                continue
            choices = chunk.get("choices", [])
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta", {})
            if delta.get("content"):
                yield delta["content"], []
            for part in delta.get("tool_calls", []):
                idx = part.get("index", 0)
                tc = calls.setdefault(idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if part.get("id"):
                    tc["id"] = part["id"]
                fn = part.get("function", {})
                if fn.get("name"):
                    tc["function"]["name"] += fn["name"]
                if fn.get("arguments") is not None:
                    tc["function"]["arguments"] += fn["arguments"]
            finish = choice.get("finish_reason")
            if finish:
                if finish not in ("stop", "tool_calls"):
                    raise RuntimeError(f"Model stopped before completion: {finish}")
                ended = True
                continue
    if not ended:
        raise ConnectionError("Provider stream ended without a completion marker; retry the request.")
    normalized = []
    for tc in calls.values():
        fn = tc.get("function", {})
        if not fn.get("name"):
            raise ValueError("Provider returned a tool call without a name.")
        args = fn.get("arguments", {})
        normalized.append({"id": tc.get("id") or f"call_{uuid.uuid4().hex}", "type": "function", "function": {"name": fn["name"], "arguments": json.dumps(args) if isinstance(args, dict) else args}})
    if normalized:
        yield "", normalized
