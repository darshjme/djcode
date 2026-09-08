"""Shell execution with bounded output and process-group cancellation."""
from __future__ import annotations

import asyncio
import os
import signal


async def run_process(*args: str, timeout: float, shell: bool = False, output_limit: int = 50_000) -> str:
    kwargs = dict(stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                  start_new_session=(os.name == "posix"))
    proc = await (asyncio.create_subprocess_shell(args[0], **kwargs) if shell
                  else asyncio.create_subprocess_exec(*args, **kwargs))
    chunks = bytearray()
    truncated = False

    async def read_output() -> None:
        nonlocal truncated
        while chunk := await proc.stdout.read(8192):
            room = max(0, output_limit - len(chunks))
            chunks.extend(chunk[:room])
            truncated |= len(chunk) > room
        await proc.wait()

    async def stop() -> None:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            elif proc.returncode is None:
                proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()

    try:
        await asyncio.wait_for(read_output(), timeout=timeout)
    except asyncio.TimeoutError:
        await stop()
        return f"Error: Command timed out after {timeout}s\n" + chunks.decode(errors="replace")
    except asyncio.CancelledError:
        await stop()
        raise
    result = chunks.decode(errors="replace").strip()
    if truncated:
        result += "\n... (output truncated)"
    if proc.returncode:
        result = f"[exit code {proc.returncode}]\n{result}"
    return result or "(no output)"


async def execute_bash(command: str, timeout: int = 120) -> str:
    if not isinstance(command, str) or not command.strip():
        return "Error: command is required"
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
        return "Error: timeout must be between 0 and 3600 seconds"
    try:
        return await run_process(command, shell=True, timeout=timeout)
    except Exception as exc:
        return f"Error: {exc}"
