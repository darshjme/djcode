"""Run portable DAF adapter requests through DJcode's real DAF/DDAL scheduler.

Applications provide approval and an async Python or Rust adapter runner. The
runner is called only after approval; this module never executes a shell or
replaces the scheduler's DDAL events with invented transport measurements.
"""

from __future__ import annotations

import json

from djcode.workflow import MAX_REPLY_BYTES, WorkflowEngine, _json_bytes, validate_nodes


async def execute_adapter_workflow(nodes, approve, run, *, concurrency=1, event_callback=None):
    """Execute nodes named ``daf_adapter_run`` with protocol-v1 request arguments.

    ``approve(name, arguments)`` must asynchronously return the boolean True to
    admit an invocation. ``run(request)`` asynchronously returns a protocol-v1
    result. Neither callback is retried. Returned values are structured adapter
    results, or an error object for denied, skipped, or malformed invocations.
    """
    validate_nodes(nodes)
    nodes = json.loads(_json_bytes(nodes, 2**20))
    for node in nodes:
        request = node["arguments"]
        if (
            node["name"] != "daf_adapter_run"
            or type(request.get("schema_version")) is not int
            or request["schema_version"] != 1
            or request.get("id") != node["id"]
        ):
            raise ValueError("adapter nodes require daf_adapter_run and matching protocol-v1 IDs")
    outcomes = {}

    async def dispatch(name, request):
        ident = request["id"]
        # Give approval a copy: a callback cannot mutate the admitted request.
        if await approve(name, json.loads(_json_bytes(request, 2**20))) is not True:
            return "Error: User denied adapter execution"
        result = await run(json.loads(_json_bytes(request, 2**20)))
        if (
            not isinstance(result, dict)
            or type(result.get("schema_version")) is not int
            or result["schema_version"] != 1
            or result.get("id") != ident
            or result.get("status") not in {"succeeded", "failed", "timed_out"}
            or "exit_code" not in result
            or (result.get("exit_code") is not None and type(result["exit_code"]) is not int)
            or not isinstance(result.get("stdout"), str)
            or not isinstance(result.get("stderr"), str)
            or type(result.get("stdout_truncated")) is not bool
            or type(result.get("stderr_truncated")) is not bool
            or ((result["status"] == "succeeded") != (result.get("exit_code") == 0))
        ):
            raise ValueError("adapter returned an invalid or mismatched result")
        encoded = _json_bytes(result, MAX_REPLY_BYTES).decode()
        outcomes[ident] = json.loads(encoded)
        # WorkflowEngine treats string errors as failures; preserve that contract.
        return encoded if result["status"] == "succeeded" else "Error: " + encoded

    engine = WorkflowEngine(event_callback, mode="daf")
    messages = await engine.execute(nodes, dispatch, concurrency)
    return {node["id"]: outcomes.get(node["id"], {"error": messages[node["id"]]}) for node in nodes}
