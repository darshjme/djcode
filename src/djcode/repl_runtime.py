"""Cancellable foreground operations for the line-oriented terminal."""

from __future__ import annotations

import asyncio
import contextlib
import signal


async def run_interruptible(operation):
    """Cancel just this operation on Ctrl+C and restore the caller's signal handler.

    External task cancellation still propagates.

    Two installation paths, because they are not interchangeable:

    * ``loop.add_signal_handler`` is the correct primitive and is what POSIX
      event loops provide.
    * Windows' ``ProactorEventLoop`` raises ``NotImplementedError`` from it.
      Plain ``signal.signal`` *does* work there, and the proactor loop installs
      a wakeup fd in its constructor, so a SIGINT wakes the loop and the Python
      handler runs promptly on the loop's own thread. Without this fallback the
      helper silently installed nothing on Windows, ``interrupted`` never became
      True, and the ``KeyboardInterrupt`` escaped ``asyncio.run`` and quit the
      whole program instead of cancelling one turn.
    """
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(operation)
    interrupted = False
    installed = False
    fallback_installed = False
    previous_handler = signal.getsignal(signal.SIGINT)

    def cancel() -> None:
        nonlocal interrupted
        if not task.done():
            interrupted = True
            task.cancel()

    def _on_sigint(_signum, _frame) -> None:
        # Runs on the main thread between bytecodes; hop onto the loop so the
        # cancellation is ordered with everything else the loop is doing.
        loop.call_soon_threadsafe(cancel)

    try:
        try:
            loop.add_signal_handler(signal.SIGINT, cancel)
            installed = True
        except (NotImplementedError, RuntimeError, ValueError):
            try:
                signal.signal(signal.SIGINT, _on_sigint)
                fallback_installed = True
            except (ValueError, OSError):
                # Not the main thread, or no signal support at all.
                pass
        try:
            return await task
        except asyncio.CancelledError:
            if not interrupted:
                raise
            return None
        except KeyboardInterrupt:
            # Neither handler could be installed, or the signal landed in the
            # window before one took effect. Still cancel the operation rather
            # than let the interrupt tear down the caller's event loop.
            cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            return None
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGINT)
            signal.signal(signal.SIGINT, previous_handler)
        elif fallback_installed:
            signal.signal(signal.SIGINT, previous_handler)
