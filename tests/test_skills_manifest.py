"""W1-1 / P0-7: skills are disclosed progressively, never dumped into the prompt.

Three properties are load-bearing and each has a test here:

* a huge skill body costs a handful of prompt characters, not its own size;
* tag and name matching is whole-word, so a ``test`` tag stops firing on
  "latest";
* the Claude-compatible scopes (``~/.claude/skills``, ``./.claude/skills``) are
  discovered, and nearer scopes shadow farther ones by skill name.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from djcode.skills import MANIFEST_HEADER, SkillManager

BASE_PROMPT = "You are DJcode."


def write_skill(
    directory: Path,
    name: str,
    description: str,
    body: str = "Do the thing.",
    tags: str = "",
    example: str = "",
) -> Path:
    """Write ``<directory>/<name>/SKILL.md`` and return its path."""
    path = directory / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        f"---\nname: {name}\ndescription: {description}\ntags: [{tags}]\n---\n\n"
        f"## Instructions\n{body}\n"
    )
    if example:
        text += f"\n## Example\n{example}\n"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def scopes(monkeypatch, tmp_path):
    """All four skill scopes relocated under ``tmp_path``.

    ``Path.home`` and ``Path.cwd`` are patched on the class so the module under
    test resolves the user and project scopes into the sandbox instead of the
    developer's real ``~/.claude/skills``, which on a working machine holds
    dozens of skills and would make every assertion here environment-dependent.
    """
    home = tmp_path / "home"
    project = tmp_path / "project"
    home_djcode = home / ".djcode" / "skills"
    home_claude = home / ".claude" / "skills"
    project_djcode = project / ".djcode" / "skills"
    project_claude = project / ".claude" / "skills"
    for directory in (home_djcode, home_claude, project_djcode, project_claude):
        directory.mkdir(parents=True)

    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(Path, "cwd", lambda: project)
    monkeypatch.setattr(SkillManager, "SKILLS_DIR", home_djcode)

    return SimpleNamespace(
        home_djcode=home_djcode,
        home_claude=home_claude,
        project_djcode=project_djcode,
        project_claude=project_claude,
        manager=SkillManager,
    )


# ---------------------------------------------------------------------------
# Progressive disclosure: size of a skill must not reach the prompt
# ---------------------------------------------------------------------------


def test_50kb_skill_body_contributes_under_200_chars(scopes):
    body = "PAYLOAD " * 6400  # 51_200 characters
    assert len(body) > 50_000
    write_skill(
        scopes.home_djcode,
        "bulk-import",
        "Import a vendor CSV dump",
        body=body,
        tags="import, csv",
    )

    manager = SkillManager()
    # The body really is loaded — this is a disclosure test, not a parse failure.
    assert len(manager.get_skill("bulk-import").instructions) > 50_000

    injected = manager.inject_skills(BASE_PROMPT, "run the csv import")
    contributed = len(injected) - len(BASE_PROMPT)

    assert contributed < 200, f"skill contributed {contributed} chars to the prompt"
    assert "PAYLOAD" not in injected
    assert MANIFEST_HEADER in injected
    assert "- bulk-import: Import a vendor CSV dump" in injected


def test_instructions_and_example_are_never_injected(scopes):
    write_skill(
        scopes.home_djcode,
        "deploy",
        "Ship a release",
        body="INSTRUCTIONS-SENTINEL: run the deploy script.",
        tags="deploy",
        example="EXAMPLE-SENTINEL: /deploy staging",
    )

    manager = SkillManager()
    skill = manager.get_skill("deploy")
    assert "INSTRUCTIONS-SENTINEL" in skill.instructions
    assert "EXAMPLE-SENTINEL" in skill.example

    for message in ("please deploy", ""):
        injected = manager.inject_skills(BASE_PROMPT, message)
        assert "INSTRUCTIONS-SENTINEL" not in injected
        assert "EXAMPLE-SENTINEL" not in injected
        assert "- deploy: Ship a release" in injected


def test_no_user_message_emits_manifest_for_all_skills_not_bodies(scopes):
    write_skill(scopes.home_djcode, "alpha", "First skill", body="ALPHA-BODY")
    write_skill(scopes.home_djcode, "beta", "Second skill", body="BETA-BODY")

    injected = SkillManager().inject_skills(BASE_PROMPT)

    assert injected.startswith(BASE_PROMPT)
    assert "- alpha: First skill" in injected
    assert "- beta: Second skill" in injected
    assert "ALPHA-BODY" not in injected
    assert "BETA-BODY" not in injected


def test_manifest_shape_is_header_hint_and_one_line_per_skill(scopes):
    write_skill(scopes.home_djcode, "alpha", "First skill")
    write_skill(scopes.home_djcode, "beta", "Second skill")

    manifest = SkillManager().inject_skills(BASE_PROMPT)[len(BASE_PROMPT) :].strip()
    lines = manifest.splitlines()

    assert lines[0] == MANIFEST_HEADER
    assert lines[1] == "Load a skill's full instructions with the `skill` tool before using it."
    assert lines[2:] == ["- alpha: First skill", "- beta: Second skill"]


def test_multiline_description_is_collapsed_and_bounded(scopes):
    write_skill(scopes.home_djcode, "verbose", "word " * 200, tags="verbose")

    injected = SkillManager().inject_skills(BASE_PROMPT, "verbose please")
    entry = next(line for line in injected.splitlines() if line.startswith("- verbose:"))

    assert len(entry) < 200
    assert "\n" not in entry


# ---------------------------------------------------------------------------
# Whole-word matching
# ---------------------------------------------------------------------------


def test_test_tag_does_not_match_latest(scopes):
    write_skill(scopes.home_djcode, "suite-runner", "Run the test suite", tags="test, pytest")

    manager = SkillManager()
    assert manager.inject_skills(BASE_PROMPT, "what is the latest version") == BASE_PROMPT
    # ...and the tag still matches when the word really is there.
    assert "- suite-runner:" in manager.inject_skills(BASE_PROMPT, "run the test suite")


def test_skill_named_test_does_not_match_latest(scopes):
    """The name check had the same substring bug as the tag check."""
    write_skill(scopes.home_djcode, "test", "Testing helper")

    manager = SkillManager()
    assert manager.inject_skills(BASE_PROMPT, "what is the latest version") == BASE_PROMPT
    assert "- test: Testing helper" in manager.inject_skills(BASE_PROMPT, "write a test for this")


def test_hyphenated_name_matches_as_a_phrase(scopes):
    write_skill(scopes.home_djcode, "code-review", "Review a diff")

    manager = SkillManager()
    assert "- code-review:" in manager.inject_skills(BASE_PROMPT, "do a code review of this")
    assert "- code-review:" in manager.inject_skills(BASE_PROMPT, "run code-review now")
    assert manager.inject_skills(BASE_PROMPT, "unrelated question") == BASE_PROMPT


def test_no_match_leaves_the_prompt_untouched(scopes):
    write_skill(scopes.home_djcode, "kubernetes", "Operate a cluster", tags="k8s")

    assert SkillManager().inject_skills(BASE_PROMPT, "what time is it") == BASE_PROMPT


def test_empty_tag_matches_nothing(scopes):
    """``tags: []`` parses to no tags; a stray blank must not match every message."""
    write_skill(scopes.home_djcode, "kubernetes", "Operate a cluster", tags=", ,")

    assert SkillManager().inject_skills(BASE_PROMPT, "what time is it") == BASE_PROMPT


# ---------------------------------------------------------------------------
# Scope discovery and nearest-scope-wins
# ---------------------------------------------------------------------------


def test_home_claude_skill_md_is_discovered(scopes):
    write_skill(scopes.home_claude, "foo", "A user-scope Claude skill", body="Foo the bar.")

    manager = SkillManager()
    skills = manager.load_skills()

    assert "foo" in skills
    assert skills["foo"].description == "A user-scope Claude skill"
    assert skills["foo"].instructions == "Foo the bar."


def test_project_claude_skill_md_is_discovered(scopes):
    write_skill(scopes.project_claude, "bar", "A project-scope Claude skill")

    assert "bar" in SkillManager().load_skills()


def test_all_four_scopes_are_searched(scopes):
    write_skill(scopes.project_djcode, "one", "cwd djcode")
    write_skill(scopes.project_claude, "two", "cwd claude")
    write_skill(scopes.home_djcode, "three", "home djcode")
    write_skill(scopes.home_claude, "four", "home claude")

    assert set(SkillManager().load_skills()) == {"one", "two", "three", "four"}


def test_nearest_scope_wins(scopes):
    """Farthest to nearest: each new definition must take over the name."""
    ladder = [
        (scopes.home_claude, "home-claude"),
        (scopes.home_djcode, "home-djcode"),
        (scopes.project_claude, "cwd-claude"),
        (scopes.project_djcode, "cwd-djcode"),
    ]
    for directory, marker in ladder:
        write_skill(directory, "shared", f"from {marker}")
        # A fresh manager: load_skills caches per instance.
        assert SkillManager().get_skill("shared").description == f"from {marker}"

    assert len(SkillManager().load_skills()["shared"].description) > 0
    assert list(SkillManager().load_skills()) == ["shared"]


def test_explicit_skills_dir_is_used_alone(scopes, tmp_path):
    """A caller-supplied directory must not silently inherit the other scopes."""
    write_skill(scopes.home_claude, "ambient", "Should not leak in")
    only = tmp_path / "only"
    only.mkdir()
    write_skill(only, "solo", "The only skill")

    assert set(SkillManager(skills_dir=only).load_skills()) == {"solo"}


def test_flat_skill_md_files_still_load(scopes):
    """The original ``<name>.skill.md`` layout must keep working."""
    (scopes.home_djcode / "legacy.skill.md").write_text(
        "---\nname: legacy\ndescription: Old format\ntags: [legacy]\n---\n\n"
        "## Instructions\nStill here.\n",
        encoding="utf-8",
    )

    manager = SkillManager()
    assert manager.get_skill("legacy").instructions == "Still here."
    assert "- legacy: Old format" in manager.inject_skills(BASE_PROMPT, "legacy mode")


def test_oversized_skill_file_is_skipped(scopes):
    write_skill(scopes.home_djcode, "huge", "Too big to load", body="x" * 200_000)
    write_skill(scopes.home_djcode, "small", "Fine")

    assert set(SkillManager().load_skills()) == {"small"}


def test_scope_dirs_order(scopes):
    manager = SkillManager()
    assert manager.scope_dirs() == [
        scopes.project_djcode,
        scopes.project_claude,
        scopes.home_djcode,
        scopes.home_claude,
    ]


def test_scope_dirs_dedupes_when_project_is_home(monkeypatch, tmp_path):
    """Running DJcode from ``$HOME`` must not scan the same directory twice."""
    home = tmp_path / "home"
    (home / ".djcode" / "skills").mkdir(parents=True)
    (home / ".claude" / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(Path, "cwd", lambda: home)
    monkeypatch.setattr(SkillManager, "SKILLS_DIR", home / ".djcode" / "skills")

    dirs = SkillManager().scope_dirs()
    resolved = [str(d.resolve()) for d in dirs]

    assert len(resolved) == len(set(resolved)) == 2
