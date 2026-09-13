"""Cancellable foreground operations for the line-oriented terminal."""

from __future__ import annotations

import asyncio
import signal


async def run_interruptible(operation):
    """Cancel just this operation on Ctrl+C and restore the caller's signal handler.

    External task cancellation still propagates. On loops without Unix signal
    handlers, the caller's normal interrupt handling remains in effect.
    """
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(operation)
    interrupted = False
    installed = False
    previous_handler = signal.getsignal(signal.SIGINT)

    def cancel() -> None:
        nonlocal interrupted
        if not task.done():
            interrupted = True
            task.cancel()

    try:
        try:
            loop.add_signal_handler(signal.SIGINT, cancel)
            installed = True
        except (NotImplementedError, RuntimeError, ValueError):
            pass
        try:
            return await task
        except asyncio.CancelledError:
            if not interrupted:
                raise
            return None
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGINT)
            signal.signal(signal.SIGINT, previous_handler)
