"""The approval card: every key produces the right Decision.

The old card could only return True or False. `Operator._approve_tool` has
understood `Decision(ALWAYS)` and `Decision(SESSION)` since W6 -- and wires
both straight into `grant_always`/`grant_session` -- so the whole of
"don't ask me again" was already built and simply unreachable from the REPL.
These tests pin each key to its action, pin the fail-closed directions, and pin
the two facts the card has to show before a user can safely say "always".
"""

from __future__ import annotations

import asyncio

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from djcode.core.permissions import DecisionAction, PermissionEngine, ToolRequest
from djcode.frontends.repl import approval


def _press(keys: str) -> str:
    """Drive `read_choice` with a synthetic keypress and return its answer."""

    async def run():
        with create_pipe_input() as pipe:
            pipe.send_text(keys)
            with create_app_session(input=pipe, output=DummyOutput()):
                return await approval.read_choice()

    return asyncio.run(run())


@pytest.mark.parametrize("key", ["y", "a", "s", "n", "d"])
def test_every_offered_key_is_read_as_itself(key):
    assert _press(key) == key


def test_uppercase_is_the_same_key():
    assert _press("Y") == "y"


@pytest.mark.parametrize("keys", ["\x1b", "\x03", "\x04"])
def test_escape_and_interrupt_mean_no(keys):
    """Fail-closed, and the same direction `Decision.__bool__` picks."""
    assert _press(keys) == "n"


def test_enter_alone_cannot_approve_anything():
    """A stray Enter must not run a tool. It is not a bound key, so the app
    keeps waiting -- and the next real key is what counts."""

    async def run():
        with create_pipe_input() as pipe:
            pipe.send_text("\r\nn")
            with create_app_session(input=pipe, output=DummyOutput()):
                return await approval.read_choice()

    assert asyncio.run(run()) == "n"


# -- the Decision each key produces ----------------------------------------


def _quiet_console():
    import io

    return Console(file=io.StringIO(), width=80)


def _ask_quiet(keys: str, name="bash", arguments=None, **kw):
    arguments = {"command": "pytest -q"} if arguments is None else arguments

    async def run():
        with create_pipe_input() as pipe:
            pipe.send_text(keys)
            with create_app_session(input=pipe, output=DummyOutput()):
                return await approval.ask(name, arguments, console=_quiet_console(), **kw)

    return asyncio.run(run())


@pytest.mark.parametrize(
    "key,action",
    [
        ("y", DecisionAction.ALLOW),
        ("a", DecisionAction.ALWAYS),
        ("s", DecisionAction.SESSION),
        ("n", DecisionAction.DENY),
    ],
)
def test_key_to_decision(key, action, monkeypatch):
    monkeypatch.setattr(approval.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(approval.sys.stdout, "isatty", lambda: True, raising=False)
    decision = _ask_quiet(key)
    assert decision.action is action


def test_always_and_session_carry_the_rule_they_would_persist(monkeypatch, tmp_path):
    """`Operator._approve_tool` hands `decision.rule` straight to
    `grant_always`. A Decision without one silently re-derives the scope."""
    monkeypatch.setattr(approval.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(approval.sys.stdout, "isatty", lambda: True, raising=False)
    engine = PermissionEngine(load=False)
    decision = _ask_quiet("a", engine=engine)
    assert decision.rule is not None
    assert decision.rule.tool == "bash"
    assert decision.rule.pattern.startswith("pytest")


def test_allow_once_persists_nothing(monkeypatch):
    monkeypatch.setattr(approval.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(approval.sys.stdout, "isatty", lambda: True, raising=False)
    engine = PermissionEngine(load=False)
    decision = _ask_quiet("y", engine=engine)
    assert decision.action is DecisionAction.ALLOW
    assert decision.rule is None, "a one-off approval must not carry a rule to persist"


def test_deny_with_a_note_reaches_the_model(monkeypatch):
    monkeypatch.setattr(approval.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(approval.sys.stdout, "isatty", lambda: True, raising=False)

    async def note():
        return "use uv, not pip"

    monkeypatch.setattr(approval, "read_note", note)
    decision = _ask_quiet("e")
    assert decision.action is DecisionAction.DENY
    assert decision.comment == "use uv, not pip"
    assert not decision, "a deny must still be falsy"


def test_an_empty_note_still_says_something_useful(monkeypatch):
    monkeypatch.setattr(approval.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(approval.sys.stdout, "isatty", lambda: True, raising=False)

    async def note():
        return ""

    monkeypatch.setattr(approval, "read_note", note)
    decision = _ask_quiet("e")
    assert decision.comment
    assert "declined" in decision.comment


def test_details_redraws_and_then_the_next_key_decides(monkeypatch):
    monkeypatch.setattr(approval.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(approval.sys.stdout, "isatty", lambda: True, raising=False)
    decision = _ask_quiet("dy")
    assert decision.action is DecisionAction.ALLOW


def test_a_non_interactive_session_denies_instead_of_crashing(monkeypatch):
    """Audit 2 §2.3: constructing a prompt_toolkit Application against a pipe
    raises NoConsoleScreenBufferError on Windows. The card must refuse the call
    and tell the model why, not take the turn down."""
    monkeypatch.setattr(approval.sys.stdin, "isatty", lambda: False, raising=False)

    async def run():
        return await approval.ask("bash", {"command": "ls"}, console=_quiet_console())

    decision = asyncio.run(run())
    assert decision.action is DecisionAction.DENY
    assert "--auto-accept" in decision.comment


# -- what the card has to show ---------------------------------------------


def test_argv_line_names_every_command_in_a_chain():
    """The user is authorising all of them, so the card shows all of them."""
    names = approval.argv_line("git status && rm -rf build")
    assert "git" in names and "rm" in names


def test_argv_line_is_the_same_list_the_engine_judged():
    from djcode.core.permissions import tokenise

    command = "cat a.txt | sort | uniq -c"
    assert approval.argv_line(command).split() == tokenise(command).names()


def test_argv_line_never_raises_on_garbage():
    assert isinstance(approval.argv_line("'''unterminated"), str)


def test_the_card_shows_the_scope_an_always_would_persist():
    engine = PermissionEngine(load=False)
    rule = engine.rule_for(ToolRequest(tool="bash", arguments={"command": "pytest -q"}))
    assert approval.describe_rule(rule) == "bash(pytest *)"


def test_a_hardline_command_is_labelled_on_the_card():
    console = _quiet_console()
    approval.render_card(console, "bash", {"command": "rm -rf /"})
    text = console.file.getvalue()
    assert "rm" in text


def test_the_card_renders_for_every_tool_without_raising():
    """22 of 24 tools have no diff preview and no command string. The fallback
    path has to draw something for all of them."""
    from djcode.frontends.repl.render import TOOL_DISPLAY

    for tool in TOOL_DISPLAY:
        console = _quiet_console()
        approval.render_card(console, tool, {"path": "x.py", "command": "ls"})
        assert console.file.getvalue().strip()


def test_the_card_names_the_agent_that_asked():
    console = _quiet_console()
    approval.render_card(console, "bash", {"command": "ls"}, agent_name="Prometheus")
    assert "Prometheus" in console.file.getvalue()


def test_the_repl_callback_advertises_agent_name():
    """`agent_spawn._accepts_agent_name` inspects this signature; without the
    parameter a subagent's card cannot say whose request it is."""
    from djcode.repl import _approve_repl_tool
    from djcode.tools.agent_spawn import _accepts_agent_name

    assert _accepts_agent_name(_approve_repl_tool)
