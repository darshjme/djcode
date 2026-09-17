"""Shell execution with a memory bound and process-group cancellation.

``output_limit`` is a MEMORY bound and nothing else. It stops ``yes`` or a
runaway build log from growing an unbounded ``bytearray`` in this process; it is
not the model-facing output policy and it no longer writes a sentence into the
result. W3-2 moved that policy to the one chokepoint
(``djcode/core/spill.py``), which keeps head+tail and spills the full text to a
file the model can read back -- something this function could never do, because
by the time it is truncating, the bytes it is dropping are already gone.

The old value was 50_000 bytes here and 30_000 in ``git.py``: two different
ceilings, both below what a real ``git diff`` or test run produces, both
silently unrecoverable. One bound, 2 MB, applied in one place.
"""

from __future__ import annotations

import asyncio
import os
import signal

from djcode.core.permissions import hardline_message, hardline_reason

#: 2 MB. Big enough that the chokepoint's spill file, not this cap, is what a
#: user hits in practice; small enough that N concurrent ``parallel_execute``
#: children cannot exhaust memory.
OUTPUT_LIMIT = 2_000_000


async def run_process(
    *args: str,
    timeout: float,
    shell: bool = False,
    output_limit: int = OUTPUT_LIMIT,
    cwd: str | None = None,
) -> str:
    kwargs = dict(
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=(os.name == "posix"),
        cwd=cwd,
    )
    if shell:
        # THE FLOOR, at the last possible moment before a shell exists (W6-2).
        # It is here rather than in `execute_bash` because `scheduler.run_once`
        # calls `execute_bash` directly and never touches `dispatch_tool` -- a
        # persisted job would otherwise run with no hook, no permission check
        # and no checkpoint. The message begins with "Error" so that
        # `workflow._result_ok`'s bare-string fallback, which is exactly what
        # the scheduler hands it, reads this as a failure and does not run
        # every dependent node.
        reason = hardline_reason(args[0])
        if reason is not None:
            return hardline_message(args[0], reason)
    proc = await (
        asyncio.create_subprocess_shell(args[0], **kwargs)
        if shell
        else asyncio.create_subprocess_exec(*args, **kwargs)
    )
    chunks = bytearray()

    async def read_output() -> None:
        # Keep draining after the bound is reached: the child blocks on a full
        # pipe otherwise and the timeout would fire on a command that finished.
        while chunk := await proc.stdout.read(8192):
            room = max(0, output_limit - len(chunks))
            chunks.extend(chunk[:room])
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
    except TimeoutError:
        await stop()
        return f"Error: Command timed out after {timeout}s\n" + chunks.decode(errors="replace")
    except asyncio.CancelledError:
        await stop()
        raise
    result = chunks.decode(errors="replace").strip()
    if proc.returncode:
        result = f"[exit code {proc.returncode}]\n{result}"
    return result or "(no output)"


async def execute_bash(command: str, timeout: int = 120, cwd: str | None = None) -> str:
    if not isinstance(command, str) or not command.strip():
        return "Error: command is required"
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
        return "Error: timeout must be between 0 and 3600 seconds"
    try:
        return await run_process(command, shell=True, timeout=timeout, cwd=cwd)
    except Exception as exc:
        return f"Error: {exc}"
