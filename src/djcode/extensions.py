"""MCP Extension System for DJcode.

Load and manage external tool extensions via the Model Context Protocol.
Extensions are subprocess servers that expose tools over stdio JSON-RPC.

Zero new dependencies — stdlib subprocess + json only.
Config stored in ~/.djcode/extensions.json
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from djcode.config import CONFIG_DIR

logger = logging.getLogger(__name__)

EXTENSIONS_FILE = CONFIG_DIR / "extensions.json"

# MCP JSON-RPC protocol constants
MCP_JSONRPC_VERSION = "2.0"
MCP_INITIALIZE = "initialize"
MCP_TOOLS_LIST = "tools/list"
MCP_TOOLS_CALL = "tools/call"


@dataclass
class Extension:
    """A registered MCP extension."""

    name: str
    cmd: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    tools: list[str] = field(default_factory=list)
    description: str = ""
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Don't persist transient fields
        d.pop("last_error", None)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Extension:
        return cls(
            name=data.get("name", "unknown"),
            cmd=data.get("cmd", ""),
            args=data.get("args", []),
            env=data.get("env", {}),
            enabled=data.get("enabled", True),
            tools=data.get("tools", []),
            description=data.get("description", ""),
        )


class MCPConnection:
    """Bounded, asynchronous MCP stdio lifecycle with notification handling."""
    def __init__(self, extension):
        self.extension = extension
        self._process = None
        self._request_id = 0
        self._lock = asyncio.Lock()
        self._stderr_task = None
        self.image_paths = []

    async def start(self):
        import os
        try:
            self._process = await asyncio.create_subprocess_exec(
                self.extension.cmd, *self.extension.args,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **self.extension.env}, limit=2**22,
                start_new_session=os.name != "nt",
            )
            async def drain():
                while await self._process.stderr.read(4096):
                    pass
            self._stderr_task = asyncio.create_task(drain())
            await self._send_request("initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "djcode", "version": "4.2.1"},
            })
            await self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except BaseException:
            await self.stop()
            raise

    async def _write(self, value):
        self._process.stdin.write((json.dumps(value) + "\n").encode())
        await self._process.stdin.drain()

    async def stop(self):
        import os
        import signal
        process = self._process
        if process and process.returncode is None:
            try:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
                await asyncio.wait_for(process.wait(), 2)
            except ProcessLookupError:
                pass
            except TimeoutError:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                await process.wait()
        if self._stderr_task:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
        self._process = None

    @property
    def is_alive(self):
        return self._process is not None and self._process.returncode is None

    async def _send_request(self, method, params=None):
        if not self.is_alive:
            raise RuntimeError(f"Extension {self.extension.name} is not running")
        async with self._lock:
            self._request_id += 1
            ident = self._request_id
            await self._write({"jsonrpc":"2.0", "id":ident, "method":method, "params":params or {}})
            try:
                async with asyncio.timeout(30):
                    while True:
                        line = await self._process.stdout.readline()
                        if not line:
                            raise RuntimeError("MCP server closed stdout")
                        response = json.loads(line)
                        if response.get("method"):
                            if "id" in response:
                                await self._write({"jsonrpc":"2.0", "id":response["id"], "error":{"code":-32601,"message":"Client method not supported"}})
                            continue
                        if response.get("id") != ident:
                            continue
                        if "error" in response:
                            raise RuntimeError(f"MCP error: {response['error'].get('message', 'request failed')}")
                        return response.get("result")
            except BaseException:
                await self.stop()
                raise

    async def list_tools(self):
        result = await self._send_request("tools/list")
        return (result or {}).get("tools", [])

    async def call_tool(self, tool_name, arguments):
        import base64
        import uuid
        self.image_paths = []
        result = await self._send_request("tools/call", {"name":tool_name,"arguments":arguments})
        if isinstance(result, dict):
            parts = []
            for block in result.get("content", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "image" and block.get("mimeType") == "image/png":
                    encoded = block.get("data", "")
                    if len(encoded) > 12 * 1024 * 1024:
                        raise ValueError("MCP image exceeds size limit")
                    data = base64.b64decode(encoded, validate=True)
                    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                        raise ValueError("MCP image is not a PNG")
                    directory = CONFIG_DIR / "screenshots"
                    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                    path = directory / f"mcp-{uuid.uuid4().hex}.png"
                    path.write_bytes(data)
                    self.image_paths.append(str(path))
                    parts.append(f"Screenshot: {path}")
                else:
                    parts.append(block.get("text", "[non-text content]"))
            return ("Error: " if result.get("isError") else "") + "\n".join(parts)
        return str(result or "")


class ExtensionManager:
    """Load and manage MCP tool extensions.

    Extensions are external processes that expose tools via MCP protocol.
    Supports add/remove/enable/disable/list operations.
    Connections are lazy — only established when tools are actually needed.
    """

    def __init__(self) -> None:
        self.extensions: dict[str, Extension] = {}
        self._connections: dict[str, MCPConnection] = {}
        self._tools_cache: dict[str, list[dict]] = {}  # ext_name -> tools
        self._load_config()

    def _load_config(self) -> None:
        """Load extensions from config file."""
        if EXTENSIONS_FILE.exists():
            try:
                data = json.loads(EXTENSIONS_FILE.read_text())
                for ext_data in data.get("extensions", []):
                    ext = Extension.from_dict(ext_data)
                    self.extensions[ext.name] = ext
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load extensions config: %s", e)

    def _save_config(self) -> None:
        """Persist extensions to config file."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "extensions": [ext.to_dict() for ext in self.extensions.values()],
            "version": 1,
        }
        EXTENSIONS_FILE.write_text(json.dumps(data, indent=2))

    def add(
        self,
        name: str,
        cmd: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        description: str = "",
    ) -> Extension:
        """Register a new MCP extension.

        Example: manager.add("github", "mcp-server-github")
        Example: manager.add("postgres", "mcp-server-postgres", env={"DATABASE_URL": "..."})
        """
        if name in self.extensions:
            # Update existing
            ext = self.extensions[name]
            ext.cmd = cmd
            ext.args = args or []
            ext.env = env or {}
            if description:
                ext.description = description
        else:
            ext = Extension(
                name=name,
                cmd=cmd,
                args=args or [],
                env=env or {},
                description=description,
            )
            self.extensions[name] = ext

        self._save_config()
        return ext

    def remove(self, name: str) -> bool:
        """Remove an extension. Returns True if it existed."""
        if name in self.extensions:
            # Kill connection if active
            if name in self._connections:
                asyncio.ensure_future(self._connections[name].stop())
                del self._connections[name]
            del self.extensions[name]
            self._tools_cache.pop(name, None)
            self._save_config()
            return True
        return False

    def enable(self, name: str) -> bool:
        """Enable a disabled extension."""
        if name in self.extensions:
            self.extensions[name].enabled = True
            self._save_config()
            return True
        return False

    def disable(self, name: str) -> bool:
        """Disable an extension without removing it."""
        if name in self.extensions:
            self.extensions[name].enabled = False
            # Kill connection if active
            if name in self._connections:
                asyncio.ensure_future(self._connections[name].stop())
                del self._connections[name]
            self._save_config()
            return True
        return False

    def list_extensions(self) -> list[Extension]:
        """List all registered extensions."""
        return list(self.extensions.values())

    async def _ensure_connection(self, name: str) -> MCPConnection:
        """Ensure we have a live connection to an extension."""
        ext = self.extensions.get(name)
        if not ext:
            raise ValueError(f"Unknown extension: {name}")
        if not ext.enabled:
            raise ValueError(f"Extension '{name}' is disabled")

        conn = self._connections.get(name)
        if conn and conn.is_alive:
            return conn

        # Need to establish connection
        conn = MCPConnection(ext)
        await conn.start()
        self._connections[name] = conn

        # Refresh tools cache
        try:
            tools = await conn.list_tools()
            self._tools_cache[name] = tools
            ext.tools = [t.get("name", "") for t in tools]
            self._save_config()
        except Exception as e:
            logger.warning("Failed to list tools for %s: %s", name, e)
            self._tools_cache[name] = []

        return conn

    async def get_tools(self) -> list[dict[str, Any]]:
        """Aggregate tools from all enabled extensions.

        Returns tools in OpenAI function-calling format for injection
        into the LLM's tool list.
        """
        all_tools: list[dict[str, Any]] = []

        for name, ext in self.extensions.items():
            if not ext.enabled:
                continue

            try:
                conn = await self._ensure_connection(name)
                tools = self._tools_cache.get(name, [])

                for tool in tools:
                    # Convert MCP tool schema to OpenAI function format
                    func_tool = {
                        "type": "function",
                        "function": {
                            "name": f"ext_{name}_{tool.get('name', '')}",
                            "description": (
                                f"[{name}] {tool.get('description', 'No description')}"
                            ),
                            "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
                        },
                        "_extension": name,
                        "_original_name": tool.get("name", ""),
                    }
                    all_tools.append(func_tool)

            except Exception as e:
                ext.last_error = str(e)
                logger.debug("Skipping extension %s: %s", name, e)

        return all_tools

    async def call_tool(self, extension: str, tool: str, arguments: dict) -> str:
        """Call a tool on a specific extension.

        Args:
            extension: Extension name (e.g., "github")
            tool: Tool name (e.g., "create_issue")
            arguments: Tool arguments dict

        Returns:
            Tool result as string
        """
        try:
            conn = await self._ensure_connection(extension)
            return await conn.call_tool(tool, arguments)
        except Exception as e:
            return f"Error calling {extension}/{tool}: {e}"

    async def dispatch_extension_tool(self, full_name: str, arguments: dict) -> str:
        """Dispatch a tool call using the ext_{name}_{tool} naming convention.

        This is the integration point for the Operator's tool-calling loop.
        """
        if not full_name.startswith("ext_"):
            return f"Error: '{full_name}' is not an extension tool"

        # Parse: ext_{extension}_{tool}
        parts = full_name[4:].split("_", 1)
        if len(parts) < 2:
            return f"Error: Malformed extension tool name: {full_name}"

        ext_name, tool_name = parts[0], parts[1]
        return await self.call_tool(ext_name, tool_name, arguments)

    async def refresh_tools(self, name: str) -> list[dict]:
        """Force refresh tools list for an extension."""
        conn = await self._ensure_connection(name)
        tools = await conn.list_tools()
        self._tools_cache[name] = tools
        ext = self.extensions[name]
        ext.tools = [t.get("name", "") for t in tools]
        self._save_config()
        return tools

    async def shutdown(self) -> None:
        """Stop all extension connections. Call on REPL exit."""
        for name, conn in self._connections.items():
            try:
                await conn.stop()
            except Exception as e:
                logger.debug("Error stopping extension %s: %s", name, e)
        self._connections.clear()

    def get_status(self) -> list[dict[str, Any]]:
        """Get status of all extensions for display."""
        statuses = []
        for name, ext in self.extensions.items():
            conn = self._connections.get(name)
            statuses.append({
                "name": name,
                "cmd": ext.cmd,
                "enabled": ext.enabled,
                "connected": conn is not None and conn.is_alive if conn else False,
                "tools_count": len(ext.tools),
                "tools": ext.tools[:10],  # Cap for display
                "description": ext.description,
                "last_error": ext.last_error,
            })
        return statuses
