"""W10-1 green gate: the JSONL wire format, its schema, and the ``--json`` surface.

The thing under test is a *contract with a program*, so the assertions are about
properties a program depends on and a human would never notice:

* every line parses on its own, with a strict parser (no ``NaN``, no raw newline
  inside a line, no un-escaped control character);
* the ``seq`` counter is dense and monotonic, which is the only way a consumer
  can tell "nothing happened" from "I lost a line";
* nothing in ``CoreEvent.data`` is silently dropped on the way out, including the
  live objects four event types deliberately carry;
* ``ToolOutcome.details`` -- ``ok_source`` above all -- survives byte for byte;
* the schema's type enum is the whole of ``EventType`` rather than a hand-typed
  subset that will drift.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from click.testing import CliRunner

from djcode.core.checkpoints import Checkpoint, FileChange
from djcode.core.diff import diff_text
from djcode.core.events import (
    CoreEvent,
    EventType,
    checkpoint_event,
    complete_event,
    diff_event,
    error_event,
    permission_decided_event,
    permission_request_event,
    queue_event,
    steer_event,
    thinking_event,
    token_event,
    tool_call_event,
    tool_result_event,
)
from djcode.core.outcome import ToolOutcome
from djcode.core.permissions import Decision, DecisionAction, ToolRequest
from djcode.frontends.headless import (
    SCHEMA_VERSION,
    EventWriter,
    dict_to_event,
    event_schema,
    event_to_dict,
    event_to_line,
)


def load(line: str) -> dict:
    """Parse one line the way a strict consumer would: no NaN, no Infinity."""
    return json.loads(line, parse_constant=_reject)


def _reject(token):  # pragma: no cover - only runs if the producer broke
    raise AssertionError(f"line contained the non-JSON token {token!r}")


# ── the envelope ───────────────────────────────────────────────────────────


def test_envelope_has_exactly_the_five_documented_keys():
    payload = load(event_to_line(token_event("hi"), seq=4))
    assert set(payload) == {"v", "seq", "ts", "type", "agent", "data"}
    assert payload["v"] == SCHEMA_VERSION
    assert payload["seq"] == 4
    assert payload["type"] == "token"
    assert payload["agent"] == {"name": "", "role": ""}
    assert payload["data"] == {"text": "hi"}
    assert isinstance(payload["ts"], float)


def test_event_to_dict_and_event_to_line_agree():
    """``event_to_dict`` is what an in-process consumer (a GUI backend on a
    socket) uses; ``event_to_line`` is the pipe. They must not drift."""
    event = complete_event("done", tool_rounds=2, elapsed_s=1.25)
    assert event_to_dict(event, seq=9) == load(event_to_line(event, seq=9))


def test_agent_identity_is_carried_when_there_is_one():
    event = CoreEvent(
        event_type=EventType.AGENT_TOKEN,
        agent_name="scout",
        agent_role="research",
        data={"token": "x", "thinking": False},
    )
    payload = load(event_to_line(event))
    assert payload["agent"] == {"name": "scout", "role": "research"}
    assert payload["data"] == {"token": "x", "thinking": False}


def test_a_line_is_one_line():
    """A token carrying newlines must not become three lines of JSONL."""
    line = event_to_line(token_event("first\nsecond\r\nthird\u2028fourth"))
    assert "\n" not in line and "\r" not in line
    assert load(line)["data"]["text"] == "first\nsecond\r\nthird\u2028fourth"


def test_writer_numbers_densely_and_flushes():
    class Sink:
        def __init__(self):
            self.lines: list[str] = []
            self.flushes = 0

        def write(self, text):
            self.lines.append(text)

        def flush(self):
            self.flushes += 1

    sink = Sink()
    writer = EventWriter(sink)
    for index in range(5):
        writer.write(token_event(str(index)))
    assert writer.count == 5
    assert sink.flushes == 5
    seqs = [load(line)["seq"] for line in sink.lines]
    assert seqs == [0, 1, 2, 3, 4]
    assert all(line.endswith("\n") for line in sink.lines)


def test_round_trip_restores_the_event():
    original = queue_event("do the other thing", action="returned", depth=2)
    restored = dict_to_event(load(event_to_line(original)))
    assert restored.event_type is EventType.QUEUE
    assert restored.data == original.data
    assert restored.timestamp == pytest.approx(original.timestamp)


def test_an_unknown_type_is_refused_rather_than_guessed():
    with pytest.raises(ValueError):
        dict_to_event({"type": "not_an_event", "data": {}})


# ── coercion: nothing in data may be lost or become unparseable ────────────


def test_tool_outcome_details_survive_byte_for_byte():
    outcome = ToolOutcome(
        content="ran 3 tests",
        ok=True,
        details={"ok_source": "handler", "exit_code": 0, "files": ["a.py", "b.py"]},
        spill_path="C:\\tmp\\spill.txt",
        duration_ms=41,
    )
    data = load(event_to_line(tool_result_event("c-1", outcome, name="bash")))["data"]
    assert data["details"] == {
        "ok_source": "handler",
        "exit_code": 0,
        "files": ["a.py", "b.py"],
    }
    assert data["content"] == "ran 3 tests"
    assert data["ok"] is True
    assert data["spill_path"] == "C:\\tmp\\spill.txt"
    assert data["duration_ms"] == 41


def test_an_unverified_ok_is_transmitted_with_the_source_that_qualifies_it():
    """The wire must never let ``ok`` travel without ``ok_source`` beside it.

    17 of 24 tools report ``unverified``, where ``ok=True`` means only "dispatch
    saw no failure signal". A consumer that cannot see the qualifier will draw a
    green tick on a failed build -- which is the regression W3-VERIFICATION.md
    records, shipped twice.
    """
    outcome = ToolOutcome(content="[exit code 7]", ok=True, details={"ok_source": "unverified"})
    data = load(event_to_line(tool_result_event("c-2", outcome, name="bash")))["data"]
    assert data["ok"] is True
    assert data["details"]["ok_source"] == "unverified"


def test_a_file_diff_goes_out_as_its_bounded_summary_and_not_as_a_repr():
    diff = diff_text("x.py", "one\ntwo\n", "one\nTWO\n")
    data = load(event_to_line(diff_event(diff)))["data"]
    assert data["path"] == "x.py"
    assert data["summary"]["hunks"], data["summary"]
    # The live object is the one thing deliberately dropped: `summary` IS its
    # serialisation, and carrying both would double the largest event's payload.
    assert "diff" not in data
    # And the bodies never reach the wire at all.
    assert "before_text" not in json.dumps(data)
    assert "after_text" not in json.dumps(data)


def test_a_checkpoint_goes_out_as_an_object_not_as_a_string():
    checkpoint = Checkpoint(
        id="cp-1",
        session_id="s-1",
        turn_id="t-1",
        seq=3,
        created_at="2026-09-17T00:00:00Z",
        tool_name="file_edit",
        label="edit x.py",
        files=[FileChange(path="x.py", pre_sha="aa", post_sha="bb", existed=True)],
    )
    data = load(event_to_line(checkpoint_event(checkpoint)))["data"]
    assert data["checkpoint_id"] == "cp-1"
    assert data["checkpoint"]["tool_name"] == "file_edit"
    assert data["checkpoint"]["files"][0]["path"] == "x.py"
    assert "object at 0x" not in json.dumps(data)


def test_a_permission_request_and_decision_go_out_as_objects():
    request = ToolRequest(tool="bash", arguments={"command": "rm -rf /"}, cwd="C:\\work")
    decision = Decision(DecisionAction.DENY, comment="absolutely not")
    asked = load(event_to_line(permission_request_event(request)))["data"]
    assert asked["tool"] == "bash"
    assert asked["arguments"] == {"command": "rm -rf /"}
    assert asked["request"]["cwd"] == "C:\\work"

    answered = load(event_to_line(permission_decided_event(request, decision)))["data"]
    assert answered["action"] == "deny"
    assert answered["allowed"] is False
    assert answered["comment"] == "absolutely not"
    assert answered["decision"]["action"] == "deny"


def test_non_finite_floats_never_reach_the_wire_as_bare_tokens():
    """``json.dumps`` writes bare ``NaN``/``Infinity`` by default and nothing
    standards-compliant on the far end will parse them."""
    event = CoreEvent(
        event_type=EventType.COMPLETE,
        data={"response": "", "tool_rounds": 0, "elapsed_s": math.nan, "cost": math.inf},
    )
    data = load(event_to_line(event))["data"]  # `load` would raise on a bare token
    assert data["elapsed_s"] == "nan"
    assert data["cost"] == "inf"


def test_exotic_values_are_coerced_rather_than_crashing_the_stream():
    class Opaque:
        def __repr__(self):
            return "<opaque>"

    event = CoreEvent(
        event_type=EventType.ERROR,
        data={
            "error": "e",
            "kind": "k",
            "recoverable": False,
            "path": Path("a") / "b",
            "action": DecisionAction.ALWAYS,
            "raw": b"\xff\xfe bytes",
            "tags": {"b", "a"},
            "weird": Opaque(),
        },
    )
    data = load(event_to_line(event))["data"]
    assert data["path"].endswith("b")
    assert data["action"] == "always"
    assert data["tags"] == ["a", "b"]
    assert data["weird"] == "<opaque>"
    assert isinstance(data["raw"], str)


def test_a_cycle_does_not_hang_or_raise():
    """A self-referential dict in ``args`` must cost a line, never the process."""
    loop: dict = {}
    loop["self"] = loop
    line = event_to_line(tool_call_event("probe", {"loop": loop}, "c-9"))
    assert load(line)["data"]["name"] == "probe"


# ── the schema ─────────────────────────────────────────────────────────────


def test_schema_enumerates_every_event_type_the_enum_defines():
    """Derived, not typed. A new EventType cannot ship an ignorant schema."""
    schema = event_schema()
    assert set(schema["properties"]["type"]["enum"]) == {m.value for m in EventType}


def test_schema_declares_a_shape_for_every_session_event():
    schema = event_schema()
    declared = {branch["if"]["properties"]["type"]["const"] for branch in schema["allOf"]}
    session_family = {
        EventType.TOKEN,
        EventType.THINKING,
        EventType.TOOL_CALL,
        EventType.TOOL_RESULT,
        EventType.DIFF,
        EventType.CHECKPOINT,
        EventType.COMPLETE,
        EventType.ERROR,
        EventType.PERMISSION_REQUEST,
        EventType.PERMISSION_DECIDED,
        EventType.STEER,
        EventType.QUEUE,
    }
    assert declared == {member.value for member in session_family}


SAMPLES = [
    token_event("hello"),
    thinking_event("hmm"),
    tool_call_event("bash", {"command": "ls"}, "c-1"),
    tool_result_event("c-1", ToolOutcome(content="ok", details={"ok_source": "handler"})),
    diff_event(diff_text("x.py", "a\n", "b\n")),
    checkpoint_event(
        Checkpoint(
            id="cp-1",
            session_id="s-1",
            turn_id="t-1",
            seq=1,
            created_at="2026-09-17T00:00:00Z",
            tool_name="file_edit",
            label="edit",
        )
    ),
    complete_event("done", tool_rounds=1, elapsed_s=0.5),
    error_event("boom", kind="Timeout", recoverable=True),
    permission_request_event(ToolRequest(tool="bash", arguments={"command": "ls"})),
    permission_decided_event(
        ToolRequest(tool="bash", arguments={}), Decision(DecisionAction.ALLOW)
    ),
    steer_event("use uv", delivered=True),
    queue_event("later", action="queued", depth=1),
]


@pytest.mark.parametrize("event", SAMPLES, ids=lambda e: e.event_type.value)
def test_every_session_event_satisfies_its_own_schema_branch(event):
    """A hand-rolled check of the envelope plus the one ``if/then`` that applies.

    Deliberately not a ``jsonschema`` dependency: this suite must keep running on
    a machine with nothing extra installed, and the properties worth checking --
    required keys present, declared types honoured, enums respected -- are cheap
    to state directly.
    """
    schema = event_schema()
    payload = load(event_to_line(event, seq=0))

    assert set(payload) == set(schema["required"])
    assert payload["type"] in schema["properties"]["type"]["enum"]

    branch = next(
        b for b in schema["allOf"] if b["if"]["properties"]["type"]["const"] == payload["type"]
    )
    shape = branch["then"]["properties"]["data"]
    missing = [key for key in shape["required"] if key not in payload["data"]]
    assert not missing, f"{payload['type']} is missing {missing} from data"
    for key, spec in shape["properties"].items():
        if key not in payload["data"]:
            continue
        _check(payload["data"][key], spec, f"{payload['type']}.data.{key}")


_PY_TYPES = {
    "string": str,
    "boolean": bool,
    "integer": int,
    "number": (int, float),
    "object": dict,
    "array": list,
    "null": type(None),
}


def _check(value, spec, where):
    declared = spec.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        allowed = tuple(
            t
            for name in names
            for t in (
                _PY_TYPES[name] if isinstance(_PY_TYPES[name], tuple) else (_PY_TYPES[name],)
            )
        )
        assert isinstance(value, allowed), f"{where}: {value!r} is not {declared}"
    if "enum" in spec:
        assert value in spec["enum"], f"{where}: {value!r} not in {spec['enum']}"


def test_schema_is_serialisable_and_stable_across_calls():
    first = json.dumps(event_schema(), sort_keys=True)
    second = json.dumps(event_schema(), sort_keys=True)
    assert first == second


# ── the CLI surface ────────────────────────────────────────────────────────


def test_output_schema_prints_valid_json_and_exits_zero():
    result = CliRunner().invoke(main_cli(), ["--output-schema"])
    assert result.exit_code == 0, result.output
    schema = json.loads(result.output)
    assert schema["title"] == "DJcode core event"
    assert "token" in schema["properties"]["type"]["enum"]


def test_json_without_a_prompt_is_a_usage_error_not_a_hang():
    """``--json`` is one-shot. Without a prompt there is no turn to stream, and
    silently dropping into the interactive REPL would hang a CI step forever."""
    result = CliRunner().invoke(main_cli(), ["--json"])
    assert result.exit_code == 2, result.output
    assert "needs a prompt" in result.output


def test_json_and_output_schema_are_documented_in_help():
    result = CliRunner().invoke(main_cli(), ["--help"])
    assert result.exit_code == 0
    assert "--json" in result.output
    assert "--output-schema" in result.output


def main_cli():
    from djcode.cli import main

    return main


# ── run_headless end to end ────────────────────────────────────────────────


def _scripted(chunks):
    """A Provider subclass that replays a scripted OpenAI-compatible stream."""
    from djcode.provider import Provider

    class Scripted(Provider):
        def validate_model(self):
            return True, ""

        async def chat_openai_compat(self, messages, stream=True):
            for chunk in chunks:
                yield chunk

        async def close(self):
            return None

    return Scripted


def _pin_custom_provider(monkeypatch, module):
    """Point ``ProviderConfig.from_config`` at an unreachable custom endpoint.

    Without this the config on this machine says ``ollama``, and ``Provider``
    routes a custom ``chat_openai_compat`` override straight past to the Ollama
    adapter -- which then tries to reach localhost:11434 and the test measures
    the developer's daemon instead of the front-end.
    """
    from djcode.provider import ProviderConfig

    class Pinned(ProviderConfig):
        @classmethod
        def from_config(cls, provider_override=None, model_override=None):
            return ProviderConfig("custom", "https://example.invalid", "fixture", "fixture")

    monkeypatch.setattr(module, "ProviderConfig", Pinned)


def _run(monkeypatch, tmp_path, chunks, **kwargs):
    import asyncio
    import io

    from djcode.frontends.headless import runner as module

    monkeypatch.setattr(module, "Provider", _scripted(chunks))
    _pin_custom_provider(monkeypatch, module)
    out, err = io.StringIO(), io.StringIO()
    code = asyncio.run(
        module.run_headless(
            "do the thing", cwd=tmp_path, stdout=out, stderr=err, **kwargs
        )
    )
    events = [load(line) for line in err.getvalue().splitlines() if line.strip()]
    return code, out.getvalue(), events


SAY_HELLO = [
    {"choices": [{"delta": {"content": "Hello "}, "finish_reason": None}]},
    {"choices": [{"delta": {"content": "world."}, "finish_reason": "stop"}]},
]


def test_stdout_carries_the_answer_and_nothing_else(monkeypatch, tmp_path):
    """The whole reason this surface exists. ``djcode --json "x" > out.md`` must
    produce the answer, not the answer interleaved with tool chatter."""
    code, stdout, events = _run(monkeypatch, tmp_path, SAY_HELLO)
    assert code == 0
    assert stdout == "Hello world.\n"
    assert [e["type"] for e in events][-1] == "complete"


def test_stderr_is_pure_jsonl_and_densely_numbered(monkeypatch, tmp_path):
    code, _stdout, events = _run(monkeypatch, tmp_path, SAY_HELLO)
    assert code == 0
    assert events, "no events were written at all"
    assert [e["seq"] for e in events] == list(range(len(events)))
    assert all(e["v"] == SCHEMA_VERSION for e in events)
    assert "token" in {e["type"] for e in events}


def test_the_complete_event_response_is_what_stdout_received(monkeypatch, tmp_path):
    _code, stdout, events = _run(monkeypatch, tmp_path, SAY_HELLO)
    complete = [e for e in events if e["type"] == "complete"]
    assert len(complete) == 1
    assert complete[0]["data"]["response"] == stdout.rstrip("\n")


def test_a_failing_turn_ends_in_an_error_event_and_a_nonzero_exit(monkeypatch, tmp_path):
    """No traceback on stdout, no half-stream with no terminal event."""
    import asyncio
    import io

    from djcode.frontends.headless import runner as module
    from djcode.provider import Provider

    class Broken(Provider):
        def validate_model(self):
            return True, ""

        async def chat_openai_compat(self, messages, stream=True):
            raise RuntimeError("the provider fell over")
            yield  # pragma: no cover - makes this an async generator

        async def close(self):
            return None

    monkeypatch.setattr(module, "Provider", Broken)
    _pin_custom_provider(monkeypatch, module)
    out, err = io.StringIO(), io.StringIO()
    code = asyncio.run(
        module.run_headless("do the thing", cwd=tmp_path, stdout=out, stderr=err)
    )
    events = [load(line) for line in err.getvalue().splitlines() if line.strip()]
    assert code == 1
    assert out.getvalue() == ""
    assert events[-1]["type"] == "error"
    assert "fell over" in events[-1]["data"]["error"]


def test_an_unusable_model_is_reported_as_an_event_not_as_an_exception(monkeypatch, tmp_path):
    import asyncio
    import io

    from djcode.frontends.headless import runner as module
    from djcode.provider import Provider

    class Unusable(Provider):
        def validate_model(self):
            return False, "model 'ghost' is not installed"

        async def close(self):
            return None

    monkeypatch.setattr(module, "Provider", Unusable)
    _pin_custom_provider(monkeypatch, module)
    out, err = io.StringIO(), io.StringIO()
    code = asyncio.run(
        module.run_headless("do the thing", cwd=tmp_path, stdout=out, stderr=err)
    )
    events = [load(line) for line in err.getvalue().splitlines() if line.strip()]
    assert code == 1
    assert out.getvalue() == ""
    assert [e["type"] for e in events] == ["error"]
    assert "ghost" in events[0]["data"]["error"]


def test_unattended_approval_denies_rather_than_auto_accepting(monkeypatch, tmp_path):
    """``--json`` is an output format. It must not widen what the agent may do.

    The deny is visible on the wire as a request/decision pair, so a CI step can
    report exactly which tool it would need to authorise.
    """
    from djcode.frontends.headless.runner import NO_HUMAN, _denier

    assert _denier(auto_accept=True) is None, "--auto-accept must not be second-guessed"

    import asyncio

    from djcode.core.permissions import DecisionAction, ToolRequest

    decide = _denier(auto_accept=False)
    decision = asyncio.run(decide(ToolRequest(tool="bash", arguments={"command": "ls"})))
    assert decision.action is DecisionAction.DENY
    assert bool(decision) is False
    assert decision.comment == NO_HUMAN
