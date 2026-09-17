"""Default DAF scheduling and DDAL transport for DJcode tool execution.

The Rust host never performs a tool's side effects. It schedules a graph and
transports requests to the owning Python session, where approval and dispatch
remain enforced. There is no automatic retry of side-effecting tools.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import uuid
from pathlib import Path

from djcode.config import CONFIG_DIR, load_config
from djcode.core.outcome import ToolOutcome

logger = logging.getLogger(__name__)

SOURCE = Path(__file__).with_name("daf_engine")
_BUILD_LOCK = None

#: Set once the process has told the user that DAF is unavailable, so a session
#: that runs a hundred tools does not print the same paragraph a hundred times.
_FALLBACK_ANNOUNCED = False

#: Drained by `drain_engine_notices`. A list rather than a print, because this
#: module is reachable from `djcode.core`, which may never touch a terminal.
_FALLBACK_NOTICES: list[str] = []


def drain_engine_notices() -> list[str]:
    """Take anything the engine needs the user to know, and forget it."""
    notices = list(_FALLBACK_NOTICES)
    _FALLBACK_NOTICES.clear()
    return notices

_ERROR_PREFIXES = ("error", "traceback", "[exit code", "command timed out")

DEPENDENCY_SKIPPED = "Error: dependency failed; tool was not executed"


class DAFUnavailableError(RuntimeError):
    """No Rust toolchain, so the DAF engine cannot be built on this machine.

    A subclass of RuntimeError so that callers written against the old
    contract keep catching it. `WorkflowEngine.execute` treats it as the
    signal to fall back to the native scheduler (SSOT non-goal 12).
    """


def _result_ok(result) -> bool:
    """Whether a dispatched result counts as success, for the DAF wire frame.

    W3-3: when the result is a ToolOutcome the flag is authoritative and is used
    directly -- no sniffing. The prefix heuristic survives only for callers that
    still hand this function a bare string (tests supply their own dispatch), and
    it mirrors what the DAF host itself does with such a value.
    """
    ok = getattr(result, "ok", None)
    source = (getattr(result, "details", None) or {}).get("ok_source")
    if isinstance(ok, bool) and source != "unverified":
        # The flag is authoritative: the handler raised, returned its own
        # outcome, or dispatch itself refused the call.
        return ok
    # ok_source == "unverified" means dispatch observed no failure signal -- it
    # is NOT a claim of success (see tools/__init__.py). 17 of 24 tools report
    # failure only as text, bash among them, so trusting the flag here would
    # tell the DAF host that `exit 7` succeeded and let it run every dependent
    # node. Fall back to the same heuristic the host applies to a bare string,
    # which is also what the native branch does -- keeping the two in step.
    text = result if isinstance(result, str) else str(result)
    return not text.lower().startswith(_ERROR_PREFIXES)


def topological_order(nodes: list[dict]) -> list[dict]:
    """Return `nodes` ordered so every node follows its dependencies.

    `validate_nodes` has already proven the graph is acyclic and that every
    dependency names a real node, so a single stable Kahn pass is enough and
    cannot loop forever. Ties keep the caller's original order.
    """
    remaining = list(nodes)
    done: set[str] = set()
    ordered: list[dict] = []
    while remaining:
        ready = [n for n in remaining if all(dep in done for dep in n.get("dependencies", []))]
        if not ready:  # pragma: no cover - validate_nodes rejects such graphs
            raise ValueError("workflow has a cycle or unknown dependency")
        for node in ready:
            ordered.append(node)
            done.add(node["id"])
        remaining = [n for n in remaining if n["id"] not in done]
    return ordered


async def engine_path() -> Path:
    override = os.environ.get("DJCODE_DAF_ENGINE")
    if override:
        path = Path(override).expanduser().resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise RuntimeError("DJCODE_DAF_ENGINE is not an executable file")
        return path
    digest = hashlib.sha256()
    for path in sorted(SOURCE.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(SOURCE)).encode())
            digest.update(path.read_bytes())
    target = CONFIG_DIR / "runtime" / "daf" / digest.hexdigest()[:16]
    binary = (
        target / "release" / ("djcode-daf-engine.exe" if os.name == "nt" else "djcode-daf-engine")
    )
    if binary.is_file():
        return binary
    cargo = shutil.which("cargo")
    if not cargo:
        raise DAFUnavailableError(
            "DAF is the default engine. Install Rust/Cargo to build it, or set"
            " DJCODE_DAF_ENGINE to a built DJcode engine. /workflow native "
            "explicitly selects the legacy engine."
        )
    target.mkdir(parents=True, exist_ok=True)
    # Cargo's target-directory lock serializes builds across sessions/processes.
    with (target / "build.log").open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            cargo,
            "build",
            "--release",
            "--locked",
            "--manifest-path",
            str(SOURCE / "Cargo.toml"),
            "--target-dir",
            str(target),
            stdout=log,
            stderr=log,
        )
        try:
            status = await process.wait()
        except BaseException:
            process.terminate()
            await process.wait()
            raise
    if status or not binary.is_file():
        raise RuntimeError(f"DAF build failed; inspect {target / 'build.log'}")
    return binary


def validate_nodes(nodes: list[dict]) -> None:
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 32:
        raise ValueError("workflow requires 1..32 nodes")
    ids = set()
    for node in nodes:
        if (
            not isinstance(node, dict)
            or not isinstance(node.get("id"), str)
            or not node["id"]
            or node["id"] in ids
        ):
            raise ValueError("workflow nodes require unique nonempty IDs")
        if not isinstance(node.get("name"), str) or not isinstance(node.get("arguments"), dict):
            raise ValueError("workflow nodes require a tool name and argument object")
        if not isinstance(node.get("dependencies", []), list):
            raise ValueError("dependencies must be a list")
        ids.add(node["id"])
    done = set()
    for _ in nodes:
        for node in nodes:
            if all(dep in done for dep in node.get("dependencies", [])):
                done.add(node["id"])
    if done != ids:
        raise ValueError("workflow has a cycle or unknown dependency")


class WorkflowEngine:
    def __init__(self, event_callback=None, *, mode=None):
        self.mode = mode or load_config().get("workflow_engine", "daf")
        self.event_callback = event_callback
        self.last_run = None
        self.last_events = []
        # W3-3: the ToolOutcome side map. `results` must stay str-valued --
        # capabilities.py JSON-encodes it and state.py slices it -- so the
        # structured outcome rides here instead, keyed by node id, and `one()`
        # hands it back intact. Without this the DAF branch stringifies the
        # outcome on the wire and `details` is destroyed before Operator ever
        # sees it, which is P0-8 silently doing nothing.
        self._outcomes: dict[str, ToolOutcome] = {}

    def _record(self, event):
        self.last_events.append(event)
        if self.event_callback:
            self.event_callback(event)

    def _fall_back_to_native(self, error: DAFUnavailableError) -> None:
        """Switch this engine to the native scheduler and say so once.

        SSOT non-goal 12: a machine without a Rust toolchain must keep working.
        The first tool call of a fresh install is the worst possible place to
        raise, so the engine degrades instead: same tools, same approvals, one
        node at a time.
        """
        global _FALLBACK_ANNOUNCED
        self.mode = "native"
        self._record({"event": "engine_fallback", "engine": "native", "reason": str(error)})
        if not _FALLBACK_ANNOUNCED:
            _FALLBACK_ANNOUNCED = True
            notice = (
                "No Rust toolchain found; running the native workflow engine instead of DAF. "
                "Tools still run one at a time with the same approvals. "
                "Install Rust/Cargo or set DJCODE_DAF_ENGINE to restore parallel scheduling."
            )
            logger.warning(notice)
            # The REPL configures no logging handler, so this warning went
            # nowhere: a user on a machine without cargo silently got a
            # degraded scheduler and no way to find out. The engine still may
            # not print -- `core` is terminal-free and this module is imported
            # by it -- so the notice is QUEUED and the front-end drains it,
            # exactly as W5's checkpoint notices already do.
            _FALLBACK_NOTICES.append(notice)

    async def _execute_native(self, nodes, dispatch):
        """Run the graph in-process, one node at a time, in dependency order.

        `validate_nodes` has already proven acyclicity, so a topological walk
        is well defined. A node whose dependency produced an error is not
        dispatched -- it records the same message the DAF host uses -- because
        skipping is what the DAG asked for and running it anyway would execute
        a side effect against a precondition that never happened.
        """
        results = {}
        for node in topological_order(nodes):
            ident = node["id"]
            dependencies = node.get("dependencies", [])
            if any(not _result_ok(results.get(dep, "")) for dep in dependencies):
                results[ident] = DEPENDENCY_SKIPPED
                continue
            self._record({"event": "tool", "id": ident, "name": node["name"]})
            # W3-1 interim, replaced by W3-3. The DAF branch has always done
            # `str(await dispatch(...))` (see `handle` below) and this branch
            # never did, so `dispatch_tool` returning a ToolOutcome made the two
            # branches return different TYPES from `one()` -- str on a box with
            # Rust, ToolOutcome on a box without. That asymmetry lands on
            # `state.py:262 result[:200]` (TypeError, every spawned agent) and
            # `capabilities.py:153 json.dumps` (TypeError, the workflow tool),
            # neither of which any test on a Rust-equipped box would catch. The
            # `str()` restores symmetry now; W3-3 replaces both branches with a
            # `self._outcomes` side map that keeps `results` str-valued anyway,
            # because `capabilities.py` JSON-encodes it.
            outcome = await dispatch(node["name"], node["arguments"])
            self._outcomes[ident] = outcome
            results[ident] = str(outcome)
        return results

    async def execute(self, nodes, dispatch, concurrency=1):
        validate_nodes(nodes)
        if not isinstance(concurrency, int) or not 1 <= concurrency <= 4:
            raise ValueError("concurrency must be 1..4")
        self.last_run = uuid.uuid4().hex
        self.last_events = []
        self._outcomes = {}
        if self.mode not in {"native", "daf"}:
            raise ValueError(f"Unknown workflow engine: {self.mode}")
        binary = None
        if self.mode == "daf":
            self._record({"event": "preparing", "engine": "DAF + DDAL"})
            try:
                binary = await engine_path()
            except DAFUnavailableError as error:
                self._fall_back_to_native(error)
        if self.mode == "native":
            return await self._execute_native(nodes, dispatch)
        process = await asyncio.create_subprocess_exec(
            str(binary),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=2**22,
        )
        stderr = asyncio.create_task(process.stderr.read(65536))
        results = {}
        pending = set()
        expected = {n["id"]: n for n in nodes}
        received = set()
        complete = None

        async def handle(event):
            ident = event["id"]
            try:
                outcome = await dispatch(event["name"], event["arguments"])
            except Exception as error:
                outcome = ToolOutcome(
                    content=f"Error: {error}",
                    ok=False,
                    details={"exception": type(error).__name__, "ok_source": "raised"},
                )
            self._outcomes[ident] = outcome
            result = str(outcome)
            results[ident] = result
            # ok now comes from the outcome rather than a prefix sniff of the
            # text. The wire frame is unchanged, so the Rust host needs no work.
            ok = _result_ok(outcome)
            process.stdin.write(
                (json.dumps({"id": ident, "ok": ok, "result": result}) + "\n").encode()
            )
            await process.stdin.drain()

        try:
            process.stdin.write(
                (json.dumps({"nodes": nodes, "concurrency": concurrency}) + "\n").encode()
            )
            await process.stdin.drain()
            async with asyncio.timeout(3610):
                while line := await process.stdout.readline():
                    event = json.loads(line)
                    if event.get("event") == "tool":
                        ident = event.get("id")
                        node = expected.get(ident)
                        if (
                            not node
                            or ident in received
                            or event.get("name") != node["name"]
                            or event.get("arguments") != node["arguments"]
                        ):
                            raise RuntimeError("DAF host returned an unexpected tool request")
                        received.add(ident)
                        self._record({"event": "tool", "id": ident, "name": node["name"]})
                        pending.add(asyncio.create_task(handle(event)))
                    elif event.get("event") in {"wire", "complete"}:
                        self._record(event)
                        if event["event"] == "complete":
                            complete = event
                            break
                if pending:
                    await asyncio.gather(*pending)
                process.stdin.close()
                status = await process.wait()
                if status or complete is None:
                    raise RuntimeError("DAF engine exited without a verified completion")
                for node in nodes:
                    results.setdefault(node["id"], DEPENDENCY_SKIPPED)
            return results
        finally:
            for task in pending:
                if not task.done():
                    task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            stderr.cancel()
            await asyncio.gather(stderr, return_exceptions=True)
            # Metadata only: no arguments, tool content, tokens or credentials.
            try:
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(CONFIG_DIR / "workflows.db") as db:
                    db.execute(
                        "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, events TEXT"
                        " NOT NULL)"
                    )
                    db.execute(
                        "INSERT INTO runs VALUES (?, ?)",
                        (self.last_run, json.dumps(self.last_events)),
                    )
            except (OSError, sqlite3.Error):
                import logging

                logging.getLogger(__name__).warning("Workflow metadata could not be saved")

    async def one(self, name, arguments, dispatch):
        result = await self.execute(
            [{"id": "tool", "name": name, "arguments": arguments}], dispatch
        )
        # Hand back the ToolOutcome, not its string form -- this is the whole
        # point of W3-3. Falls back to the string only if a caller supplied a
        # dispatch that does not produce outcomes (tests do this).
        if "tool" in self._outcomes:
            return self._outcomes["tool"]
        return result["tool"]
