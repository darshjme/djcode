"""Shared command discovery for the full-screen TUI and line-oriented REPL."""

from __future__ import annotations

from dataclasses import dataclass

from prompt_toolkit.completion import Completer, Completion


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    group: str
    #: Listed by the line-oriented REPL.
    repl: bool = True
    #: Listed by the full-screen TUI. The symmetric counterpart of ``repl``:
    #: ``tui=False`` means the TUI has no handler for this command, and
    #: ``tests/test_execution.py::test_registry_has_dispatch_for_every_command``
    #: is the reason the flag has to be honest rather than the palette
    #: advertising a command that does nothing. Blueprint W5-4 defers the TUI
    #: undo surface ("Esc-Esc in the TUI is deferred with the TUI") and SSOT
    #: non-goal 8 puts the TUI in maintenance, so the undo family ships
    #: REPL-only in W5.
    tui: bool = True


COMMANDS = (
    Command("/connect", "Connect provider, sign in and select model", "Models & setup"),
    Command(
        "/workflow",
        "Default DAF/DDAL engine status or explicit engine selection",
        "Context & tools",
    ),
    Command("/skills", "Discover user skills", "Context & tools"),
    Command("/skill", "Load a skill by name", "Context & tools"),
    Command("/schedule", "Manage durable command schedules (JSON arguments)", "Context & tools"),
    Command("/jobs", "Inspect and control background jobs", "Context & tools"),
    Command("/browser", "Browser actions using JSON arguments", "Context & tools"),
    Command("/computer", "Desktop actions using JSON arguments", "Context & tools"),
    Command("/compact", "Compact the current model context", "Session"),
    Command("/session", "Show active session and workspace", "Session"),
    Command("/fork", "Branch into a new session with current context", "Session"),
    Command("/new", "Save and begin a fresh session", "Session"),
    Command("/queue", "Inspect queued follow-ups; clear removes them", "Session", repl=False),
    Command("/help", "Browse commands and keyboard shortcuts", "Session"),
    Command("/check", "Run runtime and source checks", "Models & setup"),
    Command("/lint", "Run runtime and source checks", "Models & setup"),
    Command("/update", "Update a managed installation", "Models & setup"),
    Command("/design", "List/select an original design reference; off clears", "Context & tools"),
    Command("/model", "Switch model (fuzzy match)", "Models & setup"),
    Command("/models", "Browse available models", "Models & setup"),
    Command("/provider", "Switch LLM provider", "Models & setup"),
    Command("/auth", "Choose provider and supported authentication", "Models & setup"),
    Command("/clear", "Clear conversation history", "Session"),
    Command("/save", "Save conversation to disk", "Session"),
    Command("/config", "Show current configuration", "Models & setup"),
    Command("/set", "Set a config value (key=value)", "Models & setup"),
    Command("/auto", "Toggle auto-accept tool calls", "Session"),
    Command("/thinking", "Toggle thinking display", "Session"),
    Command("/plan", "Toggle plan/act mode", "Session"),
    Command("/agents", "Show engineering and content profiles", "Specialists"),
    Command("/scout", "Read-only codebase exploration", "Specialists"),
    Command("/architect", "Generate implementation plan", "Specialists"),
    Command("/orchestra", "Multi-agent orchestration", "Specialists"),
    Command("/review", "Code review (Dharma agent)", "Specialists"),
    Command("/debug", "Root cause analysis (Sherlock)", "Specialists"),
    Command("/test", "Write tests (Agni agent)", "Specialists"),
    Command("/refactor", "Restructure code (Shiva)", "Specialists"),
    Command("/devops", "Docker/CI/CD (Vayu agent)", "Specialists"),
    Command("/docs", "Browse built-in docs or generate project documentation", "Specialists"),
    Command("/launch", "Build + Ship + Campaign pipeline", "Specialists"),
    Command("/campaign", "Content campaign (12 agents)", "Specialists"),
    Command("/image", "Image prompts (Maya)", "Specialists"),
    Command("/video", "Cinematic video prompts (Kubera)", "Specialists"),
    Command("/social", "Social media content (Chitragupta)", "Specialists"),
    Command("/memory", "Show memory stats", "Context & tools"),
    Command("/remember", "Store a persistent fact (key=value)", "Context & tools"),
    Command("/recall", "Recall a persistent fact", "Context & tools"),
    Command("/forget", "Remove a persistent fact", "Context & tools"),
    Command("/stats", "Usage dashboard", "Context & tools"),
    Command("/extension", "Manage MCP extensions", "Context & tools"),
    Command("/recipe", "List, inspect or run a recipe", "Context & tools"),
    Command("/history", "Browse past sessions", "Session"),
    Command("/resume", "Resume a past session by ID", "Session"),
    # W5 (P0-1). These go in the registry NOW, not in W9: this list is what
    # SlashCompleter and /help read, and it is the list W9's command registry
    # will be built from. A row added only to repl.py's if/elif chain is a row
    # W9 silently drops.
    Command(
        "/undo",
        "Revert the files the last turn changed (/undo <n> goes further back)",
        "Session",
        tui=False,
    ),
    Command("/redo", "Re-apply the most recent /undo", "Session", tui=False),
    # W7 (P0-3). Same reason as the undo family above: a row added only to
    # repl.py's if/elif chain is a row W9 silently drops, and this list is what
    # SlashCompleter and /help read. tui=False because the Textual TUI has no
    # handler for it and test_registry_has_dispatch_for_every_command checks.
    Command(
        "/diff",
        "Show what changed (session | uncommitted | branch [ref])",
        "Session",
        tui=False,
    ),
    Command(
        "/rewind",
        "Pick a past turn and roll the files back to before it",
        "Session",
        tui=False,
    ),
    Command("/uncensored", "Show uncensored model info", "Models & setup"),
    Command("/shortcuts", "Show keyboard shortcuts", "Session"),
    Command("/todo", "Manage session todos (add/done/rm/list)", "Context & tools", repl=False),
    Command("/cost", "Show token cost estimates", "Context & tools", repl=False),
    Command("/search", "Web search (DuckDuckGo/Brave)", "Context & tools", repl=False),
    Command("/tasks", "List/create/update session tasks", "Context & tools", repl=False),
    Command("/spawn", "Spawn a specialist agent", "Specialists", repl=False),
    Command("/context", "Show context window utilization", "Context & tools", repl=False),
    Command("/waves", "Run multi-agent wave execution", "Specialists", repl=False),
    Command("/cancel", "Cancel the active response or specialist", "Session", repl=False),
    Command("/exit", "Quit DJcode", "Session"),
)


def commands_for(interface: str = "tui") -> tuple[Command, ...]:
    if interface == "repl":
        return tuple(command for command in COMMANDS if command.repl)
    return tuple(command for command in COMMANDS if command.tui)


def match_commands(query: str, interface: str = "tui") -> list[Command]:
    """Rank command-name prefixes before matches in names and descriptions."""
    query = query.strip().lower().lstrip("/")
    commands = commands_for(interface)
    if not query:
        return list(commands)
    matches = [
        command
        for command in commands
        if all(word in f"{command.name} {command.description}".lower() for word in query.split())
    ]
    return sorted(
        matches,
        key=lambda command: (not command.name[1:].startswith(query), command.name != f"/{query}"),
    )


def command_groups(interface: str) -> dict[str, list[tuple[str, str]]]:
    groups: dict[str, list[tuple[str, str]]] = {}
    for command in commands_for(interface):
        groups.setdefault(command.group, []).append((command.name, command.description))
    return groups


def command_help(interface: str) -> str:
    sections = []
    for group, commands in command_groups(interface).items():
        lines = [f"[bold #FFD700]{group}[/]"]
        lines.extend(f"  [cyan]{name:<16}[/] {description}" for name, description in commands)
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def plan_blocks_command(command: str, argument: str) -> bool:
    """Keep task dispatch from bypassing the no-execution Plan mode."""
    if command == "/docs":
        from djcode.docs import DOCS_SECTIONS

        return bool(argument.strip()) and argument.strip().lower() not in {
            *DOCS_SECTIONS,
            "all",
        }
    if command == "/recipe":
        return argument.split(maxsplit=1)[:1] == ["run"]
    return command in {
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
    }


class SlashCompleter(Completer):
    """Complete command names only; never replace a command's arguments."""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or any(char.isspace() for char in text):
            return
        if document.text_after_cursor:
            return
        for command in match_commands(text, "repl"):
            yield Completion(
                command.name, start_position=-len(text), display_meta=command.description
            )
