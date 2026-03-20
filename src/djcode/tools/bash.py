"""Bash tool — execute shell commands locally."""

from __future__ import annotations

import asyncio


async def execute_bash(command: str, timeout: int = 120) -> str:
    """Execute a shell command and return combined stdout + stderr."""
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return f"Command timed out after {timeout}s"

        output_parts = []
        if stdout:
            output_parts.append(stdout.decode("utf-8", errors="replace"))
        if stderr:
            output_parts.append(stderr.decode("utf-8", errors="replace"))

        result = "\n".join(output_parts).strip()

        if proc.returncode != 0:
            result = f"[exit code {proc.returncode}]\n{result}"

        # Cap output to avoid blowing up context
        if len(result) > 50_000:
            result = result[:50_000] + "\n... (output truncated)"

        return result or "(no output)"

    except Exception as e:
        return f"Error: {e}"
