"""Three things the REPL used to state that were not true.

1. The shortcuts card advertised a bare-`/` command picker, and described
   Ctrl+C as cancelling "the current response" at a time when Ctrl+C on Windows
   quit the program. A shortcuts card that is wrong is worse than no card: it
   is the one place a user goes to find out what is true.
2. Ctrl+T wrote `auto_accept` into config.json. W6 established that
   `--auto-accept` must not do that -- a toggle that silently changes the
   default for every future session is how a machine ends up permanently
   unattended with nobody having decided that -- and Ctrl+T was the same bug
   behind a different trigger.
3. The workflow engine's "no Rust toolchain, running the native scheduler"
   warning went to a logger with no handler attached. A user without cargo was
   silently downgraded and never told.
"""

from __future__ import annotations

import inspect

import pytest

# -- the shortcuts card ----------------------------------------------------


def test_the_card_no_longer_advertises_the_deleted_picker():
    from djcode.tui import SHORTCUTS_TABLE

    entries = dict(SHORTCUTS_TABLE)
    assert "picker" not in entries["/"].lower()


def test_the_card_documents_every_key_that_is_actually_bound():
    """Derived from `register_keybindings`'s own source, so a new binding with
    no card row fails here rather than being undiscoverable."""
    from djcode import tui

    source = inspect.getsource(tui.register_keybindings)
    documented = {key.lower().replace("+", "-").replace("ctrl", "c") for key, _ in
                  tui.SHORTCUTS_TABLE}
    bound = {
        line.split('"')[1]
        for line in source.splitlines()
        if line.strip().startswith("@kb.add(")
    }
    missing = {key for key in bound if key.startswith("c-") and key not in documented}
    assert not missing, f"bound but undocumented: {sorted(missing)}"


def test_the_card_documents_the_at_path_completion():
    from djcode.tui import SHORTCUTS_TABLE

    assert any(key == "@" for key, _ in SHORTCUTS_TABLE)


def test_ctrl_c_is_described_as_cancelling_the_turn():
    """True only since the Windows SIGINT fallback landed earlier in this wave.
    It was a lie when it was written."""
    from djcode.tui import SHORTCUTS_TABLE

    assert "cancel" in dict(SHORTCUTS_TABLE)["Ctrl+C"].lower()


# -- Ctrl+T is session-scoped ----------------------------------------------


def test_ctrl_t_does_not_write_to_the_config_file():
    from djcode import tui

    source = inspect.getsource(tui.register_keybindings)
    # Strip comments: the one that survives explains why set_value is gone.
    code = " ".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    assert "set_value" not in code, "a keybinding is rewriting config.json"


def test_ctrl_t_says_session_only_on_the_card():
    from djcode.tui import SHORTCUTS_TABLE

    assert "session" in dict(SHORTCUTS_TABLE)["Ctrl+T"].lower()


def test_ctrl_t_moves_the_permission_engine_with_it(monkeypatch, tmp_path):
    """The chokepoint's own `resolve` has no view of `operator.auto_accept`, so
    leaving the engine behind would make the toggle half-apply."""
    from types import SimpleNamespace

    from prompt_toolkit.key_binding import KeyBindings

    from djcode import tui
    from djcode.core.permissions import Mode, PermissionEngine
    from djcode.status import StatusBar

    engine = PermissionEngine(mode=Mode.MANUAL, load=False)
    operator = SimpleNamespace(
        auto_accept=False, permissions=engine, show_thinking=True, plan_mode=False
    )
    session = SimpleNamespace(key_bindings=None)
    bindings = tui.register_keybindings(session, operator, StatusBar())
    assert isinstance(bindings, KeyBindings)

    handler = next(
        b.handler
        for b in bindings.bindings
        if any(getattr(k, "value", str(k)) == "c-t" for k in b.keys)
    )
    event = SimpleNamespace(
        app=SimpleNamespace(output=SimpleNamespace(write=lambda _t: None, flush=lambda: None))
    )
    handler(event)
    assert operator.auto_accept is True
    assert engine.mode is Mode.AUTO
    handler(event)
    assert operator.auto_accept is False
    assert engine.mode is Mode.MANUAL


# -- the DAF fallback notice is reachable ----------------------------------


def test_the_engine_queues_its_fallback_notice_for_a_front_end():
    from djcode import workflow

    workflow._FALLBACK_NOTICES.clear()
    workflow._FALLBACK_NOTICES.append("test notice")
    assert workflow.drain_engine_notices() == ["test notice"]
    assert workflow.drain_engine_notices() == [], "a notice must only be shown once"


def test_the_fallback_path_puts_a_notice_in_the_queue():
    from djcode import workflow

    workflow._FALLBACK_ANNOUNCED = False
    workflow._FALLBACK_NOTICES.clear()
    engine = workflow.WorkflowEngine()
    engine._fall_back_to_native(RuntimeError("cargo not found"))
    notices = workflow.drain_engine_notices()
    assert notices and "native workflow engine" in notices[0]
    assert engine.mode == "native"


def test_the_notice_is_announced_once_not_every_call():
    from djcode import workflow

    workflow._FALLBACK_ANNOUNCED = False
    workflow._FALLBACK_NOTICES.clear()
    engine = workflow.WorkflowEngine()
    engine._fall_back_to_native(RuntimeError("x"))
    engine._fall_back_to_native(RuntimeError("x"))
    assert len(workflow.drain_engine_notices()) == 1


def test_the_repl_drains_engine_notices():
    from djcode import repl

    assert "drain_engine_notices" in inspect.getsource(repl.run_repl)


# -- the error renderer -----------------------------------------------------


def test_errors_carry_no_emoji():
    """DESIGN-CLI §9.4 rules emoji out of the surface: they are a font lottery,
    and on a cp1252 console they are a question mark."""
    import io

    from rich.console import Console

    from djcode.frontends.repl.errors import render_error

    console = Console(file=io.StringIO(), width=90, no_color=True, highlight=False)
    render_error(ConnectionError("refused"), console=console, provider="ollama")
    out = console.file.getvalue()
    for emoji in "\U0001f50c\U0001f9e0\U0001f511\U0001f6e0\U0001f4be❌❓":
        assert emoji not in out


def test_errors_name_the_provider_and_model():
    import io

    from rich.console import Console

    from djcode.frontends.repl.errors import render_error

    console = Console(file=io.StringIO(), width=90, no_color=True, highlight=False)
    render_error(ConnectionError("refused"), console=console, provider="ollama", model="gemma4")
    out = console.file.getvalue()
    assert "ollama" in out and "gemma4" in out


@pytest.mark.parametrize(
    "checkpoints,session_id,expected",
    [
        (None, "s1", "not tracked"),
        (object(), None, ""),
    ],
)
def test_disk_effect_degrades_honestly(checkpoints, session_id, expected):
    from djcode.frontends.repl.errors import describe_disk_effect

    result = describe_disk_effect(checkpoints, session_id)
    assert expected in result


def test_disk_effect_does_not_blame_this_turn_for_a_previous_turns_files():
    """`last_turn` returns the newest turn that touched disk. For a turn that
    touched nothing that is the PREVIOUS turn, and reporting its files here
    would tell the user their failed request changed things it never did."""
    from types import SimpleNamespace

    from djcode.frontends.repl.errors import describe_disk_effect

    store = SimpleNamespace(
        _turn_id="turn-2",
        last_turn=lambda _s: [
            SimpleNamespace(turn_id="turn-1", files=[SimpleNamespace(path="a.py")])
        ],
    )
    assert "Nothing was written" in describe_disk_effect(store, "s1")


def test_disk_effect_counts_this_turns_files():
    from types import SimpleNamespace

    from djcode.frontends.repl.errors import describe_disk_effect

    store = SimpleNamespace(
        _turn_id="turn-2",
        last_turn=lambda _s: [
            SimpleNamespace(turn_id="turn-2", files=[SimpleNamespace(path="a.py")]),
            SimpleNamespace(
                turn_id="turn-2",
                files=[SimpleNamespace(path="a.py"), SimpleNamespace(path="b.py")],
            ),
        ],
    )
    result = describe_disk_effect(store, "s1")
    assert "2 files changed" in result
    assert "/undo" in result


def test_disk_effect_never_raises_out_of_an_error_message():
    from types import SimpleNamespace

    from djcode.frontends.repl.errors import describe_disk_effect

    def explode(_s):
        raise RuntimeError("store is broken")

    store = SimpleNamespace(_turn_id="t", last_turn=explode)
    assert describe_disk_effect(store, "s1") == ""
