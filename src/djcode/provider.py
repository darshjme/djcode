"""LLM Provider abstraction for DJcode.

Supports Ollama, MLX, and generic OpenAI-compatible endpoints.
All communication is local-first via httpx async.
Includes model validation, fuzzy matching, and robust error handling.
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
        """Build provider config from saved config + CLI overrides."""
        from djcode.auth import PROVIDERS, get_api_key, get_base_url

        cfg = load_config()
        provider = provider_override or cfg["provider"]
        model = model_override or cfg["model"]

        # Use auth registry for known providers, fall back to legacy config
        if provider in PROVIDERS:
            base_url = get_base_url(provider)
            api_key = get_api_key(provider)
        else:
            # Legacy fallback for "remote" or unknown providers
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
        # Return the shortest prefix match (most likely what the user meant)
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


class Provider:
    """Async LLM provider that handles chat completions with tool calling."""

    def __init__(self, config: ProviderConfig | None = None) -> None:
        self.config = config or ProviderConfig.from_config()
        self._client = httpx.AsyncClient(timeout=300.0)

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
            # Can't reach Ollama — will fail at chat time with better error
            return True, ""

        model = self.config.model
        if model in available:
            return True, ""

        # Try fuzzy match
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
                # Model doesn't support tools — retry without them
                async for chunk in self.chat_ollama(messages, stream=stream, use_tools=False):
                    yield chunk
            else:
                raise
        except httpx.ReadTimeout:
            raise ConnectionError(
                "Request timed out. Try a smaller model or increase timeout."
            )

    # -- OpenAI-compatible API (MLX, remote) --

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

        url = f"{self.config.base_url}/v1/chat/completions"

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
                f"Cannot connect to {self.config.base_url}. Check the URL and ensure the server is running."
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise ConnectionError(
                    "Authentication failed. Check your API key (set via config or OPENAI_API_KEY env var)."
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

    # -- Unified interface --

    async def chat(
        self,
        messages: list[Message],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        """Route to the correct backend based on provider name."""
        if self.config.name == "ollama":
            async for chunk in self.chat_ollama(messages, stream=stream):
                yield chunk
        else:
            # All other providers (openai, anthropic, nvidia, google, groq,
            # together, openrouter, mlx, remote) use OpenAI-compatible API
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
            raise ConnectionError("Cannot connect to Ollama for embeddings. Start it with: ollama serve")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise ConnectionError(
                    f"Embedding model '{embed_model}' not found. Pull it with: ollama pull {embed_model}"
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
