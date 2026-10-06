"""Searchable provider/model choices backed by bounded live discovery."""
from __future__ import annotations

import asyncio
from copy import deepcopy

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Select, Static
from textual.widgets.option_list import Option

from djcode.config import load_config
from djcode.model_selection import capability_summary, model_catalog, provider_choices


class ModelPicker(ModalScreen[dict | None]):
    """Return a choice only; the application validates and saves successful use."""
    BINDINGS = [("escape", "dismiss", "Close")]
    DEFAULT_CSS = """
    ModelPicker { align: center middle; background: rgba(0,0,0,0.75); }
    #model-box { width: 76; max-width: 95%; height: 90%; max-height: 36;
        background: #141414; border: round #7C96FF; padding: 0 1; }
    #model-title { height: 1; color: #EEEAE3; text-style: bold; }
    #model-provider { height: 3; }
    #model-search { height: 3; border: round #55514A; color: #EEEAE3; }
    #model-search:focus { border: round #7C96FF; }
    #model-info { height: auto; max-height: 2; color: #A6A4A0; }
    #model-list { height: 1fr; min-height: 1; background: #101010; }
    #model-capabilities { height: auto; max-height: 2; color: #A6A4A0; }
    #model-keys { height: 1; color: #7C96FF; }
    #model-list > .option-list--option-highlighted { background: #7C96FF 20%; color: #7C96FF; }
    """

    def __init__(self, provider_name: str = "ollama", base_url: str = "", config: dict | None = None) -> None:
        super().__init__()
        self._config = deepcopy(config if config is not None else load_config())
        self._provider_name = provider_name
        self._base_url = base_url
        self._models = []
        self._generation = 0
        self._loaded = False
        self._recent = [item.get("model") for item in self._config.get("recent_model_selections", [])
                        if isinstance(item, dict) and item.get("provider") == provider_name]

    def compose(self) -> ComposeResult:
        choices = provider_choices(self._config)
        if self._provider_name not in {item["id"] for item in choices}:
            self._provider_name = choices[0]["id"]
        with Vertical(id="model-box"):
            yield Static("Models · live provider catalog", id="model-title", markup=False)
            yield Select([(item["name"], item["id"]) for item in choices], value=self._provider_name,
                         allow_blank=False, id="model-provider")
            yield Input(id="model-search", placeholder="Search model ID or name")
            yield Static("Discovering models…", id="model-info", markup=False)
            yield OptionList(id="model-list")
            yield Static("Capabilities not reported by this provider", id="model-capabilities", markup=False)
            yield Static("↑↓ choose · Enter select · Shift+Tab provider · Esc close", id="model-keys", markup=False)

    def on_mount(self) -> None:
        self._loaded = True
        self.query_one("#model-search", Input).focus()
        self.run_worker(self._load_models(), group="model-discovery", exclusive=True)

    def on_key(self, event) -> None:
        if event.key in {"up", "down"} and isinstance(self.focused, Input):
            options = self.query_one("#model-list", OptionList)
            if options.option_count:
                options.action_cursor_down() if event.key == "down" else options.action_cursor_up()
            event.prevent_default()
            event.stop()

    @on(Select.Changed, "#model-provider")
    def change_provider(self, event: Select.Changed) -> None:
        if not self._loaded or event.value == self._provider_name or event.value is Select.BLANK:
            return
        self._provider_name = str(event.value)
        self._base_url = ""
        self._models = []
        self._recent = [item.get("model") for item in self._config.get("recent_model_selections", [])
                        if isinstance(item, dict) and item.get("provider") == self._provider_name]
        self.query_one("#model-search", Input).value = ""
        self.query_one("#model-info", Static).update("Discovering models…")
        self._render_models("")
        self.run_worker(self._load_models(), group="model-discovery", exclusive=True)

    async def _load_models(self) -> None:
        self._generation += 1
        generation, provider = self._generation, self._provider_name
        cfg = deepcopy(self._config)
        if self._base_url:
            cfg["provider"] = provider
            cfg["base_url"] = self._base_url
        found = await asyncio.to_thread(model_catalog, cfg, provider)
        if generation != self._generation or provider != self._provider_name or not self.is_mounted:
            return
        self._models = found["models"]
        if self._models:
            status = f"{len(self._models)} models · {found['source']} catalog"
            if found.get("selected"):
                status += f" · current {found['selected']}"
        else:
            status = found["message"] + " F3 connects a provider."
        self.query_one("#model-info", Static).update(status)
        self._render_models(self.query_one("#model-search", Input).value)

    def _render_models(self, query: str) -> None:
        options = self.query_one("#model-list", OptionList)
        options.clear_options()
        words = query.casefold().strip().split()
        matches = [m for m in self._models if all(word in f"{m.get('id', m.get('name', ''))} {m.get('name', '')}".casefold() for word in words)]
        matches.sort(key=lambda m: m.get("id", m.get("name")) not in self._recent)
        for model in matches:
            ident = model.get("id", model.get("name", ""))
            label = ident
            if model.get("name") != ident:
                label += f" · {model['name']}"
            if ident in self._recent:
                label = "Recent · " + label
            options.add_option(Option(Text(label), id=ident))
        if not matches:
            options.add_option(Option(Text("No matching models" if self._models else "No models available · reconnect with F3"), disabled=True))
        options.highlighted = 0
        self._update_capabilities()

    @on(Input.Changed, "#model-search")
    def filter_models(self, event: Input.Changed) -> None:
        self._render_models(event.value)

    @on(OptionList.OptionHighlighted, "#model-list")
    def highlight_model(self) -> None:
        self._update_capabilities()

    def _update_capabilities(self) -> None:
        options = self.query_one("#model-list", OptionList)
        ident = options.get_option_at_index(options.highlighted).id if options.highlighted is not None and options.option_count else None
        model = next((m for m in self._models if m.get("id", m.get("name")) == ident), {})
        self.query_one("#model-capabilities", Static).update(capability_summary(model))

    def _select(self, option: Option) -> None:
        if not option.disabled and option.id:
            self.dismiss({"provider": self._provider_name, "model": str(option.id)})

    @on(OptionList.OptionSelected, "#model-list")
    def select_model(self, event: OptionList.OptionSelected) -> None:
        self._select(event.option)

    @on(Input.Submitted, "#model-search")
    def submit_search(self) -> None:
        options = self.query_one("#model-list", OptionList)
        if options.highlighted is not None and options.option_count:
            self._select(options.get_option_at_index(options.highlighted))

    def _save_recent(self, model: str) -> None:
        """Compatibility hook: successful selection is persisted by the app."""
