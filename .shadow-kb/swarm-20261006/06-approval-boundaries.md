# Approval boundaries

## Done
Unrecognized extraction approval text now denies execution. Existing cancellation/None denies. Actual native Operator and specialist write approval remains propagated through agent_context.

## Found
Router previously approved arbitrary text by default. Existing operator unattended and specialist read-only gates reviewed; no new permission-manager API semantics introduced.

## Decisions
Explicit y/yes and existing questionary default remain supported; unknown input fails closed.

## Open questions and limits
PermissionManager unknown-tool allow remains a UI/helper concern; actual executor has explicit tool allowlist and default write gate. No blanket unknown-tool grant proof claimed.

## Evidence and actual validation
- Focused combined `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_audit_execution_boundaries.py tests/test_execution.py tests/test_runtime_protocol.py`: 59 passed. Three invalid-index cases added afterward: complete new audit test file 11 passed.
- Fatal Ruff E9/F63/F7/F82 on all changed source/test files passed; git diff --check passed.
- First experimental run: 62 passed, 1 failed because early argument parsing broke existing model recovery. Removed that experiment and final existing malformed-call regression passed. No live provider calls or external services.
- Changed source: tool_router.py, streaming.py, orchestrator/engine.py. New regression file: tests/test_audit_execution_boundaries.py.

## Next
Root performs combined suite and integrates documentation; no commits or Actions run by specialist.
