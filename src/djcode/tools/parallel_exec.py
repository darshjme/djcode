"""Parallel tool execution — run multiple tool calls concurrently.

Uses asyncio.gather() to execute independent tool calls in parallel,
significantly reducing total execution time for multi-tool operations.

Handles timeouts, errors, and result aggregation.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any


async def execute_parallel(
    tool_calls: list[dict[str, Any]],
    timeout: int = 120,
) -> str:
    """Execute multiple tool calls concurrently and return all results.

    Args:
        tool_calls: List of tool call dicts, each with:
            - name: str — tool name (e.g., "file_read", "grep", "bash")
            - arguments: dict — arguments for the tool
            - id: str (optional) — identifier for tracking results
        timeout: Maximum seconds to wait for all tools (default 120).

    Returns:
        Formatted results from all tool calls with timing info.

    Example tool_calls:
        [
            {"name": "file_read", "arguments": {"path": "/tmp/a.py"}, "id": "read_a"},
            {"name": "grep", "arguments": {"pattern": "TODO", "path": "."}, "id": "find_todos"},
            {"name": "bash", "arguments": {"command": "wc -l src/*.py"}, "id": "line_count"}
        ]
    """
    # Import dispatch here to avoid circular imports
    from djcode.tools import dispatch_tool

    if not tool_calls:
        return "Error: No tool calls provided"

    if not isinstance(tool_calls, list):
        return "Error: tool_calls must be a list of tool call dicts"

    # Validate and normalize tool calls
    validated: list[dict[str, Any]] = []
    for i, tc in enumerate(tool_calls):
        if not isinstance(tc, dict):
            return f"Error: tool_calls[{i}] is not a dict"

        name = tc.get("name", "")
        if not name:
            return f"Error: tool_calls[{i}] missing 'name'"

        arguments = tc.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return f"Error: tool_calls[{i}] has invalid JSON arguments"

        call_id = tc.get("id", f"call_{i}")

        validated.append(
            {
                "name": name,
                "arguments": arguments,
                "id": call_id,
            }
        )

    # Execute all tool calls concurrently
    start_time = time.monotonic()

    async def _run_one(call: dict[str, Any]) -> dict[str, Any]:
        """Run a single tool call and capture result with timing."""
        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(
                dispatch_tool(call["name"], call["arguments"]),
                timeout=timeout,
            )
            elapsed = time.monotonic() - t0
            return {
                "id": call["id"],
                "name": call["name"],
                "result": result,
                "elapsed": elapsed,
                # W3-1: `result` is a ToolOutcome now. The old guard was
                # `not result.startswith("Error:") if isinstance(result, str) else True`
                # -- after W3 the isinstance arm is never taken, so `success`
                # would be unconditionally True and the "N succeeded, 0 failed"
                # header would lie about a batch in which every child failed.
                # `result.ok` is the structured verdict; the prefix check is the
                # W3 interim for the seventeen tools that still report failure
                # only as text (see tools/__init__.py on ok_source).
                "success": result.ok and not str(result).startswith("Error:"),
            }
        except TimeoutError:
            elapsed = time.monotonic() - t0
            return {
                "id": call["id"],
                "name": call["name"],
                "result": f"Error: Tool '{call['name']}' timed out after {timeout}s",
                "elapsed": elapsed,
                "success": False,
            }
        except Exception as e:
            elapsed = time.monotonic() - t0
            return {
                "id": call["id"],
                "name": call["name"],
                "result": f"Error: {e}",
                "elapsed": elapsed,
                "success": False,
            }

    # Use gather with return_exceptions=False (we handle errors in _run_one)
    tasks = [_run_one(call) for call in validated]
    results = await asyncio.gather(*tasks)

    total_time = time.monotonic() - start_time

    # Format output
    return _format_parallel_results(results, total_time)


def _format_parallel_results(
    results: list[dict[str, Any]],
    total_time: float,
) -> str:
    """Format parallel execution results into a readable string."""
    successes = sum(1 for r in results if r["success"])
    failures = len(results) - successes

    lines: list[str] = [
        f"Parallel execution: {len(results)} tools, "
        f"{successes} succeeded, {failures} failed, "
        f"{total_time:.2f}s total",
        "",
    ]

    # Calculate how much time was saved
    sequential_time = sum(r["elapsed"] for r in results)
    saved = sequential_time - total_time
    if saved > 0.1:
        lines.append(
            f"Time saved vs sequential: {saved:.2f}s ({sequential_time:.2f}s -> {total_time:.2f}s)"
        )
        lines.append("")

    for r in results:
        status = "OK" if r["success"] else "FAIL"
        header = f"--- [{r['id']}] {r['name']} ({status}, {r['elapsed']:.2f}s) ---"
        lines.append(header)

        # No per-result truncation here (W3-2). Every child went through
        # dispatch_tool, so every child's text is ALREADY bounded and, if it was
        # large, already spilled to a file whose path is inside that text. The
        # 10 000-char cut that used to live here sliced through those bounded
        # results a second time -- taking the head only, which is precisely
        # where it would drop the spill marker and strand the file.
        lines.append(str(r["result"]))
        lines.append("")

    return "\n".join(lines)


async def execute_parallel_batch(
    commands: list[str],
    timeout: int = 120,
) -> str:
    """Convenience wrapper: run multiple bash commands in parallel.

    Args:
        commands: List of shell commands to run concurrently.
        timeout: Maximum seconds per command (default 120).

    Returns:
        Combined results from all commands.
    """
    tool_calls = [
        {
            "name": "bash",
            "arguments": {"command": cmd, "timeout": timeout},
            "id": f"cmd_{i}",
        }
        for i, cmd in enumerate(commands)
    ]

    return await execute_parallel(tool_calls, timeout=timeout)
