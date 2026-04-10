"""LLM Provider abstraction for DJcode.

Backward-compatible shim that delegates to the new djcode.providers system.
All existing imports (Provider, ProviderConfig, Message, TOOL_DEFINITIONS,
fetch_ollama_models_sync, etc.) continue to work unchanged.

The new providers/ package adds:
- Anthropic prompt caching and extended thinking
- OpenAI o-series reasoning model support
- Google Gemini native API
- Unified ProviderChunk/TokenUsage protocol
- Model aliasing and auto-detection
- Rate limit handling with exponential backoff
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Any, AsyncIterator

import httpx

from djcode.config import load_config


@dataclass
class Message:
    """A single conversation message."""

    role: str  # "system", "user", "assistant", "tool"
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None


@dataclass
class ProviderConfig:
    """Provider connection settings."""

    name: str
    base_url: str
    model: str
    api_key: str = ""
    temperature: float = 0.7
    max_tokens: int = 8192

    @classmethod
    def from_config(
        cls,
        provider_override: str | None = None,
        model_override: str | None = None,
    ) -> ProviderConfig:
        """Build provider config from saved config + CLI overrides.

        Supports:
        - Known providers from the auth registry (ollama, openai, anthropic, etc.)
        - Custom providers defined in config["custom_providers"]
        - URL-as-provider: passing a URL (http/https) as the provider name
        - base_url override via config or DJCODE_BASE_URL env var
        """
        from djcode.auth import PROVIDERS, get_api_key, get_base_url

        cfg = load_config()
        provider = provider_override or cfg["provider"]
        model = model_override or cfg["model"]

        # 1. URL-as-provider: treat http(s) URLs as custom OpenAI-compatible endpoints
        if provider.startswith("http://") or provider.startswith("https://"):
            base_url = provider.rstrip("/")
            api_key = (
                os.environ.get("DJCODE_API_KEY", "")
                or os.environ.get("OPENAI_API_KEY", "")
                or cfg.get("remote_api_key", "")
            )
            provider = "custom"

        # 2. Check custom_providers from config
        elif provider in cfg.get("custom_providers", {}):
            custom = cfg["custom_providers"][provider]
            base_url = custom.get("base_url", "")
            api_key = custom.get("api_key", "") or os.environ.get("DJCODE_API_KEY", "")
            model = model_override or custom.get("model", model)
            provider = "custom"

        # 3. Known provider from auth registry
        elif provider in PROVIDERS:
            base_url = get_base_url(provider)
            api_key = get_api_key(provider)

        # 4. Legacy fallback for "remote" or unknown providers
        else:
            url_map = {
                "ollama": cfg.get("ollama_url", "http://localhost:11434"),
                "mlx": cfg.get("mlx_url", "http://localhost:8080"),
                "remote": cfg.get("remote_url", ""),
            }
            base_url = url_map.get(provider, cfg.get("ollama_url", "http://localhost:11434"))
            api_key = cfg.get("remote_api_key", "")
            if provider == "remote" and not api_key:
                api_key = os.environ.get("OPENAI_API_KEY", "")
                if not api_key:
                    api_key = os.environ.get("DJCODE_API_KEY", "")

        # Apply base_url override from config or env (takes precedence over everything)
        env_base_url = os.environ.get("DJCODE_BASE_URL", "")
        config_base_url = cfg.get("base_url", "")
        if env_base_url:
            base_url = env_base_url.rstrip("/")
        elif config_base_url:
            base_url = config_base_url.rstrip("/")

        return cls(
            name=provider,
            base_url=base_url,
            model=model,
            api_key=api_key,
            temperature=cfg.get("temperature", 0.7),
            max_tokens=cfg.get("max_tokens", 8192),
        )


# -- Tool definitions for the LLM --

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a shell command and return stdout/stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 120).",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_read",
            "description": "Read a file from the filesystem. Returns the file content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file to read.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Line number to start reading from (0-based).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of lines to read.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_write",
            "description": "Write content to a file (creates or overwrites).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file to write.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The content to write to the file.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_edit",
            "description": "Make a surgical edit to a file by replacing old_string with new_string.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file to edit.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "The exact string to find and replace.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "The replacement string.",
                    },
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search for a regex pattern in files. Returns matching lines.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory or file to search in.",
                    },
                    "include": {
                        "type": "string",
                        "description": "Glob pattern to filter files (e.g. '*.py').",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern (e.g. '**/*.py', 'src/**/*.rs').",
                    },
                    "path": {
                        "type": "string",
                        "description": "Base directory to search from.",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git",
            "description": "Run a git command (status, diff, log, add, commit, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "subcommand": {
                        "type": "string",
                        "description": "Git subcommand to run (e.g. 'status', 'diff', 'log --oneline -10').",
                    },
                },
                "required": ["subcommand"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch content from a URL and return its text. Useful for reading docs, APIs, or web pages.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to fetch.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum characters to return (default 10000).",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web using DuckDuckGo or Brave Search. Returns title, URL, and snippet for top results. Useful for looking up docs, finding solutions, researching libraries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query.",
                    },
                    "num_results": {
                        "type": "integer",
                        "description": "Number of results to return (1-20, default 5).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_create",
            "description": "Create a new task for tracking work items. Tasks persist across the session in SQLite.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": "Short title for the task.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Detailed description of what needs to be done.",
                    },
                    "priority": {
                        "type": "string",
                        "description": "Priority level: low, medium, high, critical (default: medium).",
                    },
                    "depends_on": {
                        "type": "string",
                        "description": "Comma-separated task IDs this depends on.",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tags for categorization.",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Associate with a specific DJcode session.",
                    },
                },
                "required": ["subject"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_update",
            "description": "Update an existing task's status, subject, description, priority, dependencies, or tags.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The ID of the task to update.",
                    },
                    "status": {
                        "type": "string",
                        "description": "New status: pending, in_progress, completed, blocked, cancelled.",
                    },
                    "subject": {
                        "type": "string",
                        "description": "New subject/title.",
                    },
                    "description": {
                        "type": "string",
                        "description": "New description.",
                    },
                    "priority": {
                        "type": "string",
                        "description": "New priority: low, medium, high, critical.",
                    },
                    "depends_on": {
                        "type": "string",
                        "description": "New dependency list (comma-separated task IDs).",
                    },
                    "tags": {
                        "type": "string",
                        "description": "New tags (comma-separated).",
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_list",
            "description": "List tasks with optional filtering by status, session, or tag. Shows progress summary.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filter by status: pending, in_progress, completed, blocked, cancelled.",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Filter by session ID.",
                    },
                    "tag": {
                        "type": "string",
                        "description": "Filter by tag.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of tasks to return (default 50).",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notebook_read",
            "description": "Read a Jupyter notebook (.ipynb) and display cells with their source code and outputs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the .ipynb file.",
                    },
                    "cell_index": {
                        "type": "integer",
                        "description": "Show only this cell (0-based index).",
                    },
                    "cell_type": {
                        "type": "string",
                        "description": "Filter by cell type: code, markdown, or raw.",
                    },
                    "max_output_chars": {
                        "type": "integer",
                        "description": "Maximum characters per cell output (default 5000).",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notebook_edit",
            "description": "Edit a Jupyter notebook cell: replace source, change type, insert new cells, or delete cells.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the .ipynb file.",
                    },
                    "cell_index": {
                        "type": "integer",
                        "description": "Index of the cell to edit (0-based).",
                    },
                    "new_source": {
                        "type": "string",
                        "description": "New cell content (replaces existing source).",
                    },
                    "cell_type": {
                        "type": "string",
                        "description": "Change cell type to: code, markdown, or raw.",
                    },
                    "insert_before": {
                        "type": "boolean",
                        "description": "If true, insert a new cell before cell_index instead of editing.",
                    },
                    "delete": {
                        "type": "boolean",
                        "description": "If true, delete the cell at cell_index.",
                    },
                },
                "required": ["path", "cell_index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_agent",
            "description": "Spawn a specialist sub-agent (debugger, tester, reviewer, coder, etc.) to handle a specific task. The agent runs with its own context and tool access policies.",
            "parameters": {
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "description": "Agent role: coder, debugger, tester, reviewer, architect, scout, refactorer, devops, docs, security_compliance, data_scientist, sre.",
                    },
                    "task": {
                        "type": "string",
                        "description": "Description of what the agent should do.",
                    },
                    "background": {
                        "type": "boolean",
                        "description": "If true, run agent in background and return tracking ID (default: false).",
                    },
                    "max_tool_rounds": {
                        "type": "integer",
                        "description": "Override max tool execution rounds for this agent.",
                    },
                },
                "required": ["role", "task"],
            },
        },
    },
]


# -- Model management helpers --


def fetch_ollama_models_sync(base_url: str = "http://localhost:11434") -> list[dict[str, Any]]:
    """Fetch available models from Ollama synchronously. Returns list of model dicts."""
    try:
        resp = httpx.get(f"{base_url}/api/tags", timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
        return data.get("models", [])
    except httpx.ConnectError:
        return []
    except Exception:
        return []


def get_ollama_model_names(base_url: str = "http://localhost:11434") -> list[str]:
    """Get just the model name strings from Ollama."""
    models = fetch_ollama_models_sync(base_url)
    return [m.get("name", "") for m in models if m.get("name")]


def fuzzy_match_model(query: str, available: list[str]) -> str | None:
    """Fuzzy-match a partial model name against available models.

    Tries exact match first, then prefix match, then substring, then difflib.
    Returns the best match or None.
    """
    if not available:
        return None

    # Exact match
    if query in available:
        return query

    # Exact match with :latest suffix
    if f"{query}:latest" in available:
        return f"{query}:latest"

    # Prefix match (e.g., "qwen" matches "qwen2.5-coder:7b")
    prefix_matches = [m for m in available if m.startswith(query)]
    if len(prefix_matches) == 1:
        return prefix_matches[0]
    if prefix_matches:
        return min(prefix_matches, key=len)

    # Substring match (e.g., "dolphin" matches "dolphin3:latest")
    sub_matches = [m for m in available if query in m]
    if len(sub_matches) == 1:
        return sub_matches[0]
    if sub_matches:
        return min(sub_matches, key=len)

    # difflib fuzzy matching
    close = get_close_matches(query, available, n=1, cutoff=0.4)
    if close:
        return close[0]

    return None


def format_model_size(size_bytes: int) -> str:
    """Format bytes to human readable."""
    if size_bytes <= 0:
        return ""
    gb = size_bytes / (1024 ** 3)
    if gb >= 1.0:
        return f"{gb:.1f} GB"
    mb = size_bytes / (1024 ** 2)
    return f"{mb:.0f} MB"


def _messages_to_dicts(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert Message dataclass instances to plain dicts for the new providers."""
    result = []
    for m in messages:
        d: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls:
            d["tool_calls"] = m.tool_calls
        if m.tool_call_id:
            d["tool_call_id"] = m.tool_call_id
        if m.name:
            d["name"] = m.name
        result.append(d)
    return result


class Provider:
    """Async LLM provider that handles chat completions with tool calling.

    This class maintains the original API surface but now delegates to the
    new providers/ system for Anthropic, OpenAI, and Google. Ollama and
    OpenAI-compat keep their original direct implementation for zero-risk
    backward compatibility with local-first workflows.
    """

    def __init__(self, config: ProviderConfig | None = None) -> None:
        self.config = config or ProviderConfig.from_config()
        self._client = httpx.AsyncClient(timeout=300.0)
        self._new_provider = None  # Lazy-loaded new provider instance

    def _get_new_provider(self):
        """Lazy-initialize the new provider system for supported backends."""
        if self._new_provider is not None:
            return self._new_provider

        name = self.config.name

        if name == "anthropic":
            from djcode.providers.anthropic import AnthropicProvider
            cfg = load_config()
            self._new_provider = AnthropicProvider(
                model=self.config.model,
                api_key=self.config.api_key,
                base_url=self.config.base_url,
                enable_caching=cfg.get("enable_caching", True),
                enable_thinking=cfg.get("enable_thinking", False),
                thinking_budget=cfg.get("thinking_budget", 10_000),
            )
        elif name == "openai":
            from djcode.providers.openai import OpenAIProvider
            self._new_provider = OpenAIProvider(
                model=self.config.model,
                api_key=self.config.api_key,
                base_url=self.config.base_url,
            )
        elif name == "google":
            from djcode.providers.google import GoogleProvider
            self._new_provider = GoogleProvider(
                model=self.config.model,
                api_key=self.config.api_key,
                base_url=self.config.base_url,
            )
        return self._new_provider

    @property
    def is_ollama(self) -> bool:
        return self.config.name == "ollama"

    @property
    def display_name(self) -> str:
        return f"{self.config.name}:{self.config.model}"

    # -- Model validation --

    def validate_model(self) -> tuple[bool, str]:
        """Validate the current model exists. Returns (ok, message).

        For Ollama, checks against /api/tags.
        For remote providers, we can't validate — always returns ok.
        """
        if self.config.name != "ollama":
            return True, ""

        available = get_ollama_model_names(self.config.base_url)
        if not available:
            return True, ""

        model = self.config.model
        if model in available:
            return True, ""

        match = fuzzy_match_model(model, available)
        if match:
            self.config.model = match
            return True, f"Resolved '{model}' to '{match}'"

        names_str = ", ".join(available[:10])
        return False, f"Model '{model}' not found. Available: {names_str}"

    # -- Ollama native API --

    async def chat_ollama(
        self,
        messages: list[Message],
        *,
        stream: bool = True,
        use_tools: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        """Call Ollama /api/chat with streaming. Falls back without tools if model rejects them."""
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [self._msg_to_ollama(m) for m in messages],
            "stream": stream,
            "options": {
                "temperature": self.config.temperature,
                "num_predict": self.config.max_tokens,
            },
        }
        if use_tools:
            payload["tools"] = TOOL_DEFINITIONS

        url = f"{self.config.base_url}/api/chat"

        try:
            if stream:
                async with self._client.stream("POST", url, json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if line.strip():
                            try:
                                yield json.loads(line)
                            except json.JSONDecodeError:
                                continue
            else:
                resp = await self._client.post(url, json=payload)
                resp.raise_for_status()
                yield resp.json()

        except httpx.ConnectError:
            raise ConnectionError(
                "Cannot connect to Ollama. Start it with: ollama serve"
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                available = get_ollama_model_names(self.config.base_url)
                suggestion = fuzzy_match_model(self.config.model, available) if available else None
                msg = f"Model '{self.config.model}' not found."
                if suggestion:
                    msg += f" Did you mean '{suggestion}'?"
                elif available:
                    msg += f" Available: {', '.join(available[:8])}"
                msg += f"\nPull it with: ollama pull {self.config.model}"
                raise ConnectionError(msg)
            elif e.response.status_code == 400 and use_tools:
                async for chunk in self.chat_ollama(messages, stream=stream, use_tools=False):
                    yield chunk
            else:
                raise
        except httpx.ReadTimeout:
            raise ConnectionError(
                "Request timed out. Try a smaller model or increase timeout."
            )

    # -- OpenAI-compatible API (MLX, remote, groq, together, nvidia, etc.) --

    async def chat_openai_compat(
        self,
        messages: list[Message],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        """Call OpenAI-compatible /v1/chat/completions."""
        headers = {}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        payload = {
            "model": self.config.model,
            "messages": [self._msg_to_openai(m) for m in messages],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": stream,
            "tools": TOOL_DEFINITIONS,
        }

        base = self.config.base_url.rstrip("/")
        if base.endswith("/v1"):
            url = f"{base}/chat/completions"
        else:
            url = f"{base}/v1/chat/completions"

        try:
            if stream:
                async with self._client.stream(
                    "POST", url, json=payload, headers=headers
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if line.startswith("data: ") and line != "data: [DONE]":
                            try:
                                yield json.loads(line[6:])
                            except json.JSONDecodeError:
                                continue
            else:
                resp = await self._client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                yield resp.json()

        except httpx.ConnectError:
            raise ConnectionError(
                f"Cannot connect to {self.config.base_url}. "
                "Check the URL and ensure the server is running."
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise ConnectionError(
                    "Authentication failed. Check your API key "
                    "(set via config or OPENAI_API_KEY env var)."
                )
            elif e.response.status_code == 404:
                raise ConnectionError(
                    f"Model '{self.config.model}' not found at {self.config.base_url}."
                )
            else:
                raise ConnectionError(
                    f"API error {e.response.status_code}: {e.response.text[:200]}"
                )
        except httpx.ReadTimeout:
            raise ConnectionError(
                "Request timed out. Try a smaller model or increase timeout."
            )

    # -- Anthropic: delegate to new provider with prompt caching + thinking --

    async def chat_anthropic(
        self,
        messages: list[Message],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        """Call Anthropic via the new provider system with prompt caching.

        Delegates to providers/anthropic.py which supports:
        - Prompt caching (cache_control blocks for system prompt + large context)
        - Extended thinking (thinking blocks with budget_tokens)
        - Proper content_block_start/delta/stop handling
        - Rate limit handling with exponential backoff
        - Full usage tracking (input, output, cache_creation, cache_read tokens)

        Converts ProviderChunks back to OpenAI-compat dict format expected
        by the rest of the codebase.
        """
        provider = self._get_new_provider()
        if provider is None:
            async for chunk in self.chat_openai_compat(messages, stream=stream):
                yield chunk
            return

        from djcode.providers.base import FinishReason
        msg_dicts = _messages_to_dicts(messages)

        async for chunk in provider.chat(
            msg_dicts,
            stream=stream,
            tools=TOOL_DEFINITIONS,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        ):
            if chunk.content:
                yield {"choices": [{"delta": {"content": chunk.content}}]}

            if chunk.thinking:
                yield {"choices": [{"delta": {"thinking": chunk.thinking}}]}

            if chunk.tool_calls:
                for tc in chunk.tool_calls:
                    yield {"choices": [{"delta": {"tool_calls": [{
                        "index": 0,
                        "id": tc.id,
                        "function": {"name": tc.name, "arguments": tc.arguments},
                    }]}}]}

            if chunk.finish_reason is not None:
                if chunk.finish_reason == FinishReason.TOOL_USE:
                    yield {"choices": [{"finish_reason": "tool_calls"}]}
                elif chunk.finish_reason == FinishReason.MAX_TOKENS:
                    yield {"choices": [{"finish_reason": "length"}]}
                else:
                    yield {"choices": [{"finish_reason": "stop"}]}

                if chunk.usage:
                    yield {"usage": {
                        "prompt_tokens": chunk.usage.input_tokens,
                        "completion_tokens": chunk.usage.output_tokens,
                        "cache_creation_tokens": chunk.usage.cache_creation_tokens,
                        "cache_read_tokens": chunk.usage.cache_read_tokens,
                        "total_cost": chunk.usage.total_cost,
                    }}

    # -- OpenAI native: delegate to new provider for reasoning models --

    async def chat_openai_native(
        self,
        messages: list[Message],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        """Call OpenAI via the new provider system.

        Handles o1/o3/o4-mini reasoning models properly:
        - Developer role instead of system
        - No temperature parameter
        - max_completion_tokens instead of max_tokens
        - Reasoning content (thinking) streaming

        Converts ProviderChunks back to OpenAI-compat dict format.
        """
        provider = self._get_new_provider()
        if provider is None:
            async for chunk in self.chat_openai_compat(messages, stream=stream):
                yield chunk
            return

        from djcode.providers.base import FinishReason
        msg_dicts = _messages_to_dicts(messages)

        async for chunk in provider.chat(
            msg_dicts,
            stream=stream,
            tools=TOOL_DEFINITIONS,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        ):
            if chunk.content:
                yield {"choices": [{"delta": {"content": chunk.content}}]}

            if chunk.thinking:
                yield {"choices": [{"delta": {"thinking": chunk.thinking}}]}

            if chunk.tool_calls:
                for tc in chunk.tool_calls:
                    yield {"choices": [{"delta": {"tool_calls": [{
                        "index": 0,
                        "id": tc.id,
                        "function": {"name": tc.name, "arguments": tc.arguments},
                    }]}}]}

            if chunk.finish_reason is not None:
                if chunk.finish_reason == FinishReason.TOOL_USE:
                    yield {"choices": [{"finish_reason": "tool_calls"}]}
                elif chunk.finish_reason == FinishReason.MAX_TOKENS:
                    yield {"choices": [{"finish_reason": "length"}]}
                else:
                    yield {"choices": [{"finish_reason": "stop"}]}

                if chunk.usage:
                    yield {"usage": {
                        "prompt_tokens": chunk.usage.input_tokens,
                        "completion_tokens": chunk.usage.output_tokens,
                        "total_cost": chunk.usage.total_cost,
                    }}

    # -- Google Gemini: delegate to new provider --

    async def chat_google(
        self,
        messages: list[Message],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        """Call Google Gemini via the new native provider system.

        Uses Gemini's generateContent API directly (not OpenAI-compat shim):
        - 1M context window
        - Native function calling with functionDeclarations
        - Thinking/reasoning content support
        - Token counting via countTokens endpoint

        Converts ProviderChunks back to OpenAI-compat dict format.
        """
        provider = self._get_new_provider()
        if provider is None:
            async for chunk in self.chat_openai_compat(messages, stream=stream):
                yield chunk
            return

        from djcode.providers.base import FinishReason
        msg_dicts = _messages_to_dicts(messages)

        async for chunk in provider.chat(
            msg_dicts,
            stream=stream,
            tools=TOOL_DEFINITIONS,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        ):
            if chunk.content:
                yield {"choices": [{"delta": {"content": chunk.content}}]}

            if chunk.thinking:
                yield {"choices": [{"delta": {"thinking": chunk.thinking}}]}

            if chunk.tool_calls:
                for tc in chunk.tool_calls:
                    yield {"choices": [{"delta": {"tool_calls": [{
                        "index": 0,
                        "id": tc.id,
                        "function": {"name": tc.name, "arguments": tc.arguments},
                    }]}}]}

            if chunk.finish_reason is not None:
                if chunk.finish_reason == FinishReason.TOOL_USE:
                    yield {"choices": [{"finish_reason": "tool_calls"}]}
                elif chunk.finish_reason == FinishReason.MAX_TOKENS:
                    yield {"choices": [{"finish_reason": "length"}]}
                else:
                    yield {"choices": [{"finish_reason": "stop"}]}

                if chunk.usage:
                    yield {"usage": {
                        "prompt_tokens": chunk.usage.input_tokens,
                        "completion_tokens": chunk.usage.output_tokens,
                        "total_cost": chunk.usage.total_cost,
                    }}

    # -- Unified interface --

    async def chat(
        self,
        messages: list[Message],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        """Route to the correct backend based on provider name.

        Uses new native providers for anthropic, openai, and google.
        Keeps original implementations for ollama and openai-compat.
        """
        if self.config.name == "ollama":
            async for chunk in self.chat_ollama(messages, stream=stream):
                yield chunk
        elif self.config.name == "anthropic":
            async for chunk in self.chat_anthropic(messages, stream=stream):
                yield chunk
        elif self.config.name == "openai":
            async for chunk in self.chat_openai_native(messages, stream=stream):
                yield chunk
        elif self.config.name == "google":
            async for chunk in self.chat_google(messages, stream=stream):
                yield chunk
        else:
            # All other providers (nvidia, groq, together, openrouter,
            # mlx, remote, custom) use OpenAI-compatible API
            async for chunk in self.chat_openai_compat(messages, stream=stream):
                yield chunk

    # -- Embedding --

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        """Get embeddings via Ollama."""
        cfg = load_config()
        embed_model = model or cfg.get("embedding_model", "nomic-embed-text")

        try:
            resp = await self._client.post(
                f"{self.config.base_url}/api/embed",
                json={"model": embed_model, "input": text},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("embeddings", [data.get("embedding", [])])[0]
        except httpx.ConnectError:
            raise ConnectionError(
                "Cannot connect to Ollama for embeddings. "
                "Start it with: ollama serve"
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise ConnectionError(
                    f"Embedding model '{embed_model}' not found. "
                    f"Pull it with: ollama pull {embed_model}"
                )
            raise

    # -- Message formatting --

    @staticmethod
    def _msg_to_ollama(msg: Message) -> dict[str, Any]:
        d: dict[str, Any] = {"role": msg.role, "content": msg.content}
        if msg.tool_calls:
            d["tool_calls"] = msg.tool_calls
        return d

    @staticmethod
    def _msg_to_openai(msg: Message) -> dict[str, Any]:
        d: dict[str, Any] = {"role": msg.role, "content": msg.content}
        if msg.tool_calls:
            d["tool_calls"] = msg.tool_calls
        if msg.tool_call_id:
            d["tool_call_id"] = msg.tool_call_id
        if msg.name:
            d["name"] = msg.name
        return d

    async def close(self) -> None:
        await self._client.aclose()
        if self._new_provider is not None:
            await self._new_provider.close()
