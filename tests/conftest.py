"""Keep regression tests out of the developer's real DJcode history/configuration."""
import os
import shutil
import tempfile
from pathlib import Path

import pytest

_test_config = tempfile.TemporaryDirectory(prefix="djcode-tests-")
os.environ["DJCODE_CONFIG_DIR"] = _test_config.name
os.environ["DJCODE_NO_UPDATE_CHECK"] = "1"

os.environ["DJCODE_SKIP_STARTUP_CHECK"] = "1"

# Stable on purpose. workflow.engine_path() keys its target directory on a
# sha256 of the daf_engine sources, so a stale binary can never be picked up,
# and cargo's own target-directory lock serialises concurrent sessions.
DAF_ENGINE_CACHE = Path(tempfile.gettempdir()) / "djcode-daf-test-engine"


@pytest.fixture(scope="session")
def daf_runtime():
    """Build the Rust DAF engine once per session; every DAF test shares it.

    workflow.engine_path() builds into CONFIG_DIR/runtime/daf/<source digest>.
    Each DAF test used to point CONFIG_DIR at its own tmp_path, so each one
    paid a full `cargo build --release`. The two tests that wait five seconds
    for their handler to start could never win that race, and a full run
    serialised several multi-minute builds behind cargo's target lock.

    Returns the directory the tests should use as workflow.CONFIG_DIR.
    """
    if not os.environ.get("DJCODE_DAF_ENGINE") and shutil.which("cargo") is None:
        pytest.skip("DAF engine needs a Rust toolchain; cargo is not on PATH")
    import asyncio

    import djcode.workflow as workflow

    DAF_ENGINE_CACHE.mkdir(parents=True, exist_ok=True)
    original = workflow.CONFIG_DIR
    workflow.CONFIG_DIR = DAF_ENGINE_CACHE
    try:
        asyncio.run(workflow.engine_path())
    finally:
        workflow.CONFIG_DIR = original
    return DAF_ENGINE_CACHE


@pytest.fixture
def settle():
    """Wait for a Textual condition to hold across refresh cycles.

    Pushing a screen mounts it, lays it out and moves focus over several
    message-pump cycles; a single `await pilot.pause()` drains one of them. On
    a loaded machine that is a coin flip -- app.focused is still None, or a
    button's region is still NULL_REGION and pilot.click raises OutOfBounds.
    Observed here as a 2-in-5 flake in test_compact_tui and test_execution.

    This is a harness race, not a product defect: a real user cannot click a
    screen that has not rendered yet, and ToolApprovalScreen.on_mount does
    focus its deny button. So the tests wait for the frame instead of
    assuming it.
    """

    async def wait(pilot, predicate, *, what, tries=200, interval=0.01):
        # A predicate that queries a widget which has not been composed yet
        # raises NoMatches; that is simply "not settled", not a test failure.
        last = None
        for _ in range(tries):
            try:
                if predicate():
                    return
            except Exception as error:
                last = error
            await pilot.pause(interval)
        detail = f" (last error: {last!r})" if last is not None else ""
        raise AssertionError(f"Textual never settled: {what}{detail}")

    return wait
