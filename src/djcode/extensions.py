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
    """Manages a single MCP subprocess connection.

    Speaks JSON-RPC 2.0 over stdin/stdout with the extension process.
    Lifecycle: spawn -> initialize -> tools/list -> tools/call* -> kill
    """

    def __init__(self, extension: Extension) -> None:
        self.extension = extension
        self._process: subprocess.Popen | None = None
        self._request_id = 0
        self._lock = asyncio.Lock()

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def start(self) -> None:
        """Spawn the extension subprocess."""
        try:
            import os

            env = {**os.environ, **self.extension.env}
            self._process = subprocess.Popen(
                [self.extension.cmd, *self.extension.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
                bufsize=0,
            )
            # Give it a moment to start
            await asyncio.sleep(0.1)

            if self._process.poll() is not None:
                stderr = self._process.stderr.read() if self._process.stderr else ""
                raise RuntimeError(
                    f"Extension '{self.extension.name}' exited immediately: {stderr[:200]}"
                )

            # Send MCP initialize
            await self._send_request(MCP_INITIALIZE, {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "djcode", "version": "2.0.1"},
            })

        except FileNotFoundError:
            self.extension.last_error = f"Command not found: {self.extension.cmd}"
            raise RuntimeError(self.extension.last_error)
        except Exception as e:
            self.extension.last_error = str(e)
            raise

    async def stop(self) -> None:
        """Terminate the extension subprocess."""
        if self._process and self._process.poll() is None:
            try:
                self._process.terminate()
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
            except Exception:
                pass
        self._process = None

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    async def _send_request(self, method: str, params: dict | None = None) -> Any:
        """Send a JSON-RPC request and read the response."""
        if not self._process or not self._process.stdin or not self._process.stdout:
            raise RuntimeError(f"Extension '{self.extension.name}' is not running")

        async with self._lock:
            request_id = self._next_id()
            request = {
                "jsonrpc": MCP_JSONRPC_VERSION,
                "method": method,
                "id": request_id,
            }
            if params is not None:
                request["params"] = params

            request_line = json.dumps(request) + "\n"

            loop = asyncio.get_event_loop()

            # Write request in executor to avoid blocking
            def _write():
                try:
                    self._process.stdin.write(request_line)
                    self._process.stdin.flush()
                except BrokenPipeError:
                    raise RuntimeError(
                        f"Extension '{self.extension.name}' pipe broken — process likely crashed"
                    )

            await loop.run_in_executor(None, _write)

            # Read response line in executor with timeout
            def _read() -> str:
                line = self._process.stdout.readline()
                if not line:
                    stderr_out = ""
                    if self._process.stderr:
                        try:
                            stderr_out = self._process.stderr.read(500)
                        except Exception:
                            pass
                    raise RuntimeError(
                        f"Extension '{self.extension.name}' returned empty response. "
                        f"stderr: {stderr_out[:200]}"
                    )
                return line.strip()

            try:
                raw_response = await asyncio.wait_for(
                    loop.run_in_executor(None, _read),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"Extension '{self.extension.name}' timed out (30s) on {method}"
                )

            try:
                response = json.loads(raw_response)
            except json.JSONDecodeError:
                raise RuntimeError(
                    f"Extension '{self.extension.name}' returned invalid JSON: "
                    f"{raw_response[:200]}"
                )

            if "error" in response:
                err = response["error"]
                code = err.get("code", -1)
                msg = err.get("message", "Unknown error")
                raise RuntimeError(
                    f"Extension '{self.extension.name}' RPC error ({code}): {msg}"
                )

            return response.get("result")

    async def list_tools(self) -> list[dict[str, Any]]:
        """Fetch available tools from the extension."""
        result = await self._send_request(MCP_TOOLS_LIST)
        if result and "tools" in result:
            return result["tools"]
        return []

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Call a tool on the extension and return the text result."""
        result = await self._send_request(MCP_TOOLS_CALL, {
            "name": tool_name,
            "arguments": arguments,
        })

        if result is None:
            return ""

        # MCP tools return content as a list of content blocks
        if isinstance(result, dict) and "content" in result:
            parts = []
            for block in result["content"]:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif block.get("type") == "image":
                        parts.append(f"[image: {block.get('mimeType', 'unknown')}]")
                    else:
                        parts.append(json.dumps(block))
                else:
                    parts.append(str(block))
            return "\n".join(parts)

        if isinstance(result, str):
            return result

        return json.dumps(result, indent=2)


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
