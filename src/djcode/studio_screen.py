"""Keyboard-accessible editor for project definitions."""

import json

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, TextArea

from djcode.studio import Studio

TEMPLATES = {
    "agent": {
        "name": "my-coder",
        "role": "coder",
        "prompt": "Implement focused changes. Read existing code and verify your work.",
        "tools": ["file_read", "file_edit", "file_write", "grep", "glob", "bash", "git"],
    },
    "organisation": {"name": "my-team", "agents": ["my-coder"]},
    "flow": {
        "name": "my-workflow",
        "organisation": "my-team",
        "concurrency": 1,
        "nodes": [
            {
                "id": "implement",
                "agent": "my-coder",
                "task": "Describe the change to make",
                "dependencies": [],
            }
        ],
    },
    "roadmap": {
        "name": "first-release",
        "description": "Describe the intended outcome",
        "status": "planned",
        "target_date": "",
    },
}


class StudioScreen(ModalScreen[str | None]):
    DEFAULT_CSS = """
    StudioScreen { align: center middle; background: #080808 80%; }
    #studio-dialog { width: 96%; max-width: 110; height: 96%; padding: 0 1;
        background: #111112; border: round #7C96FF; }
    #studio-title { height: 1; color: #7C96FF; text-style: bold; }
    #studio-fields { height: 3; }
    #studio-kind { width: 16; }
    #studio-name { width: 1fr; }
    #studio-editor { height: 1fr; min-height: 4; margin: 0; }
    #studio-help { height: 1; color: #A0A0A5; }
    #studio-actions { height: 3; }
    #studio-actions { grid-size: 5; grid-rows: 3; }
    #studio-actions Button { width: 1fr; min-width: 7; padding: 0; }
    #studio-error { height: 1; width: 1fr; color: #F4A7A7; }
    """
    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("ctrl+s", "save_definition", "Save", priority=True),
        Binding("ctrl+n", "template", "Template", priority=True),
        Binding("ctrl+r", "load_definition", "Load", priority=True),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="studio-dialog"):
            yield Label("PROJECT STUDIO", id="studio-title")
            with Horizontal(id="studio-fields"):
                yield Select(
                    [(name.title(), name) for name in TEMPLATES],
                    value="agent",
                    allow_blank=False,
                    id="studio-kind",
                )
                yield Input(placeholder="Saved name · load/run", id="studio-name")
            yield Label("Ctrl+S save · Tab focus · Esc close", id="studio-help")
            yield TextArea(json.dumps(TEMPLATES["agent"], indent=2), id="studio-editor")
            yield Label("", id="studio-error", markup=False)
            with Grid(id="studio-actions"):
                yield Button("Template", id="studio-template")
                yield Button("Load", id="studio-load")
                yield Button("Save", id="studio-save", variant="primary")
                yield Button("Run flow", id="studio-run", disabled=True)
                yield Button("Close", id="studio-close")

    def action_close(self):
        self.dismiss(None)

    def on_mount(self):
        self._resize_actions(self.size.width, self.size.height)
        self.query_one("#studio-editor", TextArea).focus()

    def on_resize(self, event):
        if self.is_mounted:
            self._resize_actions(event.size.width, event.size.height)

    def _resize_actions(self, width, height):
        actions = self.query_one("#studio-actions", Grid)
        actions.styles.grid_size_columns = 3 if width < 70 else 5
        actions.styles.height = 6 if width < 70 else 3
        self.query_one("#studio-help").display = height >= 22

    @on(Select.Changed, "#studio-kind")
    def change_kind(self, event):
        if event.value not in TEMPLATES:
            return
        editor = self.query_one("#studio-editor", TextArea)
        try:
            clean_template = json.loads(editor.text) in TEMPLATES.values()
        except ValueError:
            clean_template = False
        if clean_template:
            editor.load_text(json.dumps(TEMPLATES[event.value], indent=2))
        else:
            self.query_one("#studio-error", Label).update("Draft retained. Template replaces the editor.")
        self._update_run()

    @on(Input.Changed, "#studio-name")
    def change_name(self):
        self._update_run()

    def _update_run(self):
        self.query_one("#studio-run", Button).disabled = (
            self.query_one("#studio-kind", Select).value != "flow"
            or not self.query_one("#studio-name", Input).value.strip()
        )

    def action_save_definition(self):
        self._operate("studio-save")

    def action_load_definition(self):
        self._operate("studio-load")

    def action_template(self):
        self._operate("studio-template")

    def on_button_pressed(self, event: Button.Pressed):
        self._operate(event.button.id)

    def _operate(self, action):
        kind = self.query_one("#studio-kind", Select).value
        editor = self.query_one("#studio-editor", TextArea)
        name = self.query_one("#studio-name", Input).value.strip()
        try:
            match action:
                case "studio-close":
                    self.dismiss(None)
                case "studio-template":
                    editor.load_text(json.dumps(TEMPLATES[kind], indent=2))
                    self.query_one("#studio-error", Label).update("")
                case "studio-load":
                    if not name:
                        raise ValueError("Enter the saved definition name first.")
                    editor.load_text(json.dumps(Studio().get(kind, name), indent=2))
                    self.query_one("#studio-error", Label).update("")
                case "studio-save":
                    data = json.loads(editor.text)
                    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
                        raise ValueError("Definition must be a JSON object with a name.")
                    self.dismiss(f"/{kind} save " + json.dumps(data))
                case "studio-run":
                    if kind != "flow" or not name:
                        raise ValueError("Select Flow and enter a saved workflow name to run.")
                    self.dismiss("/flow run " + name)
        except (ValueError, KeyError) as error:
            self.query_one("#studio-error", Label).update(str(error))
