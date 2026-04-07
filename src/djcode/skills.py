"""Teachable skill system for DJcode.

Users define skills as .skill.md files in ~/.djcode/skills/.
Skills are loaded at startup and injected into the system prompt
when relevant tags match the user's intent.

Slash commands:
    /skill list            — show all skills
    /skill add <name>      — interactive skill creation
    /skill remove <name>   — remove a skill
    /skill show <name>     — show skill details
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Skill dataclass
# ---------------------------------------------------------------------------

@dataclass
class Skill:
    """A single teachable skill definition."""

    name: str
    description: str
    instructions: str  # The actual instructions for the LLM
    example: str = ""
    created: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def filename(self) -> str:
        return f"{self.name}.skill.md"

    def to_markdown(self) -> str:
        """Serialize this skill to the .skill.md format."""
        tags_str = ", ".join(self.tags) if self.tags else ""
        lines = [
            "---",
            f"name: {self.name}",
            f"description: {self.description}",
            f"tags: [{tags_str}]",
            f"created: {self.created}",
            "---",
            "",
            "## Instructions",
            self.instructions,
        ]
        if self.example:
            lines.extend(["", "## Example", self.example])
        return "\n".join(lines) + "\n"

    @staticmethod
    def from_markdown(text: str, filename: str = "") -> Skill:
        """Parse a .skill.md file into a Skill object."""
        name = ""
        description = ""
        tags: list[str] = []
        created = ""
        instructions = ""
        example = ""

        # Parse frontmatter
        fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
        body = text
        if fm_match:
            frontmatter = fm_match.group(1)
            body = text[fm_match.end():]

            for line in frontmatter.splitlines():
                line = line.strip()
                if line.startswith("name:"):
                    name = line[len("name:"):].strip()
                elif line.startswith("description:"):
                    description = line[len("description:"):].strip()
                elif line.startswith("tags:"):
                    raw_tags = line[len("tags:"):].strip()
                    # Parse [tag1, tag2, tag3] format
                    raw_tags = raw_tags.strip("[]")
                    tags = [t.strip().strip("'\"") for t in raw_tags.split(",") if t.strip()]
                elif line.startswith("created:"):
                    created = line[len("created:"):].strip()

        # Fallback name from filename
        if not name and filename:
            name = filename.replace(".skill.md", "")

        # Parse body sections
        sections = re.split(r"^##\s+", body, flags=re.MULTILINE)
        for section in sections:
            if section.lower().startswith("instructions"):
                instructions = section[len("instructions"):].strip()
            elif section.lower().startswith("example"):
                example = section[len("example"):].strip()

        return Skill(
            name=name,
            description=description,
            instructions=instructions,
            example=example,
            created=created,
            tags=tags,
        )


# ---------------------------------------------------------------------------
# SkillManager
# ---------------------------------------------------------------------------

class SkillManager:
    """Manages user-defined skills stored at ~/.djcode/skills/."""

    SKILLS_DIR = Path.home() / ".djcode" / "skills"

    def __init__(self, skills_dir: Path | None = None) -> None:
        self.skills_dir = skills_dir or self.SKILLS_DIR
        self._cache: dict[str, Skill] | None = None

    def _ensure_dir(self) -> None:
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    def _invalidate_cache(self) -> None:
        self._cache = None

    # ------------------------------------------------------------------
    # Core CRUD
    # ------------------------------------------------------------------

    def load_skills(self) -> dict[str, Skill]:
        """Load all .skill.md files from skills directory."""
        if self._cache is not None:
            return dict(self._cache)

        self._ensure_dir()
        skills: dict[str, Skill] = {}

        for path in sorted(self.skills_dir.glob("*.skill.md")):
            try:
                text = path.read_text(encoding="utf-8")
                skill = Skill.from_markdown(text, filename=path.name)
                if skill.name:
                    skills[skill.name] = skill
            except (OSError, UnicodeDecodeError):
                continue

        self._cache = skills
        return dict(skills)

    def save_skill(
        self,
        name: str,
        description: str,
        instructions: str,
        example: str = "",
        tags: list[str] | None = None,
    ) -> Skill:
        """Save a new skill definition. Returns the created Skill."""
        self._ensure_dir()

        # Sanitize name: lowercase, hyphens only
        safe_name = re.sub(r"[^a-z0-9-]", "-", name.lower().strip())
        safe_name = re.sub(r"-+", "-", safe_name).strip("-")
        if not safe_name:
            raise ValueError(f"Invalid skill name: {name!r}")

        skill = Skill(
            name=safe_name,
            description=description,
            instructions=instructions,
            example=example,
            created=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            tags=tags or [],
        )

        path = self.skills_dir / skill.filename
        path.write_text(skill.to_markdown(), encoding="utf-8")
        self._invalidate_cache()
        return skill

    def get_skill(self, name: str) -> Skill | None:
        """Get a skill by name."""
        skills = self.load_skills()
        return skills.get(name)

    def list_skills(self) -> list[Skill]:
        """List all available skills sorted by name."""
        return sorted(self.load_skills().values(), key=lambda s: s.name)

    def remove_skill(self, name: str) -> bool:
        """Remove a skill. Returns True if it existed and was removed."""
        self._ensure_dir()
        path = self.skills_dir / f"{name}.skill.md"
        if path.exists():
            path.unlink()
            self._invalidate_cache()
            return True
        return False

    # ------------------------------------------------------------------
    # Prompt injection
    # ------------------------------------------------------------------

    def inject_skills(self, system_prompt: str, user_message: str = "") -> str:
        """Inject relevant skill instructions into the system prompt.

        Skills are matched by checking if any of their tags appear in the
        user's message. If no user message is provided, all skills are injected.
        """
        skills = self.load_skills()
        if not skills:
            return system_prompt

        matched: list[Skill] = []
        if user_message:
            msg_lower = user_message.lower()
            for skill in skills.values():
                # Match on tags or skill name appearing in message
                if skill.name.replace("-", " ") in msg_lower:
                    matched.append(skill)
                    continue
                if skill.name in msg_lower:
                    matched.append(skill)
                    continue
                if any(tag.lower() in msg_lower for tag in skill.tags):
                    matched.append(skill)
                    continue
        else:
            # No message context — inject all
            matched = list(skills.values())

        if not matched:
            return system_prompt

        skill_blocks: list[str] = []
        for skill in matched:
            block = f"### Skill: {skill.name}\n{skill.description}\n\n{skill.instructions}"
            if skill.example:
                block += f"\n\n**Example:**\n{skill.example}"
            skill_blocks.append(block)

        injection = (
            "\n\n---\n## User-Defined Skills\n"
            "The following skills have been taught by the user. "
            "Follow these instructions when relevant.\n\n"
            + "\n\n".join(skill_blocks)
        )

        return system_prompt + injection

    # ------------------------------------------------------------------
    # Search / filter
    # ------------------------------------------------------------------

    def search_skills(self, query: str) -> list[Skill]:
        """Search skills by name, description, or tags."""
        query_lower = query.lower()
        results: list[Skill] = []
        for skill in self.load_skills().values():
            if query_lower in skill.name.lower():
                results.append(skill)
            elif query_lower in skill.description.lower():
                results.append(skill)
            elif any(query_lower in tag.lower() for tag in skill.tags):
                results.append(skill)
        return results

    def get_skills_by_tag(self, tag: str) -> list[Skill]:
        """Get all skills with a specific tag."""
        tag_lower = tag.lower()
        return [
            s for s in self.load_skills().values()
            if any(t.lower() == tag_lower for t in s.tags)
        ]

    def get_all_tags(self) -> list[str]:
        """Get a sorted list of all unique tags across all skills."""
        tags: set[str] = set()
        for skill in self.load_skills().values():
            tags.update(skill.tags)
        return sorted(tags)


# ---------------------------------------------------------------------------
# Slash command handler
# ---------------------------------------------------------------------------

def handle_skill_command(args: str, manager: SkillManager | None = None) -> str:
    """Handle /skill <subcommand> <args>.

    Returns a string to display to the user.
    """
    if manager is None:
        manager = SkillManager()

    parts = args.strip().split(maxsplit=1)
    subcommand = parts[0].lower() if parts else "list"
    rest = parts[1].strip() if len(parts) > 1 else ""

    if subcommand == "list":
        return _cmd_list(manager)
    elif subcommand == "show":
        return _cmd_show(manager, rest)
    elif subcommand == "remove":
        return _cmd_remove(manager, rest)
    elif subcommand == "add":
        return _cmd_add_info(rest)
    elif subcommand == "help":
        return _cmd_help()
    else:
        return f"Unknown skill subcommand: {subcommand}\n\n{_cmd_help()}"


def _cmd_list(manager: SkillManager) -> str:
    skills = manager.list_skills()
    if not skills:
        return (
            "No skills defined yet.\n"
            "Create one with: /skill add <name>\n"
            "Or place .skill.md files in ~/.djcode/skills/"
        )

    lines = ["Skills:"]
    for s in skills:
        tags = f" [{', '.join(s.tags)}]" if s.tags else ""
        lines.append(f"  {s.name}{tags} — {s.description}")
    lines.append(f"\n{len(skills)} skill(s) loaded from {manager.skills_dir}")
    return "\n".join(lines)


def _cmd_show(manager: SkillManager, name: str) -> str:
    if not name:
        return "Usage: /skill show <name>"
    skill = manager.get_skill(name)
    if not skill:
        return f"Skill '{name}' not found."

    lines = [
        f"Skill: {skill.name}",
        f"Description: {skill.description}",
        f"Tags: {', '.join(skill.tags) if skill.tags else '(none)'}",
        f"Created: {skill.created or 'unknown'}",
        "",
        "Instructions:",
        skill.instructions,
    ]
    if skill.example:
        lines.extend(["", "Example:", skill.example])
    return "\n".join(lines)


def _cmd_remove(manager: SkillManager, name: str) -> str:
    if not name:
        return "Usage: /skill remove <name>"
    if manager.remove_skill(name):
        return f"Skill '{name}' removed."
    return f"Skill '{name}' not found."


def _cmd_add_info(name: str) -> str:
    """Return instructions for creating a skill (interactive creation needs the REPL)."""
    if not name:
        return (
            "Usage: /skill add <name>\n\n"
            "This will create a new skill interactively.\n"
            "Or create a .skill.md file directly in ~/.djcode/skills/\n\n"
            "Format:\n"
            "---\n"
            "name: my-skill\n"
            "description: What this skill does\n"
            "tags: [tag1, tag2]\n"
            "---\n\n"
            "## Instructions\n"
            "Your instructions here...\n\n"
            "## Example\n"
            "Optional example usage..."
        )
    return (
        f"To create skill '{name}', provide:\n"
        f"  1. Description: what does this skill do?\n"
        f"  2. Instructions: what should the AI do?\n"
        f"  3. Tags: comma-separated keywords\n"
        f"  4. Example (optional): usage example\n\n"
        f"Or create the file directly: ~/.djcode/skills/{name}.skill.md"
    )


def _cmd_help() -> str:
    return (
        "Skill commands:\n"
        "  /skill list            — show all skills\n"
        "  /skill add <name>      — create a new skill\n"
        "  /skill remove <name>   — remove a skill\n"
        "  /skill show <name>     — show skill details\n"
        "  /skill help            — this help message"
    )


def create_skill_interactive(
    manager: SkillManager,
    name: str,
    description: str,
    instructions: str,
    example: str = "",
    tags: str = "",
) -> Skill:
    """Programmatic skill creation for use from the REPL.

    Args:
        manager: The SkillManager instance.
        name: Skill name (will be sanitized).
        description: One-line description.
        instructions: Full instructions for the LLM.
        example: Optional usage example.
        tags: Comma-separated tag string.

    Returns:
        The created Skill.
    """
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    return manager.save_skill(
        name=name,
        description=description,
        instructions=instructions,
        example=example,
        tags=tag_list,
    )
