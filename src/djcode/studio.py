"""Project-scoped teams, dependency workflows and an evidence-based timeline."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import subprocess
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from djcode import __version__
from djcode.config import CONFIG_DIR

COMMANDS = {"/agent", "/organisation", "/flow", "/roadmap", "/timeline", "/doctor", "/finish"}


class Studio:
    def __init__(self, workspace=None, path=None):
        self.workspace = Path(workspace or Path.cwd()).resolve()
        key = hashlib.sha256(str(self.workspace).encode()).hexdigest()[:24]
        self.path = Path(path or CONFIG_DIR / "projects" / key / "studio.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS definitions "
                "(kind TEXT, name TEXT, body TEXT, PRIMARY KEY(kind,name))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS events "
                "(id INTEGER PRIMARY KEY, at TEXT, kind TEXT, name TEXT, status TEXT, "
                "detail TEXT, version TEXT)"
            )

    def connect(self):
        return sqlite3.connect(self.path)

    def event(self, kind, name, status, detail=""):
        with self.connect() as db:
            db.execute(
                "INSERT INTO events(at,kind,name,status,detail,version) VALUES(?,?,?,?,?,?)",
                (datetime.now(UTC).isoformat(), kind, name, status, detail[:2000], __version__),
            )

    def get(self, kind, name):
        if not isinstance(name, str):
            raise ValueError("Definition reference must be a name")
        with self.connect() as db:
            row = db.execute(
                "SELECT body FROM definitions WHERE kind=? AND name=?", (kind, name)
            ).fetchone()
        if not row:
            raise ValueError(f"Unknown {kind}: {name}")
        return json.loads(row[0])

    def list(self, kind):
        with self.connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT body FROM definitions WHERE kind=? ORDER BY name", (kind,)
                )
            ]

    def save(self, kind, data):
        if not isinstance(data, dict) or not re.fullmatch(
            r"[a-z][a-z0-9_-]{0,47}", str(data.get("name", ""))
        ):
            raise ValueError("Definition name must be 1..48 lowercase letters, digits, _ or -")
        if kind == "agent":
            from djcode.agents.registry import AGENT_SPECS, AgentRole

            base = AGENT_SPECS[AgentRole(data.get("role", "coder"))]
            if not isinstance(data.get("prompt"), str) or not data["prompt"].strip():
                raise ValueError("Agent requires a nonempty prompt")
            allowed = data.get("tools", sorted(base.tools_allowed))
            if not isinstance(allowed, list) or any(
                not isinstance(t, str) or t not in base.tools_allowed for t in allowed
            ):
                raise ValueError("Agent tools must be a subset of its base role's tools")
            data = {**data, "role": base.role.value, "tools": allowed}
        elif kind == "organisation":
            members = data.get("agents")
            if (
                not isinstance(members, list)
                or not members
                or any(not isinstance(m, str) for m in members)
                or len(set(members)) != len(members)
            ):
                raise ValueError("Organisation requires distinct agent names")
            for member in members:
                self.get("agent", member)
        elif kind == "flow":
            from djcode.workflow import validate_nodes

            nodes = data.get("nodes")
            if not isinstance(nodes, list):
                raise ValueError("Flow requires nodes")
            normal = []
            members = None
            if data.get("organisation"):
                members = self.get("organisation", data["organisation"])["agents"]
            for node in nodes:
                if (
                    not isinstance(node, dict)
                    or not isinstance(node.get("task"), str)
                    or not node["task"].strip()
                ):
                    raise ValueError("Each node requires a task")
                self.get("agent", node.get("agent"))
                if members is not None and node["agent"] not in members:
                    raise ValueError("Node agent is not in this organisation")
                normal.append(
                    {
                        "id": node.get("id"),
                        "name": "agent",
                        "arguments": {},
                        "dependencies": node.get("dependencies", []),
                    }
                )
            validate_nodes(normal)
            concurrency = data.get("concurrency", 1)
            if type(concurrency) is not int or not 1 <= concurrency <= 4:
                raise ValueError("Concurrency must be 1..4")
        elif kind == "roadmap":
            from djcode.milestones import validate_milestone_status

            validate_milestone_status(data.get("status", "planned"))
            if not isinstance(data.get("description"), str):
                raise ValueError("Milestone requires a description")
            data.setdefault("status", "planned")
        else:
            raise ValueError("Unknown definition kind")
        with self.connect() as db:
            db.execute(
                "INSERT INTO definitions VALUES(?,?,?) ON CONFLICT(kind,name) "
                "DO UPDATE SET body=excluded.body",
                (kind, data["name"], json.dumps(data)),
            )
        self.event(
            kind,
            data["name"],
            "saved",
            "User-defined milestone status: " + data["status"] if kind == "roadmap" else "",
        )
        return data

    def timeline(self):
        with self.connect() as db:
            rows = db.execute(
                "SELECT at,kind,name,status,detail,version FROM events ORDER BY id DESC LIMIT 100"
            ).fetchall()
        history = []
        try:
            result = subprocess.run(
                ["git", "log", "-30", "--format=%aI %h %s"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode == 0:
                history = result.stdout.splitlines()
        except (OSError, subprocess.TimeoutExpired):
            pass
        return {
            "workspace": str(self.workspace),
            "git_history": history,
            "events": [
                dict(
                    zip(
                        ("at", "kind", "name", "status", "detail", "djcode_version"),
                        row,
                        strict=True,
                    )
                )
                for row in rows
            ],
            "roadmap": self.list("roadmap"),
            "provenance": "Git history is source history; events record this DJcode runtime. "
            "Milestones are user reports, not verified completion.",
        }

    async def run(self, name, operator):
        if operator is None:
            raise ValueError("Connect a provider before running a workflow")
        if operator.plan_mode:
            raise ValueError("Plan mode blocks workflow execution")
        from djcode.agents.executor import AgentExecutor
        from djcode.agents.registry import AGENT_SPECS, AgentRole
        from djcode.orchestrator.context_bus import ContextBus
        from djcode.workflow import WorkflowEngine

        flow = self.get("flow", name)
        # Validate again: organisation membership may have changed since saving.
        self.save("flow", flow)
        definitions = {n["agent"]: self.get("agent", n["agent"]) for n in flow["nodes"]}
        bus = ContextBus()
        run_id = uuid.uuid4().hex
        nodes = [
            {
                "id": n["id"],
                "name": "agent",
                "arguments": n,
                "dependencies": n.get("dependencies", []),
            }
            for n in flow["nodes"]
        ]
        self.event("flow", name, "running", run_id)

        async def dispatch(_, node):
            definition = definitions[node["agent"]]
            base = AGENT_SPECS[AgentRole(definition["role"])]
            spec = replace(
                base,
                name=definition["name"],
                system_prompt=definition["prompt"],
                tools_allowed=frozenset(definition["tools"]),
            )
            self.event("node", node["id"], "running", run_id)
            executor = AgentExecutor(
                spec,
                operator.provider,
                bus,
                enable_ra=False,
                auto_accept=False,
                approval_callback=operator._approve_tool,
            )
            try:
                result = await executor.execute(node["task"])
            except asyncio.CancelledError:
                self.event("node", node["id"], "interrupted", run_id)
                raise
            except Exception:
                self.event("node", node["id"], "failed", run_id)
                raise
            self.event("node", node["id"], "passed" if result.succeeded else "failed", run_id)
            return result.response if result.succeeded else "Error: specialist execution failed"

        try:
            results = await WorkflowEngine(mode="daf").execute(
                nodes, dispatch, concurrency=flow.get("concurrency", 1)
            )
            ok = all(not value.lower().startswith("error") for value in results.values())
            self.event("flow", name, "passed" if ok else "failed", run_id)
            return {"run_id": run_id, "ok": ok, "results": results}
        except BaseException:
            self.event("flow", name, "interrupted", run_id)
            raise


def doctor(deep=False):
    """Local diagnostics only; never updates the install or calls model services."""
    import shutil

    from djcode.maintenance import run_checks
    from djcode.managed_update import installation

    checks = run_checks()
    checks["runtime"] = {
        "djcode": __version__,
        "cargo": bool(shutil.which("cargo")),
        "managed_install": installation() is not None,
        "update": "Use djcode --update for verified canonical updates; --rollback restores "
        "the previous managed release. Source checkouts are not overwritten.",
    }

    if deep:
        import tempfile

        from djcode.sessions import SessionDB
        from djcode.workflow import WorkflowEngine

        try:
            with tempfile.TemporaryDirectory(prefix="djcode-doctor-") as directory:
                path = Path(directory) / "sessions.db"
                db = SessionDB(path)
                sid = db.create_session("doctor", "offline")
                db.save_message(sid, "user", "memory roundtrip")
                restored = SessionDB(path).load_conversation(sid)
                if not restored or restored[0]["content"] != "memory roundtrip":
                    raise RuntimeError("Session memory did not survive reopening")
            checks["checks"].append({"name": "Memory restart", "status": "passed"})
        except Exception:
            checks["checks"].append({"name": "Memory restart", "status": "failed"})

        async def probe():
            engine = WorkflowEngine(mode="daf")

            async def echo(name, args):
                return "doctor-ok"

            response = await engine.one("doctor_echo", {}, echo)
            if response != "doctor-ok":
                raise RuntimeError("DAF echo returned an unexpected response")
            if not any(event.get("event") == "wire" for event in engine.last_events):
                raise RuntimeError("DAF completed without a DDAL wire event")

        try:
            asyncio.run(probe())
            checks["checks"].append({"name": "DAF + DDAL roundtrip", "status": "passed"})
        except Exception:
            checks["checks"].append(
                {
                    "name": "DAF + DDAL roundtrip",
                    "status": "failed",
                    "detail": "Check Cargo or DJCODE_DAF_ENGINE configuration",
                }
            )
        checks["ok"] = all(c["status"] == "passed" for c in checks["checks"])
        checks["summary"] = "Doctor passed" if checks["ok"] else "Doctor found failing checks"
    return checks


async def handle(operator, command, argument=""):
    store = Studio()
    if command == "/timeline":
        return json.dumps(await asyncio.to_thread(store.timeline), indent=2)
    if command == "/doctor":
        report = await asyncio.to_thread(doctor, True)
        store.event("doctor", "installation", "passed" if report["ok"] else "failed")
        return json.dumps(report, indent=2)
    if command == "/finish":
        if operator is None or operator.plan_mode:
            raise ValueError("Connect a provider and leave Plan mode to run completion checks")
        data = json.loads(argument) if argument else {}
        checks = data.get("checks")
        if (
            not isinstance(checks, list)
            or not checks
            or any(not isinstance(c, str) or not c.strip() for c in checks)
        ):
            raise ValueError('Use /finish {"checks":["lint command","test command"]}')
        from djcode.tools.bash import execute_bash
        from djcode.workflow import WorkflowEngine

        report = await asyncio.to_thread(doctor, True)
        outcomes = []
        engine = WorkflowEngine(mode="daf")

        async def dispatch(name, args):
            return await execute_bash(**args)

        for command_text in checks:
            args = {"command": command_text, "cwd": str(store.workspace)}
            if not await operator._approve_tool("bash", args):
                outcomes.append({"check": command_text, "passed": False, "result": "Denied"})
                continue
            result = await engine.one("bash", args, dispatch)
            ok = not result.lower().startswith(("error", "[exit code", "command timed out"))
            outcomes.append({"check": command_text, "passed": ok, "result": result})
        ok = report["ok"] and all(c["passed"] for c in outcomes)
        store.event(
            "project",
            store.workspace.name,
            "completed" if ok else "checks_failed",
            json.dumps(
                {
                    "doctor_passed": report["ok"],
                    "checks": [{"index": i, "passed": c["passed"]} for i, c in enumerate(outcomes)],
                    "scope": "Explicitly configured checks",
                }
            ),
        )
        return json.dumps({"ok": ok, "doctor": report, "checks": outcomes}, indent=2)
    kind = command[1:]
    action, _, payload = argument.partition(" ")
    if action in {"", "list"}:
        return json.dumps(store.list(kind), indent=2) + (
            f'\n/{kind} save {{"name":"example",...}} · /{kind} show NAME'
            + (" · /flow run NAME" if kind == "flow" else "")
        )
    if action == "show":
        return json.dumps(store.get(kind, payload.strip()), indent=2)
    if action == "save":
        if operator is not None and operator.plan_mode:
            raise ValueError("Leave Plan mode before changing project definitions")
        return json.dumps(store.save(kind, json.loads(payload)), indent=2)
    if kind == "flow" and action == "run":
        return json.dumps(await store.run(payload.strip(), operator), indent=2)
    raise ValueError("Use list, show NAME, save JSON, or /flow run NAME")
