"""Default DAF scheduling and DDAL transport for DJcode tool execution.

The Rust host never performs a tool's side effects. It schedules a graph and
transports requests to the owning Python session, where approval and dispatch
remain enforced. There is no automatic retry of side-effecting tools.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path

from djcode.config import CONFIG_DIR, load_config

SOURCE = Path(__file__).with_name("daf_engine")
MAX_BATCH_BYTES = 2**20
MAX_REPLY_BYTES = 2**20
MAX_EVENT_BYTES = 2**22
MAX_FRAME_BYTES = 16 * 2**20 + 21


def _json_bytes(value, limit):
    try:
        encoded = json.dumps(value, allow_nan=False, sort_keys=True).encode()
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise ValueError("workflow data must be finite JSON") from error
    if len(encoded) > limit:
        raise ValueError(f"workflow data exceeds {limit} bytes")
    return encoded


async def _stop_process(process):
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), 3)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


async def _drain_stderr(stream):
    # Drain the entire pipe so verbose diagnostics cannot deadlock the host.
    # Diagnostics are never persisted as workflow metadata.
    while await stream.read(65536):
        pass


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
        raise RuntimeError(
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
            await _stop_process(process)
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
            or not node["id"].strip()
            or len(node["id"]) > 128
            or node["id"] in ids
        ):
            raise ValueError("workflow nodes require unique nonempty IDs")
        if (
            not isinstance(node.get("name"), str)
            or not node["name"].strip()
            or len(node["name"]) > 128
            or not isinstance(node.get("arguments"), dict)
        ):
            raise ValueError("workflow nodes require a tool name and argument object")
        dependencies = node.get("dependencies", [])
        if (
            not isinstance(dependencies, list)
            or len(dependencies) > len(nodes)
            or any(not isinstance(dep, str) or not dep.strip() for dep in dependencies)
            or len(set(dependencies)) != len(dependencies)
        ):
            raise ValueError("dependencies must be a bounded list of unique nonempty IDs")
        ids.add(node["id"])
    done = set()
    for _ in nodes:
        for node in nodes:
            if all(dep in done for dep in node.get("dependencies", [])):
                done.add(node["id"])
    if done != ids:
        raise ValueError("workflow has a cycle or unknown dependency")
    _json_bytes(nodes, MAX_BATCH_BYTES)


class WorkflowEngine:
    def __init__(self, event_callback=None, *, mode=None):
        self.mode = mode or load_config().get("workflow_engine", "daf")
        self.event_callback = event_callback
        self.last_run = None
        self.last_events = []

    def _record(self, event):
        self.last_events.append(event)
        self._persist()
        if self.event_callback:
            self.event_callback(event)

    def _persist(self):
        """Checkpoint metadata before dispatch; interrupted tools are never replayed."""
        if self.last_run is None:
            return
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(CONFIG_DIR / "workflows.db") as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, events TEXT NOT NULL)"
                )
                db.execute(
                    "INSERT INTO runs VALUES (?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET events=excluded.events",
                    (self.last_run, json.dumps(self.last_events)),
                )
        except (OSError, sqlite3.Error):
            import logging

            logging.getLogger(__name__).warning("Workflow metadata could not be saved")

    async def execute(self, nodes, dispatch, concurrency=1):
        validate_nodes(nodes)
        if type(concurrency) is not int or not 1 <= concurrency <= 4:
            raise ValueError("concurrency must be 1..4")
        if self.mode not in {"native", "daf"}:
            raise ValueError(f"Unknown workflow engine: {self.mode}")
        if self.mode == "native" and any(node.get("dependencies") for node in nodes):
            raise ValueError("DAG dependencies require the DAF engine")
        # Snapshot the validated request before the first await. Caller changes
        # cannot alter the approved tool arguments while the engine is building.
        batch = _json_bytes({"nodes": nodes, "concurrency": concurrency}, MAX_BATCH_BYTES)
        nodes = json.loads(batch)["nodes"]
        self.last_run = uuid.uuid4().hex
        self.last_events = []
        if self.mode == "native":
            results = {}
            for node in nodes:
                results[node["id"]] = await dispatch(node["name"], node["arguments"])
            return results
        self._record({"event": "preparing", "engine": "DAF + DDAL"})
        try:
            binary = await engine_path()
            process = await asyncio.create_subprocess_exec(
                str(binary),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=MAX_EVENT_BYTES,
            )
        except asyncio.CancelledError:
            self._record({"event": "interrupted", "reason": "cancelled"})
            raise
        except Exception:
            self._record({"event": "failed", "reason": "engine_start_failure"})
            raise
        stderr = asyncio.create_task(_drain_stderr(process.stderr))
        results = {}
        pending = set()
        expected = {n["id"]: n for n in nodes}
        received = set()
        wired = set()
        succeeded = set()
        outcomes = {}
        slots = asyncio.Semaphore(concurrency)
        complete = None

        async def handle(event):
            ident = event["id"]
            async with slots:
                try:
                    result = str(await dispatch(event["name"], event["arguments"]))
                except Exception as error:
                    result = f"Error: {error}"
            results[ident] = result
            ok = not result.lower().startswith(
                ("error", "traceback", "[exit code", "command timed out")
            )
            try:
                reply = _json_bytes({"id": ident, "ok": ok, "result": result}, MAX_REPLY_BYTES)
            except ValueError:
                result = "Error: tool result exceeds the workflow reply limit or is invalid JSON"
                results[ident] = result
                ok = False
                reply = _json_bytes({"id": ident, "ok": False, "result": result}, MAX_REPLY_BYTES)
            outcomes[ident] = ok
            if ok:
                succeeded.add(ident)
            process.stdin.write(reply + b"\n")
            await process.stdin.drain()

        try:
            process.stdin.write(batch + b"\n")
            await process.stdin.drain()
            async with asyncio.timeout(3610):
                while line := await process.stdout.readline():
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise RuntimeError("DAF host returned an invalid event")
                    if event.get("event") == "tool":
                        ident = event.get("id")
                        node = expected.get(ident) if isinstance(ident, str) else None
                        if (
                            not node
                            or ident in received
                            or event.get("name") != node["name"]
                            or _json_bytes(event.get("arguments"), MAX_BATCH_BYTES)
                            != _json_bytes(node["arguments"], MAX_BATCH_BYTES)
                            or not all(dep in succeeded for dep in node.get("dependencies", []))
                        ):
                            raise RuntimeError("DAF host returned an unexpected tool request")
                        received.add(ident)
                        self._record({"event": "tool", "id": ident, "name": node["name"]})
                        pending.add(asyncio.create_task(handle(event)))
                    elif event.get("event") == "wire":
                        if (
                            not isinstance(event.get("id"), str)
                            or event["id"] not in outcomes
                            or event["id"] in wired
                            or type(event.get("ok")) is not bool
                            or event["ok"] is not outcomes[event["id"]]
                            or any(
                                type(event.get(key)) is not int
                                or not 21 <= event[key] <= MAX_FRAME_BYTES
                                for key in ("sent_bytes", "received_bytes")
                            )
                        ):
                            raise RuntimeError("DAF host returned invalid wire metadata")
                        wired.add(event["id"])
                        self._record(
                            {
                                key: event[key]
                                for key in ("event", "id", "sent_bytes", "received_bytes", "ok")
                            }
                        )
                    elif event.get("event") == "complete":
                        complete = event
                        break
                    else:
                        raise RuntimeError("DAF host returned an unknown event")
                if complete is None:
                    raise RuntimeError("DAF engine exited without a verified completion")
                states = complete.get("states")
                valid_states = {"Succeeded", "Failed", "Skipped", "Cancelled"}
                if (
                    type(complete.get("ok")) is not bool
                    or not isinstance(states, list)
                    or len(states) != len(expected)
                    or any(
                        not isinstance(state, dict)
                        or not isinstance(state.get("id"), str)
                        or state["id"] not in expected
                        or not isinstance(state.get("state"), str)
                        or state.get("state") not in valid_states
                        for state in states
                    )
                    or len({state["id"] for state in states}) != len(expected)
                    or complete["ok"] != all(state["state"] == "Succeeded" for state in states)
                    or any(
                        state["state"] == "Succeeded" and outcomes.get(state["id"]) is not True
                        for state in states
                    )
                    or any(
                        state["state"] != "Succeeded" and outcomes.get(state["id"]) is True
                        for state in states
                    )
                    or not received.issubset(outcomes)
                    or received != wired
                ):
                    raise RuntimeError("DAF host returned an unverified completion")
                if pending:
                    await asyncio.gather(*pending)
                process.stdin.close()
                status = await process.wait()
                if status:
                    raise RuntimeError("DAF engine exited without a verified completion")
                self._record(
                    {
                        "event": "complete",
                        "ok": complete["ok"],
                        "states": [
                            {"id": state["id"], "state": state["state"]} for state in states
                        ],
                    }
                )
                for node in nodes:
                    results.setdefault(
                        node["id"], "Error: dependency failed; tool was not executed"
                    )
            return results
        except asyncio.CancelledError:
            self._record({"event": "interrupted", "reason": "cancelled"})
            raise
        except Exception:
            self._record({"event": "failed", "reason": "host_or_dispatch_failure"})
            raise
        finally:
            for task in pending:
                if not task.done():
                    task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await _stop_process(process)
            stderr.cancel()
            await asyncio.gather(stderr, return_exceptions=True)
            # Metadata only: no arguments, tool content, tokens or credentials.
            self._persist()

    async def one(self, name, arguments, dispatch):
        result = await self.execute(
            [{"id": "tool", "name": name, "arguments": arguments}], dispatch
        )
        return result["tool"]
