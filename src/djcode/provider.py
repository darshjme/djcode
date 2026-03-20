"""LLM Provider abstraction for DJcode.

Supports Ollama, MLX, and generic OpenAI-compatible endpoints.
All communication is local-first via httpx async.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
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
        cfg = load_config()
        provider = provider_override or cfg["provider"]
        model = model_override or cfg["model"]

        url_map = {
            "ollama": cfg.get("ollama_url", "http://localhost:11434"),
            "mlx": cfg.get("mlx_url", "http://localhost:8080"),
            "remote": cfg.get("remote_url", ""),
        }

        return cls(
            name=provider,
            base_url=url_map.get(provider, cfg.get("ollama_url", "http://localhost:11434")),
            model=model,
            api_key=cfg.get("remote_api_key", ""),
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

    # -- Ollama native API --

    async def chat_ollama(
        self,
        messages: list[Message],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        """Call Ollama /api/chat with streaming."""
        payload = {
            "model": self.config.model,
            "messages": [self._msg_to_ollama(m) for m in messages],
            "stream": stream,
            "options": {
                "temperature": self.config.temperature,
                "num_predict": self.config.max_tokens,
            },
            "tools": TOOL_DEFINITIONS,
        }

        url = f"{self.config.base_url}/api/chat"

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

        if stream:
            async with self._client.stream("POST", url, json=payload, headers=headers) as resp:
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
            async for chunk in self.chat_openai_compat(messages, stream=stream):
                yield chunk

    # -- Embedding --

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        """Get embeddings via Ollama."""
        cfg = load_config()
        embed_model = model or cfg.get("embedding_model", "nomic-embed-text")
        resp = await self._client.post(
            f"{self.config.base_url}/api/embed",
            json={"model": embed_model, "input": text},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("embeddings", [data.get("embedding", [])])[0]

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
