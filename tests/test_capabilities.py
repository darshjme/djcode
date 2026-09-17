"""Behavioral coverage for the real DAF bridge and new session capabilities."""

import asyncio
import json
import sys
from types import SimpleNamespace

import httpx
import pytest

from djcode.workflow import WorkflowEngine, validate_nodes


def node(ident, name=None, deps=()):
    return {"id": ident, "name": name or ident, "arguments": {}, "dependencies": list(deps)}


def test_real_daf_ddal_order_and_failures(monkeypatch, tmp_path):
    import djcode.workflow as workflow

    monkeypatch.setattr(workflow, "CONFIG_DIR", tmp_path)

    async def run():
        engine = WorkflowEngine(mode="daf")
        active = set()
        peak = 0
        done = set()

        async def dispatch(name, args):
            nonlocal peak
            if name == "verify":
                assert done == {"a", "b"}
            active.add(name)
            peak = max(peak, len(active))
            await asyncio.sleep(0.03)
            active.remove(name)
            done.add(name)
            return "ok"

        results = await engine.execute(
            [node("a"), node("b"), node("v", "verify", ["a", "b"])], dispatch, 2
        )
        assert set(results.values()) == {"ok"}
        assert peak == 2
        wires = [e for e in engine.last_events if e["event"] == "wire"]
        assert len(wires) == 3
        assert all(e["sent_bytes"] > 0 and e["received_bytes"] > 0 for e in wires)
        assert engine.last_events[-1]["ok"] is True
        called = []

        async def fail(name, args):
            called.append(name)
            return "Error: failed fixture"

        results = await engine.execute([node("a"), node("v", "verify", ["a"])], fail)
        assert called == ["a"]
        assert "not executed" in results["v"]
        assert engine.last_events[-1]["ok"] is False

    asyncio.run(run())


def test_daf_rejects_invalid_graph_before_dispatch():
    for graph in (
        [node("a", deps=["b"]), node("b", deps=["a"])],
        [node("a"), node("a")],
        [node("a", deps=["missing"])],
    ):
        with pytest.raises(ValueError):
            validate_nodes(graph)


def test_real_daf_cancellation_cancels_python_handler(monkeypatch, tmp_path):
    import djcode.workflow as workflow

    monkeypatch.setattr(workflow, "CONFIG_DIR", tmp_path)

    async def run():
        # Compilation is setup, not part of the handler cancellation deadline.
        await workflow.engine_path()
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def dispatch(*args):
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                stopped.set()

        task = asyncio.create_task(WorkflowEngine().execute([node("a")], dispatch))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()

    asyncio.run(run())


def test_skill_md_body_and_local_discovery(monkeypatch, tmp_path):
    from djcode.skills import SkillManager

    monkeypatch.chdir(tmp_path)
    root = tmp_path / "global"
    root.mkdir()
    monkeypatch.setattr(SkillManager, "SKILLS_DIR", root)
    path = tmp_path / ".djcode/skills/testing/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nname: testing\ndescription: Verify behavior\n"
        "---\nRead the implementation before testing.\n"
    )
    manager = SkillManager()
    assert manager.get_skill("testing").instructions == "Read the implementation before testing."


def test_jobs_output_exit_and_shutdown(monkeypatch, tmp_path):
    import djcode.extensions as extensions
    from djcode.capabilities import Capabilities

    monkeypatch.setattr(extensions, "EXTENSIONS_FILE", tmp_path / "extensions.json")

    async def run():
        runtime = Capabilities(SimpleNamespace())
        started = json.loads(await runtime.process("start", command="printf 'fixture output'"))
        await asyncio.wait_for(runtime.jobs[started["id"]]["task"], 3)
        done = json.loads(await runtime.process("read", job_id=started["id"]))
        assert done["output"] == "fixture output" and done["exit_code"] == 0
        scheduled = json.loads(
            await runtime.process("schedule", command="echo should-not-run", delay=60)
        )
        await runtime.close()
        assert runtime.jobs[scheduled["id"]]["task"].done()

    asyncio.run(run())


def test_mcp_notifications_and_initialized_handshake(tmp_path):
    from djcode.extensions import Extension, MCPConnection

    server = tmp_path / "server.py"
    server.write_text("""import json,sys
initialized=False
for line in sys.stdin:
 r=json.loads(line)
 if r['method']=='notifications/initialized':initialized=True;continue
 if r['method']=='initialize':
  result={'protocolVersion':'2024-11-05','capabilities':{},
          'serverInfo':{'name':'fixture','version':'1'}}
 elif r['method']=='tools/list':
  assert initialized
  result={'tools':[{'name':'echo','inputSchema':{'type':'object'}}]}
 else:result={'content':[{'type':'text','text':r['params']['arguments']['text']}],'isError':False}
 print(json.dumps({'jsonrpc':'2.0','method':'notifications/message','params':{}}),flush=True)
 print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}),flush=True)
""")

    async def run():
        connection = MCPConnection(Extension("fixture", sys.executable, [str(server)]))
        try:
            await connection.start()
            assert (await connection.list_tools())[0]["name"] == "echo"
            assert await connection.call_tool("echo", {"text": "verified"}) == "verified"
        finally:
            await connection.stop()
        assert not connection.is_alive

    asyncio.run(run())


def test_openrouter_pkce_and_sanitized_failure():
    import base64
    import hashlib
    from urllib.parse import parse_qs, urlparse

    from djcode.openrouter_auth import begin, exchange

    verifier, url = begin()
    args = parse_qs(urlparse(url).query)
    assert args["code_challenge"] == [
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    ]
    assert "callback_url" not in args

    async def run():
        def respond(request):
            body = json.loads(request.content)
            assert body["code_verifier"] == verifier
            return httpx.Response(200, json={"key": "fixture-key"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            assert await exchange("fixture-code", verifier, client=client) == "fixture-key"
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(403, text="sensitive-provider-body")
            )
        ) as client:
            with pytest.raises(RuntimeError) as error:
                await exchange("expired", verifier, client=client)
            assert "sensitive" not in str(error.value)

    asyncio.run(run())


def test_session_fork_preserves_source_and_new_clears(tmp_path):
    from djcode.provider import Message
    from djcode.session_commands import handle
    from djcode.sessions import SessionDB

    db = SessionDB(tmp_path / "sessions.db")
    first = db.create_session("fixture", "fixture")
    op = SimpleNamespace(
        session_db=db,
        session_id=first,
        provider=SimpleNamespace(config=SimpleNamespace(model="fixture", name="fixture")),
        messages=[Message("system", "rules"), Message("user", "original")],
    )
    op.reset = lambda: op.messages.__setitem__(slice(1, None), [])

    async def run():
        await handle(op, "/fork")
        assert op.session_id != first
        assert db.load_conversation(first)[-1]["content"] == "original"
        fork = op.session_id
        op.messages.append(Message("assistant", "fork result"))
        await handle(op, "/new")
        assert len(op.messages) == 1
        assert db.load_conversation(fork)[-1]["content"] == "fork result"
        assert db.load_conversation(first)[-1]["content"] == "original"

    asyncio.run(run())


def test_connect_transaction_cancel_and_success(monkeypatch, tmp_path):
    from textual.app import App
    from textual.widgets import Input

    import djcode.connect as connect
    from djcode.connect import ConnectScreen

    saved = []
    monkeypatch.setattr(
        connect,
        "load_config",
        lambda: {"provider": "openai", "model": "old", "openai_api_key": "existing"},
    )
    monkeypatch.setattr(connect, "save_config", lambda value: saved.append(dict(value)))
    monkeypatch.setattr(
        connect,
        "probe",
        lambda cfg: {"status": "ready", "models": ["fixture-model"], "message": "Ready"},
    )

    async def run():
        app = App()
        async with app.run_test(size=(80, 28)) as pilot:
            screen = ConnectScreen()
            app.push_screen(screen)
            await pilot.pause()
            await screen._choose("openai")
            await screen._choose("api_key")
            screen.action_cancel()
            await pilot.pause()
            assert saved == []
            screen = ConnectScreen()
            app.push_screen(screen)
            await pilot.pause()
            await screen._choose("openai")
            await screen._choose("api_key")
            await screen.submit(Input.Submitted(screen.query_one(Input), ""))
            assert screen.stage == "model"
            await screen._choose("fixture-model")
            await pilot.pause()
            assert saved[0]["model"] == "fixture-model"
            assert saved[0]["openai_api_key"] == "existing"

    asyncio.run(run())


def test_durable_scheduler_restart_claim_and_failure(monkeypatch, tmp_path):
    import djcode.workflow as workflow
    from djcode.scheduler import Scheduler

    monkeypatch.setattr(workflow, "CONFIG_DIR", tmp_path)
    path = tmp_path / "schedules.db"
    store = Scheduler(path)
    ident = store.create("printf 'scheduled' > result.txt", cwd=str(tmp_path))
    reopened = Scheduler(path)
    assert reopened.list()[0]["id"] == ident
    assert asyncio.run(reopened.run_once())
    assert (tmp_path / "result.txt").read_text() == "scheduled"
    assert reopened.list()[0]["state"] == "completed"
    assert not asyncio.run(store.run_once())
    failed = store.create("exit 7", interval=60, cwd=str(tmp_path))
    assert asyncio.run(store.run_once())
    assert next(j for j in store.list() if j["id"] == failed)["state"] == "failed"
    pending = store.create("echo claimed", cwd=str(tmp_path))
    assert store.claim()["id"] == pending
    assert reopened.claim() is None


def test_vision_payloads_and_persistence(tmp_path):
    import base64

    from djcode.provider import Message, Provider, ProviderConfig, _messages_to_dicts
    from djcode.sessions import SessionDB

    image = tmp_path / "pixel.png"
    image.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6YJ0AAAAASUVORK5CYII="
        )
    )
    message = Message("user", "Inspect this", images=[str(image)])
    db = SessionDB(tmp_path / "vision.db")
    sid = db.create_session("fixture", "fixture")
    db.save_conversation(sid, [message])
    assert db.load_conversation(sid)[0]["images"] == [str(image)]
    converted = _messages_to_dicts([message])

    async def run():
        for name in ("openai", "anthropic", "google"):
            provider = Provider(
                ProviderConfig(name, "https://example.invalid", "fixture", "fixture")
            )
            native = provider._get_new_provider()
            try:
                if name == "openai":
                    assert native._build_messages(converted)[0]["content"][1]["type"] == "image_url"
                elif name == "anthropic":
                    assert (
                        native._build_messages(converted)[0]["content"][1]["source"]["media_type"]
                        == "image/png"
                    )
                else:
                    assert (
                        native._build_contents(converted)[0][0]["parts"][1]["inlineData"][
                            "mimeType"
                        ]
                        == "image/png"
                    )
            finally:
                await provider.close()

    asyncio.run(run())


def test_operator_model_tools_reach_capabilities_and_daf(monkeypatch, tmp_path):
    import djcode.extensions as extensions
    import djcode.workflow as workflow
    from djcode.agents.operator import Operator
    from djcode.provider import Provider, ProviderConfig

    monkeypatch.setattr(workflow, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(extensions, "EXTENSIONS_FILE", tmp_path / "extensions.json")

    class Fixture(Provider):
        async def chat_openai_compat(self, messages, stream=True):
            if len(messages) < 3:
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "jobs",
                                        "function": {
                                            "name": "process",
                                            "arguments": '{"action":"list"}',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            else:
                assert messages[-1].role == "tool" and messages[-1].content == "[]"
                yield {
                    "choices": [{"delta": {"content": "No running jobs"}, "finish_reason": "stop"}]
                }

    async def run():
        provider = Fixture(
            ProviderConfig("custom", "https://example.invalid", "fixture", "fixture")
        )
        op = Operator(provider, auto_accept=True, raw=True)
        try:
            answer = "".join([token async for token in op.send("List jobs")])
            assert answer == "No running jobs"
            assert any(e["event"] == "wire" for e in op.workflow.last_events)
        finally:
            await provider.close()

    asyncio.run(run())


def test_operator_cancel_completes_tool_protocol(monkeypatch, tmp_path):
    import djcode.agents.operator as module
    import djcode.workflow as workflow
    from djcode.agents.operator import Operator
    from djcode.provider import Provider, ProviderConfig

    monkeypatch.setattr(workflow, "CONFIG_DIR", tmp_path)

    class Fixture(Provider):
        calls = 0

        async def chat_openai_compat(self, messages, stream=True):
            self.calls += 1
            if self.calls == 1:
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "cancelled-tool",
                                        "function": {
                                            "name": "file_read",
                                            "arguments": '{"path":"fixture"}',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            else:
                assert messages[-2].tool_call_id == "cancelled-tool"
                assert "cancelled" in messages[-2].content.lower()
                yield {"choices": [{"delta": {"content": "Recovered"}, "finish_reason": "stop"}]}

    async def run():
        # Compilation is setup, not part of the handler cancellation deadline.
        await workflow.engine_path()
        started = asyncio.Event()

        async def hang(*args):
            started.set()
            await asyncio.sleep(60)

        monkeypatch.setattr(module, "dispatch_tool", hang)
        provider = Fixture(
            ProviderConfig("custom", "https://example.invalid", "fixture", "fixture")
        )
        op = Operator(provider, auto_accept=True, raw=True)
        try:

            async def consume():
                return [token async for token in op.send("Start a read")]

            task = asyncio.create_task(consume())
            await asyncio.wait_for(started.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert "".join([token async for token in op.send("Continue")]) == "Recovered"
        finally:
            await provider.close()

    asyncio.run(run())
