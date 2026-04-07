"""Smart error detection, user-friendly messages, and fallback logic for DJcode.

Catches common errors from Ollama, MLX, providers, tools, and network —
translates cryptic tracebacks into actionable messages with fix suggestions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass
class DJcodeError:
    """A structured error with user-friendly message and suggested fix."""
    category: str           # connection, model, tool, auth, memory, system
    message: str            # One-line human-readable description
    suggestion: str         # What the user should do
    original: str           # Original exception message
    recoverable: bool       # Can the system retry/fallback?
    fallback: str | None    # Fallback action to take (or None)


# ── Error pattern registry ─────────────────────────────────────────────────
# Each pattern: (regex on exception str, category, message, suggestion, recoverable, fallback)

_ERROR_PATTERNS: list[tuple[re.Pattern[str], str, str, str, bool, str | None]] = [
    # ── Connection errors ──────────────────────────────────────────────
    (
        re.compile(r"(connect|connection).*(refuse|reset|abort)", re.I),
        "connection",
        "Cannot connect to the model server.",
        "Start Ollama with: ollama serve",
        True,
        "retry_after_delay",
    ),
    (
        re.compile(r"(timeout|timed out|read timeout)", re.I),
        "connection",
        "Request timed out — the model is taking too long.",
        "Try a smaller model (/model qwen2.5-coder:7b) or increase timeout.",
        True,
        "retry_with_smaller_model",
    ),
    (
        re.compile(r"(name or service not known|nodename nor servname|dns)", re.I),
        "connection",
        "Cannot resolve hostname — check your network.",
        "Verify Ollama is running: ollama serve",
        False,
        None,
    ),
    (
        re.compile(r"(ssl|certificate|handshake)", re.I),
        "connection",
        "SSL/TLS error — secure connection failed.",
        "Check your network or proxy settings.",
        False,
        None,
    ),

    # ── Model errors ───────────────────────────────────────────────────
    (
        re.compile(r"model.*not found|404.*model|pull.*model", re.I),
        "model",
        "Model not found on this machine.",
        "Pull it with: ollama pull <model-name>",
        True,
        "suggest_available_models",
    ),
    (
        re.compile(r"out of memory|oom|alloc|mmap|memory", re.I),
        "model",
        "Not enough memory to run this model.",
        "Try a smaller model: /model qwen2.5-coder:7b (4.7 GB)",
        True,
        "retry_with_smaller_model",
    ),
    (
        re.compile(r"(context length|token limit|too long|max.*token)", re.I),
        "model",
        "Input exceeds the model's context window.",
        "Use /clear to reset conversation, or try a model with larger context.",
        True,
        "clear_and_retry",
    ),
    (
        re.compile(r"(does not support|unsupported|not supported).*tool", re.I),
        "model",
        "This model doesn't support tool calling.",
        "DJcode will retry without tools. For best results, use gemma4 or qwen2.5-coder.",
        True,
        "retry_without_tools",
    ),

    # ── Auth errors ────────────────────────────────────────────────────
    (
        re.compile(r"(401|unauthorized|invalid.*key|api.key|authentication)", re.I),
        "auth",
        "Authentication failed — invalid or missing API key.",
        "Run /auth to configure your API key.",
        False,
        None,
    ),
    (
        re.compile(r"(403|forbidden|permission|access denied)", re.I),
        "auth",
        "Access denied — your API key lacks permissions.",
        "Check your API key permissions, or run /auth to reconfigure.",
        False,
        None,
    ),
    (
        re.compile(r"(429|rate.limit|too many|throttl)", re.I),
        "auth",
        "Rate limited — too many requests.",
        "Wait a moment, then retry. Or switch to a local model: /model gemma4",
        True,
        "retry_after_delay",
    ),

    # ── Tool errors ────────────────────────────────────────────────────
    (
        re.compile(r"(permission denied|errno 13|eacces)", re.I),
        "tool",
        "Permission denied — cannot access the file or directory.",
        "Check file permissions, or run from a directory you own.",
        False,
        None,
    ),
    (
        re.compile(r"(no such file|enoent|FileNotFoundError|does not exist|not found.*file|not found.*dir)", re.I),
        "tool",
        "File or directory not found.",
        "Check the path. Use /scout to explore the codebase first.",
        False,
        None,
    ),
    (
        re.compile(r"(command not found|not recognized|no such command)", re.I),
        "tool",
        "Command not found — the program isn't installed.",
        "Install the missing tool, or check your PATH.",
        False,
        None,
    ),

    # ── JSON / parsing ─────────────────────────────────────────────────
    (
        re.compile(r"(json.*decode|invalid json|unexpected token|parse error)", re.I),
        "system",
        "Failed to parse the model's response.",
        "The model returned malformed output. Try again or switch models.",
        True,
        "retry",
    ),

    # ── Python / system ────────────────────────────────────────────────
    (
        re.compile(r"asyncio.*run.*running.*loop", re.I),
        "system",
        "Internal async conflict detected.",
        "This is a DJcode bug. Please report it on GitHub.",
        False,
        None,
    ),
]


def classify_error(exc: BaseException) -> DJcodeError:
    """Classify an exception into a structured DJcodeError with actionable message."""
    error_str = str(exc)
    exc_type = type(exc).__name__

    # Try each pattern
    for pattern, category, message, suggestion, recoverable, fallback in _ERROR_PATTERNS:
        if pattern.search(error_str) or pattern.search(exc_type):
            return DJcodeError(
                category=category,
                message=message,
                suggestion=suggestion,
                original=error_str,
                recoverable=recoverable,
                fallback=fallback,
            )

    # Fallback: unknown error
    short = error_str[:200] if len(error_str) > 200 else error_str
    return DJcodeError(
        category="unknown",
        message=f"Unexpected error: {exc_type}",
        suggestion="Try again. If this persists, check /config or report on GitHub.",
        original=short,
        recoverable=False,
        fallback=None,
    )


def format_error(err: DJcodeError, *, verbose: bool = False) -> str:
    """Format a DJcodeError as a Rich-compatible string for display."""
    icon = {
        "connection": "\U0001f50c",  # plug
        "model": "\U0001f9e0",       # brain
        "auth": "\U0001f511",        # key
        "tool": "\U0001f6e0",        # wrench
        "system": "\u26a0\ufe0f",    # warning
        "memory": "\U0001f4be",      # floppy
        "unknown": "\u2753",         # question
    }.get(err.category, "\u274c")

    lines = [
        f"[red]{icon} {err.message}[/]",
        f"  [dim yellow]{err.suggestion}[/]",
    ]

    if verbose and err.original:
        lines.append(f"  [dim]{err.original[:300]}[/]")

    if err.recoverable and err.fallback:
        lines.append(f"  [dim green]Auto-recovery: {err.fallback.replace('_', ' ')}[/]")

    return "\n".join(lines)


# ── Fallback actions ───────────────────────────────────────────────────────

FALLBACK_MODELS = ["qwen2.5-coder:7b", "gemma4", "dolphin3"]


def get_fallback_model(current: str) -> str | None:
    """Suggest a smaller fallback model."""
    for m in FALLBACK_MODELS:
        if m != current:
            return m
    return None
