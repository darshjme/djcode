import asyncio
import json
from types import SimpleNamespace

import pytest

from djcode import studio
from djcode.commands import commands_for, plan_blocks_command
from djcode.studio import Studio


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(studio, "CONFIG_DIR", tmp_path / "config")
    return Studio()


def seed(store):
    store.save(
        "agent",
        {
            "name": "coder-one",
            "role": "coder",
            "prompt": "Make focused changes",
            "tools": ["file_read", "file_write"],
        },
    )
    store.save("organisation", {"name": "team", "agents": ["coder-one"]})
    store.save(
        "flow",
        {
            "name": "release",
            "organisation": "team",
            "nodes": [
                {"id": "a", "agent": "coder-one", "task": "Read"},
                {"id": "b", "agent": "coder-one", "task": "Verify", "dependencies": ["a"]},
            ],
        },
    )


def test_definitions_persist_and_validate(store):
    seed(store)
    assert Studio().get("organisation", "team")["agents"] == ["coder-one"]
    with pytest.raises(ValueError, match="subset"):
        store.save(
            "agent", {"name": "scout", "role": "scout", "prompt": "Read", "tools": ["file_write"]}
        )
    with pytest.raises(ValueError, match="cycle"):
        store.save(
            "flow",
            {
                "name": "cycle",
                "nodes": [{"id": "a", "agent": "coder-one", "task": "x", "dependencies": ["a"]}],
            },
        )
    store.save("roadmap", {"name": "launch", "description": "Ship", "status": "planned"})
    assert Studio().timeline()["roadmap"][0]["status"] == "planned"
    assert all(e["status"] != "completed" for e in store.timeline()["events"])


def test_plan_mode_and_command_discovery(store):
    for interface in ("repl", "tui"):
        assert studio.COMMANDS <= {c.name for c in commands_for(interface)}
    for command in ("/flow", "/agent", "/organisation", "/roadmap"):
        assert plan_blocks_command(command, "save {}")
        assert not plan_blocks_command(command, "list")
    assert plan_blocks_command("/flow", "run release")
    with pytest.raises(ValueError, match="Plan mode"):
        asyncio.run(
            studio.handle(
                SimpleNamespace(plan_mode=True), "/agent", 'save {"name":"x","prompt":"x"}'
            )
        )


def test_finish_requires_successful_checks_and_approval(store, monkeypatch):
    monkeypatch.setattr(studio, "doctor", lambda deep: {"ok": True})
    approved = []

    async def approve(name, args):
        approved.append(args["command"])
        return args["command"] != "echo denied"

    operator = SimpleNamespace(plan_mode=False, _approve_tool=approve)

    async def run():
        result = json.loads(
            await studio.handle(operator, "/finish", '{"checks":["exit 3","echo denied"]}')
        )
        assert not result["ok"]
        assert store.timeline()["events"][0]["status"] == "checks_failed"
        result = json.loads(await studio.handle(operator, "/finish", '{"checks":["exit 0"]}'))
        assert result["ok"]
        assert store.timeline()["events"][0]["status"] == "completed"

    asyncio.run(run())
    assert approved == ["exit 3", "echo denied", "exit 0"]


def test_real_daf_flow_uses_custom_agents_and_records_results(store, monkeypatch):
    seed(store)
    from djcode.agents.executor import AgentExecutor

    calls = []

    async def execute(self, task):
        calls.append((self.spec.name, task, self.spec.tools_allowed, self.approval_callback))
        return SimpleNamespace(succeeded=True, response="done")

    monkeypatch.setattr(AgentExecutor, "execute", execute)

    async def approve(name, args):
        return False

    operator = SimpleNamespace(plan_mode=False, provider=object(), _approve_tool=approve)
    result = asyncio.run(store.run("release", operator))
    assert result["ok"] and list(result["results"]) == ["a", "b"]
    assert [c[1] for c in calls] == ["Read", "Verify"]
    assert all(c[0] == "coder-one" and c[3] == approve for c in calls)
    assert any(
        e["kind"] == "flow" and e["status"] == "passed" for e in Studio().timeline()["events"]
    )


def test_studio_editor_loads_template_and_returns_command(store):
    from textual.widgets import TextArea

    from djcode.app import DJcodeApp
    from djcode.studio_screen import StudioScreen

    async def initialize(self):
        pass

    async def run():
        app = DJcodeApp()
        async with app.run_test(size=(120, 36)) as pilot:
            app.push_screen(StudioScreen(), lambda result: captured.append(result))
            await pilot.pause()
            editor = app.screen.query_one("#studio-editor", TextArea)
            assert json.loads(editor.text)["name"] == "my-coder"
            await pilot.click("#studio-save")
            await pilot.pause()

    captured = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(DJcodeApp, "_initialize", initialize)
        asyncio.run(run())
    assert captured[0].startswith("/agent save ")


@pytest.mark.parametrize("value", [None, [], {}, 3, "", "completed"])
def test_milestone_validator_rejects_invalid_values(value):
    from djcode.milestones import validate_milestone_status

    with pytest.raises(ValueError):
        validate_milestone_status(value)


@pytest.mark.parametrize("value", ["planned", "in_progress", "blocked", "done"])
def test_milestone_validator_accepts_reported_status(value):
    from djcode.milestones import validate_milestone_status

    assert validate_milestone_status(value) == value
