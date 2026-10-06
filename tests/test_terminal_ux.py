"""Terminal presentation must remain navigable and reflect measured state."""

import asyncio

import pytest
from rich.cells import cell_len
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Label, Static, Switch

from djcode.app import DJcodeApp
from djcode.tui_hacker import AgentStatusBar, HackerHeader
from djcode.tui_panels import CostPanel, IntelPanel, MCPPanel, TodoPanel


def content(widget: Static) -> str:
    return str(widget.render())


@pytest.mark.parametrize("width", [40, 60, 80, 120])
def test_header_resize_preserves_mode_and_context_state(monkeypatch, tmp_path, width):
    monkeypatch.chdir(tmp_path)

    async def initialize(self):
        pass

    monkeypatch.setattr(DJcodeApp, "_initialize", initialize)

    async def run():
        app = DJcodeApp()
        async with app.run_test(size=(width, 24)) as pilot:
            header = app.query_one(HackerHeader)
            header.model_name = "模型-" * 80 + "[bold]provider[/]"
            await pilot.pause()
            display = content(header.query_one(Static))
            assert "DJcode" in display and "ACT" in display
            assert cell_len(display) <= width - 2
            assert "0%" not in display
            await pilot.press("ctrl+g")
            assert "PLAN" in content(header.query_one(Static))
            header.update_context(350, 1000)
            await pilot.pause()
            if width >= 70:
                assert "Context 35%" in content(header.query_one(Static))
            header.update_context(350, 0)
            await pilot.pause()
            if width >= 70:
                assert "Context --" in content(header.query_one(Static))

    asyncio.run(run())


class PanelFixture(App):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel

    def compose(self) -> ComposeResult:
        yield self.panel


def test_cost_unknown_and_explicit_zero_rates_are_distinct():
    async def run():
        panel = CostPanel()
        async with PanelFixture(panel).run_test(size=(48, 18)) as pilot:
            panel.update_cost(tokens_in=1500, tokens_out=2000)
            await pilot.pause()
            assert "not estimated" in content(panel.query_one("#cost-dollars-total", Static))
            panel.update_cost(cost_per_1k_in=0.0, cost_per_1k_out=0.01)
            await pilot.pause()
            assert "$0.0200" in content(panel.query_one("#cost-dollars-total", Static))
            assert isinstance(panel, VerticalScroll)
            assert panel.max_scroll_y > 0
            panel.focus()
            await pilot.press("end")
            await pilot.pause()
            assert panel.scroll_y > 0

    asyncio.run(run())


def test_todo_identifiers_literal_markup_and_empty_count():
    async def run():
        panel = TodoPanel()
        async with PanelFixture(panel).run_test(size=(32, 18)) as pilot:
            todo_id = panel.add_todo("[red]literal[/red] with a long wrapped task")
            await pilot.pause()
            label = panel.query_one(".todo-label", Label)
            assert content(label).startswith("1. [red]literal[/red]")
            assert label.size.height > 1
            panel.toggle_todo(todo_id)
            panel.remove_todo(todo_id)
            await pilot.pause()
            count = content(panel.query_one("#todo-count-display", Static))
            assert "0 / 0 done" in count
            assert "No objectives" in content(panel.query_one(".todo-empty", Static))

    asyncio.run(run())


def test_extensions_use_real_command_and_read_only_state():
    async def run():
        panel = MCPPanel()
        async with PanelFixture(panel).run_test(size=(60, 20)) as pilot:
            await pilot.pause()
            assert "/extension add" in content(panel.query_one(".mcp-empty", Static))
            panel.load_extensions([{"name": "[test] ext name", "connected": True,
                                    "enabled": False, "tools_count": 3}])
            await pilot.pause()
            assert len(panel.query(Switch)) == 0
            assert content(panel.query_one(".ext-name", Label)) == "[test] ext name"
            assert content(panel.query_one(".ext-enabled", Static)) == "off"
            panel.load_extensions([])
            await pilot.pause()
            assert "0 connected" in content(panel.query_one("#mcp-summary-display", Static))

    asyncio.run(run())


def test_intel_reports_unknown_context_and_received_alerts():
    async def run():
        panel = IntelPanel()
        async with PanelFixture(panel).run_test(size=(48, 18)) as pilot:
            await pilot.pause()
            assert "unavailable" in content(panel.query_one("#intel-context-bar", Static))
            assert "monitoring" not in content(panel.query_one("#intel-sentinel-line", Static))
            assert "continuous security monitor" in content(panel.query_one("#intel-threat-scroll Static", Static))
            panel.add_threat("[test]", "warning", "[red]must remain literal[/red]")
            await pilot.pause()
            text = content(panel.query_one("#intel-threat-scroll Static", Static))
            assert "[test]" in text and "[red]must remain literal[/red]" in text
            panel.update_context(20, 100)
            assert "20%" in content(panel.query_one("#intel-context-bar", Static))

    asyncio.run(run())


def test_agent_profiles_ready_are_not_active_workers():
    async def run():
        bar = AgentStatusBar()
        async with PanelFixture(bar).run_test(size=(40, 10)) as pilot:
            assert "agent profiles" in content(bar.query_one(Static))
            names = list(bar._states)
            bar.set_agent_state(names[0], "ready")
            assert bar.get_active_count() == 0
            for name in names:
                bar.set_agent_state(name, "executing")
            await pilot.pause()
            text = content(bar.query_one(Static))
            assert ("active" in text or "executing" in text) and "F5" in text
            assert cell_len(text) <= 38
            bar.set_all_idle()
            assert bar.get_active_count() == 0

    asyncio.run(run())
