# 11 · Session recovery and identity

## Done
- Resume accepts the unique short IDs displayed by history, rejects ambiguous prefixes, and explicitly rejects changes while a task is running or no provider is connected.
- Resume checkpoints and closes the previous session, reopens the target, updates both app/operator session IDs and checkpoint callbacks, restores tool protocol metadata, and synchronizes the context manager.
- Restored user/assistant history appears in the conversation pane with literal text; the status identifies historical and current models separately. Current workspace system rules are preserved.
- New/fork session commands synchronize context and checkpoints; a new session clears prior pane/session memory. New clears injected context, whereas fork retains the current conversation.
- Closing a resumed session retains its prior summary unless an explicit replacement is provided.
- Context manager and checkpoint callbacks are optional for supported lightweight callers; full Operators still synchronize both. Compact also permits an absent checkpoint callback.

## Found
- History printed eight-character IDs but resume required full IDs.
- Resume only changed operator messages: checkpoint writes and exit save still targeted the previous record, the context manager remained stale, and the pane did not show restored history.
- Parent combined suite caught a lightweight `SimpleNamespace` caller without the optional context manager/checkpoint attributes; guarded synchronization repairs that regression.

## Evidence and tests
- `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_session_ui_recovery.py tests/test_memory_recovery.py tests/test_input_flow.py`: **51 passed**, including the final summary-preservation assertion.
- Final targeted aggregate across terminal, recovery, Studio and narrow acceptance: **86 passed**. Fatal Ruff (`E9,F63,F7,F82`) passed for every owned source module and new test file.
- After the parent combined-suite compatibility finding, `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_capabilities.py::test_session_fork_preserves_source_and_new_clears tests/test_session_ui_recovery.py tests/test_memory_recovery.py tests/test_input_flow.py`: **52 passed in 4.75 seconds**. Fatal Ruff for the repaired session command module passed. Parent reruns the combined suite after this final repair.
- New tests exercise real temporary SQLite files, unique/ambiguous/literal prefixes, summary retention, fork/new durable separation, actual headless TUI resume, tool-call correlation, displayed history, and subsequent checkpoint routing.

## Decisions
- Continue the selected session ID rather than copying into an unrelated current record.
- Keep the connected provider/model unchanged and explicitly show the distinction from the historical model.

## Open questions and residuals
- No model inference is needed for persistence acceptance; no live provider was called.
- SQLite schema initialization's broad error handling is existing behavior, outside this focused UI recovery correction.

## Next
- Root runs the combined suite; documentation can mention unique short IDs and current-model continuation.
