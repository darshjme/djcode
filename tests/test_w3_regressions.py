"""Regressions found by the W3 verification pass, after W3 was committed.

Both bugs were introduced by W3-3 (the outcome passthrough) and neither was
caught by the 641-test suite. They share one root cause: ``ToolOutcome.ok`` is
only meaningful when ``details["ok_source"]`` says it is. 17 of the 24 tools
report failure only as text, so an "unverified" ok=True means "dispatch saw no
failure signal", NOT "the tool succeeded". Treating it as success made a failed
build look green.
"""

from __future__ import annotations

import shutil

import pytest

from djcode.core.outcome import ToolOutcome

UNVERIFIED = {"ok_source": "unverified"}
AUTHORITATIVE = {"ok_source": "handler"}


# ── Regression 1: dependency gating on the DAF wire ─────────────────────────


def test_result_ok_ignores_an_unverified_flag():
    """An unverified ok=True must not override failure text.

    This is the exact substitution that broke the DAF branch: _result_ok was
    changed to trust outcome.ok, and because bash is "unverified" a command
    that exited 7 reported success on the wire.
    """
    from djcode.workflow import _result_ok

    assert _result_ok(ToolOutcome(content="[exit code 7]", ok=True, details=UNVERIFIED)) is False
    assert _result_ok(ToolOutcome(content="Error: boom", ok=True, details=UNVERIFIED)) is False
    assert _result_ok(ToolOutcome(content="all fine", ok=True, details=UNVERIFIED)) is True


def test_result_ok_trusts_an_authoritative_flag_over_the_text():
    """When the handler really told us, the flag wins -- even against the text."""
    from djcode.workflow import _result_ok

    # Content that merely starts with the word Error, on a genuine success.
    assert _result_ok(ToolOutcome(content="Error: none found", ok=True, details=AUTHORITATIVE))
    # A genuine failure whose content reads perfectly cheerful.
    assert not _result_ok(ToolOutcome(content="all good", ok=False, details=AUTHORITATIVE))
    # Dispatch-level refusals are authoritative too.
    assert not _result_ok(
        ToolOutcome(content="Error: Unknown tool 'x'", ok=False, details={"ok_source": "dispatch"})
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["native", "daf"])
async def test_failed_dependency_stops_dependents_on_both_branches(mode):
    """A failed build must not let the deploy run -- on EITHER engine.

    Measured before the fix: native skipped the dependent, DAF ran it. The two
    branches disagreeing is the same class of bug W3-3 set out to fix, pointed
    the other way, and it only appears on a machine with a Rust toolchain.
    """
    from djcode.tools import dispatch_tool
    from djcode.workflow import WorkflowEngine

    if mode == "daf" and shutil.which("cargo") is None:
        pytest.skip("DAF engine needs a Rust toolchain; the native branch covers the logic")

    nodes = [
        {"id": "build", "name": "bash", "arguments": {"command": "exit 7"}},
        {
            "id": "deploy",
            "name": "bash",
            "arguments": {"command": "echo DEPLOYED"},
            "dependencies": ["build"],
        },
    ]
    engine = WorkflowEngine(mode=mode)
    results = await engine.execute(nodes, dispatch_tool)
    if mode == "daf":
        assert engine.mode == "daf", "engine fell back to native; this proved nothing"

    deploy = str(results.get("deploy", ""))
    assert "DEPLOYED" not in deploy, (
        f"{engine.mode} branch ran the dependent node after its dependency failed: {deploy!r}"
    )


# ── Regression 2: the code-block router silently swallowed a TypeError ──────
#
# tool_router.py is the fallback path for providers WITHOUT native function
# calling, and it had no test coverage at all. Three sites treated the dispatch
# result as a str. After W3 they raised TypeError / AttributeError, which the
# blanket `except Exception` turned into success=False -- so the file really was
# written, and the model was told the write failed.


@pytest.mark.parametrize(
    ("result", "expected_ok"),
    [
        (ToolOutcome(content="wrote 3 lines", ok=True, details=AUTHORITATIVE), True),
        (ToolOutcome(content="Error: denied", ok=False, details=AUTHORITATIVE), False),
        # The dangerous case: unverified flag says True, the text says otherwise.
        (ToolOutcome(content="Error: no such file", ok=True, details=UNVERIFIED), False),
        (ToolOutcome(content="[exit code 3]", ok=True, details=UNVERIFIED), False),
        (ToolOutcome(content="ok", ok=True, details=UNVERIFIED), True),
        # Bare strings still work: callers may supply their own dispatcher.
        ("Error: boom", False),
        ("Traceback (most recent call last):", False),
        ("done", True),
    ],
)
def test_intent_result_normalises_both_shapes(result, expected_ok):
    from djcode.tool_router import _intent_result

    text, ok = _intent_result(result)
    assert isinstance(text, str), "output must always be a str for ToolResult.output"
    assert text == str(result)
    assert ok is expected_ok


def test_intent_result_never_raises_on_a_tool_outcome():
    """The specific failure: `"Error" not in outcome` and `outcome.startswith`.

    Both raise on a ToolOutcome. They were swallowed by a blanket except, so the
    tool's side effect happened while the model was told it had failed.
    """
    from djcode.tool_router import _intent_result

    outcome = ToolOutcome(content="fine", ok=True, details=AUTHORITATIVE)
    with pytest.raises(TypeError):
        _ = "Error" not in outcome  # the old site, kept here to pin why it moved
    with pytest.raises(AttributeError):
        outcome.startswith("Error")  # the other old site

    # The replacement handles it.
    assert _intent_result(outcome) == ("fine", True)
