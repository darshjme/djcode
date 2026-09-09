"""Bundled references reach the active prompt without changing tool protocol/history."""
from types import SimpleNamespace

from click.testing import CliRunner
import pytest

from djcode.cli import main
from djcode.provider import Message


def test_select_replace_clear_preserves_system_and_tool_protocol():
    from djcode.design_selection import select_pack
    system = Message(role="system", content="Original permissions and rules.")
    calls = Message(role="assistant", content="", tool_calls=[{"id": "call-1", "type": "function", "function": {"name": "file_read", "arguments": "{}"}}])
    result = Message(role="tool", content="file", tool_call_id="call-1")
    operator = SimpleNamespace(messages=[system, calls, result])
    assert "selected" in select_pack(operator, "dashboard")
    assert "Original permissions and rules." in system.content
    select_pack(operator, "settings")
    assert system.content.count("[DJCODE_DESIGN_REFERENCE]") == 1
    assert operator.messages[1:] == [calls, result]
    assert result.tool_call_id == "call-1"
    select_pack(operator, "off")
    assert system.content == "Original permissions and rules."


def test_invalid_selection_preserves_existing_context():
    from djcode.design_selection import select_pack
    operator = SimpleNamespace(messages=[Message(role="system", content="Rules")])
    select_pack(operator, "dashboard")
    before = operator.messages[0].content
    with pytest.raises(ValueError):
        select_pack(operator, "../../config")
    assert operator.messages[0].content == before


def test_offline_list_read_export_and_existing_directory_preserved(tmp_path):
    runner = CliRunner()
    listed = runner.invoke(main, ["--design-packs"])
    assert listed.exit_code == 0
    assert "command-palette" in listed.output
    read = runner.invoke(main, ["--design-pack", "dashboard"])
    assert read.exit_code == 0
    assert "dashboard" in read.output.lower()
    destination = tmp_path / "original-example"
    exported = runner.invoke(main, ["--design-pack", "dashboard", "--design-export", str(destination)])
    assert exported.exit_code == 0, exported.output
    original = (destination / "dashboard.md").read_bytes()
    assert (destination / "dashboard.svg").is_file()
    again = runner.invoke(main, ["--design-pack", "dashboard", "--design-export", str(destination)])
    assert again.exit_code != 0
    assert (destination / "dashboard.md").read_bytes() == original


def test_reference_reaches_one_shot_prompt(monkeypatch):
    from djcode import repl, startup
    from djcode.design_packs import get_pack
    captured = []

    async def run(prompt, **kwargs):
        captured.append(prompt)

    monkeypatch.setattr(startup, "prepare", lambda *a, **k: (None, None))
    monkeypatch.setattr(repl, "run_oneshot", run)
    result = CliRunner().invoke(main, ["--no-update", "--design-pack", "settings", "Build my preferences page"])
    assert result.exit_code == 0, result.output
    assert "Build my preferences page" in captured[0]
    assert get_pack("settings") in captured[0]


def test_missing_export_pack_is_actionable(tmp_path):
    result = CliRunner().invoke(main, ["--design-export", str(tmp_path / "new")])
    assert result.exit_code != 0
    assert "requires --design-pack" in result.output
    assert not (tmp_path / "new").exists()


def test_native_and_classic_selection_share_the_live_prompt(monkeypatch, tmp_path):
    import asyncio
    from djcode.app import DJcodeApp
    from djcode.repl import handle_slash_command
    monkeypatch.chdir(tmp_path)

    async def initialize(self):
        self._operator = SimpleNamespace(messages=[Message(role="system", content="Rules")])

    monkeypatch.setattr(DJcodeApp, "_initialize", initialize)

    async def exercise():
        app = DJcodeApp()
        async with app.run_test(size=(80, 28)) as pilot:
            await pilot.pause()
            await app._handle_slash_command("/design data-table")
            assert "[DJCODE_DESIGN_REFERENCE]" in app._operator.messages[0].content
            await app._handle_slash_command("/design off")
            assert app._operator.messages[0].content == "Rules"
            assert await handle_slash_command("/design settings", app._operator, None, None)
            assert "[DJCODE_DESIGN_REFERENCE]" in app._operator.messages[0].content
            await handle_slash_command("/design off", app._operator, None, None)
            assert app._operator.messages[0].content == "Rules"

    asyncio.run(exercise())
