"""The proof obligation of the whole ten-wave refactor (BLUEPRINT-CLI.md section 2.5).

    Drive a FULL TURN -- user text in; TOKEN, TOOL_CALL, TOOL_RESULT, DIFF,
    CHECKPOINT and COMPLETE out; one file edited; one checkpoint restored --
    with ``djcode.frontends`` NEVER imported.

    If that test passes, a GUI can be written. If it does not, W9's REPL is just
    a prettier fork.

HOW THIS IS PROVED, AND WHY IT IS A SUBPROCESS
----------------------------------------------
"Never imported" cannot be checked in-process. By the time pytest reaches this
file, ``sys.modules`` already holds ``djcode.frontends.repl.render`` (imported by
``test_repl_render.py``), ``rich`` (pytest's own output), ``click`` and
``prompt_toolkit``. An in-process assertion would either pass vacuously or fail
for reasons that have nothing to do with core -- the same trap
``test_headless_purity.py`` documents for its AST walk.

So the turn runs in a **fresh interpreter** with a ``sys.meta_path`` finder
installed *before* ``djcode`` is imported, which RAISES on any attempt to import
``djcode.frontends``. Not a post-hoc ``sys.modules`` inspection: an import that
happened and was then deleted from ``sys.modules`` would be invisible to that,
while this one cannot be got past. The same run also proves there is no terminal
-- stdin is ``/dev/null`` and stdout is a file the harness asserts is empty, so
any surviving ``print()`` in the engine's path shows up as a failure here rather
than as a stray line in somebody's CI log.

``test_headless_purity.py`` proves core is *statically* free of terminal code.
This proves it is *dynamically* free of front-end code while doing real work:
streaming, a tool call through the chokepoint, a checkpoint capture, a diff, and
an undo that puts the bytes back.

WHAT IS DELIBERATELY NOT FAKED
------------------------------
* The tool is the real ``file_edit`` running through the real ``dispatch_tool``
  chokepoint, and it really changes a file on disk.
* The workflow engine is left at its configured default, which is ``daf`` -- the
  Rust host, the branch that runs in production on this machine. A contract test
  that quietly pinned ``native`` would prove the GUI works on the fallback
  scheduler and say nothing about the path a user is on. The engine actually
  taken is reported back and asserted.
* The checkpoint store is the real one, built the way ``CoreSession`` builds it.
* ``restore`` is the real restore, and the assertion is on bytes read back off
  disk, not on a report field.

Only the model is a fixture -- a ``Provider`` subclass that yields a scripted
OpenAI-compatible stream. There is no network in this test and no API key.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

requires_cargo = pytest.mark.skipif(
    shutil.which("cargo") is None and not os.environ.get("DJCODE_DAF_ENGINE"),
    reason="DAF engine needs a Rust toolchain; set DJCODE_DAF_ENGINE for a prebuilt one",
)

REPO_SRC = Path(__file__).resolve().parent.parent / "src"


DRIVER = r'''
"""Run one full CoreSession turn with djcode.frontends made unimportable."""

import json
import os
import sys
import traceback
from pathlib import Path

RESULT = Path(sys.argv[1])
ROOT = Path(sys.argv[2])

# --------------------------------------------------------------------------
# 1. The guard. Installed before djcode is imported, so there is no window in
#    which a front-end could be pulled in unobserved. It RAISES rather than
#    recording, because a recorded violation that the engine swallowed in a
#    `try: import ... except ImportError:` would still be a violation the test
#    never saw.
# --------------------------------------------------------------------------
BANNED = ("djcode.frontends", "djcode.repl", "djcode.app", "djcode.tui")


class FrontendGuard:
    def find_spec(self, fullname, path=None, target=None):
        for banned in BANNED:
            if fullname == banned or fullname.startswith(banned + "."):
                raise ImportError(
                    "core contract violation: the engine imported the front-end "
                    "module " + fullname
                )
        return None


sys.meta_path.insert(0, FrontendGuard())

# --------------------------------------------------------------------------
# 2. No terminal. stdin is closed off entirely; stdout goes to a file the
#    harness asserts is empty.
# --------------------------------------------------------------------------
devnull = os.open(os.devnull, os.O_RDWR)
os.dup2(devnull, 0)
out_fd = os.open(str(ROOT / "stdout.txt"), os.O_RDWR | os.O_CREAT | os.O_TRUNC)
err_fd = os.open(str(ROOT / "stderr.txt"), os.O_RDWR | os.O_CREAT | os.O_TRUNC)
os.dup2(out_fd, 1)
os.dup2(err_fd, 2)

report = {"ok": False}


def finish():
    RESULT.write_text(json.dumps(report), encoding="utf-8")


try:
    import asyncio

    # Only djcode.core and the two already-headless modules its contract
    # re-exports. Note what is NOT here: no djcode.repl, no djcode.cli.
    from djcode.core import (
        CoreSession,
        EventBus,
        EventType,
        SessionOptions,
    )
    from djcode.provider import Provider, ProviderConfig

    TARGET = ROOT / "target.py"
    BEFORE = b"alpha\nbeta\ngamma\n"
    TARGET.write_bytes(BEFORE)

    def chunk(**delta):
        finish_reason = delta.pop("finish_reason", None)
        return {"choices": [{"delta": delta, "finish_reason": finish_reason}]}

    class ScriptedModel(Provider):
        """Two rounds: say something, edit the file, say something else."""

        turns = 0

        async def chat_openai_compat(self, messages, stream=True):
            ScriptedModel.turns += 1
            if ScriptedModel.turns == 1:
                yield chunk(content="Renaming beta. ")
                yield chunk(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call-edit-1",
                            "function": {
                                "name": "file_edit",
                                "arguments": json.dumps(
                                    {
                                        "path": str(TARGET),
                                        "old_string": "beta",
                                        "new_string": "BETA",
                                    }
                                ),
                            },
                        }
                    ],
                    finish_reason="tool_calls",
                )
            else:
                yield chunk(content="Renamed beta to BETA.", finish_reason="stop")

    async def main():
        provider = ScriptedModel(
            ProviderConfig("custom", "https://example.invalid", "fixture", "fixture")
        )
        session = CoreSession(
            {},
            provider=provider,
            event_bus=EventBus(),
            options=SessionOptions(auto_accept=True, cwd=ROOT),
        )
        try:
            events = [event async for event in session.send("Rename beta to BETA in target.py")]
            report["engine"] = str(session.operator.workflow.mode)
            report["events"] = [event.event_type.value for event in events]
            report["tokens"] = "".join(
                event.data.get("text", "")
                for event in events
                if event.event_type is EventType.TOKEN
            )
            report["response"] = next(
                (
                    event.data.get("response", "")
                    for event in events
                    if event.event_type is EventType.COMPLETE
                ),
                None,
            )
            report["tool_calls"] = [
                {"name": event.data.get("name"), "call_id": event.data.get("call_id")}
                for event in events
                if event.event_type is EventType.TOOL_CALL
            ]
            report["tool_results"] = [
                {
                    "name": event.data.get("name"),
                    "call_id": event.data.get("call_id"),
                    "ok": event.data.get("ok"),
                    "ok_source": (event.data.get("details") or {}).get("ok_source"),
                    "content": event.data.get("content"),
                }
                for event in events
                if event.event_type is EventType.TOOL_RESULT
            ]
            report["diffs"] = [
                {
                    "path": event.data.get("path"),
                    "hunks": len((event.data.get("summary") or {}).get("hunks") or []),
                }
                for event in events
                if event.event_type is EventType.DIFF
            ]
            report["checkpoint_events"] = [
                event.data.get("checkpoint_id")
                for event in events
                if event.event_type is EventType.CHECKPOINT
            ]

            # The file really changed.
            report["after_edit"] = TARGET.read_bytes().decode("utf-8")

            # The undo. Real store, real restore, bytes read back off disk.
            marks = await session.checkpoints()
            report["checkpoints"] = [
                {"id": m.checkpoint_id, "tool": m.tool_name, "files": [f.path for f in m.files]}
                for m in marks
            ]
            restored_report = None
            if marks:
                restored_report = await session.restore(marks[0].checkpoint_id)
                report["restore"] = restored_report.as_dict()
            report["after_restore"] = TARGET.read_bytes().decode("utf-8")
            report["bytes_match"] = TARGET.read_bytes() == BEFORE

            # 3. Nothing from a front-end made it into this interpreter, and
            #    neither did a terminal library core is forbidden to touch.
            #    rich/click are excluded from this list on purpose: httpx
            #    imports both for its own CLI, which says nothing about djcode.
            report["frontend_modules"] = sorted(
                name
                for name in sys.modules
                if any(name == b or name.startswith(b + ".") for b in BANNED)
            )
            report["terminal_modules"] = sorted(
                name
                for name in ("textual", "prompt_toolkit", "questionary")
                if name in sys.modules
            )
            report["ok"] = True
        finally:
            await session.close()

    asyncio.run(main())
except BaseException:
    report["traceback"] = traceback.format_exc()
finally:
    finish()
'''


def _run_contract(tmp_path: Path, config_dir: Path) -> dict:
    """Run the driver in a fresh interpreter and hand back its report."""
    root = tmp_path / "work"
    root.mkdir()
    driver = tmp_path / "contract_driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    result_path = tmp_path / "report.json"

    env = dict(os.environ)
    env["DJCODE_CONFIG_DIR"] = str(config_dir)
    env["DJCODE_NO_UPDATE_CHECK"] = "1"
    env["DJCODE_SKIP_STARTUP_CHECK"] = "1"
    env["PYTHONPATH"] = str(REPO_SRC) + os.pathsep + env.get("PYTHONPATH", "")

    completed = subprocess.run(
        [sys.executable, str(driver), str(result_path), str(root)],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(root),
        env=env,
    )
    assert result_path.is_file(), (
        "the contract driver produced no report at all\n"
        f"exit={completed.returncode}\nstdout={completed.stdout}\nstderr={completed.stderr}"
    )
    report = json.loads(result_path.read_text(encoding="utf-8"))
    report["_root"] = str(root)
    report["_stdout_file"] = (root / "stdout.txt").read_text(encoding="utf-8", errors="replace")
    report["_stderr_file"] = (root / "stderr.txt").read_text(encoding="utf-8", errors="replace")
    return report


@pytest.fixture(scope="module")
def contract(tmp_path_factory, daf_runtime):
    """One run of the full turn, shared by every assertion below.

    ``daf_runtime`` builds (or reuses) the Rust engine and returns the config
    directory holding it, which the subprocess then points ``DJCODE_CONFIG_DIR``
    at -- so the turn takes the DAF branch without paying for a cargo build
    inside the subprocess, where a several-minute compile would look like a hang.
    """
    return _run_contract(tmp_path_factory.mktemp("contract"), Path(daf_runtime))


# ── the obligation, one property per test so a failure names itself ────────


@requires_cargo
def test_the_turn_ran_at_all(contract):
    assert contract["ok"], (
        "the full-turn contract driver failed:\n"
        + contract.get("traceback", "(no traceback captured)")
        + "\n--- driver stderr ---\n"
        + contract["_stderr_file"]
    )


@requires_cargo
def test_no_frontend_module_was_imported(contract):
    """The headline. A GUI is possible exactly when this is true."""
    assert contract["ok"], contract.get("traceback", "")
    assert contract["frontend_modules"] == [], (
        "a front-end module reached the engine's import graph: "
        f"{contract['frontend_modules']}"
    )
    assert contract["terminal_modules"] == [], (
        "core dragged in a terminal library: " f"{contract['terminal_modules']}"
    )


@requires_cargo
def test_core_wrote_nothing_to_stdout(contract):
    """A GUI owns stdout. Core printing into it is a stolen frame."""
    assert contract["ok"], contract.get("traceback", "")
    assert contract["_stdout_file"] == "", (
        "the engine wrote to stdout during a turn: " f"{contract['_stdout_file']!r}"
    )


@requires_cargo
def test_the_daf_engine_was_the_one_that_ran(contract):
    """cargo is installed on this machine, so DAF is the production branch.

    If this reports ``native`` the engine fell back and every assertion below it
    is about the fallback scheduler, not about what a user runs.
    """
    assert contract["ok"], contract.get("traceback", "")
    assert contract["engine"] == "daf", (
        f"the workflow engine fell back to {contract['engine']!r}; this run proved "
        "nothing about the branch that executes on this box"
    )


@requires_cargo
def test_every_event_type_the_obligation_names_was_emitted(contract):
    assert contract["ok"], contract.get("traceback", "")
    seen = set(contract["events"])
    required = {"token", "tool_call", "tool_result", "diff", "checkpoint", "complete"}
    assert required <= seen, f"missing {sorted(required - seen)}; got {contract['events']}"


@requires_cargo
def test_tokens_streamed_and_complete_carries_the_whole_answer(contract):
    assert contract["ok"], contract.get("traceback", "")
    assert "Renaming beta." in contract["tokens"]
    assert "Renamed beta to BETA." in contract["tokens"]
    # COMPLETE.data["response"] is what `--json` writes to stdout, so it has to
    # be the whole answer and not the last chunk of it.
    assert contract["response"] == contract["tokens"]


@requires_cargo
def test_the_tool_call_and_its_result_pair_on_call_id(contract):
    """A UI draws a pending card on TOOL_CALL and completes it on TOOL_RESULT.

    They are only joinable if the id survives the round trip through the DAF
    wire -- which is exactly what W3-3's outcome side map exists to protect.
    """
    assert contract["ok"], contract.get("traceback", "")
    assert [c["name"] for c in contract["tool_calls"]] == ["file_edit"]
    assert [r["name"] for r in contract["tool_results"]] == ["file_edit"]
    assert contract["tool_calls"][0]["call_id"] == contract["tool_results"][0]["call_id"]


@requires_cargo
def test_tool_outcome_details_survive_to_the_front_end(contract):
    """P0-8. ``details`` is the channel a GUI renders from, and it is the thing
    the DAF wire used to destroy by stringifying the outcome.

    ``ok_source`` is asserted, not ``ok``. ``ok`` on its own is not a claim:
    17 of 24 tools report ``ok_source="unverified"``, which means "dispatch saw
    no failure signal" and NOT "the tool succeeded" -- two shipped regressions
    came from reading it the other way (``W3-VERIFICATION.md``). ``file_edit``
    is one of the two W7-2 converted (``tools/__init__.py::OUTCOME_TOOLS``), so
    here ``handler`` is the expected source and ``ok`` genuinely means something.
    A future refactor that demoted it to ``unverified`` would be a silent loss of
    signal, so it is pinned.

    Either way the edit itself is proved by bytes on disk, below -- never by a flag.
    """
    assert contract["ok"], contract.get("traceback", "")
    result = contract["tool_results"][0]
    assert result["ok_source"] == "handler", (
        "file_edit is an OUTCOME_TOOL and must report an authoritative ok_source; "
        f"got {result}"
    )
    assert result["ok"] is True, result


@requires_cargo
def test_one_file_was_really_edited(contract):
    assert contract["ok"], contract.get("traceback", "")
    assert contract["after_edit"] == "alpha\nBETA\ngamma\n"


@requires_cargo
def test_the_diff_event_describes_that_edit(contract):
    assert contract["ok"], contract.get("traceback", "")
    assert contract["diffs"], "a file changed and no DIFF event was emitted"
    assert contract["diffs"][0]["path"].endswith("target.py")
    assert contract["diffs"][0]["hunks"] >= 1


@requires_cargo
def test_one_checkpoint_was_captured_and_restored(contract):
    assert contract["ok"], contract.get("traceback", "")
    assert contract["checkpoint_events"], "no CHECKPOINT event for a tool that wrote"
    assert contract["checkpoints"], "CoreSession.checkpoints() came back empty"
    first = contract["checkpoints"][0]
    assert first["tool"] == "file_edit"
    assert contract["restore"]["failed"] == []
    assert contract["restore"]["skipped"] == []
    assert contract["bytes_match"], (
        "restore did not put the original bytes back: " f"{contract['after_restore']!r}"
    )
