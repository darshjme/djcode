# Stream/toolcall robustness

## Done
Shared adapter rejects tool-bearing data after completion; provider finally cleanup regression passes. Validates nonnegative integer indexes (reject boolean/string); final calls emitted sorted by index.

## Found
After-completion tool deltas could previously append calls. Existing malformed JSON recovery safely denies dispatch and feeds tool error back to model.

## Decisions
Preserve downstream argument validation and recovery rather than aborting every malformed JSON response. Existing completion/truncation/cancel tests pass.

## Open questions and limits
No new arbitrary stream byte/call-count quota introduced. Live provider interoperability not exercised; fixtures exercise real shared adapter and HTTP serializer.

## Evidence and actual validation
- Focused combined `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_audit_execution_boundaries.py tests/test_execution.py tests/test_runtime_protocol.py`: 59 passed. Three invalid-index cases added afterward: complete new audit test file 11 passed.
- Fatal Ruff E9/F63/F7/F82 on all changed source/test files passed; git diff --check passed.
- First experimental run: 62 passed, 1 failed because early argument parsing broke existing model recovery. Removed that experiment and final existing malformed-call regression passed. No live provider calls or external services.
- Changed source: tool_router.py, streaming.py, orchestrator/engine.py. New regression file: tests/test_audit_execution_boundaries.py.

## Next
Root performs combined suite and integrates documentation; no commits or Actions run by specialist.
