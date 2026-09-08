"""The classic interface remains usable without the removed companion."""
import asyncio
from io import StringIO

from prompt_toolkit.formatted_text import to_plain_text
from rich.console import Console


def test_classic_status_and_commands_without_companion(monkeypatch):
    from djcode import repl
    from djcode.status import StatusBar

    bar = StatusBar()
    bar.update(model="test-model", provider="openai", token_count=1200)
    rendered = to_plain_text(bar.render())
    assert "DJcode" in rendered and "test-model" in rendered and "1.2K" in rendered
    output = StringIO()
    monkeypatch.setattr(repl, "console", Console(file=output, color_system=None))
    assert asyncio.run(repl.handle_slash_command("/help", None, None, bar))
    assert "/buddy" not in output.getvalue()
    output.seek(0)
    output.truncate()
    assert asyncio.run(repl.handle_slash_command("/buddy", None, None, bar))
    assert "Unknown command" in output.getvalue()
