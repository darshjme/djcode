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

logger = logging.getLogger(__name__)

SOURCE = Path(__file__).with_name("daf_engine")
_BUILD_LOCK = None

#: Set once the process has told the user that DAF is unavailable, so a session
#: that runs a hundred tools does not print the same paragraph a hundred times.
_FALLBACK_ANNOUNCED = False

_ERROR_PREFIXES = ("error", "traceback", "[exit code", "command timed out")

DEPENDENCY_SKIPPED = "Error: dependency failed; tool was not executed"


class DAFUnavailableError(RuntimeError):
    """No Rust toolchain, so the DAF engine cannot be built on this machine.

    A subclass of RuntimeError so that callers written against the old
    contract keep catching it. `WorkflowEngine.execute` treats it as the
    signal to fall back to the native scheduler (SSOT non-goal 12).
    """


def _result_ok(result) -> bool:
    """Mirror the DAF host's success heuristic for a dispatched tool result."""
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
            logger.warning(
                "No Rust toolchain found; running the native workflow engine instead of DAF. "
                "Tools still run one at a time with the same approvals. "
                "Install Rust/Cargo or set DJCODE_DAF_ENGINE to restore parallel scheduling."
            )

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
            results[ident] = await dispatch(node["name"], node["arguments"])
        return results

    async def execute(self, nodes, dispatch, concurrency=1):
        validate_nodes(nodes)
        if not isinstance(concurrency, int) or not 1 <= concurrency <= 4:
            raise ValueError("concurrency must be 1..4")
        self.last_run = uuid.uuid4().hex
        self.last_events = []
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
                result = str(await dispatch(event["name"], event["arguments"]))
            except Exception as error:
                result = f"Error: {error}"
            results[ident] = result
            ok = _result_ok(result)
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
        return result["tool"]
