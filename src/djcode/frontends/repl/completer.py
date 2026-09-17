"""Completion for the REPL: commands, their subcommands, their arguments, and @paths.

`djcode/commands.py::SlashCompleter` completes command NAMES and then stops --
its docstring says so explicitly ("never replace a command's arguments"). So
`/diff ` offered nothing, and the three words it accepts (`session`,
`uncommitted`, `branch`) were discoverable only by reading the source or
guessing. Same for `/recipe`, `/set`, `/docs`, `/undo` and the rest.

This replaces it with four layers:

1. **command** -- `/re` offers `/resume /redo /rewind /recipe`, ranked by
   `commands.match_commands` (which already put prefix matches first; nothing
   about that ranking changes).
2. **subcommand** -- `/diff ` offers its three modes, `/recipe ` its five verbs.
   Static, from a table, because these are the command's grammar rather than
   its data.
3. **argument** -- `/docs ` offers the real section names, `/set ` the real
   config keys, `/recipe run ` the recipes actually on disk. Computed live, so
   a completion can never advertise something that is not there.
4. **@path** -- `@src/dj` completes to a real path anywhere in the line,
   including mid-sentence in an ordinary prompt. This is the biggest single
   ergonomic win on Windows, where paths are long and tab-completion is the
   only sane way to type one.

It also lets the **bare-`/` picker** be deleted. That picker ran
`questionary.select(...).ask()` on a thread-pool thread while the main
`PromptSession` was still alive -- two readers on one stdin, which is GAP B22.
With `/` offering the same list inline there is nothing left for it to do.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

from prompt_toolkit.completion import Completer, Completion

from djcode.commands import COMMANDS, match_commands

__all__ = ["DjcodeCompleter", "SUBCOMMANDS", "path_candidates"]

#: A command's grammar: the fixed words it accepts after its name. Sourced from
#: each command's own handler in `repl.py`, not invented here -- a completion
#: that offers a word the handler does not accept is worse than no completion.
SUBCOMMANDS: dict[str, tuple[tuple[str, str], ...]] = {
    "/diff": (
        ("session", "everything this session changed"),
        ("uncommitted", "working tree against HEAD"),
        ("branch", "this branch against its base"),
    ),
    "/recipe": (
        ("list", "show available recipes"),
        ("show", "print one recipe"),
        ("run", "run a recipe"),
        ("create", "define a new recipe"),
        ("delete", "remove a recipe"),
    ),
    "/memory": (
        ("stats", "what memory holds"),
        ("clear", "forget everything"),
    ),
    "/extension": (
        ("list", "installed MCP extensions"),
        ("add", "install one"),
        ("remove", "uninstall one"),
    ),
    "/workflow": (
        ("status", "which engine is live"),
        ("daf", "force the Rust engine"),
        ("native", "force the Python engine"),
    ),
}


def _config_keys() -> Iterable[tuple[str, str]]:
    from djcode.config import DEFAULT_CONFIG, load_config

    current = load_config()
    for key in sorted(DEFAULT_CONFIG):
        value = current.get(key, DEFAULT_CONFIG[key])
        shown = "***" if "key" in key.lower() or "token" in key.lower() else repr(value)
        yield f"{key}=", f"currently {shown}"


def _docs_sections() -> Iterable[tuple[str, str]]:
    from djcode.docs import DOCS_SECTIONS

    for name in sorted(DOCS_SECTIONS):
        yield name, "built-in documentation"
    yield "all", "every section"


def _recipe_names() -> Iterable[tuple[str, str]]:
    try:
        from djcode.recipes import RecipeManager

        for recipe in RecipeManager().list_recipes():
            yield getattr(recipe, "name", str(recipe)), getattr(recipe, "description", "")
    except Exception:  # pragma: no cover - completion must never raise
        return


def _theme_names() -> Iterable[tuple[str, str]]:
    from djcode.frontends.repl.theme import PALETTES

    for name in PALETTES:
        yield name, "colour palette"
    yield "auto", "detect from the terminal"


def _set_values(key: str) -> Iterable[tuple[str, str]]:
    """What the RIGHT of `/set key=` can be, when the answer is knowable.

    Only for keys with a closed set of legal values. Offering a guess for
    `base_url` would be worse than offering nothing.
    """
    from djcode.config import DEFAULT_CONFIG

    if key == "theme":
        yield from _theme_names()
        return
    if key == "permission_mode":
        from djcode.core.permissions import Mode

        for mode in Mode:
            yield str(mode.value), "approval mode"
        return
    if isinstance(DEFAULT_CONFIG.get(key), bool):
        yield "true", ""
        yield "false", ""


#: Argument suggestions computed at completion time. Keyed by the full prefix
#: typed so far, so `/recipe run ` and `/recipe show ` can differ.
DYNAMIC: dict[str, object] = {
    "/set": _config_keys,
    "/docs": _docs_sections,
    "/recipe run": _recipe_names,
    "/recipe show": _recipe_names,
    "/recipe delete": _recipe_names,
}


def path_candidates(fragment: str, cwd: str | None = None, limit: int = 40) -> list[str]:
    """Real filesystem entries matching ``fragment``, directories first.

    Always returns forward slashes: they work in every shell DJcode talks to,
    they survive being pasted into a prompt, and a backslash in a model-facing
    string is an escape waiting to happen.
    """
    base = cwd or os.getcwd()
    fragment = fragment.replace("\\", "/")
    directory, _, prefix = fragment.rpartition("/")
    root = os.path.join(base, directory) if directory else base
    try:
        entries = list(os.scandir(root))
    except OSError:
        return []
    out: list[tuple[int, str]] = []
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        if entry.name.startswith(".") and not prefix.startswith("."):
            continue
        name = entry.name + "/" if entry.is_dir() else entry.name
        out.append((0 if entry.is_dir() else 1, f"{directory}/{name}" if directory else name))
    out.sort()
    return [name for _, name in out[:limit]]


class DjcodeCompleter(Completer):
    """The REPL's completer. See the module docstring for the four layers."""

    def __init__(self, cwd: str | None = None) -> None:
        self._cwd = cwd

    # -- layers ------------------------------------------------------------

    def _complete_at(self, text: str):
        """Yield ``(value, display_meta, start_position)`` for the line so far."""
        # Layer 4 first: `@path` is legal anywhere, including inside prose.
        at = text.rfind("@")
        if at != -1 and not text[at + 1 :].strip().startswith("/"):
            fragment = text[at + 1 :]
            if " " not in fragment and "\t" not in fragment:
                for candidate in path_candidates(fragment, self._cwd):
                    yield candidate, "path", -len(fragment)
                return

        if not text.startswith("/"):
            return

        parts = text.split()
        typing_new_word = text.endswith((" ", "\t"))

        # Layer 1: the command name itself.
        if len(parts) == 1 and not typing_new_word:
            for command in match_commands(parts[0], "repl"):
                yield command.name, command.description, -len(parts[0])
            return

        command_name = parts[0].lower()
        if command_name not in {c.name for c in COMMANDS} | {"/q", "/quit"}:
            return

        typed = "" if typing_new_word else parts[-1]
        prefix = " ".join(parts[: -1 if not typing_new_word else len(parts)])

        # `/set key=` is more specific still: the key is already chosen and
        # what is being completed is its VALUE, so this has to run before the
        # key list, which would otherwise match "theme=" against "theme=" and
        # stop there.
        if command_name == "/set" and "=" in typed:
            key, _, partial = typed.partition("=")
            for value, meta in _set_values(key):
                if value.startswith(partial):
                    yield f"{key}={value}", meta, -len(typed)
            return

        # Layer 3 before layer 2: `/recipe run <name>` is more specific than
        # `/recipe <verb>`, and the more specific match wins.
        provider = DYNAMIC.get(prefix)
        if provider is not None:
            for value, meta in provider():  # type: ignore[operator]
                if value.startswith(typed):
                    yield value, meta, -len(typed)
            return

        # Layer 2: the command's own grammar.
        if prefix == command_name:
            for value, meta in SUBCOMMANDS.get(command_name, ()):
                if value.startswith(typed):
                    yield value, meta, -len(typed)


    # -- Completer ---------------------------------------------------------

    def get_completions(self, document, complete_event):
        if document.text_after_cursor.strip():
            # Completing in the middle of a line replaces text the user can see
            # but the menu cannot, which reads as corruption.
            return
        seen: set[str] = set()
        for value, meta, start in self._complete_at(document.text_before_cursor):
            if value in seen:
                continue
            seen.add(value)
            yield Completion(value, start_position=start, display_meta=meta)
