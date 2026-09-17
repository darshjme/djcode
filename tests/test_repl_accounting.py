"""P1-6, the half W1 could not finish: the engine now collects the accounting.

W1-3 built `UsageSink` and taught `stream_turn` to fill it, but `Operator`
called `stream_turn` without a sink at both of its call sites, so every token
and cost figure any surface printed was `len(text) // 4` -- and printed with no
mark saying so. These tests pin both halves: the numbers arrive, and when they
do not arrive the display says `~`.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from djcode.status import StatusBar


def _final(text="done", usage=None):
    chunk = {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}
    if usage is not None:
        chunk["usage"] = usage
    return chunk


def _call(name, arguments="{}", usage=None):
    chunk = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "abc",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ]
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


class _ScriptedProvider:
    """One list of chunks per request, in order."""

    def __init__(self, turns, model="gpt-4o-mini"):
        self._turns = iter(turns)
        self.config = SimpleNamespace(model=model, name="scripted", context_window=128000)
        self.messages = []

    async def chat(self, messages, stream=True):
        self.messages.append(list(messages))
        for chunk in next(self._turns):
            yield chunk

    async def close(self):
        return None


def _operator(provider):
    from djcode.agents.operator import Operator

    return Operator(provider, auto_accept=True)


def _drain(operator, text="hello"):
    async def run():
        return "".join([token async for token in operator.send(text)])

    return asyncio.run(run())


def test_provider_usage_reaches_the_operator():
    provider = _ScriptedProvider(
        [[_final("answer", usage={"prompt_tokens": 1200, "completion_tokens": 40})]]
    )
    operator = _operator(provider)
    assert _drain(operator) == "answer"
    assert operator.turn_usage.received is True
    assert operator.turn_usage.usage.input_tokens == 1200
    assert operator.turn_usage.usage.output_tokens == 40
    assert operator.turn_usage.requests == 1


def test_a_multi_round_turn_sums_every_request():
    """A tool round is a second request. The turn figure must be the turn's,
    not the last request's."""
    provider = _ScriptedProvider(
        [
            [
                _call(
                    "file_read",
                    json.dumps({"path": "pyproject.toml"}),
                    usage={"prompt_tokens": 1000, "completion_tokens": 20},
                )
            ],
            [_final("read it", usage={"prompt_tokens": 1500, "completion_tokens": 30})],
        ]
    )
    operator = _operator(provider)
    assert "read it" in _drain(operator)
    assert operator.turn_usage.requests == 2
    assert operator.turn_usage.usage.input_tokens == 2500
    assert operator.turn_usage.usage.output_tokens == 50
    # `last` is the most recent request, which is the context actually sent.
    assert operator.turn_usage.last.input_tokens == 1500


def test_session_total_accumulates_across_turns_while_turn_figure_resets():
    provider = _ScriptedProvider(
        [
            [_final("one", usage={"prompt_tokens": 100, "completion_tokens": 10})],
            [_final("two", usage={"prompt_tokens": 200, "completion_tokens": 20})],
        ]
    )
    operator = _operator(provider)
    _drain(operator, "first")
    assert operator.turn_usage.usage.output_tokens == 10
    _drain(operator, "second")
    assert operator.turn_usage.usage.output_tokens == 20, "turn sink must reset per turn"
    assert operator.session_usage.usage.output_tokens == 30
    assert operator.session_usage.usage.input_tokens == 300
    assert operator.session_usage.requests == 2


def test_a_provider_that_reports_nothing_leaves_received_false():
    """The whole point of the flag. Ollama and the local runtimes report no
    usage, and a figure derived from silence must never be shown as a
    measurement."""
    provider = _ScriptedProvider([[_final("answer")]])
    operator = _operator(provider)
    _drain(operator)
    assert operator.turn_usage.received is False
    assert operator.turn_usage.usage.output_tokens == 0


def test_cost_is_priced_locally_when_the_provider_reports_no_dollars():
    provider = _ScriptedProvider(
        [[_final("answer", usage={"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000})]]
    )
    operator = _operator(provider)
    _drain(operator)
    assert operator.turn_usage.cost("gpt-4o-mini") > 0


@pytest.mark.parametrize(
    "estimated,expected",
    [(True, "~1.2K"), (False, "1.2K")],
)
def test_status_bar_marks_estimates_with_a_tilde(estimated, expected):
    bar = StatusBar()
    bar.update(model="m", provider="p", token_count=1200, tokens_estimated=estimated)
    assert expected in str(bar.render().value)


def test_status_bar_defaults_to_calling_its_number_an_estimate():
    """A caller that has not been taught the difference must not accidentally
    claim precision."""
    bar = StatusBar()
    bar.update(token_count=500)
    assert "~500" in str(bar.render().value)


def test_repl_token_formatter_is_unit_consistent():
    from djcode.repl import _format_token_count

    assert _format_token_count(999) == "999"
    assert _format_token_count(1200) == "1.2k"
