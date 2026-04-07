"""Tool Extraction Router — enables ANY model to execute tools from plain text.

Models like dolphin3, llama3, mistral don't support function calling / tool_calls.
They output plain text describing what they want to do. This router:

1. Parses the model's text output for tool intents (file writes, commands, edits, etc.)
2. Extracts file paths, code blocks, shell commands
3. Asks for user confirmation (unless auto_accept)
4. Executes using DJcode's existing tool dispatch
5. Returns results for potential feedback to the model

Works with ANY model output — messy, unstructured, or clean.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import questionary
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from djcode.tools import dispatch_tool

logger = logging.getLogger(__name__)
console = Console()

GOLD = "#FFD700"

# ── Intent data structures ────────────────────────────────────────────────


@dataclass
class ToolIntent:
    """A single extracted tool intent from model text output."""

    action: str  # "file_write", "file_edit", "bash", "mkdir", "git"
    path: str | None  # File/dir path if applicable
    content: str | None  # File content or command string
    description: str  # Human-readable description
    confidence: float  # 0.0-1.0 confidence in this extraction
    line_range: tuple[int, int] | None = None  # For edits: which lines
    old_string: str | None = None  # For file_edit: text to replace
    new_string: str | None = None  # For file_edit: replacement text


@dataclass
class ToolResult:
    """Result from executing a tool intent."""

    intent: ToolIntent
    success: bool
    output: str
    skipped: bool = False  # True if user declined execution


# ── Code block extraction ─────────────────────────────────────────────────

# Matches fenced code blocks: ```lang\ncontent\n```
_CODE_BLOCK_RE = re.compile(
    r"```(\w+)?\s*\n(.*?)```",
    re.DOTALL,
)

# ── File path patterns ────────────────────────────────────────────────────

# Common file path patterns in model output
_FILE_PATH_PATTERNS = [
    # Explicit file path references (Unix-style)
    re.compile(r"(?:^|\s)((?:~/|/|\.\.?/)?(?:[\w\-./]+/)*[\w\-]+\.[\w]+)", re.MULTILINE),
]

# Language to extension mapping for inferring missing extensions
_LANG_TO_EXT: dict[str, str] = {
    "python": ".py",
    "py": ".py",
    "javascript": ".js",
    "js": ".js",
    "typescript": ".ts",
    "ts": ".ts",
    "tsx": ".tsx",
    "jsx": ".jsx",
    "html": ".html",
    "css": ".css",
    "scss": ".scss",
    "json": ".json",
    "yaml": ".yaml",
    "yml": ".yml",
    "toml": ".toml",
    "rust": ".rs",
    "rs": ".rs",
    "go": ".go",
    "java": ".java",
    "c": ".c",
    "cpp": ".cpp",
    "h": ".h",
    "hpp": ".hpp",
    "ruby": ".rb",
    "rb": ".rb",
    "php": ".php",
    "swift": ".swift",
    "kotlin": ".kt",
    "scala": ".scala",
    "sql": ".sql",
    "sh": ".sh",
    "bash": ".sh",
    "shell": ".sh",
    "zsh": ".sh",
    "dockerfile": "Dockerfile",
    "docker": "Dockerfile",
    "makefile": "Makefile",
    "make": "Makefile",
    "markdown": ".md",
    "md": ".md",
    "xml": ".xml",
    "svg": ".svg",
    "graphql": ".graphql",
    "proto": ".proto",
    "r": ".r",
    "lua": ".lua",
    "vim": ".vim",
    "conf": ".conf",
    "ini": ".ini",
    "env": ".env",
    "nginx": ".conf",
}

# ── File creation intent patterns ─────────────────────────────────────────

_FILE_CREATE_PATTERNS = [
    # "create a file called X.ext" / "create X.ext" / "let me create X.ext"
    # MUST have a file extension to avoid matching generic words like "the"
    re.compile(
        r"(?:let\s+me\s+)?(?:create|make|generate)\s+(?:a\s+)?(?:new\s+)?(?:file\s+)?(?:called\s+|named\s+)?[`\"']?([\w\-./~]+\.[\w]+)[`\"']?",
        re.IGNORECASE,
    ),
    # "write to X.ext" / "save this to X.ext"
    re.compile(
        r"(?:write|save|output)\s+(?:this\s+|the\s+following\s+)?(?:to|into)\s+[`\"']?([\w\-./~]+\.[\w]+)[`\"']?",
        re.IGNORECASE,
    ),
    # "Here's the content for X.ext:" / "Here is X.ext:"
    re.compile(
        r"(?:here(?:'s|\s+is)\s+(?:the\s+)?(?:content\s+|code\s+)?(?:for|of)\s+)[`\"']?([\w\-./~]+\.[\w]+)[`\"']?",
        re.IGNORECASE,
    ),
    # "I'll create X.ext" / "I will write X.ext"
    re.compile(
        r"I(?:'ll|\s+will)\s+(?:create|write|make|generate)\s+[`\"']?([\w\-./~]+\.[\w]+)[`\"']?",
        re.IGNORECASE,
    ),
    # "The file X should contain:" / "X will have:"
    re.compile(
        r"(?:the\s+)?(?:file\s+)?[`\"']?([\w\-./~]+\.[\w]+)[`\"']?\s+(?:should\s+)?(?:contain|have|look\s+like)",
        re.IGNORECASE,
    ),
    # Backtick-wrapped path: `path/to/file.ext`
    re.compile(
        r"`([\w\-./~]+\.[\w]+)`",
        re.IGNORECASE,
    ),
    # **path/to/file.ext** (bold in markdown)
    re.compile(
        r"\*\*([\w\-./~]+\.[\w]+)\*\*",
        re.IGNORECASE,
    ),
]

# ── File edit patterns ────────────────────────────────────────────────────

_FILE_EDIT_PATTERNS = [
    # "edit X" / "modify X" / "change X" / "update X"
    re.compile(
        r"(?:edit|modify|change|update|fix)\s+(?:the\s+)?(?:file\s+)?[`\"']?([\w\-./~]+\.[\w]+)[`\"']?",
        re.IGNORECASE,
    ),
    # "in file X" / "in X, line N"
    re.compile(
        r"in\s+(?:the\s+)?(?:file\s+)?[`\"']?([\w\-./~]+\.[\w]+)[`\"']?(?:,?\s+(?:at\s+)?line\s+(\d+))?",
        re.IGNORECASE,
    ),
    # "add the following to X" / "append to X"
    re.compile(
        r"(?:add|append|insert)\s+(?:the\s+following\s+)?(?:to|into)\s+[`\"']?([\w\-./~]+\.[\w]+)[`\"']?",
        re.IGNORECASE,
    ),
]

# "replace X with Y" pattern
_REPLACE_PATTERN = re.compile(
    r"replace\s+[`\"'](.+?)[`\"']\s+with\s+[`\"'](.+?)[`\"']",
    re.IGNORECASE | re.DOTALL,
)

# ── Command execution patterns ────────────────────────────────────────────

_COMMAND_PATTERNS = [
    # "run this command:" / "execute:" / "run:"
    re.compile(
        r"(?:run|execute|type|enter)\s+(?:this\s+)?(?:command|the\s+following)?:?\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    # "I'll run X" / "let me run X"
    re.compile(
        r"(?:let\s+me\s+|I(?:'ll|\s+will)\s+)(?:run|execute)\s+(?:this\s+)?(?:command\s+)?:?\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    # Lines starting with $ or >
    re.compile(r"^\s*[\$>]\s+(.+)$", re.MULTILINE),
]

# "install X" → detect package manager
_INSTALL_PATTERN = re.compile(
    r"(?:^|\s)(?:install|add)\s+(?:the\s+)?(?:package\s+)?[`\"']?([\w\-@/]+)[`\"']?\s*(?:(?:using|with|via)\s+(pip|npm|yarn|pnpm|brew|apt|cargo|go))?",
    re.IGNORECASE,
)

# "start the server" / "run the app" / "build the project"
_RUN_APP_PATTERNS = [
    re.compile(r"start\s+(?:the\s+)?(?:dev\s+)?server", re.IGNORECASE),
    re.compile(r"run\s+(?:the\s+)?(?:app|application|project|dev server)", re.IGNORECASE),
    re.compile(r"build\s+(?:the\s+)?(?:project|app|application)", re.IGNORECASE),
]

# ── Directory creation patterns ───────────────────────────────────────────

_MKDIR_PATTERNS = [
    re.compile(
        r"(?:create|make)\s+(?:a\s+)?(?:directory|folder|dir)\s+(?:called\s+|named\s+)?[`\"']?([\w\-./~]+)[`\"']?",
        re.IGNORECASE,
    ),
    re.compile(
        r"mkdir\s+(?:-p\s+)?[`\"']?([\w\-./~]+)[`\"']?",
        re.IGNORECASE,
    ),
]

# ── Git patterns ──────────────────────────────────────────────────────────

_GIT_PATTERNS = [
    re.compile(r"(?:git\s+)?(commit)\s+(?:this|these|the\s+changes)", re.IGNORECASE),
    re.compile(r"(?:git\s+)?(push)\s+(?:to\s+)?(\w+)?", re.IGNORECASE),
    re.compile(r"(?:create|make)\s+(?:a\s+)?(?:new\s+)?(?:git\s+)?(branch)\s+(?:called\s+|named\s+)?[`\"']?(\S+)[`\"']?", re.IGNORECASE),
    re.compile(r"(git\s+\w+(?:\s+[\w\-./\"']+)*)", re.IGNORECASE),
]


# ── Main Router ───────────────────────────────────────────────────────────


class ToolExtractionRouter:
    """Extracts and executes tool calls from plain text model output.

    For models that don't support function calling, this router:
    1. Scans the response for code blocks, file paths, shell commands
    2. Detects the INTENT (create file, edit file, run command, etc.)
    3. Asks for user confirmation (unless auto_accept)
    4. Executes using DJcode's existing tool dispatch
    5. Returns the results
    """

    # Confidence threshold — only execute intents above this
    CONFIDENCE_THRESHOLD = 0.6

    def __init__(self) -> None:
        self._cwd = os.getcwd()

    # ── Public API ────────────────────────────────────────────────────────

    def extract_intents(self, text: str) -> list[ToolIntent]:
        """Parse model text output and extract all tool intents.

        Returns a list of ToolIntent objects sorted by position in the text,
        filtered to confidence > CONFIDENCE_THRESHOLD.
        """
        intents: list[ToolIntent] = []

        # Extract code blocks first — they are the richest signal
        code_blocks = self._extract_code_blocks(text)
        logger.debug("Extracted %d code blocks", len(code_blocks))

        # Track which code blocks have been claimed by an intent
        claimed_blocks: set[int] = set()

        # 1. File creation intents
        file_intents, file_claimed = self._extract_file_creates(text, code_blocks)
        intents.extend(file_intents)
        claimed_blocks.update(file_claimed)

        # 2. File edit intents
        edit_intents = self._extract_file_edits(text, code_blocks)
        intents.extend(edit_intents)

        # 3. Directory creation intents
        dir_intents = self._extract_mkdir(text)
        intents.extend(dir_intents)

        # 4. Git intents
        git_intents = self._extract_git(text)
        intents.extend(git_intents)

        # 5. Command execution intents (bash/shell code blocks + explicit commands)
        cmd_intents, cmd_claimed = self._extract_commands(text, code_blocks, claimed_blocks)
        intents.extend(cmd_intents)
        claimed_blocks.update(cmd_claimed)

        # 6. Unclaimed code blocks with file paths nearby — treat as file_write
        orphan_intents = self._handle_orphan_blocks(text, code_blocks, claimed_blocks)
        intents.extend(orphan_intents)

        # Deduplicate by (action, path, content hash)
        intents = self._deduplicate(intents)

        # Filter by confidence
        intents = [i for i in intents if i.confidence >= self.CONFIDENCE_THRESHOLD]

        logger.debug("Final intents: %d", len(intents))
        for i in intents:
            logger.debug(
                "  %s: %s (conf=%.2f) %s",
                i.action,
                i.path or "<no path>",
                i.confidence,
                i.description,
            )

        return intents

    async def extract_and_execute(
        self,
        text: str,
        auto_accept: bool = False,
    ) -> list[ToolResult]:
        """Main pipeline: parse text -> extract intents -> confirm -> execute -> return results."""

        intents = self.extract_intents(text)
        if not intents:
            return []

        # Show user what we found
        if not auto_accept:
            approved = await self._confirm_intents(intents)
        else:
            approved = intents
            self._display_intents_summary(intents, auto=True)

        results: list[ToolResult] = []
        for intent in intents:
            if intent not in approved:
                results.append(ToolResult(
                    intent=intent,
                    success=False,
                    output="Skipped by user",
                    skipped=True,
                ))
                continue

            result = await self._execute_intent(intent)
            results.append(result)

            # Display result inline
            if not result.skipped:
                icon = "\u2705" if result.success else "\u274c"
                style = "green" if result.success else "red"
                console.print(f"  {icon} [{style}]{intent.description}[/]")
                if result.output and not result.success:
                    console.print(f"    [dim red]{result.output[:200]}[/]")

        return results

    def format_results_for_context(self, results: list[ToolResult]) -> str:
        """Format tool results as context text to feed back to the model."""
        if not results:
            return ""

        parts = ["[Tool Execution Results]"]
        for r in results:
            if r.skipped:
                parts.append(f"- SKIPPED: {r.intent.description}")
            elif r.success:
                parts.append(f"- SUCCESS: {r.intent.description}")
                if r.output:
                    # Truncate long output
                    output = r.output if len(r.output) < 500 else r.output[:500] + "..."
                    parts.append(f"  Output: {output}")
            else:
                parts.append(f"- FAILED: {r.intent.description}")
                parts.append(f"  Error: {r.output}")

        return "\n".join(parts)

    # ── Code block extraction ─────────────────────────────────────────────

    def _extract_code_blocks(self, text: str) -> list[dict[str, Any]]:
        """Extract all fenced code blocks from the text.

        Returns list of dicts with keys: lang, content, start, end, index
        """
        blocks = []
        for idx, m in enumerate(_CODE_BLOCK_RE.finditer(text)):
            lang = (m.group(1) or "").strip().lower()
            content = m.group(2)
            # Strip trailing whitespace but preserve internal structure
            content = content.rstrip()
            blocks.append({
                "lang": lang,
                "content": content,
                "start": m.start(),
                "end": m.end(),
                "index": idx,
            })
        return blocks

    # ── File creation extraction ──────────────────────────────────────────

    def _extract_file_creates(
        self,
        text: str,
        code_blocks: list[dict[str, Any]],
    ) -> tuple[list[ToolIntent], set[int]]:
        """Extract file creation intents.

        Looks for file path mentions followed by code blocks.
        Returns (intents, set of claimed block indices).
        """
        intents: list[ToolIntent] = []
        claimed: set[int] = set()

        # Strategy: find file path mentions, then find the nearest code block after them
        file_mentions: list[tuple[str, int, float]] = []  # (path, text_position, confidence)

        for pattern in _FILE_CREATE_PATTERNS:
            for m in pattern.finditer(text):
                path = m.group(1)
                pos = m.start()
                # Higher confidence for explicit "create file" language
                conf = 0.9 if any(
                    kw in m.group(0).lower()
                    for kw in ("create", "write to", "save to", "i'll create", "i will create")
                ) else 0.7
                file_mentions.append((path, pos, conf))

        # Sort by position in text
        file_mentions.sort(key=lambda x: x[1])

        # For each file mention, find the closest code block that follows it
        for path, pos, conf in file_mentions:
            # Skip if this looks like a command, not a file
            if path.startswith("$") or path.startswith(">"):
                continue

            best_block = None
            best_dist = float("inf")

            for block in code_blocks:
                if block["index"] in claimed:
                    continue
                # Block must come AFTER the file mention
                if block["start"] > pos:
                    dist = block["start"] - pos
                    if dist < best_dist:
                        best_dist = dist
                        best_block = block

            if best_block is not None and best_dist < 2000:
                # Resolve path
                resolved = self._resolve_path(path, lang=best_block["lang"])
                content = best_block["content"]
                line_count = content.count("\n") + 1

                intents.append(ToolIntent(
                    action="file_write",
                    path=resolved,
                    content=content,
                    description=f"Create {resolved} ({line_count} lines)",
                    confidence=conf,
                ))
                claimed.add(best_block["index"])

        return intents, claimed

    # ── File edit extraction ──────────────────────────────────────────────

    def _extract_file_edits(
        self,
        text: str,
        code_blocks: list[dict[str, Any]],
    ) -> list[ToolIntent]:
        """Extract file edit intents (modify, replace, append)."""
        intents: list[ToolIntent] = []

        # "replace X with Y" pattern
        for m in _REPLACE_PATTERN.finditer(text):
            old = m.group(1)
            new = m.group(2)

            # Try to find which file this applies to — look backwards for a file path
            before_text = text[: m.start()]
            file_path = self._find_nearest_file_path(before_text, direction="backward")

            if file_path:
                resolved = self._resolve_path(file_path)
                intents.append(ToolIntent(
                    action="file_edit",
                    path=resolved,
                    content=None,
                    description=f"Edit {resolved}: replace text",
                    confidence=0.85,
                    old_string=old,
                    new_string=new,
                ))

        # Edit/modify/update patterns with code blocks
        for pattern in _FILE_EDIT_PATTERNS:
            for m in pattern.finditer(text):
                path = m.group(1)
                resolved = self._resolve_path(path)
                line_num = int(m.group(2)) if m.lastindex and m.lastindex >= 2 and m.group(2) else None

                # Check if the file exists — if it does, it's probably an edit, not create
                if Path(resolved).exists():
                    intents.append(ToolIntent(
                        action="file_edit",
                        path=resolved,
                        content=None,
                        description=f"Edit {resolved}" + (f" at line {line_num}" if line_num else ""),
                        confidence=0.7,
                        line_range=(line_num, line_num) if line_num else None,
                    ))

        return intents

    # ── Command extraction ────────────────────────────────────────────────

    def _extract_commands(
        self,
        text: str,
        code_blocks: list[dict[str, Any]],
        claimed: set[int],
    ) -> tuple[list[ToolIntent], set[int]]:
        """Extract bash/shell command intents."""
        intents: list[ToolIntent] = []
        new_claimed: set[int] = set()

        # 1. Code blocks with bash/shell/sh language tag
        for block in code_blocks:
            if block["index"] in claimed:
                continue
            if block["lang"] in ("bash", "shell", "sh", "zsh", "console", "terminal", ""):
                content = block["content"].strip()
                if not content:
                    continue

                # Check if this looks like a command (not a file)
                is_command = self._looks_like_command(content, block["lang"])
                if not is_command:
                    continue

                # Split multi-line commands — each line could be a separate command
                lines = content.splitlines()
                commands = []
                current_cmd = []
                for line in lines:
                    stripped = line.strip()
                    # Strip leading $ or > prompt markers
                    if stripped.startswith("$ "):
                        stripped = stripped[2:]
                    elif stripped.startswith("> "):
                        stripped = stripped[2:]

                    # Skip comment-only lines and empty lines
                    if not stripped or stripped.startswith("#"):
                        if current_cmd:
                            commands.append("\n".join(current_cmd))
                            current_cmd = []
                        continue

                    # Line continuation
                    if stripped.endswith("\\"):
                        current_cmd.append(stripped[:-1].rstrip())
                        continue

                    current_cmd.append(stripped)
                    commands.append("\n".join(current_cmd))
                    current_cmd = []

                if current_cmd:
                    commands.append("\n".join(current_cmd))

                for cmd in commands:
                    if not cmd.strip():
                        continue
                    # Determine confidence based on context
                    conf = 0.85 if block["lang"] in ("bash", "shell", "sh", "zsh") else 0.65
                    cmd_short = cmd if len(cmd) < 60 else cmd[:57] + "..."

                    intents.append(ToolIntent(
                        action="bash",
                        path=None,
                        content=cmd,
                        description=f"Run: {cmd_short}",
                        confidence=conf,
                    ))

                new_claimed.add(block["index"])

        # 2. Lines starting with $ or > (inline commands, not in code blocks)
        for m in _COMMAND_PATTERNS[2].finditer(text):
            cmd = m.group(1).strip()
            # Make sure this isn't inside a code block
            in_block = any(
                b["start"] <= m.start() <= b["end"]
                for b in code_blocks
            )
            if not in_block and cmd:
                intents.append(ToolIntent(
                    action="bash",
                    path=None,
                    content=cmd,
                    description=f"Run: {cmd}",
                    confidence=0.7,
                ))

        # 3. "install X" patterns
        for m in _INSTALL_PATTERN.finditer(text):
            pkg = m.group(1)
            manager = m.group(2) if m.lastindex and m.lastindex >= 2 and m.group(2) else None

            # Skip if inside a code block (already handled)
            in_block = any(
                b["start"] <= m.start() <= b["end"]
                for b in code_blocks
            )
            if in_block:
                continue

            if manager:
                cmd = f"{manager} install {pkg}"
            else:
                # Infer package manager from context
                cmd = self._infer_install_command(pkg, text)

            if cmd:
                intents.append(ToolIntent(
                    action="bash",
                    path=None,
                    content=cmd,
                    description=f"Run: {cmd}",
                    confidence=0.65,
                ))

        return intents, new_claimed

    # ── Directory creation extraction ─────────────────────────────────────

    def _extract_mkdir(self, text: str) -> list[ToolIntent]:
        """Extract directory creation intents."""
        intents: list[ToolIntent] = []

        for pattern in _MKDIR_PATTERNS:
            for m in pattern.finditer(text):
                path = m.group(1)
                resolved = self._resolve_path(path)

                intents.append(ToolIntent(
                    action="mkdir",
                    path=resolved,
                    content=None,
                    description=f"Create directory: {resolved}",
                    confidence=0.9,
                ))

        # Also detect project structure descriptions like:
        # src/
        #   components/
        #   utils/
        structure_re = re.compile(r"^(\s*)([\w\-]+)/\s*$", re.MULTILINE)
        struct_matches = list(structure_re.finditer(text))
        if len(struct_matches) >= 3:
            # Looks like a directory tree listing — extract paths
            root_indent = None
            paths: list[str] = []
            indent_stack: list[tuple[int, str]] = []

            for m in struct_matches:
                indent = len(m.group(1))
                name = m.group(2)

                if root_indent is None:
                    root_indent = indent

                # Pop stack to find parent
                while indent_stack and indent_stack[-1][0] >= indent:
                    indent_stack.pop()

                if indent_stack:
                    full_path = indent_stack[-1][1] + "/" + name
                else:
                    full_path = name

                indent_stack.append((indent, full_path))
                paths.append(full_path)

            for p in paths:
                resolved = self._resolve_path(p)
                intents.append(ToolIntent(
                    action="mkdir",
                    path=resolved,
                    content=None,
                    description=f"Create directory: {resolved}",
                    confidence=0.5,  # Lower confidence for inferred structures
                ))

        return intents

    # ── Git extraction ────────────────────────────────────────────────────

    def _extract_git(self, text: str) -> list[ToolIntent]:
        """Extract git-related intents."""
        intents: list[ToolIntent] = []

        for pattern in _GIT_PATTERNS[:-1]:  # Skip the catch-all
            for m in pattern.finditer(text):
                action = m.group(1).lower()
                extra = m.group(2) if m.lastindex and m.lastindex >= 2 and m.group(2) else ""

                if action == "commit":
                    intents.append(ToolIntent(
                        action="bash",
                        path=None,
                        content='git add -A && git commit -m "auto-commit from djcode"',
                        description="Git: commit changes",
                        confidence=0.75,
                    ))
                elif action == "push":
                    remote = extra or "origin"
                    intents.append(ToolIntent(
                        action="bash",
                        path=None,
                        content=f"git push {remote}",
                        description=f"Git: push to {remote}",
                        confidence=0.75,
                    ))
                elif action == "branch":
                    branch_name = extra
                    if branch_name:
                        intents.append(ToolIntent(
                            action="bash",
                            path=None,
                            content=f"git checkout -b {branch_name}",
                            description=f"Git: create branch {branch_name}",
                            confidence=0.8,
                        ))

        return intents

    # ── Orphan blocks ─────────────────────────────────────────────────────

    def _handle_orphan_blocks(
        self,
        text: str,
        code_blocks: list[dict[str, Any]],
        claimed: set[int],
    ) -> list[ToolIntent]:
        """Handle code blocks that weren't claimed by any other extraction.

        Looks backwards from the block for a file path mention to associate it with.
        """
        intents: list[ToolIntent] = []

        for block in code_blocks:
            if block["index"] in claimed:
                continue

            lang = block["lang"]
            content = block["content"].strip()

            if not content:
                continue

            # Skip blocks that look like commands
            if lang in ("bash", "shell", "sh", "zsh", "console", "terminal"):
                continue

            # Look backwards for a file path
            before_text = text[: block["start"]]
            file_path = self._find_nearest_file_path(before_text, direction="backward")

            if file_path:
                resolved = self._resolve_path(file_path, lang=lang)
                line_count = content.count("\n") + 1

                # Check if the file already exists — if so, this might be a full rewrite
                exists = Path(resolved).exists()
                action = "file_write"

                intents.append(ToolIntent(
                    action=action,
                    path=resolved,
                    content=content,
                    description=f"{'Overwrite' if exists else 'Create'} {resolved} ({line_count} lines)",
                    confidence=0.65,
                ))
            elif lang and lang not in ("text", "output", "log", "console"):
                # Has a language but no file path — lower confidence
                ext = _LANG_TO_EXT.get(lang, f".{lang}")
                if ext.startswith("."):
                    # Can't determine the file name, skip with very low confidence
                    logger.debug(
                        "Orphan %s block (no file path), skipping", lang
                    )

        return intents

    # ── Path utilities ────────────────────────────────────────────────────

    def _resolve_path(self, path: str, lang: str | None = None) -> str:
        """Resolve a file path to an absolute path.

        - Relative paths resolved from cwd
        - ~/ expanded to home dir
        - If no extension and lang is known, infer extension
        """
        path = path.strip().strip("'\"`")

        # Expand ~
        if path.startswith("~"):
            path = os.path.expanduser(path)

        # If no extension and we know the language, add one
        if "." not in os.path.basename(path) and lang:
            ext = _LANG_TO_EXT.get(lang)
            if ext and not ext.startswith("/") and ext.startswith("."):
                path = path + ext

        # Resolve relative to cwd
        p = Path(path)
        if not p.is_absolute():
            p = Path(self._cwd) / p

        return str(p.resolve())

    def _find_nearest_file_path(
        self, text: str, direction: str = "backward"
    ) -> str | None:
        """Find the nearest file path mention in text.

        If direction is "backward", searches from the end of text.
        """
        # Look for file paths in the text
        candidates: list[tuple[str, int]] = []

        for pattern in _FILE_PATH_PATTERNS:
            for m in pattern.finditer(text):
                path = m.group(1).strip()
                # Filter out unlikely paths
                if self._is_plausible_file_path(path):
                    candidates.append((path, m.start()))

        # Also check backtick-wrapped paths
        backtick_re = re.compile(r"`([\w\-./~]+\.[\w]+)`")
        for m in backtick_re.finditer(text):
            path = m.group(1)
            if self._is_plausible_file_path(path):
                candidates.append((path, m.start()))

        # Bold markdown paths
        bold_re = re.compile(r"\*\*([\w\-./~]+\.[\w]+)\*\*")
        for m in bold_re.finditer(text):
            path = m.group(1)
            if self._is_plausible_file_path(path):
                candidates.append((path, m.start()))

        if not candidates:
            return None

        if direction == "backward":
            # Return the one closest to the end
            candidates.sort(key=lambda x: x[1], reverse=True)
        else:
            candidates.sort(key=lambda x: x[1])

        return candidates[0][0]

    def _is_plausible_file_path(self, path: str) -> bool:
        """Check if a string looks like a plausible file path (not a URL, not a word)."""
        # Must have an extension or a slash
        if "." not in path and "/" not in path:
            return False

        # Filter out URLs
        if path.startswith("http://") or path.startswith("https://"):
            return False

        # Filter out common non-path patterns
        if path in ("e.g.", "i.e.", "etc.", "vs.", "no.", "Dr.", "Mr.", "Mrs.", "Ms."):
            return False

        # Must have a reasonable extension if it has a dot
        if "." in path:
            ext = path.rsplit(".", 1)[-1]
            if len(ext) > 10 or not ext.isalnum():
                return False

        return True

    def _looks_like_command(self, content: str, lang: str) -> bool:
        """Determine if a code block content looks like a shell command vs file content."""
        # Explicit bash/shell tag is strong signal
        if lang in ("bash", "shell", "sh", "zsh", "console", "terminal"):
            return True

        # No language tag — heuristic: short, starts with common commands
        if not lang:
            lines = content.strip().splitlines()
            if len(lines) <= 5:
                first = lines[0].strip()
                # Common command prefixes
                cmd_prefixes = (
                    "npm", "npx", "yarn", "pnpm", "pip", "python", "python3",
                    "node", "deno", "bun", "cargo", "go ", "make", "cmake",
                    "docker", "kubectl", "git ", "curl", "wget", "cat ",
                    "ls", "cd ", "mkdir", "rm ", "cp ", "mv ", "chmod",
                    "brew", "apt", "sudo", "ssh", "scp", "rsync",
                    "echo ", "export ", "source ", ".", "&&", "||",
                    "$", ">",
                )
                if any(first.lstrip("$ >").startswith(p) for p in cmd_prefixes):
                    return True

        return False

    def _infer_install_command(self, package: str, text: str) -> str | None:
        """Infer the right install command based on project context."""
        # Check for package.json in cwd
        if Path(self._cwd, "package.json").exists():
            if Path(self._cwd, "yarn.lock").exists():
                return f"yarn add {package}"
            elif Path(self._cwd, "pnpm-lock.yaml").exists():
                return f"pnpm add {package}"
            return f"npm install {package}"

        # Check for Python project markers
        if (
            Path(self._cwd, "pyproject.toml").exists()
            or Path(self._cwd, "setup.py").exists()
            or Path(self._cwd, "requirements.txt").exists()
        ):
            if Path(self._cwd, "pyproject.toml").exists():
                return f"pip install {package}"
            return f"pip install {package}"

        # Check for Cargo.toml (Rust)
        if Path(self._cwd, "Cargo.toml").exists():
            return f"cargo add {package}"

        # Check for go.mod (Go)
        if Path(self._cwd, "go.mod").exists():
            return f"go get {package}"

        # Default: look at text context for hints
        lower = text.lower()
        if "npm" in lower or "node" in lower or "javascript" in lower:
            return f"npm install {package}"
        if "pip" in lower or "python" in lower:
            return f"pip install {package}"

        return None

    # ── Deduplication ─────────────────────────────────────────────────────

    def _deduplicate(self, intents: list[ToolIntent]) -> list[ToolIntent]:
        """Remove duplicate intents based on action + path + content hash."""
        seen: set[str] = set()
        unique: list[ToolIntent] = []

        for intent in intents:
            content_hash = hash(intent.content) if intent.content else 0
            key = f"{intent.action}:{intent.path}:{content_hash}"
            if key not in seen:
                seen.add(key)
                unique.append(intent)

        return unique

    # ── User confirmation ─────────────────────────────────────────────────

    def _display_intents_summary(
        self, intents: list[ToolIntent], auto: bool = False
    ) -> None:
        """Display a Rich panel summarizing extracted intents."""
        lines: list[str] = []
        for intent in intents:
            icon = self._intent_icon(intent)
            conf = f"[dim](conf: {intent.confidence:.0%})[/]"

            if intent.action == "file_write":
                lc = intent.content.count("\n") + 1 if intent.content else 0
                lines.append(f"  {icon} [bold]Create:[/] {intent.path} ({lc} lines) {conf}")
            elif intent.action == "file_edit":
                lines.append(f"  {icon} [bold]Edit:[/] {intent.path} {conf}")
            elif intent.action == "bash":
                cmd = intent.content or ""
                if len(cmd) > 60:
                    cmd = cmd[:57] + "..."
                lines.append(f"  {icon} [bold]Run:[/] {cmd} {conf}")
            elif intent.action == "mkdir":
                lines.append(f"  {icon} [bold]Directory:[/] {intent.path} {conf}")
            else:
                lines.append(f"  {icon} [bold]{intent.action}:[/] {intent.description} {conf}")

        body = "\n".join(lines)
        if auto:
            body += f"\n\n  [dim]Auto-executing {len(intents)} actions...[/]"

        console.print(Panel(
            body,
            title=f"[bold {GOLD}]Tool Extraction[/]",
            border_style=GOLD,
            padding=(1, 1),
        ))

    async def _confirm_intents(self, intents: list[ToolIntent]) -> list[ToolIntent]:
        """Show intent summary and ask user for confirmation.

        Returns the list of approved intents.
        """
        self._display_intents_summary(intents)

        console.print(
            f"  [bold]Execute all?[/] [dim]([/][bold]Y[/][dim])es / ([/][bold]n[/][dim])o / ([/][bold]s[/][dim])elect[/]"
        )

        with concurrent.futures.ThreadPoolExecutor() as pool:
            choice = await asyncio.get_event_loop().run_in_executor(
                pool,
                lambda: questionary.text(
                    "",
                    default="y",
                ).ask(),
            )

        if choice is None:
            return []

        choice = (choice or "y").strip().lower()

        if choice in ("y", "yes", ""):
            return intents
        elif choice in ("n", "no"):
            return []
        elif choice in ("s", "select"):
            return await self._select_intents(intents)
        else:
            return intents  # Default to yes

    async def _select_intents(self, intents: list[ToolIntent]) -> list[ToolIntent]:
        """Interactive picker to select which intents to execute."""
        choices = []
        for intent in intents:
            icon = self._intent_icon(intent)
            label = f"{icon} {intent.description}"
            choices.append(questionary.Choice(label, value=intent, checked=True))

        with concurrent.futures.ThreadPoolExecutor() as pool:
            selected = await asyncio.get_event_loop().run_in_executor(
                pool,
                lambda: questionary.checkbox(
                    "Select actions to execute:",
                    choices=choices,
                ).ask(),
            )

        return selected or []

    def _intent_icon(self, intent: ToolIntent) -> str:
        """Get an icon for an intent type."""
        icons = {
            "file_write": "\u270f\ufe0f",  # pencil
            "file_edit": "\u2702\ufe0f",    # scissors
            "bash": "\u2699\ufe0f",         # gear
            "mkdir": "\U0001f4c1",          # folder
            "git": "\U0001f500",            # shuffle
        }
        return icons.get(intent.action, "\u26a1")

    # ── Execution ─────────────────────────────────────────────────────────

    async def _execute_intent(self, intent: ToolIntent) -> ToolResult:
        """Execute a single tool intent via DJcode's dispatch_tool."""
        try:
            if intent.action == "file_write":
                if not intent.path or intent.content is None:
                    return ToolResult(
                        intent=intent,
                        success=False,
                        output="Missing path or content for file_write",
                    )
                result = await dispatch_tool("file_write", {
                    "path": intent.path,
                    "content": intent.content,
                })
                return ToolResult(
                    intent=intent,
                    success="Error" not in result,
                    output=result,
                )

            elif intent.action == "file_edit":
                if not intent.path:
                    return ToolResult(
                        intent=intent,
                        success=False,
                        output="Missing path for file_edit",
                    )
                if intent.old_string and intent.new_string:
                    result = await dispatch_tool("file_edit", {
                        "path": intent.path,
                        "old_string": intent.old_string,
                        "new_string": intent.new_string,
                    })
                else:
                    return ToolResult(
                        intent=intent,
                        success=False,
                        output="Edit detected but old/new strings not fully extracted. Manual edit needed.",
                    )
                return ToolResult(
                    intent=intent,
                    success="Error" not in result,
                    output=result,
                )

            elif intent.action == "bash":
                if not intent.content:
                    return ToolResult(
                        intent=intent,
                        success=False,
                        output="No command to execute",
                    )
                result = await dispatch_tool("bash", {"command": intent.content})
                return ToolResult(
                    intent=intent,
                    success=True,  # bash always "succeeds" (returns output)
                    output=result,
                )

            elif intent.action == "mkdir":
                if not intent.path:
                    return ToolResult(
                        intent=intent,
                        success=False,
                        output="No directory path specified",
                    )
                result = await dispatch_tool("bash", {
                    "command": f"mkdir -p {intent.path}",
                })
                return ToolResult(
                    intent=intent,
                    success=True,
                    output=result or f"Created {intent.path}",
                )

            else:
                return ToolResult(
                    intent=intent,
                    success=False,
                    output=f"Unsupported action: {intent.action}",
                )

        except Exception as e:
            logger.exception("Error executing intent: %s", intent)
            return ToolResult(
                intent=intent,
                success=False,
                output=f"Exception: {e}",
            )
