"""W1-6 / GAP B10 / SSOT P1-12: consent does not descend into a subagent.

The operator approving their own session -- or switching it to auto-accept --
has said nothing about what a spawned agent may do on their machine. These
tests pin that: the child never inherits `auto_accept`, every non-read tool it
calls is presented through the parent's approval callback, and the request
carries the name of the agent that made it so the prompt can say
"Approve file_write - via Prometheus" (DESIGN-CLI.md 5.3).
"""

import asyncio
import json
import logging

import pytest

from djcode.tools import agent_spawn

CODER = "Prometheus"
TESTER = "Agni"


# -- Fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def native_engine(monkeypatch):
    """Run tools on the native scheduler instead of building the Rust engine.

    Exercises W1-5's fallback as a side effect: `engine_path` reporting no
    toolchain must degrade the workflow engine, not fail the tool call.
    """
    import djcode.workflow as workflow

    async def unavailable():
        raise workflow.DAFUnavailableError("no Rust toolchain in this test")

    monkeypatch.setattr(workflow, "engine_path", unavailable)
    monkeypatch.setattr(workflow, "_FALLBACK_ANNOUNCED", False)


@pytest.fixture(autouse=True)
def clean_context():
    """No parent context leaks between tests."""
    token = agent_spawn._parent_context.set(None)
    yield
    agent_spawn._parent_context.reset(token)


def final(text="done"):
    return {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}


def call(name, **arguments):
    return {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "abc",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ]
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


class ScriptedProvider:
    """Replays one scripted turn per `chat` call, in order."""

    def __init__(self, turns):
        self.turns = iter(turns)
        self.messages = []

    async def chat(self, messages, stream=True):
        self.messages.append(list(messages))
        for chunk in next(self.turns):
            yield chunk


def spawn(provider, task="write the file", *, auto_accept=False, approval=None, role="coder"):
    async def run():
        with agent_spawn.agent_context(provider, auto_accept, approval):
            return await agent_spawn.execute_spawn_agent(role, task, max_tool_rounds=3)

    return asyncio.run(run())


def writing_provider(target, final_text="child done"):
    return ScriptedProvider(
        [
            [call("file_write", path=str(target), content="written by the child")],
            [final(final_text)],
        ]
    )


# -- The regression itself -----------------------------------------------------


def test_child_does_not_inherit_parent_auto_accept(tmp_path):
    """The parent is in auto-accept. The child still has to ask, and is refused."""
    target = tmp_path / "must-not-exist.txt"
    decisions = []

    async def approve(name, arguments):
        decisions.append(name)
        return False

    result = spawn(writing_provider(target), auto_accept=True, approval=approve)

    assert decisions == ["file_write"], "the child's write was never presented"
    assert not target.exists(), "auto-accept descended into the child"
    assert "child done" in result


def test_child_request_is_presented_even_when_the_parent_auto_accepts(tmp_path):
    """Presented, not skipped -- and honoured when the operator says yes."""
    target = tmp_path / "approved.txt"
    decisions = []

    async def approve(name, arguments):
        decisions.append((name, arguments.get("path")))
        return True

    result = spawn(writing_provider(target), auto_accept=True, approval=approve)

    assert decisions == [("file_write", str(target))]
    assert target.read_text(encoding="utf-8") == "written by the child"
    assert "child done" in result


def test_child_with_no_approval_channel_and_no_grant_is_denied(tmp_path):
    """No callback and no auto-accept: the executor refuses and says why."""
    target = tmp_path / "denied.txt"

    spawn(writing_provider(target), auto_accept=False, approval=None)

    assert not target.exists()
    assert agent_spawn._child_approval("Prometheus", False, None) is None


# -- The request carries the agent's name --------------------------------------


def test_request_carries_the_agent_name_as_a_keyword(tmp_path):
    seen = {}

    async def approve(name, arguments, agent_name=None):
        seen[name] = agent_name
        return True

    spawn(writing_provider(tmp_path / "named.txt"), auto_accept=True, approval=approve)

    assert seen == {"file_write": CODER}


def test_two_argument_callback_can_read_the_agent_name(tmp_path):
    """A UI that cannot take another argument still gets the label."""
    seen = []

    async def approve(name, arguments):
        seen.append((name, agent_spawn.current_approval_agent()))
        return True

    assert agent_spawn.current_approval_agent() is None
    spawn(writing_provider(tmp_path / "named2.txt"), auto_accept=True, approval=approve)

    assert seen == [("file_write", CODER)]
    assert agent_spawn.current_approval_agent() is None, "the label leaked out of the request"


def test_nested_spawn_names_the_agent_that_actually_asked(tmp_path):
    """Two levels down, the prompt names the grandchild, not the child."""
    target = tmp_path / "nested.txt"
    provider = ScriptedProvider(
        [
            [call("spawn_agent", role="tester", task="write the file")],
            [call("file_write", path=str(target), content="written by the grandchild")],
            [final("grandchild done")],
            [final("child done")],
        ]
    )
    seen = []

    async def approve(name, arguments, agent_name=None):
        seen.append((name, agent_name))
        return True

    result = spawn(provider, auto_accept=True, approval=approve)

    assert seen == [("spawn_agent", CODER), ("file_write", TESTER)]
    assert target.read_text(encoding="utf-8") == "written by the grandchild"
    assert "child done" in result


# -- The headless case the blueprint does not cover ----------------------------


def test_headless_standing_grant_is_explicit_and_logged(caplog):
    """`djcode --wave ... --yes` has auto-accept and no approval channel.

    `cli.py` builds that Orchestrator with `auto_accept` and no callback, so
    there is no human to present anything to and the grant was given on the
    command line. The child runs under that standing grant rather than being
    silently denied every write -- and each covered tool is logged.
    """
    granted = agent_spawn._child_approval(CODER, True, None)
    assert granted is not None

    with caplog.at_level(logging.INFO, logger="djcode.tools.agent_spawn"):
        assert asyncio.run(granted("bash", {"command": "ls"})) is True

    assert "standing grant" in caplog.text
    assert CODER in caplog.text


# -- A sync callback is still honoured -----------------------------------------


def test_synchronous_approval_callback_is_supported(tmp_path):
    target = tmp_path / "sync.txt"
    seen = []

    def approve(name, arguments):
        seen.append(name)
        return True

    spawn(writing_provider(target), auto_accept=False, approval=approve)

    assert seen == ["file_write"]
    assert target.exists()
