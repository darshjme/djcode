"""Real DDAL integration acceptance; set DJCODE_DAF_ENGINE and PYTHONPATH first."""

import asyncio
import json
import os
import sys
import tempfile

# Avoid touching a user's configuration or history during acceptance.
_config = tempfile.TemporaryDirectory(prefix="daf-adapter-acceptance-")
os.environ["DJCODE_CONFIG_DIR"] = _config.name

from daf_adapter.runner import run  # noqa: E402

from djcode.adapter_workflow import execute_adapter_workflow  # noqa: E402


def node(ident, code, dependencies=()):
    return {
        "id": ident,
        "name": "daf_adapter_run",
        "dependencies": list(dependencies),
        "arguments": {
            "schema_version": 1,
            "id": ident,
            "argv": [sys.executable, "-c", code],
            "timeout_ms": 5000,
        },
    }


async def main():
    events, calls = [], []

    async def approve(name, request):
        calls.append(request["id"])
        return request["id"] != "denied"

    async def checked_run(request):
        assert request["id"] != "denied", "denied invocation reached adapter"
        return await run(request)

    nodes = [
        node("ok", "print('portable')"),
        node("after", "print('dependent')", ["ok"]),
        node("fail", "raise SystemExit(7)"),
        node("blocked", "print('MUST NOT RUN')", ["fail"]),
        node("denied", "print('MUST NOT RUN')"),
    ]
    result = await execute_adapter_workflow(
        nodes, approve, checked_run, concurrency=2, event_callback=events.append
    )
    assert result["ok"]["stdout"] == "portable\n"
    assert result["after"]["status"] == "succeeded"
    assert result["fail"]["exit_code"] == 7
    assert "dependency failed" in result["blocked"]["error"]
    assert "denied" in result["denied"]["error"]
    assert "blocked" not in calls
    wire = [e for e in events if e["event"] == "wire"]
    assert len(wire) == 4
    assert all(e["sent_bytes"] >= 21 and e["received_bytes"] >= 21 for e in wire)
    assert events[-1]["event"] == "complete" and events[-1]["ok"] is False
    report = {
        "passed": True,
        "cases": [
            "successful subprocess",
            "dependent success",
            "nonzero exit",
            "blocked dependent",
            "approval denial",
        ],
        "wire_events": len(wire),
        "approved_callback_ids": calls,
        "transport": "real bundled Rust scheduler and DDAL loopback",
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
