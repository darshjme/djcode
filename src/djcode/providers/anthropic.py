"""Anthropic native provider for DJcode.

Production-grade Anthropic Messages API integration with:
- Prompt caching (cache_control blocks for system prompts and large context)
- Extended thinking (thinking blocks with configurable budget_tokens)
- Token counting via response headers
- Full streaming with content_block_start/delta/stop handling
- Native tool use format
- Model-specific optimizations for Opus, Sonnet, Haiku
- Rate limit handling with exponential backoff
- Complete usage tracking: input, output, cache_creation, cache_read tokens
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from djcode.providers.base import (
    BaseProvider,
    FinishReason,
    ProviderChunk,
    TokenUsage,
    ToolCall,
    get_model_info,
)

logger = logging.getLogger(__name__)

# Anthropic API version header
_API_VERSION = "2023-06-01"

# Minimum token count to enable caching (Anthropic requires >= 1024 for cacheable blocks)
_CACHE_MIN_TOKENS = 1024


def _estimate_tokens(text: str) -> int:
    """Rough token estimate for deciding whether to cache a block."""
    return max(1, len(text) // 4)


class AnthropicProvider(BaseProvider):
    """Anthropic Messages API provider with prompt caching and extended thinking."""

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        enable_caching: bool = True,
        enable_thinking: bool = False,
        thinking_budget: int = 10_000,
    ) -> None:
        super().__init__(model, api_key, base_url)
        self._enable_caching = enable_caching
        self._enable_thinking = enable_thinking
        self._thinking_budget = thinking_budget
        # Auto-detect thinking support from model info
        info = get_model_info(model)
        if info.supports_thinking and self._enable_thinking:
            self._thinking_budget = max(thinking_budget, 5_000)

    def _headers(self) -> dict[str, str]:
        """Build request headers with API key and required version."""
        h = {
            "x-api-key": self._api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }
        # Enable beta features when using caching or thinking
        beta_parts = []
        if self._enable_caching:
            beta_parts.append("prompt-caching-2024-07-31")
        if self._enable_thinking:
            beta_parts.append("extended-thinking-2025-04-11")
        if beta_parts:
            h["anthropic-beta"] = ",".join(beta_parts)
        return h

    def _build_system(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Extract system messages and format with optional cache_control.

        Anthropic uses system as a top-level array of content blocks.
        Large system prompts get cache_control for prompt caching.
        """
        system_texts = [m["content"] for m in messages if m.get("role") == "system"]
        if not system_texts:
            return []

        combined = "\n\n".join(system_texts)
        block: dict[str, Any] = {"type": "text", "text": combined}

        # Add cache control if system prompt is large enough
        if self._enable_caching and _estimate_tokens(combined) >= _CACHE_MIN_TOKENS:
            block["cache_control"] = {"type": "ephemeral"}

        return [block]

    def _build_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert messages to Anthropic format, adding cache_control to large user messages."""
        result = []
        for msg in messages:
            role = msg.get("role", "user")
            if role == "system":
                continue  # Handled separately

            # Tool result messages
            if role == "tool":
                result.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": msg.get("content", ""),
                    }],
                })
                continue

            content = msg.get("content", "")
            tool_calls = msg.get("tool_calls", [])

            if role == "assistant" and tool_calls:
                # Assistant message with tool calls
                blocks: list[dict[str, Any]] = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for tc in tool_calls:
                    func = tc.get("function", tc)
                    args_str = func.get("arguments", "{}")
                    try:
                        args = json.loads(args_str) if isinstance(args_str, str) else args_str
                    except json.JSONDecodeError:
                        args = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": func.get("name", ""),
                        "input": args,
                    })
                result.append({"role": "assistant", "content": blocks})
            elif role == "user":
                block: dict[str, Any] = {"type": "text", "text": content}
                # Cache large user context messages (file contents, etc.)
                if self._enable_caching and _estimate_tokens(content) >= _CACHE_MIN_TOKENS:
                    block["cache_control"] = {"type": "ephemeral"}
                result.append({"role": "user", "content": [block]})
            else:
                result.append({"role": role, "content": content})

        return result

    def _build_tools(self, tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        """Convert OpenAI-format tool definitions to Anthropic format."""
        if not tools:
            return []
        anthropic_tools = []
        for td in tools:
            func = td.get("function", td)
            tool_def: dict[str, Any] = {
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", func.get("input_schema", {})),
            }
            # Cache tool definitions if there are many (saves tokens on repeated calls)
            if self._enable_caching and len(tools) > 4:
                tool_def["cache_control"] = {"type": "ephemeral"}
            anthropic_tools.append(tool_def)
        return anthropic_tools

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        stream: bool = True,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 8192,
    ) -> AsyncIterator[ProviderChunk]:
        """Stream a chat completion via Anthropic Messages API."""
        system_blocks = self._build_system(messages)
        api_messages = self._build_messages(messages)
        anthropic_tools = self._build_tools(tools)

        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": api_messages,
        }
        if system_blocks:
            payload["system"] = system_blocks
        if anthropic_tools:
            payload["tools"] = anthropic_tools

        # Extended thinking: requires temperature unset or 1.0
        if self._enable_thinking and get_model_info(self._model).supports_thinking:
            payload["thinking"] = {
                "type": "enabled",
                "budget_tokens": self._thinking_budget,
            }
            # Anthropic requires no temperature param or temperature=1 for thinking
        else:
            payload["temperature"] = temperature

        if stream:
            payload["stream"] = True

        url = f"{self._base_url}/v1/messages"
        headers = self._headers()

        max_retries = 3
        for attempt in range(max_retries):
            try:
                if stream:
                    async for chunk in self._stream_response(url, payload, headers):
                        yield chunk
                else:
                    async for chunk in self._sync_response(url, payload, headers):
                        yield chunk
                return  # Success — exit retry loop
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status == 429 or status >= 500:
                    if attempt < max_retries - 1:
                        logger.warning(
                            "Anthropic rate limit/server error %d, retry %d/%d",
                            status, attempt + 1, max_retries,
                        )
                        await self._backoff_sleep(attempt)
                        continue
                self._raise_connection_error(e)
            except httpx.ConnectError:
                raise ConnectionError(
                    f"Cannot connect to Anthropic API at {self._base_url}. "
                    "Check your network connection."
                )
            except httpx.ReadTimeout:
                if attempt < max_retries - 1:
                    logger.warning("Anthropic timeout, retry %d/%d", attempt + 1, max_retries)
                    await self._backoff_sleep(attempt)
                    continue
                raise ConnectionError("Anthropic request timed out after retries.")

    async def _stream_response(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[ProviderChunk]:
        """Handle Anthropic SSE streaming with all content block types."""
        current_tool_id = ""
        current_tool_name = ""
        tool_args_buffer = ""
        usage = TokenUsage()

        async with self._client.stream("POST", url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line.startswith("data: "):
                    continue
                raw = line[6:]
                if raw == "[DONE]":
                    break

                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type", "")

                # -- message_start: contains usage info --
                if event_type == "message_start":
                    msg_usage = event.get("message", {}).get("usage", {})
                    usage.input_tokens = msg_usage.get("input_tokens", 0)
                    usage.cache_creation_tokens = msg_usage.get(
                        "cache_creation_input_tokens", 0
                    )
                    usage.cache_read_tokens = msg_usage.get(
                        "cache_read_input_tokens", 0
                    )

                # -- content_block_start --
                elif event_type == "content_block_start":
                    block = event.get("content_block", {})
                    block_type = block.get("type", "")

                    if block_type == "tool_use":
                        current_tool_id = block.get("id", "")
                        current_tool_name = block.get("name", "")
                        tool_args_buffer = ""

                    elif block_type == "thinking":
                        # Thinking block started — text will arrive in deltas
                        pass

                # -- content_block_delta --
                elif event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    delta_type = delta.get("type", "")

                    if delta_type == "text_delta":
                        yield ProviderChunk(content=delta.get("text", ""))

                    elif delta_type == "thinking_delta":
                        yield ProviderChunk(thinking=delta.get("thinking", ""))

                    elif delta_type == "input_json_delta":
                        tool_args_buffer += delta.get("partial_json", "")

                # -- content_block_stop: emit complete tool call --
                elif event_type == "content_block_stop":
                    if current_tool_id and current_tool_name:
                        yield ProviderChunk(
                            tool_calls=[ToolCall(
                                id=current_tool_id,
                                name=current_tool_name,
                                arguments=tool_args_buffer,
                            )]
                        )
                        current_tool_id = ""
                        current_tool_name = ""
                        tool_args_buffer = ""

                # -- message_delta: stop reason + final usage --
                elif event_type == "message_delta":
                    delta_data = event.get("delta", {})
                    stop_reason = delta_data.get("stop_reason", "")
                    msg_usage = event.get("usage", {})
                    usage.output_tokens = msg_usage.get("output_tokens", usage.output_tokens)

                    finish = FinishReason.STOP
                    if stop_reason == "tool_use":
                        finish = FinishReason.TOOL_USE
                    elif stop_reason == "max_tokens":
                        finish = FinishReason.MAX_TOKENS

                    usage.calculate_cost(self._model)
                    self._track_usage(usage)
                    yield ProviderChunk(usage=usage, finish_reason=finish)

    async def _sync_response(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[ProviderChunk]:
        """Handle non-streaming Anthropic response."""
        payload_copy = {**payload}
        payload_copy.pop("stream", None)

        resp = await self._client.post(url, json=payload_copy, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        content_blocks = data.get("content", [])
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in content_blocks:
            btype = block.get("type", "")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "thinking":
                thinking_parts.append(block.get("thinking", ""))
            elif btype == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    arguments=json.dumps(block.get("input", {})),
                ))

        # Build usage from response
        resp_usage = data.get("usage", {})
        usage = TokenUsage(
            input_tokens=resp_usage.get("input_tokens", 0),
            output_tokens=resp_usage.get("output_tokens", 0),
            cache_creation_tokens=resp_usage.get("cache_creation_input_tokens", 0),
            cache_read_tokens=resp_usage.get("cache_read_input_tokens", 0),
        )
        usage.calculate_cost(self._model)
        self._track_usage(usage)

        stop_reason = data.get("stop_reason", "end_turn")
        finish = FinishReason.STOP
        if stop_reason == "tool_use":
            finish = FinishReason.TOOL_USE
        elif stop_reason == "max_tokens":
            finish = FinishReason.MAX_TOKENS

        yield ProviderChunk(
            content="".join(text_parts),
            thinking="".join(thinking_parts),
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=finish,
        )

    async def count_tokens(self, text: str) -> int:
        """Count tokens using Anthropic's token counting endpoint.

        Falls back to character-based estimation if the endpoint is unavailable.
        """
        try:
            resp = await self._client.post(
                f"{self._base_url}/v1/messages/count_tokens",
                json={
                    "model": self._model,
                    "messages": [{"role": "user", "content": text}],
                },
                headers=self._headers(),
            )
            if resp.status_code == 200:
                return resp.json().get("input_tokens", len(text) // 4)
        except Exception:
            pass
        # Fallback: ~4 chars per token for English
        return max(1, len(text) // 4)

    @staticmethod
    def _raise_connection_error(e: httpx.HTTPStatusError) -> None:
        """Convert HTTP errors to user-friendly ConnectionError messages."""
        status = e.response.status_code
        body = e.response.text[:300]
        if status == 401:
            raise ConnectionError(
                "Anthropic authentication failed. Check your API key (/auth or ANTHROPIC_API_KEY)."
            )
        elif status == 403:
            raise ConnectionError(
                "Anthropic access forbidden. Your API key may lack required permissions."
            )
        elif status == 404:
            raise ConnectionError(f"Anthropic model not found. Response: {body}")
        elif status == 429:
            raise ConnectionError(
                "Anthropic rate limit exceeded. Wait a moment and retry."
            )
        elif status == 529:
            raise ConnectionError("Anthropic API is overloaded. Try again shortly.")
        else:
            raise ConnectionError(f"Anthropic API error {status}: {body}")
