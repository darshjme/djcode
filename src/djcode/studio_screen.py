"""Keyboard-accessible editor for project definitions."""

import json

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
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
    #studio-dialog { width: 90%; max-width: 110; height: 90%; padding: 1 2;
        background: #111112; border: round #7C96FF; }
    #studio-editor { height: 1fr; margin: 1 0; }
    #studio-actions { height: 3; }
    #studio-actions Button { min-width: 8; margin-right: 1; }
    #studio-error { height: auto; max-height: 3; color: #F4A7A7; }
    """
    BINDINGS = [("escape", "close", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="studio-dialog"):
            yield Label("PROJECT STUDIO · agents, organisations, workflows and roadmap")
            yield Select(
                [(name.title(), name) for name in TEMPLATES],
                value="agent",
                allow_blank=False,
                id="studio-kind",
            )
            yield Input(placeholder="Existing definition name to load", id="studio-name")
            yield TextArea(json.dumps(TEMPLATES["agent"], indent=2), id="studio-editor")
            yield Label(
                "Save definitions explicitly. Run uses normal tool approvals.", id="studio-error"
            )
            with Horizontal(id="studio-actions"):
                yield Button("Template", id="studio-template")
                yield Button("Load", id="studio-load")
                yield Button("Save", id="studio-save", variant="primary")
                yield Button("Run flow", id="studio-run")
                yield Button("Close", id="studio-close")

    def action_close(self):
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed):
        kind = self.query_one("#studio-kind", Select).value
        editor = self.query_one("#studio-editor", TextArea)
        name = self.query_one("#studio-name", Input).value.strip()
        try:
            match event.button.id:
                case "studio-close":
                    self.dismiss(None)
                case "studio-template":
                    editor.load_text(json.dumps(TEMPLATES[kind], indent=2))
                case "studio-load":
                    editor.load_text(json.dumps(Studio().get(kind, name), indent=2))
                case "studio-save":
                    data = json.loads(editor.text)
                    self.dismiss(f"/{kind} save " + json.dumps(data))
                case "studio-run":
                    if kind != "flow" or not name:
                        raise ValueError("Select Flow and enter a saved workflow name to run.")
                    self.dismiss("/flow run " + name)
        except (ValueError, KeyError) as error:
            self.query_one("#studio-error", Label).update(str(error))
