"""Workflow preflight and subprocess fault acceptance without provider calls."""

import asyncio
import json
import os
import sqlite3
import sys

import pytest

import djcode.workflow as workflow
from djcode.workflow import WorkflowEngine, validate_nodes


def node(ident="a", *, args=None, deps=()):
    return {"id": ident, "name": "fixture", "arguments": args or {}, "dependencies": list(deps)}


def fake_host(monkeypatch, tmp_path, body):
    script = tmp_path / "host.py"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys\n"
        "batch=json.loads(sys.stdin.readline())\n"
        "nodes=batch['nodes']\n"
        "def emit(value): print(json.dumps(value),flush=True)\n"
        "def tool(n): emit({'event':'tool','id':n['id'],"
        "'name':n['name'],'arguments':n['arguments']})\n"
        "def wire(r): emit({'event':'wire','id':r['id'],"
        "'ok':r['ok'],'sent_bytes':50,'received_bytes':100})\n" + body
    )
    script.chmod(0o700)
    monkeypatch.setenv("DJCODE_DAF_ENGINE", str(script))
    monkeypatch.setattr(workflow, "CONFIG_DIR", tmp_path / "config")
    return script


def saved_events(engine):
    with sqlite3.connect(workflow.CONFIG_DIR / "workflows.db") as db:
        row = db.execute("SELECT events FROM runs WHERE id=?", (engine.last_run,)).fetchone()
    return json.loads(row[0])


@pytest.mark.parametrize(
    "nodes",
    [
        [],
        [node(str(i)) for i in range(33)],
        [node(), node()],
        [{**node(), "id": " "}],
        [{**node(), "id": "x" * 129}],
        [{**node(), "name": " "}],
        [{**node(), "name": "x" * 129}],
        [{**node(), "arguments": []}],
        [{**node(), "dependencies": [dict(id="a")]}],
        [{**node(), "dependencies": [["a"]]}],
        [{**node(), "dependencies": [1]}],
        [node(), node("b", deps=["a", "a"])],
        [node(deps=["missing"])],
        [node(deps=["a"])],
        [node("a", deps=["b"]), node("b", deps=["a"])],
        [node(args={"value": float("nan")})],
        [node(args={"value": float("inf")})],
        [node(args={"value": object()})],
        [node(args={"value": "x" * workflow.MAX_BATCH_BYTES})],
    ],
)
def test_invalid_graph_is_a_value_error_before_effects(nodes):
    with pytest.raises(ValueError):
        validate_nodes(nodes)


def test_native_dag_rejection_precedes_all_effects():
    calls = []

    async def dispatch(*args):
        calls.append(args)
        return "ok"

    with pytest.raises(ValueError, match="DAG dependencies"):
        asyncio.run(
            WorkflowEngine(mode="native").execute([node(), node("b", deps=["a"])], dispatch)
        )
    assert calls == []


@pytest.mark.parametrize("concurrency", [True, False, 0, 5, 1.0, "1"])
def test_invalid_concurrency_precedes_native_effects(concurrency):
    async def dispatch(*args):
        pytest.fail("invalid concurrency dispatched a tool")

    with pytest.raises(ValueError, match="concurrency"):
        asyncio.run(WorkflowEngine(mode="native").execute([node()], dispatch, concurrency))


@pytest.mark.parametrize(
    "event",
    [
        [],
        {"event": "unknown"},
        {"event": "tool", "id": [], "name": "fixture", "arguments": {}},
        {"event": "tool", "id": "a", "name": "other", "arguments": {}},
        {"event": "tool", "id": "a", "name": "fixture", "arguments": {"value": True}},
        {"event": "wire", "id": "a", "sent_bytes": 1, "received_bytes": 1, "ok": True},
        {"event": "complete", "ok": True, "states": [{"id": "a", "state": "Succeeded"}]},
        {"event": "complete", "ok": False, "states": [{"id": "a", "state": []}]},
        {"event": "complete", "ok": False, "states": [{"id": "other", "state": "Skipped"}]},
    ],
)
def test_untrusted_host_event_cannot_trigger_effects(monkeypatch, tmp_path, event):
    fake_host(monkeypatch, tmp_path, f"emit({event!r})\n")
    engine = WorkflowEngine(mode="daf")
    calls = []

    async def dispatch(*args):
        calls.append(args)
        return "ok"

    with pytest.raises(RuntimeError):
        asyncio.run(engine.execute([node(args={"value": 1})], dispatch))
    assert calls == []
    assert saved_events(engine)[-1]["event"] == "failed"


def test_host_cannot_request_a_dependency_before_success(monkeypatch, tmp_path):
    fake_host(monkeypatch, tmp_path, "tool(nodes[1])\n")
    calls = []

    async def dispatch(*args):
        calls.append(args)
        return "ok"

    with pytest.raises(RuntimeError, match="unexpected tool"):
        asyncio.run(WorkflowEngine(mode="daf").execute([node(), node("b", deps=["a"])], dispatch))
    assert calls == []


def test_verified_success_drains_stderr_and_saves_metadata_only(monkeypatch, tmp_path):
    fake_host(
        monkeypatch,
        tmp_path,
        "sys.stderr.write('diagnostic' * 100000);sys.stderr.flush()\n"
        "tool(nodes[0])\n"
        "reply=json.loads(sys.stdin.readline())\n"
        "assert reply['result']=='private tool output'\n"
        "emit({'event':'wire','id':'a','sent_bytes':50,'received_bytes':100,"
        "'ok':True,'extra':'private wire'})\n"
        "emit({'event':'complete','ok':True,'states':[{'id':'a','state':'Succeeded',"
        "'extra':'private state'}], 'extra':'private completion'})\n",
    )
    engine = WorkflowEngine(mode="daf")

    async def dispatch(*args):
        return "private tool output"

    async def run():
        return await asyncio.wait_for(
            engine.execute([node(args={"private argument": "fixture"})], dispatch), 5
        )

    assert asyncio.run(run()) == {"a": "private tool output"}
    events = saved_events(engine)
    assert events == engine.last_events
    assert events[-1] == {
        "event": "complete",
        "ok": True,
        "states": [{"id": "a", "state": "Succeeded"}],
    }
    assert "private" not in json.dumps(events)


def test_host_failure_checkpoints_completed_effect_without_replay(monkeypatch, tmp_path):
    fake_host(
        monkeypatch, tmp_path, "tool(nodes[0])\njson.loads(sys.stdin.readline())\nsys.exit(7)\n"
    )
    engine = WorkflowEngine(mode="daf")
    calls = []

    async def dispatch(*args):
        calls.append(args)
        return "ok"

    with pytest.raises(RuntimeError, match="verified completion"):
        asyncio.run(engine.execute([node()], dispatch))
    assert len(calls) == 1
    assert [e["event"] for e in saved_events(engine)] == ["preparing", "tool", "failed"]


def test_cancellation_reaps_host_and_cancels_handler(monkeypatch, tmp_path):
    pid_file = tmp_path / "host.pid"
    fake_host(
        monkeypatch,
        tmp_path,
        f"open({str(pid_file)!r},'w').write(str(os.getpid()))\n"
        "tool(nodes[0])\nsys.stdin.readline()\n",
    )
    engine = WorkflowEngine(mode="daf")

    async def run():
        started, stopped = asyncio.Event(), asyncio.Event()

        async def dispatch(*args):
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                stopped.set()

        task = asyncio.create_task(engine.execute([node()], dispatch))
        await asyncio.wait_for(started.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert stopped.is_set()

    asyncio.run(run())
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
    assert saved_events(engine)[-1] == {"event": "interrupted", "reason": "cancelled"}


def test_bounded_tool_reply_reports_failure_once(monkeypatch, tmp_path):
    monkeypatch.setattr(workflow, "MAX_REPLY_BYTES", 256)
    fake_host(
        monkeypatch,
        tmp_path,
        "tool(nodes[0])\nreply=json.loads(sys.stdin.readline())\n"
        "assert reply['ok'] is False and len(json.dumps(reply).encode())<=256\n"
        "wire(reply)\n"
        "emit({'event':'complete','ok':False,'states':[{'id':'a','state':'Failed'}]})\n",
    )
    calls = []

    async def dispatch(*args):
        calls.append(args)
        return "x" * 1024

    result = asyncio.run(WorkflowEngine(mode="daf").execute([node()], dispatch))
    assert result["a"].startswith("Error: tool result exceeds")
    assert len(calls) == 1


def test_snapshot_preserves_arguments_during_engine_preparation(monkeypatch, tmp_path):
    script = fake_host(
        monkeypatch,
        tmp_path,
        "tool(nodes[0])\nwire(json.loads(sys.stdin.readline()))\n"
        "emit({'event':'complete','ok':True,'states':[{'id':'a','state':'Succeeded'}]})\n",
    )
    nodes = [node(args={"value": "approved"})]

    async def run():
        preparing, resume = asyncio.Event(), asyncio.Event()
        observed = []

        async def prepare():
            preparing.set()
            await resume.wait()
            return script

        async def dispatch(name, arguments):
            observed.append(arguments)
            return "ok"

        monkeypatch.setattr(workflow, "engine_path", prepare)
        task = asyncio.create_task(WorkflowEngine(mode="daf").execute(nodes, dispatch))
        await preparing.wait()
        nodes[0]["arguments"]["value"] = "changed"
        resume.set()
        await task
        assert observed == [{"value": "approved"}]

    asyncio.run(run())


def test_python_enforces_concurrency_even_for_an_uncooperative_host(monkeypatch, tmp_path):
    fake_host(
        monkeypatch,
        tmp_path,
        "for n in nodes: tool(n)\n"
        "for _ in nodes: wire(json.loads(sys.stdin.readline()))\n"
        "emit({'event':'complete','ok':True,"
        "'states':[{'id':n['id'],'state':'Succeeded'} for n in nodes]})\n",
    )

    async def run():
        active = peak = 0

        async def dispatch(*args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return "ok"

        await WorkflowEngine(mode="daf").execute([node(str(i)) for i in range(4)], dispatch, 2)
        assert peak == 2

    asyncio.run(run())


@pytest.mark.parametrize("fault", ["duplicate", "missing"])
def test_completion_requires_one_wire_outcome_per_requested_tool(monkeypatch, tmp_path, fault):
    wires = "wire(reply)\nwire(reply)\n" if fault == "duplicate" else ""
    fake_host(
        monkeypatch,
        tmp_path,
        "tool(nodes[0])\nreply=json.loads(sys.stdin.readline())\n"
        + wires
        + "emit({'event':'complete','ok':True,'states':[{'id':'a','state':'Succeeded'}]})\n",
    )
    calls = []
    engine = WorkflowEngine(mode="daf")

    async def dispatch(*args):
        calls.append(args)
        return "ok"

    with pytest.raises(RuntimeError, match="invalid wire|unverified completion"):
        asyncio.run(engine.execute([node()], dispatch))
    assert len(calls) == 1
    assert len(engine.last_events) <= 4
    assert saved_events(engine)[-1]["event"] == "failed"
