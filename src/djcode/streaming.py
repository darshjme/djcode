"""One streaming protocol adapter shared by the interactive and specialist loops."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from djcode.provider import Message, Provider
from djcode.providers.base import TokenUsage

# Provider accounting key -> TokenUsage attribute.
#
# Three shapes reach stream_turn and this one table covers all of them:
# provider.py re-emits every native provider in an OpenAI-compatible dict
# (chat_anthropic adds the two cache counters, chat_openai_native and
# chat_google do not), raw OpenAI-compatible endpoints pass their own
# `stream_options.include_usage` event straight through chat_openai_compat,
# and Ollama reports its counters on the final NDJSON frame. Unknown keys are
# ignored, so feeding a whole chunk through is safe.
_USAGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("prompt_tokens", "input_tokens"),
    ("input_tokens", "input_tokens"),
    ("prompt_eval_count", "input_tokens"),
    ("completion_tokens", "output_tokens"),
    ("output_tokens", "output_tokens"),
    ("eval_count", "output_tokens"),
    ("cache_creation_tokens", "cache_creation_tokens"),
    ("cache_creation_input_tokens", "cache_creation_tokens"),
    ("cache_read_tokens", "cache_read_tokens"),
    ("cache_read_input_tokens", "cache_read_tokens"),
    ("thinking_tokens", "thinking_tokens"),
    ("reasoning_tokens", "thinking_tokens"),
)


def _as_int(value: Any) -> int:
    """Coerce a provider-supplied counter to a non-negative int."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return max(0, int(value))


def _as_float(value: Any) -> float:
    """Coerce a provider-supplied cost to a non-negative float."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return max(0.0, float(value))


def _read_usage(raw: Mapping[str, Any]) -> TokenUsage:
    """Translate one provider accounting payload into a TokenUsage."""
    usage = TokenUsage()
    for source, target in _USAGE_FIELDS:
        value = _as_int(raw.get(source))
        if value > getattr(usage, target):
            setattr(usage, target, value)
    # OpenAI nests reasoning tokens one level down. They are already inside
    # completion_tokens, so they are carried for display only and never added
    # to the billable totals. prompt_tokens_details.cached_tokens is
    # deliberately NOT mapped onto cache_read_tokens: OpenAI counts cached
    # prompt tokens inside prompt_tokens, while TokenUsage.calculate_cost adds
    # the cache counters on top of input_tokens (the Anthropic convention), so
    # mapping it across would bill the same tokens twice.
    details = raw.get("completion_tokens_details")
    if isinstance(details, Mapping):
        nested = _as_int(details.get("reasoning_tokens"))
        usage.thinking_tokens = max(usage.thinking_tokens, nested)
    usage.total_cost = _as_float(raw.get("total_cost"))
    return usage


def _merge_usage(current: TokenUsage | None, reported: TokenUsage) -> TokenUsage:
    """Keep the high-water mark for one request; never sum within a stream.

    Providers report cumulative totals rather than deltas, and some report more
    than once per stream: providers/google.py emits usage on the candidate's
    finishReason and again on the trailing usageMetadata-only event. Summing
    those would double the bill.
    """
    if current is None:
        return reported
    current.input_tokens = max(current.input_tokens, reported.input_tokens)
    current.output_tokens = max(current.output_tokens, reported.output_tokens)
    current.cache_creation_tokens = max(
        current.cache_creation_tokens, reported.cache_creation_tokens
    )
    current.cache_read_tokens = max(current.cache_read_tokens, reported.cache_read_tokens)
    current.thinking_tokens = max(current.thinking_tokens, reported.thinking_tokens)
    current.total_cost = max(current.total_cost, reported.total_cost)
    return current


@dataclass(slots=True)
class UsageSink:
    """Mutable token accounting the caller hands to :func:`stream_turn`.

    The sink exists so the generator keeps its two-tuple yield contract: the
    accounting travels out of band instead of widening every ``async for`` that
    consumes a turn. One sink can follow a whole multi-round turn -- each
    completed request is folded in by :meth:`record`, so the totals are the
    turn's, not the last request's.
    """

    usage: TokenUsage = field(default_factory=TokenUsage)
    last: TokenUsage | None = None
    requests: int = 0

    @property
    def received(self) -> bool:
        """True once at least one provider accounting event has landed.

        False means the figures are estimates, not the provider's own numbers.
        """
        return self.requests > 0

    def record(self, usage: TokenUsage) -> None:
        """Fold one finished request's totals into the running total."""
        self.last = usage
        self.requests += 1
        self.usage.input_tokens += usage.input_tokens
        self.usage.output_tokens += usage.output_tokens
        self.usage.cache_creation_tokens += usage.cache_creation_tokens
        self.usage.cache_read_tokens += usage.cache_read_tokens
        self.usage.thinking_tokens += usage.thinking_tokens
        self.usage.total_cost = round(self.usage.total_cost + usage.total_cost, 6)

    def cost(self, model: str) -> float:
        """USD spent so far, priced locally when the provider reported none."""
        if self.usage.total_cost:
            return self.usage.total_cost
        priced = TokenUsage(
            input_tokens=self.usage.input_tokens,
            output_tokens=self.usage.output_tokens,
            cache_creation_tokens=self.usage.cache_creation_tokens,
            cache_read_tokens=self.usage.cache_read_tokens,
            thinking_tokens=self.usage.thinking_tokens,
        )
        return priced.calculate_cost(model)


async def stream_turn(
    provider: Provider,
    messages: list[Message],
    *,
    usage_sink: UsageSink | None = None,
) -> AsyncIterator[tuple[str, list[dict[str, Any]]]]:
    """Yield text followed by complete, indexed tool calls; reject truncated streams.

    Pass ``usage_sink`` to collect the provider's own token accounting for this
    request. Without it the behaviour is what it always was: a chunk carrying
    nothing but ``usage`` is consumed and never yielded as text.
    """
    calls: dict[int, dict[str, Any]] = {}
    ended = False
    reported_usage: TokenUsage | None = None
    try:
        async with aclosing(provider.chat(messages, stream=True)) as response:
            async for chunk in response:
                if chunk.get("error"):
                    raise ConnectionError(str(chunk["error"]))
                reported = chunk.get("usage")
                if isinstance(reported, Mapping):
                    reported_usage = _merge_usage(reported_usage, _read_usage(reported))
                    # OpenAI sends `"choices": []` alongside its usage event and
                    # the native providers omit the key entirely; either way
                    # there is nothing else in this chunk to process.
                    if "message" not in chunk and not chunk.get("choices"):
                        continue
                if "message" in chunk:  # Ollama NDJSON
                    msg = chunk["message"]
                    if msg.get("content"):
                        yield msg["content"], []
                    for tc in msg.get("tool_calls", []):
                        calls[len(calls)] = tc
                    if chunk.get("done"):
                        # Ollama puts prompt_eval_count/eval_count on the frame
                        # itself rather than under a "usage" key.
                        reported_usage = _merge_usage(reported_usage, _read_usage(chunk))
                        if chunk.get("done_reason") == "length":
                            raise RuntimeError("Model output limit reached before completion.")
                        ended = True
                        continue
                    continue
                choices = chunk.get("choices", [])
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta", {})
                if delta.get("content"):
                    yield delta["content"], []
                for part in delta.get("tool_calls", []):
                    idx = part.get("index", 0)
                    tc = calls.setdefault(
                        idx,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if part.get("id"):
                        tc["id"] = part["id"]
                    fn = part.get("function", {})
                    if fn.get("name"):
                        tc["function"]["name"] += fn["name"]
                    if fn.get("arguments") is not None:
                        tc["function"]["arguments"] += fn["arguments"]
                finish = choice.get("finish_reason")
                if finish:
                    if finish not in ("stop", "tool_calls"):
                        raise RuntimeError(f"Model stopped before completion: {finish}")
                    ended = True
                    continue
        if not ended:
            raise ConnectionError(
                "Provider stream ended without a completion marker; retry the request."
            )
    finally:
        # Tokens the provider reported were paid for even when the stream then
        # failed or the caller walked away, so the sink is written either way.
        if reported_usage is not None and usage_sink is not None:
            usage_sink.record(reported_usage)
    normalized = []
    for tc in calls.values():
        fn = tc.get("function", {})
        if not fn.get("name"):
            raise ValueError("Provider returned a tool call without a name.")
        args = fn.get("arguments", {})
        normalized.append(
            {
                "id": tc.get("id") or f"call_{uuid.uuid4().hex}",
                "type": "function",
                "function": {
                    "name": fn["name"],
                    "arguments": json.dumps(args) if isinstance(args, dict) else args,
                },
            }
        )
    if normalized:
        yield "", normalized
