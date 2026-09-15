"""W1-3: the three links that turn DJcode's cost figures from fiction into accounting.

Link 1 - streaming.stream_turn must not drop a chunk that carries only usage.
Link 2 - providers/openai must ask for that chunk (stream_options.include_usage).
Link 3 - providers/base._match_pricing_key must price the model the user ran,
         not the shorter key that happens to be a substring of its name.

Any one of the three missing and the number on screen is made up, so all three
are pinned here together.
"""

import asyncio
import copy
import json

import httpx
import pytest

from djcode.provider import Message, Provider, ProviderConfig
from djcode.providers.base import MODEL_PRICING, TokenUsage, _match_pricing_key
from djcode.providers.openai import OpenAIProvider
from djcode.streaming import UsageSink, stream_turn

# -- helpers ---------------------------------------------------------------


class FakeProvider:
    """Minimal stand-in for Provider: chat() is an async generator of dicts."""

    def __init__(self, chunks):
        self._chunks = chunks

    async def chat(self, messages, stream=True):  # noqa: ARG002 - protocol shape
        for chunk in self._chunks:
            yield chunk


def drain(chunks, sink=None):
    """Run stream_turn to exhaustion, returning (text, tool_calls)."""

    async def run():
        text = ""
        calls = []
        async for piece, tool_calls in stream_turn(
            FakeProvider(chunks), [], **({"usage_sink": sink} if sink is not None else {})
        ):
            text += piece
            calls.extend(tool_calls)
        return text, calls

    return asyncio.run(run())


def delta(content):
    return {"choices": [{"delta": {"content": content}}]}


def finish(reason="stop"):
    return {"choices": [{"delta": {}, "finish_reason": reason}]}


# -- link 3: longest-key pricing -------------------------------------------

SUBSTRING_PAIRS = [
    (shorter, longer)
    for shorter in MODEL_PRICING
    for longer in MODEL_PRICING
    if shorter != longer and shorter in longer
]


def test_o3_mini_is_not_priced_as_o3():
    """The exact regression: 'o3' is a substring of 'o3-mini' and came first."""
    assert _match_pricing_key("o3-mini") == "o3-mini"


def test_pricing_table_still_contains_substring_traps():
    """Guard the pair test below from silently degenerating to zero cases."""
    assert SUBSTRING_PAIRS, "MODEL_PRICING no longer has overlapping keys to test"


@pytest.mark.parametrize(("shorter", "longer"), SUBSTRING_PAIRS, ids=lambda k: k)
def test_longer_key_wins_for_every_overlapping_pair(shorter, longer):
    """For every pair where one key is a substring of another, the longer wins."""
    assert _match_pricing_key(longer) == longer
    assert _match_pricing_key(longer) != shorter


@pytest.mark.parametrize("key", list(MODEL_PRICING), ids=lambda k: k)
def test_every_pricing_key_matches_itself(key):
    assert _match_pricing_key(key) == key


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("o3-mini-2025-01-31", "o3-mini"),
        ("gpt-4o-mini-2024-07-18", "gpt-4o-mini"),
        ("gpt-4.1-nano", "gpt-4.1-nano"),
        ("openai/gpt-4o-mini", "gpt-4o-mini"),
        ("CLAUDE-SONNET-4-6", "claude-sonnet-4-6"),
        ("mistral-large", None),
    ],
)
def test_real_model_ids_resolve_to_the_right_key(model, expected):
    assert _match_pricing_key(model) == expected


def test_cost_uses_the_matched_key_not_the_prefix():
    """1M in + 1M out on o3-mini is $5.50, not o3's $50.00."""
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert usage.calculate_cost("o3-mini") == pytest.approx(5.50)
    o3 = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert o3.calculate_cost("o3") == pytest.approx(50.0)


# -- link 1: usage-only chunks reach the sink ------------------------------


def test_anthropic_shaped_usage_chunk_reaches_the_sink():
    """{'usage': {...}} with no choices and no message used to be dropped."""
    sink = UsageSink()
    text, calls = drain(
        [
            delta("hello"),
            finish("stop"),
            {
                "usage": {
                    "prompt_tokens": 1200,
                    "completion_tokens": 340,
                    "cache_creation_tokens": 64,
                    "cache_read_tokens": 900,
                    "total_cost": 0.0123,
                }
            },
        ],
        sink,
    )
    assert text == "hello"
    assert calls == []
    assert sink.received is True
    assert sink.requests == 1
    assert sink.usage.input_tokens == 1200
    assert sink.usage.output_tokens == 340
    assert sink.usage.cache_creation_tokens == 64
    assert sink.usage.cache_read_tokens == 900
    assert sink.usage.total_cost == pytest.approx(0.0123)
    assert sink.last is not None and sink.last.output_tokens == 340


def test_openai_shaped_usage_chunk_with_empty_choices_reaches_the_sink():
    """The real stream_options event carries 'choices': [] alongside usage."""
    sink = UsageSink()
    text, _ = drain(
        [
            delta("hi"),
            finish("stop"),
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 90,
                    "completion_tokens": 12,
                    "completion_tokens_details": {"reasoning_tokens": 8},
                },
            },
        ],
        sink,
    )
    assert text == "hi"
    assert (sink.usage.input_tokens, sink.usage.output_tokens) == (90, 12)
    assert sink.usage.thinking_tokens == 8


def test_usage_is_never_yielded_as_text_and_sink_is_optional():
    """Existing callers pass no sink; the stream must behave exactly as before."""
    chunks = [delta("a"), {"usage": {"prompt_tokens": 5, "completion_tokens": 1}}, finish("stop")]
    assert drain(chunks) == ("a", [])


def test_repeated_cumulative_reports_are_not_summed():
    """providers/google.py reports usage twice per stream; totals, not deltas."""
    sink = UsageSink()
    report = {"usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_cost": 0.002}}
    drain([delta("x"), finish("stop"), copy.deepcopy(report), copy.deepcopy(report)], sink)
    assert sink.requests == 1
    assert (sink.usage.input_tokens, sink.usage.output_tokens) == (50, 20)
    assert sink.usage.total_cost == pytest.approx(0.002)


def test_sink_accumulates_across_the_rounds_of_one_turn():
    """One sink follows a whole multi-round turn, so the total is the turn's."""
    sink = UsageSink()
    for _ in range(3):
        drain(
            [
                delta("x"),
                finish("stop"),
                {"usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_cost": 0.001}},
            ],
            sink,
        )
    assert sink.requests == 3
    assert (sink.usage.input_tokens, sink.usage.output_tokens) == (300, 30)
    assert sink.usage.total_cost == pytest.approx(0.003)


def test_ollama_counters_on_the_done_frame_reach_the_sink():
    """Ollama reports on the frame itself, not under a 'usage' key."""
    sink = UsageSink()
    text, _ = drain(
        [
            {"message": {"content": "local"}},
            {"message": {"content": ""}, "done": True, "prompt_eval_count": 31, "eval_count": 7},
        ],
        sink,
    )
    assert text == "local"
    assert (sink.usage.input_tokens, sink.usage.output_tokens) == (31, 7)


def test_usage_survives_a_stream_that_then_fails():
    """Tokens already reported were paid for even if the stream never completed."""
    sink = UsageSink()
    with pytest.raises(ConnectionError):
        drain([delta("partial"), {"usage": {"prompt_tokens": 42, "completion_tokens": 3}}], sink)
    assert sink.received is True
    assert sink.usage.input_tokens == 42


def test_garbage_usage_payload_cannot_poison_the_books():
    sink = UsageSink()
    drain(
        [delta("x"), finish("stop"), {"usage": {"prompt_tokens": None, "completion_tokens": -9}}],
        sink,
    )
    assert (sink.usage.input_tokens, sink.usage.output_tokens) == (0, 0)


def test_sink_cost_prices_locally_when_the_provider_sends_none():
    """Links 1 and 3 together: real tokens, priced with the longest-key match."""
    sink = UsageSink()
    drain(
        [
            delta("x"),
            finish("stop"),
            {"choices": [], "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}},
        ],
        sink,
    )
    assert sink.cost("o3-mini-2025-01-31") == pytest.approx(5.50)
    # Pricing must not mutate the accumulator it reads.
    assert sink.usage.total_cost == 0.0


def test_sink_prefers_the_cost_the_provider_reported():
    sink = UsageSink()
    drain([delta("x"), finish("stop"), {"usage": {"total_cost": 0.42}}], sink)
    assert sink.cost("claude-opus-4-6") == pytest.approx(0.42)


# -- link 2: OpenAI must request the usage event ---------------------------


def _capture_payloads(store):
    async def fake(url, payload, headers):  # noqa: ARG001 - signature must match
        store.append(copy.deepcopy(payload))
        return
        yield  # pragma: no cover - makes this an async generator

    return fake


def _run_chat(stream):
    provider = OpenAIProvider(
        model="gpt-4o", api_key="test-key", base_url="https://fixture.test/v1"
    )
    payloads = []
    provider._stream_response = _capture_payloads(payloads)
    provider._sync_response = _capture_payloads(payloads)

    async def run():
        try:
            async for _ in provider.chat([{"role": "user", "content": "hi"}], stream=stream):
                pass
        finally:
            await provider.close()

    asyncio.run(run())
    assert len(payloads) == 1
    return payloads[0]


def test_streaming_payload_requests_usage():
    """Without this flag the API never sends the event _stream_response waits for."""
    payload = _run_chat(stream=True)
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}


def test_non_streaming_payload_omits_stream_options():
    """The API rejects stream_options unless stream is true."""
    payload = _run_chat(stream=False)
    assert payload["stream"] is False
    assert "stream_options" not in payload


def test_sync_response_strips_stream_options_from_a_reused_payload():
    provider = OpenAIProvider(
        model="gpt-4o", api_key="test-key", base_url="https://fixture.test/v1"
    )
    sent = {}

    class FakeResponse:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    async def fake_post(url, json=None, headers=None):  # noqa: A002 - httpx kwarg name
        sent.update(json or {})
        return FakeResponse()

    provider._client.post = fake_post

    async def run():
        try:
            payload = {"model": "gpt-4o", "stream": True, "stream_options": {"include_usage": True}}
            return [c async for c in provider._sync_response("https://fixture.test", payload, {})]
        finally:
            await provider.close()

    chunks = asyncio.run(run())
    assert chunks and chunks[0].content == "ok"
    assert sent["stream"] is False
    assert "stream_options" not in sent


# -- links 1+2+3 together, over a real SSE transport -----------------------


def _sse(events):
    """Frame events the way the Chat Completions API does, usage frame last."""
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"


OPENAI_STREAM = _sse(
    [
        {"choices": [{"delta": {"content": "priced"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        # The include_usage frame lands AFTER finish_reason, which is exactly
        # why the final ProviderChunk has to be held back one frame.
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 2_000,
                "completion_tokens": 500,
                "completion_tokens_details": {"reasoning_tokens": 120},
            },
        },
    ]
)


def _mock_openai(handler, model="o3-mini"):
    provider = OpenAIProvider(
        model=model, api_key="test-key", base_url="https://fixture.test/v1"
    )
    original = provider._client
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider, original


def test_final_chunk_carries_the_usage_that_arrives_after_finish():
    """Reporting usage at the finish frame would publish zeros forever."""
    provider, original = _mock_openai(lambda request: httpx.Response(200, text=OPENAI_STREAM))

    async def run():
        try:
            return [c async for c in provider.chat([{"role": "user", "content": "hi"}])]
        finally:
            await provider.close()
            await original.aclose()

    chunks = asyncio.run(run())
    assert "".join(c.content for c in chunks) == "priced"
    final = chunks[-1]
    assert final.finish_reason is not None
    assert final.usage is not None
    assert (final.usage.input_tokens, final.usage.output_tokens) == (2_000, 500)
    assert final.usage.thinking_tokens == 120
    # o3-mini rates ($1.10/$4.40 per 1M), not o3's ($10/$40).
    assert final.usage.total_cost == pytest.approx(0.0044)


def test_real_tokens_reach_the_sink_end_to_end():
    """The whole chain: stream_options -> usage frame -> provider dict -> sink."""
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, text=OPENAI_STREAM)

    native, original = _mock_openai(handler)
    outer = Provider(ProviderConfig("openai", "https://fixture.test/v1", "o3-mini"))
    outer._new_provider = native
    sink = UsageSink()

    async def run():
        try:
            return "".join(
                [
                    text
                    async for text, _ in stream_turn(
                        outer, [Message(role="user", content="hi")], usage_sink=sink
                    )
                ]
            )
        finally:
            await outer.close()
            await original.aclose()

    assert asyncio.run(run()) == "priced"
    assert requests[0]["stream_options"] == {"include_usage": True}
    assert sink.received is True
    assert (sink.usage.input_tokens, sink.usage.output_tokens) == (2_000, 500)
    assert sink.cost("o3-mini") == pytest.approx(0.0044)


def test_stream_options_rejection_degrades_instead_of_failing():
    """A gateway that refuses the extension must not take the request down."""
    payloads = []

    def handler(request):
        body = json.loads(request.content)
        payloads.append(body)
        if "stream_options" in body:
            return httpx.Response(
                400,
                json={"error": {"message": "Unrecognized request argument: stream_options"}},
            )
        return httpx.Response(200, text=_sse([{"choices": [{"delta": {"content": "ok"}}]}]))

    provider, original = _mock_openai(handler)

    async def run():
        try:
            return [c async for c in provider.chat([{"role": "user", "content": "hi"}])]
        finally:
            await provider.close()
            await original.aclose()

    chunks = asyncio.run(run())
    assert [p.get("stream_options") for p in payloads] == [{"include_usage": True}, None]
    assert "".join(c.content for c in chunks) == "ok"


def test_streaming_http_error_reports_a_useful_message():
    """A streamed error body must be read before anyone inspects it."""
    provider, original = _mock_openai(
        lambda request: httpx.Response(401, json={"error": {"message": "Incorrect API key"}})
    )

    async def run():
        try:
            with pytest.raises(ConnectionError, match="authentication failed"):
                async for _ in provider.chat([{"role": "user", "content": "hi"}]):
                    pass
        finally:
            await provider.close()
            await original.aclose()

    asyncio.run(run())
