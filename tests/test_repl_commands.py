"""The safety net for the REPL's command surface.

`tests/test_execution.py::test_registry_has_dispatch_for_every_command` covers
the Textual TUI's dispatch. Nothing covered the REPL's, so a rewrite of
`handle_slash_command` could silently drop a command and the suite would stay
green. These tests are written against the *current* if/elif chain on purpose:
they are the contract any future table-driven dispatcher has to keep.
"""

from __future__ import annotations

import _thread
import ast
import asyncio
import inspect
import signal
import textwrap
import threading

import pytest

from djcode.commands import COMMANDS, commands_for, plan_blocks_command
from djcode.session_commands import NAMES


def _dispatched_literals() -> set[str]:
    """Every "/..." string constant `handle_slash_command` compares against."""
    from djcode import repl

    tree = ast.parse(textwrap.dedent(inspect.getsource(repl.handle_slash_command)))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("/")
    }


def test_every_repl_command_reaches_a_handler():
    dispatched = _dispatched_literals()
    missing = [
        command.name
        for command in commands_for("repl")
        if command.name not in dispatched and command.name not in NAMES
    ]
    assert not missing, f"registry rows with no REPL handler: {missing}"


def test_repl_only_aliases_stay_reachable():
    """`/q` and `/quit` have no registry row, so only this test protects them."""
    dispatched = _dispatched_literals()
    assert {"/q", "/quit"} <= dispatched


def test_no_handler_dispatches_a_command_the_registry_does_not_list():
    """Catches a typo'd branch and a command the completer can never offer."""
    known = {command.name for command in COMMANDS} | set(NAMES) | {"/q", "/quit"}
    stray = {name for name in _dispatched_literals() if name not in known}
    assert not stray, f"dispatched but not in the registry: {sorted(stray)}"


def test_repl_excluded_commands_are_the_tui_only_set():
    """The port list for W9. If a row moves, this test is the record of it."""
    assert {command.name for command in COMMANDS if not command.repl} == {
        "/cancel",
        "/context",
        "/cost",
        "/queue",
        "/search",
        "/spawn",
        "/tasks",
        "/todo",
        "/waves",
    }


@pytest.mark.parametrize(
    "command",
    [
        "/orchestra",
        "/review",
        "/debug",
        "/test",
        "/refactor",
        "/devops",
        "/launch",
        "/campaign",
        "/image",
        "/video",
        "/social",
        "/spawn",
        "/waves",
    ],
)
def test_plan_mode_blocks_task_dispatch(command):
    assert plan_blocks_command(command, "")


@pytest.mark.parametrize("command", ["/undo", "/redo", "/rewind", "/diff"])
def test_plan_mode_never_blocks_recovery_or_inspection(command):
    """Undo and diff change or read local files, not the outside world; plan
    mode is exactly when you want them."""
    assert not plan_blocks_command(command, "")
    assert not plan_blocks_command(command, "3")


def test_plan_mode_special_cases():
    assert plan_blocks_command("/recipe", "run build")
    assert not plan_blocks_command("/recipe", "list")
    assert not plan_blocks_command("/docs", "")
    assert plan_blocks_command("/docs", "generate the project docs")


def test_ctrl_c_cancels_the_operation_without_killing_the_loop():
    """Regression: on Windows `loop.add_signal_handler` raises
    NotImplementedError, the helper swallowed it and installed nothing, and the
    KeyboardInterrupt escaped `asyncio.run` -- Ctrl+C quit DJcode instead of
    cancelling one turn."""
    from djcode.repl_runtime import run_interruptible

    observed = {}

    async def slow():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            observed["cancelled"] = True
            raise
        return "ran to completion"

    async def main():
        before = signal.getsignal(signal.SIGINT)
        timer = threading.Timer(0.3, _thread.interrupt_main)
        timer.start()
        try:
            result = await run_interruptible(slow())
        finally:
            timer.cancel()
        assert signal.getsignal(signal.SIGINT) is before, "signal handler not restored"
        return result

    assert asyncio.run(main()) is None
    assert observed.get("cancelled") is True


def test_run_interruptible_returns_the_value_when_nothing_interrupts():
    from djcode.repl_runtime import run_interruptible

    async def quick():
        return "value"

    async def main():
        return await run_interruptible(quick())

    assert asyncio.run(main()) == "value"


def test_external_cancellation_still_propagates():
    """`None` means "the user pressed Ctrl+C". A cancel from anywhere else must
    stay an exception or callers cannot tell the two apart."""
    from djcode.repl_runtime import run_interruptible

    async def main():
        async def slow():
            await asyncio.sleep(30)

        task = asyncio.create_task(run_interruptible(slow()))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
