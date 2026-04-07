"""Smart prompt enhancement — enriches user prompts before sending to the model.

Detects intent, injects relevant context (cwd, git state, recent files),
and adds structured instructions so the model gives better answers.

The buddy announces what was enhanced in its speech bubble.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


# ── Intent detection ───────────────────────────────────────────────────────

_INTENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Order matters — more specific intents first to avoid false matches
    ("debug", re.compile(
        r"\b(fix|debug|broken|crash|error|bug|issue|failing|wrong|not working"
        r"|doesn't work|doesn.t work|traceback|exception|stack trace)\b", re.I)),
    ("test", re.compile(
        r"\b(test|tests|testing|write tests|add tests|unit test|pytest|spec"
        r"|coverage|assert|expect|mock|fixture)\b", re.I)),
    ("refactor", re.compile(
        r"\b(refactor|rename|extract|simplify|clean up|reorganize|restructure"
        r"|move|split|merge|consolidate|optimize|improve)\b", re.I)),
    ("explain", re.compile(
        r"\b(explain|what does|what is|how does|how do|why does|why is|describe"
        r"|tell me about|walk me through|break down|understand)\b", re.I)),
    ("review", re.compile(
        r"\b(review|check|audit|look at|examine|inspect|analyze|evaluate"
        r"|code review|security|vulnerability)\b", re.I)),
    ("deploy", re.compile(
        r"\b(deploy|ship|push|release|publish|docker|ci|cd|pipeline"
        r"|production|staging|build and deploy)\b", re.I)),
    ("git", re.compile(
        r"\b(commit|branch|merge|rebase|cherry.pick|stash|diff|log|blame"
        r"|pull request|pr|push)\b", re.I)),
    ("build", re.compile(
        r"\b(create|build|make|add|implement|write|generate|scaffold|setup|init"
        r"|new file|new component|new endpoint|new function|new class)\b", re.I)),
]

# File path patterns in user input
_FILE_REF_RE = re.compile(r"(?:^|\s)((?:[~/.]|[\w]+/)[\w./\-]+\.\w+)", re.MULTILINE)
_QUOTED_FILE_RE = re.compile(r"['\"`]([\w./\-]+\.\w+)['\"`]")


@dataclass
class EnhancedPrompt:
    """Result of prompt enhancement."""

    original: str
    enhanced: str
    intent: str          # detected primary intent
    context_added: list[str]  # what was injected (for buddy to announce)
    was_enhanced: bool   # False if prompt was already specific enough


def detect_intent(prompt: str) -> str:
    """Detect the primary intent of a user prompt."""
    for intent, pattern in _INTENT_PATTERNS:
        if pattern.search(prompt):
            return intent
    return "general"


def _get_git_context() -> str | None:
    """Get compact git state (branch + short status)."""
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        if branch.returncode != 0:
            return None

        status = subprocess.run(
            ["git", "status", "--porcelain", "-u"],
            capture_output=True, text=True, timeout=3,
        )
        branch_name = branch.stdout.strip()
        changed = len([l for l in status.stdout.strip().split("\n") if l.strip()])

        if changed > 0:
            return f"Git: branch={branch_name}, {changed} changed files"
        return f"Git: branch={branch_name}, clean"
    except Exception:
        return None


def _get_project_files() -> str | None:
    """Get a compact snapshot of key project files in cwd."""
    cwd = Path.cwd()
    key_files = []

    # Check for common project indicators
    indicators = [
        "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
        "Makefile", "Dockerfile", "docker-compose.yml",
        "README.md", ".gitignore", "tsconfig.json", "setup.py",
    ]
    for f in indicators:
        if (cwd / f).exists():
            key_files.append(f)

    if not key_files:
        return None
    return f"Project files: {', '.join(key_files)}"


def _get_referenced_file_context(prompt: str) -> list[tuple[str, str]]:
    """If user references files, check if they exist and get basic info."""
    files_found: list[tuple[str, str]] = []
    candidates = _FILE_REF_RE.findall(prompt) + _QUOTED_FILE_RE.findall(prompt)

    for fpath in candidates[:3]:  # max 3 files
        expanded = os.path.expanduser(fpath)
        if os.path.isfile(expanded):
            size = os.path.getsize(expanded)
            ext = os.path.splitext(expanded)[1]
            lang_map = {
                ".py": "Python", ".rs": "Rust", ".ts": "TypeScript",
                ".tsx": "TypeScript/React", ".js": "JavaScript",
                ".jsx": "JavaScript/React", ".go": "Go", ".java": "Java",
                ".rb": "Ruby", ".css": "CSS", ".html": "HTML",
                ".json": "JSON", ".yaml": "YAML", ".yml": "YAML",
                ".toml": "TOML", ".md": "Markdown", ".sh": "Shell",
            }
            lang = lang_map.get(ext, ext)
            files_found.append((fpath, f"{lang}, {size} bytes"))

    return files_found


# ── Intent-specific enhancement instructions ───────────────────────────────

_INTENT_INSTRUCTIONS: dict[str, str] = {
    "debug": (
        "Approach: Read the relevant code first, identify the root cause, "
        "then fix it surgically. Show what was wrong and why the fix works. "
        "If you need to run commands to reproduce, do so."
    ),
    "build": (
        "Approach: Understand existing patterns in the codebase before writing. "
        "Follow the project's conventions. Write complete, working code — no stubs "
        "or placeholders. Include necessary imports and type hints."
    ),
    "explain": (
        "Approach: Be clear and concise. Start with the high-level answer, then "
        "go deeper. Use code snippets to illustrate. Reference the actual codebase "
        "when relevant."
    ),
    "refactor": (
        "Approach: Read the current code first. Preserve behavior exactly — only "
        "change structure. Show before/after for key changes. Run existing tests "
        "if available."
    ),
    "test": (
        "Approach: Read the code being tested to understand its contract. Write "
        "tests that cover happy path, edge cases, and error cases. Use the "
        "project's existing test framework and patterns."
    ),
    "review": (
        "Approach: Read the code thoroughly. Check for bugs, security issues, "
        "performance problems, and style violations. Be specific — reference "
        "line numbers and explain the impact of each issue."
    ),
    "deploy": (
        "Approach: Check existing deployment configs first. Make changes "
        "incrementally. Verify each step works before proceeding to the next."
    ),
    "git": (
        "Approach: Check git status first. Be precise with git operations. "
        "Prefer safe operations (new commits over amends, merge over rebase "
        "for shared branches)."
    ),
}


def enhance_prompt(
    user_input: str,
    *,
    include_git: bool = True,
    include_project: bool = True,
    include_files: bool = True,
) -> EnhancedPrompt:
    """Enhance a user prompt with context and structured instructions.

    Returns an EnhancedPrompt with the enriched version and metadata
    about what was added (so the buddy can announce it).
    """
    # Skip enhancement for very short commands or questions
    stripped = user_input.strip()
    if len(stripped) < 5 or stripped.startswith("/"):
        return EnhancedPrompt(
            original=user_input,
            enhanced=user_input,
            intent="general",
            context_added=[],
            was_enhanced=False,
        )

    # Skip if already very detailed (> 500 chars probably has enough context)
    if len(stripped) > 500:
        intent = detect_intent(stripped)
        return EnhancedPrompt(
            original=user_input,
            enhanced=user_input,
            intent=intent,
            context_added=[],
            was_enhanced=False,
        )

    intent = detect_intent(stripped)
    context_parts: list[str] = []
    context_added: list[str] = []

    # 1. Working directory
    cwd = os.getcwd()
    home = os.path.expanduser("~")
    cwd_display = "~" + cwd[len(home):] if cwd.startswith(home) else cwd
    context_parts.append(f"Working directory: {cwd_display}")
    context_added.append("cwd")

    # 2. Git state
    if include_git:
        git_ctx = _get_git_context()
        if git_ctx:
            context_parts.append(git_ctx)
            context_added.append("git")

    # 3. Project files
    if include_project:
        proj_ctx = _get_project_files()
        if proj_ctx:
            context_parts.append(proj_ctx)
            context_added.append("project")

    # 4. Referenced file info
    if include_files:
        file_refs = _get_referenced_file_context(stripped)
        if file_refs:
            for fpath, info in file_refs:
                context_parts.append(f"Referenced: {fpath} ({info})")
            context_added.append("files")

    # 5. Intent-specific instructions
    instructions = _INTENT_INSTRUCTIONS.get(intent, "")

    # Build the enhanced prompt
    parts: list[str] = []

    # Context block
    if context_parts:
        ctx_block = "\n".join(f"- {p}" for p in context_parts)
        parts.append(f"[Context]\n{ctx_block}")

    # Intent instruction
    if instructions:
        parts.append(f"[{intent.title()} Mode]\n{instructions}")
        context_added.append(f"{intent} mode")

    # Original prompt (always last, always prominent)
    parts.append(f"[User Request]\n{stripped}")

    enhanced = "\n\n".join(parts)

    return EnhancedPrompt(
        original=user_input,
        enhanced=enhanced,
        intent=intent,
        context_added=context_added,
        was_enhanced=True,
    )


def describe_enhancement(result: EnhancedPrompt) -> str:
    """Generate a short description of what was enhanced (for buddy bubble)."""
    if not result.was_enhanced:
        return ""

    parts = []
    if "git" in result.context_added:
        parts.append("git state")
    if "project" in result.context_added:
        parts.append("project info")
    if "files" in result.context_added:
        parts.append("file context")

    mode = result.intent if result.intent != "general" else ""

    if mode and parts:
        return f"*enhanced* +{', '.join(parts)} [{mode} mode]"
    elif mode:
        return f"*enhanced* [{mode} mode]"
    elif parts:
        return f"*enhanced* +{', '.join(parts)}"
    return "*enhanced* +context"
