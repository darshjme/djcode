# Coding tool correctness

## Done
Extraction file_edit accepts empty replacement; actual temporary file deletion edit passes. mkdir dispatch failure propagates, proven with existing file blocking directory creation. Successful filename containing Error no longer misclassified.

## Found
Truthiness rejected valid delete edits; mkdir always reported success. Substring Error classification confused legitimate paths.

## Decisions
Preserve string-return dispatcher contract; classify known failure prefixes.

## Open questions and limits
String-result classification cannot distinguish arbitrary shell output intentionally beginning with error prefixes. Typed tool result conversion deferred.

## Evidence and actual validation
- Focused combined `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_audit_execution_boundaries.py tests/test_execution.py tests/test_runtime_protocol.py`: 59 passed. Three invalid-index cases added afterward: complete new audit test file 11 passed.
- Fatal Ruff E9/F63/F7/F82 on all changed source/test files passed; git diff --check passed.
- First experimental run: 62 passed, 1 failed because early argument parsing broke existing model recovery. Removed that experiment and final existing malformed-call regression passed. No live provider calls or external services.
- Changed source: tool_router.py, streaming.py, orchestrator/engine.py. New regression file: tests/test_audit_execution_boundaries.py.

## Next
Root performs combined suite and integrates documentation; no commits or Actions run by specialist.
