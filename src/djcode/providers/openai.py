"""OpenAI native provider for DJcode.

Production-grade OpenAI API integration with:
- Native Chat Completions API (not just compat shim)
- o1/o3/o4-mini reasoning model support (developer messages, no system prompt)
- GPT-4o with structured outputs (response_format)
- Full streaming with proper SSE delta handling
- Parallel function calling
- Token counting via tiktoken fallback to estimation
- Rate limit handling with exponential backoff
- Complete usage tracking
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
)

logger = logging.getLogger(__name__)

# Models that use "developer" role instead of "system" and have no temperature
_REASONING_MODELS = {"o1", "o1-mini", "o1-preview", "o3", "o3-mini", "o4-mini"}


def _is_reasoning_model(model: str) -> bool:
    """Check if this model is an OpenAI reasoning model (o-series)."""
    model_lower = model.lower()
    for rm in _REASONING_MODELS:
        if rm in model_lower:
            return True
    return False


class OpenAIProvider(BaseProvider):
    """OpenAI Chat Completions API provider."""

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
    ) -> None:
        super().__init__(model, api_key, base_url)
        self._is_reasoning = _is_reasoning_model(model)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _build_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert messages, handling reasoning model differences.

        Reasoning models (o1/o3/o4-mini):
        - Use "developer" role instead of "system"
        - No temperature parameter
        - max_completion_tokens instead of max_tokens
        """
        result = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                if self._is_reasoning:
                    # Reasoning models use "developer" instead of "system"
                    result.append({"role": "developer", "content": content})
                else:
                    result.append({"role": "system", "content": content})
            elif role == "tool":
                result.append(
                    {
                        "role": "tool",
                        "content": content,
                        "tool_call_id": msg.get("tool_call_id", ""),
                    }
                )
            elif role == "assistant":
                entry: dict[str, Any] = {"role": "assistant", "content": content}
                if msg.get("tool_calls"):
                    entry["tool_calls"] = msg["tool_calls"]
                result.append(entry)
            else:
                from djcode.vision import openai_content

                result.append(
                    {"role": role, "content": openai_content(content, msg.get("images", []))}
                )

        return result

    def _build_tools(self, tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        """Format tools for OpenAI's function calling. Returns None if no tools."""
        if not tools:
            return None
        # OpenAI expects {"type": "function", "function": {...}} format
        formatted = []
        for td in tools:
            if "function" in td:
                formatted.append(td)
            else:
                formatted.append(
                    {
                        "type": "function",
                        "function": td,
                    }
                )
        return formatted

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        stream: bool = True,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 8192,
    ) -> AsyncIterator[ProviderChunk]:
        """Stream a chat completion via OpenAI API."""
        api_messages = self._build_messages(messages)
        formatted_tools = self._build_tools(tools)

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": api_messages,
            "stream": stream,
        }

        if stream:
            # Without this the API never sends the trailing usage event that
            # _stream_response waits for, and every token count -- and so every
            # cost figure downstream -- stays zero for the life of the session.
            payload["stream_options"] = {"include_usage": True}

        if self._is_reasoning:
            # Reasoning models: use max_completion_tokens, no temperature
            payload["max_completion_tokens"] = max_tokens
        else:
            payload["max_tokens"] = max_tokens
            payload["temperature"] = temperature

        if formatted_tools:
            payload["tools"] = formatted_tools
            payload["parallel_tool_calls"] = True

        # Build URL — handle base_url that may or may not include /v1
        base = self._base_url.rstrip("/")
        if base.endswith("/v1"):
            url = f"{base}/chat/completions"
        else:
            url = f"{base}/v1/chat/completions"

        headers = self._headers()

        max_retries = 3
        attempt = 0
        while attempt < max_retries:
            try:
                if stream:
                    async for chunk in self._stream_response(url, payload, headers):
                        yield chunk
                else:
                    async for chunk in self._sync_response(url, payload, headers):
                        yield chunk
                return
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status == 400 and self._rejected_stream_options(e.response, payload):
                    # Deliberately does not spend a retry: nothing was streamed
                    # (the status is raised before the first chunk) and the flag
                    # is gone from the payload, so this branch cannot loop.
                    logger.warning(
                        "Endpoint rejected stream_options; retrying without usage reporting. "
                        "Token counts stay zero for this request."
                    )
                    payload.pop("stream_options", None)
                    continue
                if status == 429 or status >= 500:
                    attempt += 1
                    if attempt < max_retries:
                        logger.warning(
                            "OpenAI rate limit/server error %d, retry %d/%d",
                            status,
                            attempt,
                            max_retries,
                        )
                        await self._backoff_sleep(attempt - 1)
                        continue
                self._raise_connection_error(e)
            except httpx.ConnectError:
                raise ConnectionError(
                    f"Cannot connect to OpenAI API at {self._base_url}. "
                    "Check your network connection."
                )
            except httpx.ReadTimeout:
                attempt += 1
                if attempt < max_retries:
                    await self._backoff_sleep(attempt - 1)
                    continue
                raise ConnectionError("OpenAI request timed out after retries.")
        raise ConnectionError("OpenAI request failed after retries.")

    async def _stream_response(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[ProviderChunk]:
        """Handle OpenAI SSE streaming with delta accumulation."""
        # Track tool calls being built across deltas
        tool_buffers: dict[int, dict[str, str]] = {}  # index -> {id, name, arguments}
        usage = TokenUsage()
        # The include_usage event is the LAST frame before [DONE], after the
        # frame carrying finish_reason. Emitting the final chunk the moment the
        # finish arrives would therefore always report zero tokens, so it is
        # held back one frame and flushed below with the real numbers.
        pending_calls: list[ToolCall] = []
        pending_finish: FinishReason | None = None

        async with self._client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code >= 400:
                # A streamed response has no .content until it is read. Without
                # this, every error handler in chat() that inspects the body --
                # _raise_connection_error and _rejected_stream_options -- would
                # raise httpx.ResponseNotRead instead of a useful message.
                await resp.aread()
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

                choices = event.get("choices", [])
                if not choices:
                    # Check for usage in the final event (stream_options)
                    if "usage" in event:
                        u = event["usage"]
                        usage.input_tokens = u.get("prompt_tokens", 0)
                        usage.output_tokens = u.get("completion_tokens", 0)
                        usage.thinking_tokens = u.get("completion_tokens_details", {}).get(
                            "reasoning_tokens", 0
                        )
                    continue

                choice = choices[0]
                delta = choice.get("delta", {})
                finish_reason = choice.get("finish_reason")

                # Text content
                content = delta.get("content", "")
                if content:
                    yield ProviderChunk(content=content)

                # Reasoning content (o1/o3 thinking)
                reasoning = delta.get("reasoning_content", "")
                if reasoning:
                    yield ProviderChunk(thinking=reasoning)

                # Tool calls — accumulate across deltas
                tc_deltas = delta.get("tool_calls", [])
                for tc_delta in tc_deltas:
                    idx = tc_delta.get("index", 0)
                    if idx not in tool_buffers:
                        tool_buffers[idx] = {
                            "id": tc_delta.get("id", ""),
                            "name": tc_delta.get("function", {}).get("name", ""),
                            "arguments": "",
                        }
                    else:
                        # Update ID/name if provided in this delta
                        if tc_delta.get("id"):
                            tool_buffers[idx]["id"] = tc_delta["id"]
                        fn = tc_delta.get("function", {})
                        if fn.get("name"):
                            tool_buffers[idx]["name"] = fn["name"]

                    # Accumulate argument fragments
                    args_chunk = tc_delta.get("function", {}).get("arguments", "")
                    if args_chunk:
                        tool_buffers[idx]["arguments"] += args_chunk

                # Finish reason
                if finish_reason:
                    # Emit accumulated tool calls
                    calls: list[ToolCall] = []
                    for idx in sorted(tool_buffers.keys()):
                        buf = tool_buffers[idx]
                        calls.append(
                            ToolCall(
                                id=buf["id"],
                                name=buf["name"],
                                arguments=buf["arguments"],
                            )
                        )
                    tool_buffers.clear()

                    fr = FinishReason.STOP
                    if finish_reason == "tool_calls":
                        fr = FinishReason.TOOL_USE
                    elif finish_reason == "length":
                        fr = FinishReason.MAX_TOKENS

                    if pending_finish is not None:
                        # n=1 Chat Completions never sends two finish frames;
                        # if one ever does, release the earlier one rather than
                        # silently dropping its tool calls.
                        yield ProviderChunk(
                            tool_calls=pending_calls,
                            finish_reason=pending_finish,
                        )
                    pending_calls = calls
                    pending_finish = fr

        if pending_finish is not None:
            usage.calculate_cost(self._model)
            self._track_usage(usage)
            yield ProviderChunk(
                tool_calls=pending_calls,
                usage=usage,
                finish_reason=pending_finish,
            )

    async def _sync_response(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[ProviderChunk]:
        """Handle non-streaming OpenAI response."""
        payload_copy = {**payload, "stream": False}
        # stream_options is only legal alongside stream=true; the API rejects
        # the pair with a 400. chat() only sets it for streaming requests, so
        # this is belt and braces for a caller that reuses a payload.
        payload_copy.pop("stream_options", None)

        resp = await self._client.post(url, json=payload_copy, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        choices = data.get("choices", [])
        if not choices:
            yield ProviderChunk(finish_reason=FinishReason.ERROR)
            return

        msg = choices[0].get("message", {})
        content = msg.get("content", "") or ""
        reasoning = msg.get("reasoning_content", "") or ""

        # Parse tool calls
        tool_calls: list[ToolCall] = []
        for tc in msg.get("tool_calls", []):
            func = tc.get("function", {})
            tool_calls.append(
                ToolCall(
                    id=tc.get("id", ""),
                    name=func.get("name", ""),
                    arguments=func.get("arguments", "{}"),
                )
            )

        # Usage
        u = data.get("usage", {})
        usage = TokenUsage(
            input_tokens=u.get("prompt_tokens", 0),
            output_tokens=u.get("completion_tokens", 0),
            thinking_tokens=u.get("completion_tokens_details", {}).get("reasoning_tokens", 0),
        )
        usage.calculate_cost(self._model)
        self._track_usage(usage)

        finish_reason = choices[0].get("finish_reason", "stop")
        fr = FinishReason.STOP
        if finish_reason == "tool_calls":
            fr = FinishReason.TOOL_USE
        elif finish_reason == "length":
            fr = FinishReason.MAX_TOKENS

        yield ProviderChunk(
            content=content,
            thinking=reasoning,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=fr,
        )

    async def count_tokens(self, text: str) -> int:
        """Estimate token count. Uses tiktoken if available, else heuristic."""
        try:
            import tiktoken

            enc = tiktoken.encoding_for_model(self._model)
            return len(enc.encode(text))
        except (ImportError, KeyError):
            pass
        # Heuristic: ~4 chars per token for English
        return max(1, len(text) // 4)

    @staticmethod
    def _rejected_stream_options(response: httpx.Response, payload: dict[str, Any]) -> bool:
        """True when a 400 is the endpoint refusing the include_usage flag.

        stream_options is an OpenAI extension. Gateways and self-hosted
        OpenAI-compatible servers behind a custom base_url may reject unknown
        body fields outright, so asking for usage reporting must degrade to no
        usage reporting rather than take the whole request down with it.
        """
        if "stream_options" not in payload:
            return False
        try:
            body = response.text.lower()
        except httpx.ResponseNotRead:  # pragma: no cover - body already closed
            return False
        return "stream_options" in body or "stream options" in body

    @staticmethod
    def _raise_connection_error(e: httpx.HTTPStatusError) -> None:
        """Convert HTTP errors to user-friendly messages."""
        status = e.response.status_code
        body = e.response.text[:300]
        if status == 401:
            raise ConnectionError(
                "OpenAI authentication failed. Check your API key (/auth or OPENAI_API_KEY)."
            )
        elif status == 403:
            raise ConnectionError("OpenAI access forbidden. Check API key permissions.")
        elif status == 404:
            raise ConnectionError(f"OpenAI model not found. Response: {body}")
        elif status == 429:
            raise ConnectionError("OpenAI rate limit exceeded. Wait and retry.")
        else:
            raise ConnectionError(f"OpenAI API error {status}: {body}")
