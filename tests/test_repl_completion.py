"""Completion beyond the command name, and the two blocking-stdin bugs it closes.

`commands.SlashCompleter` completed command NAMES and stopped -- its docstring
said so on purpose. The effect was that `/diff ` offered nothing and the three
words it accepts were discoverable only by reading repl.py.

This file also pins the two stdin bugs that go away with it:

* **B22** -- the bare-`/` picker ran `questionary.select(...).ask()` on a
  thread-pool thread while the main PromptSession was still alive. Two readers,
  one stdin.
* **B21** -- `/recipe create` and `/recipe run` called builtin `input()`, which
  blocks the thread it runs on; on the REPL that thread is running the event
  loop, so every background agent, stream and timer stopped dead until the user
  finished typing.
"""

from __future__ import annotations

import inspect

import pytest
from prompt_toolkit.document import Document

from djcode.frontends.repl.completer import SUBCOMMANDS, DjcodeCompleter, path_candidates


def complete(line: str, cwd: str | None = None) -> list[str]:
    completer = DjcodeCompleter(cwd)
    document = Document(line, len(line))
    return [c.text for c in completer.get_completions(document, None)]


# -- layer 1: command names (must not regress) -----------------------------


def test_a_command_prefix_still_completes():
    got = complete("/re")
    assert "/resume" in got and "/recipe" in got


def test_prefix_matches_are_still_ranked_first():
    """`commands.match_commands` already did this; replacing the completer must
    not lose it."""
    got = complete("/undo")
    assert got[0] == "/undo"


def test_an_exact_command_with_no_trailing_space_does_not_offer_arguments():
    assert "session" not in complete("/diff")


# -- layer 2: subcommands --------------------------------------------------


@pytest.mark.parametrize("command,expected", sorted(SUBCOMMANDS.items()))
def test_every_subcommand_table_entry_is_offered(command, expected):
    got = complete(f"{command} ")
    assert [value for value, _ in expected] == got


def test_diff_offers_its_three_modes():
    assert complete("/diff ") == ["session", "uncommitted", "branch"]


def test_a_partial_subcommand_narrows():
    assert complete("/diff unc") == ["uncommitted"]


def test_subcommands_are_words_the_handler_actually_accepts():
    """A completion offering a word the handler ignores is worse than none.
    `/diff`'s modes are read straight out of its handler in repl.py."""
    from djcode import repl

    source = inspect.getsource(repl._handle_diff)
    for value, _ in SUBCOMMANDS["/diff"]:
        assert value in source, f"/diff does not handle {value!r}"


# -- layer 3: live arguments -----------------------------------------------


def test_set_offers_real_config_keys():
    from djcode.config import DEFAULT_CONFIG

    got = complete("/set ")
    assert got, "no config keys offered"
    assert all(g.rstrip("=") in DEFAULT_CONFIG for g in got)


def test_set_theme_offers_the_real_palettes():
    """B20's other half: `/set theme=light` now works, so the completer has to
    be able to say what the legal values are."""
    from djcode.frontends.repl.theme import PALETTES

    got = complete("/set theme=")
    assert {g.split("=", 1)[1] for g in got} == set(PALETTES) | {"auto"}


def test_set_offers_booleans_for_boolean_keys():
    assert complete("/set auto_accept=") == ["auto_accept=true", "auto_accept=false"]


def test_set_offers_nothing_for_a_key_with_no_closed_value_set():
    """Guessing a base_url would be worse than offering nothing."""
    assert complete("/set base_url=") == []


def test_docs_offers_real_sections():
    from djcode.docs import DOCS_SECTIONS

    got = complete("/docs ")
    assert set(DOCS_SECTIONS) <= set(got)
    assert "all" in got


def test_an_unknown_command_offers_nothing():
    assert complete("/nosuchcommand ") == []


# -- layer 4: @path --------------------------------------------------------


def test_at_path_completes_a_directory(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x", encoding="utf-8")
    assert complete("@sr", str(tmp_path)) == ["src/"]


def test_at_path_completes_a_file_inside_a_directory(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x", encoding="utf-8")
    assert complete("@src/ma", str(tmp_path)) == ["src/main.py"]


def test_at_path_works_in_the_middle_of_a_sentence(tmp_path):
    (tmp_path / "notes.md").write_text("x", encoding="utf-8")
    assert complete("please read @not", str(tmp_path)) == ["notes.md"]


def test_directories_sort_before_files(tmp_path):
    (tmp_path / "aaa.txt").write_text("x", encoding="utf-8")
    (tmp_path / "zzz").mkdir()
    assert path_candidates("", str(tmp_path))[0] == "zzz/"


def test_dotfiles_are_hidden_until_asked_for(tmp_path):
    (tmp_path / ".env").write_text("x", encoding="utf-8")
    (tmp_path / "app.py").write_text("x", encoding="utf-8")
    assert ".env" not in path_candidates("", str(tmp_path))
    assert ".env" in path_candidates(".", str(tmp_path))


def test_paths_are_offered_with_forward_slashes(tmp_path):
    """Backslashes survive neither a shell nor a model-facing string."""
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b.txt").write_text("x", encoding="utf-8")
    assert path_candidates("a\\", str(tmp_path)) == ["a/b.txt"]


def test_a_missing_directory_completes_to_nothing_rather_than_raising():
    assert path_candidates("no/such/dir/x") == []


def test_path_results_are_bounded(tmp_path):
    for i in range(80):
        (tmp_path / f"f{i:03d}.txt").write_text("x", encoding="utf-8")
    assert len(path_candidates("", str(tmp_path))) <= 40


# -- general safety --------------------------------------------------------


def test_completing_mid_line_is_refused():
    """Replacing text the user can see but the menu cannot reads as
    corruption."""
    completer = DjcodeCompleter()
    document = Document("/diff session", 5)
    assert list(completer.get_completions(document, None)) == []


def test_completions_are_deduplicated():
    got = complete("/set ")
    assert len(got) == len(set(got))


def test_plain_prose_offers_nothing():
    assert complete("explain what this does") == []


# -- B22: one stdin reader --------------------------------------------------


def test_the_bare_slash_thread_picker_is_gone():
    """It ran questionary on a thread-pool thread while the PromptSession was
    live. Two readers on one stdin is B22."""
    from djcode import repl

    source = inspect.getsource(repl.run_repl)
    assert "show_command_picker" not in source
    assert "ThreadPoolExecutor" not in source


def test_repl_no_longer_imports_the_picker():
    from djcode import repl

    assert not hasattr(repl, "show_command_picker")


# -- B21: nothing blocks the event loop -------------------------------------


def test_recipe_create_does_not_call_builtin_input():
    from djcode import repl

    source = inspect.getsource(repl.handle_slash_command)
    assert 'input("  Name: ")' not in source
    assert "ask_async" in source


def test_the_repl_collects_recipe_parameters_asynchronously():
    from djcode import repl

    source = inspect.getsource(repl.handle_slash_command)
    assert "collect_params_async" in source
    assert "collect_params_interactive" not in source


def test_the_async_collector_exists_and_is_a_coroutine():
    from djcode.recipes import RecipeManager

    assert inspect.iscoroutinefunction(RecipeManager.collect_params_async)


def test_the_sync_collector_is_kept_for_non_async_callers():
    """Deleting it would break anything driving RecipeManager outside a loop,
    and it is still the right call there."""
    from djcode.recipes import RecipeManager

    assert callable(RecipeManager.collect_params_interactive)
    assert not inspect.iscoroutinefunction(RecipeManager.collect_params_interactive)


def test_both_collectors_apply_the_same_defaulting_rules():
    from djcode.recipes import RecipeManager, RecipeParam

    manager = RecipeManager()
    params: dict[str, str] = {}
    manager._accept(params, RecipeParam(key="a", description="", default="dflt"), "")
    assert params["a"] == "dflt"
    manager._accept(params, RecipeParam(key="b", description=""), "typed")
    assert params["b"] == "typed"


def test_a_missing_required_parameter_is_an_error_not_a_silent_empty():
    from djcode.recipes import RecipeManager, RecipeParam

    manager = RecipeManager()
    with pytest.raises(ValueError, match="Required parameter"):
        manager._accept({}, RecipeParam(key="a", description="", required=True), "")
