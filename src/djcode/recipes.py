"""Recipe System for DJcode — reusable workflow templates.

Port of Goose's recipe system. Recipes are parameterized workflow templates
that define system prompts, initial prompts, agent pipelines, and model overrides.

Stored as .recipe.json files in ~/.djcode/recipes/
Uses JSON (no PyYAML dependency). Built-in recipes included.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, AsyncIterator

from djcode.config import CONFIG_DIR

logger = logging.getLogger(__name__)

RECIPES_DIR = CONFIG_DIR / "recipes"

GOLD = "#FFD700"


@dataclass
class RecipeParam:
    """A parameter slot in a recipe template."""

    key: str
    type: str = "string"  # string, int, bool, choice
    required: bool = True
    description: str = ""
    default: str = ""
    choices: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RecipeParam:
        return cls(
            key=data.get("key", ""),
            type=data.get("type", "string"),
            required=data.get("required", True),
            description=data.get("description", ""),
            default=data.get("default", ""),
            choices=data.get("choices", []),
        )


@dataclass
class Recipe:
    """A reusable workflow template."""

    name: str
    description: str
    instructions: str  # System prompt override/addition
    prompt: str  # Prompt template with {{param}} placeholders
    parameters: list[RecipeParam] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    model: str | None = None  # Optional model override
    tags: list[str] = field(default_factory=list)
    version: str = "1.0"
    author: str = "djcode"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["parameters"] = [p.to_dict() for p in self.parameters]
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Recipe:
        return cls(
            name=data.get("name", "unknown"),
            description=data.get("description", ""),
            instructions=data.get("instructions", ""),
            prompt=data.get("prompt", ""),
            parameters=[RecipeParam.from_dict(p) for p in data.get("parameters", [])],
            agents=data.get("agents", []),
            model=data.get("model"),
            tags=data.get("tags", []),
            version=data.get("version", "1.0"),
            author=data.get("author", "djcode"),
        )

    def render_prompt(self, params: dict[str, str]) -> str:
        """Render the prompt template with parameter values.

        Replaces {{key}} placeholders with provided values.
        Raises ValueError for missing required parameters.
        """
        # Validate required params
        for p in self.parameters:
            if p.required and p.key not in params and not p.default:
                raise ValueError(f"Missing required parameter: {p.key} — {p.description}")

        rendered = self.prompt
        for p in self.parameters:
            value = params.get(p.key, p.default)
            rendered = rendered.replace(f"{{{{{p.key}}}}}", str(value))

        # Warn about unreplaced placeholders
        leftover = re.findall(r"\{\{(\w+)\}\}", rendered)
        if leftover:
            logger.warning("Unreplaced placeholders in recipe '%s': %s", self.name, leftover)

        return rendered

    def render_instructions(self, params: dict[str, str]) -> str:
        """Render the instructions template with parameter values."""
        rendered = self.instructions
        for p in self.parameters:
            value = params.get(p.key, p.default)
            rendered = rendered.replace(f"{{{{{p.key}}}}}", str(value))
        return rendered


# ── Built-in Recipes ──────────────────────────────────────────────────────

BUILTIN_RECIPES: list[Recipe] = [
    Recipe(
        name="new-project",
        description="Scaffold any project type with best-practice structure",
        instructions=(
            "You are a senior software architect. Your task is to scaffold a new project "
            "with production-grade structure. Include: proper directory layout, config files, "
            "CI/CD templates, README, .gitignore, dependency management. Follow the language's "
            "community conventions. Make it complete — no stubs, no placeholders."
        ),
        prompt=(
            "Create a new {{language}} project called '{{name}}' of type '{{type}}'.\n"
            "Requirements: {{requirements}}\n"
            "Initialize git, set up the build system, create the entry point, "
            "and include a comprehensive .gitignore."
        ),
        parameters=[
            RecipeParam(key="name", description="Project name"),
            RecipeParam(key="language", description="Primary language (e.g., rust, python, typescript)"),
            RecipeParam(key="type", description="Project type (e.g., cli, web-api, library, fullstack)", default="cli"),
            RecipeParam(key="requirements", required=False, description="Additional requirements", default="production-ready defaults"),
        ],
        agents=["coder"],
        tags=["scaffold", "init"],
    ),
    Recipe(
        name="code-review",
        description="Thorough code review — security, performance, correctness, style",
        instructions=(
            "You are Dharma, the code review agent. You review with the eye of a senior "
            "engineer who has seen every bug pattern. Check for: security vulnerabilities, "
            "performance bottlenecks, race conditions, error handling gaps, API design issues, "
            "naming clarity, test coverage gaps. Be specific — cite line numbers, suggest fixes."
        ),
        prompt=(
            "Review this code thoroughly:\n\n"
            "Path: {{path}}\n"
            "Focus areas: {{focus}}\n\n"
            "Read the file(s), then provide a structured review with severity ratings "
            "(critical/warning/info) for each finding."
        ),
        parameters=[
            RecipeParam(key="path", description="File or directory to review"),
            RecipeParam(key="focus", required=False, description="Specific areas to focus on", default="all — security, performance, correctness, style"),
        ],
        agents=["reviewer"],
        tags=["review", "quality"],
    ),
    Recipe(
        name="debug-fix",
        description="Debug, fix, and verify — 3-agent pipeline",
        instructions=(
            "You are running a debug-fix pipeline. Phase 1: Investigate the issue like Sherlock — "
            "find the root cause, not just symptoms. Phase 2: Fix with minimal, surgical changes — "
            "don't refactor unrelated code. Phase 3: Verify the fix — run existing tests, "
            "add a regression test if none exists. Report each phase clearly."
        ),
        prompt=(
            "Debug and fix this issue:\n\n"
            "{{issue}}\n\n"
            "Steps:\n"
            "1. Investigate: Find the root cause (read logs, trace code paths, reproduce)\n"
            "2. Fix: Apply the minimal correct fix\n"
            "3. Verify: Run tests, confirm the fix, add a regression test if needed"
        ),
        parameters=[
            RecipeParam(key="issue", description="The bug or issue to fix — include error messages, reproduction steps"),
        ],
        agents=["debugger", "coder", "tester"],
        tags=["debug", "fix", "pipeline"],
    ),
    Recipe(
        name="launch-campaign",
        description="Plan and execute a product launch campaign",
        instructions=(
            "You are a launch strategist. Plan the campaign first: target audience, "
            "messaging, channels, timeline. Then generate all launch assets: "
            "announcement post, social media threads, documentation updates, "
            "changelog entry, and email template. Everything should be coherent "
            "and on-brand."
        ),
        prompt=(
            "Plan and execute a launch campaign for:\n\n"
            "Product: {{product}}\n"
            "Target audience: {{audience}}\n"
            "Key message: {{message}}\n\n"
            "Generate:\n"
            "1. Launch announcement (blog post style)\n"
            "2. Twitter/X thread (5-7 tweets)\n"
            "3. Changelog entry\n"
            "4. Email announcement template\n"
            "5. README update snippet"
        ),
        parameters=[
            RecipeParam(key="product", description="Product or feature being launched"),
            RecipeParam(key="audience", description="Target audience", default="developers"),
            RecipeParam(key="message", description="Core message or value proposition", default=""),
        ],
        agents=["content", "social", "docs"],
        tags=["launch", "marketing", "content"],
    ),
    Recipe(
        name="refactor-safe",
        description="Refactor code with regression safety net",
        instructions=(
            "You are running a safe refactoring pipeline. Phase 1: Analyze the current code — "
            "understand all callers, side effects, and implicit contracts. Phase 2: Run existing "
            "tests to establish a green baseline. Phase 3: Apply the refactoring in small, "
            "atomic steps — each step should pass tests. Phase 4: Verify no regression — "
            "run the full test suite. If any test fails, revert and report."
        ),
        prompt=(
            "Safely refactor this code:\n\n"
            "Target: {{target}}\n"
            "Goal: {{goal}}\n\n"
            "Steps:\n"
            "1. Analyze: Read the code, find all callers and dependencies\n"
            "2. Baseline: Run tests to confirm current state is green\n"
            "3. Refactor: Apply changes in small atomic steps\n"
            "4. Verify: Run tests after each step, confirm no regression"
        ),
        parameters=[
            RecipeParam(key="target", description="File or function to refactor"),
            RecipeParam(key="goal", description="What the refactoring should achieve (e.g., 'extract into separate module', 'simplify control flow')"),
        ],
        agents=["refactorer", "coder", "tester"],
        tags=["refactor", "safety"],
    ),
]


class RecipeManager:
    """Manage recipe templates — load, save, list, execute."""

    def __init__(self) -> None:
        RECIPES_DIR.mkdir(parents=True, exist_ok=True)
        self._ensure_builtins()

    def _ensure_builtins(self) -> None:
        """Write built-in recipes to disk if they don't exist."""
        for recipe in BUILTIN_RECIPES:
            path = RECIPES_DIR / f"{recipe.name}.recipe.json"
            if not path.exists():
                try:
                    path.write_text(json.dumps(recipe.to_dict(), indent=2))
                except OSError as e:
                    logger.warning("Failed to write built-in recipe %s: %s", recipe.name, e)

    def load(self, name: str) -> Recipe:
        """Load a recipe by name.

        Raises FileNotFoundError if not found.
        """
        path = RECIPES_DIR / f"{name}.recipe.json"
        if not path.exists():
            # Try fuzzy match
            matches = list(RECIPES_DIR.glob(f"*{name}*.recipe.json"))
            if len(matches) == 1:
                path = matches[0]
            elif len(matches) > 1:
                names = [m.stem.replace(".recipe", "") for m in matches]
                raise FileNotFoundError(
                    f"Ambiguous recipe name '{name}'. Matches: {', '.join(names)}"
                )
            else:
                raise FileNotFoundError(
                    f"Recipe '{name}' not found. Use /recipe list to see available recipes."
                )

        try:
            data = json.loads(path.read_text())
            return Recipe.from_dict(data)
        except (json.JSONDecodeError, OSError) as e:
            raise ValueError(f"Failed to parse recipe '{name}': {e}")

    def save(self, recipe: Recipe) -> Path:
        """Save a recipe to disk. Returns the file path."""
        path = RECIPES_DIR / f"{recipe.name}.recipe.json"
        path.write_text(json.dumps(recipe.to_dict(), indent=2))
        return path

    def list_recipes(self) -> list[Recipe]:
        """List all available recipes."""
        recipes: list[Recipe] = []
        for path in sorted(RECIPES_DIR.glob("*.recipe.json")):
            try:
                data = json.loads(path.read_text())
                recipes.append(Recipe.from_dict(data))
            except (json.JSONDecodeError, OSError, KeyError) as e:
                logger.warning("Skipping malformed recipe %s: %s", path.name, e)
        return recipes

    def delete(self, name: str) -> bool:
        """Delete a recipe. Returns True if deleted."""
        path = RECIPES_DIR / f"{name}.recipe.json"
        if path.exists():
            path.unlink()
            return True
        return False

    def search(self, query: str) -> list[Recipe]:
        """Search recipes by name, description, or tags."""
        query_lower = query.lower()
        results = []
        for recipe in self.list_recipes():
            if (
                query_lower in recipe.name.lower()
                or query_lower in recipe.description.lower()
                or any(query_lower in tag.lower() for tag in recipe.tags)
            ):
                results.append(recipe)
        return results

    def collect_params_from_args(
        self, recipe: Recipe, arg_string: str
    ) -> dict[str, str]:
        """Parse parameter values from a command argument string.

        Supports two formats:
        - Positional: /recipe run debug-fix "the server crashes on POST /api"
          (fills first required param)
        - Named: /recipe run debug-fix issue="the server crashes" path=src/
        """
        params: dict[str, str] = {}

        if not arg_string.strip():
            return params

        # Try named params: key=value or key="value with spaces"
        named_pattern = re.findall(r'(\w+)=(?:"([^"]+)"|(\S+))', arg_string)
        if named_pattern:
            for key, quoted_val, plain_val in named_pattern:
                params[key] = quoted_val or plain_val
            return params

        # Positional: entire string is the first required param
        required = [p for p in recipe.parameters if p.required]
        if required:
            params[required[0].key] = arg_string.strip().strip('"').strip("'")

        return params

    def collect_params_interactive(self, recipe: Recipe) -> dict[str, str]:
        """Interactively prompt for missing parameter values.

        Uses simple input() — no extra dependencies.
        """
        params: dict[str, str] = {}

        for p in recipe.parameters:
            prompt_text = f"  {p.key}"
            if p.description:
                prompt_text += f" ({p.description})"
            if p.default:
                prompt_text += f" [{p.default}]"
            if not p.required:
                prompt_text += " (optional)"
            prompt_text += ": "

            try:
                value = input(prompt_text).strip()
            except (EOFError, KeyboardInterrupt):
                value = ""

            if not value and p.default:
                value = p.default

            if value:
                params[p.key] = value
            elif p.required:
                raise ValueError(f"Required parameter '{p.key}' was not provided")

        return params

    async def execute(
        self,
        recipe: Recipe,
        params: dict[str, str],
        operator: Any,
    ) -> AsyncIterator[str]:
        """Execute a recipe — render the prompt and send to operator.

        Yields streamed response tokens.
        """
        rendered_prompt = recipe.render_prompt(params)

        # If recipe has custom instructions, inject them
        if recipe.instructions:
            rendered_instructions = recipe.render_instructions(params)
            # Prepend instructions as system context
            full_prompt = (
                f"[Recipe: {recipe.name}]\n"
                f"[Instructions: {rendered_instructions}]\n\n"
                f"{rendered_prompt}"
            )
        else:
            full_prompt = rendered_prompt

        async for token in operator.send(full_prompt):
            yield token

    def create_from_conversation(
        self,
        name: str,
        description: str,
        messages: list[Any],
        parameters: list[RecipeParam] | None = None,
    ) -> Recipe:
        """Create a recipe from a conversation — extract the pattern.

        Takes a conversation that worked well and templatizes it.
        """
        # Extract the first user message as the prompt template
        user_msgs = [m for m in messages if getattr(m, "role", None) == "user"]
        if not user_msgs:
            raise ValueError("No user messages found in conversation")

        # Use the first system message as instructions
        sys_msgs = [m for m in messages if getattr(m, "role", None) == "system"]
        instructions = getattr(sys_msgs[0], "content", "") if sys_msgs else ""

        # Use the first user message as prompt
        prompt = getattr(user_msgs[0], "content", "")

        recipe = Recipe(
            name=name,
            description=description,
            instructions=instructions,
            prompt=prompt,
            parameters=parameters or [],
            author="user",
        )

        self.save(recipe)
        return recipe


def render_recipe_list(console: Any) -> None:
    """Render a formatted list of all recipes."""
    from rich.table import Table
    from rich.text import Text

    manager = RecipeManager()
    recipes = manager.list_recipes()

    if not recipes:
        console.print(f"[{GOLD}]No recipes found.[/] Create one with /recipe create")
        return

    table = Table(
        show_header=True,
        header_style=f"bold {GOLD}",
        border_style="dim",
        title=f"[bold {GOLD}]Recipes[/]",
        title_justify="left",
    )
    table.add_column("Name", style="bold white", min_width=15)
    table.add_column("Description", style="white")
    table.add_column("Params", style="dim", justify="center")
    table.add_column("Agents", style=f"{GOLD}", max_width=30)

    for r in recipes:
        param_keys = ", ".join(p.key for p in r.parameters if p.required)
        agents = ", ".join(r.agents[:4]) if r.agents else "default"
        table.add_row(
            r.name,
            r.description[:60],
            param_keys or "none",
            agents,
        )

    console.print()
    console.print(table)
    console.print()
    console.print("[dim]Usage: /recipe run <name> [params]  |  /recipe show <name>  |  /recipe create[/]")
    console.print()


def render_recipe_detail(console: Any, recipe: Recipe) -> None:
    """Render detailed view of a single recipe."""
    from rich.panel import Panel
    from rich.text import Text

    content = Text()
    content.append(f"{recipe.description}\n\n", style="white")

    if recipe.parameters:
        content.append("Parameters:\n", style=f"bold {GOLD}")
        for p in recipe.parameters:
            req = "*" if p.required else " "
            content.append(f"  {req} {p.key}", style="bold white")
            if p.description:
                content.append(f" — {p.description}", style="dim")
            if p.default:
                content.append(f" [default: {p.default}]", style="dim")
            content.append("\n")
        content.append("\n")

    if recipe.agents:
        content.append("Agents: ", style=f"bold {GOLD}")
        content.append(", ".join(recipe.agents), style="white")
        content.append("\n")

    if recipe.model:
        content.append("Model: ", style=f"bold {GOLD}")
        content.append(recipe.model, style="white")
        content.append("\n")

    if recipe.tags:
        content.append("Tags: ", style=f"bold {GOLD}")
        content.append(", ".join(recipe.tags), style="dim")
        content.append("\n")

    content.append("\nPrompt template:\n", style=f"bold {GOLD}")
    content.append(recipe.prompt[:500], style="dim italic")

    console.print()
    console.print(Panel(
        content,
        title=f"[bold {GOLD}]{recipe.name}[/]",
        border_style=GOLD,
    ))
    console.print()
