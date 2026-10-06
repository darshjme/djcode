# Swarm admission and cancellation

## Done
ShadowOrchestrator.execute_parallel validates 1–32 unique roles and resolves all specs before events/provider dispatch. Duplicate admission regression proves no provider contact.

## Found
ParallelCoordinator already cancels and joins child tasks on exit; existing cancellation integration regressions passed. Shadow API lacked duplicate-role admission.

## Decisions
Add bounded admission to actual public parallel path; preserve orchestration architecture.

## Open questions and limits
32 is admission count, not newly enforced concurrency. Other strategy paths retain current behavior; no claim of durable swarm execution or unified global admission.

## Evidence and actual validation
- Focused combined `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_audit_execution_boundaries.py tests/test_execution.py tests/test_runtime_protocol.py`: 59 passed. Three invalid-index cases added afterward: complete new audit test file 11 passed.
- Fatal Ruff E9/F63/F7/F82 on all changed source/test files passed; git diff --check passed.
- First experimental run: 62 passed, 1 failed because early argument parsing broke existing model recovery. Removed that experiment and final existing malformed-call regression passed. No live provider calls or external services.
- Changed source: tool_router.py, streaming.py, orchestrator/engine.py. New regression file: tests/test_audit_execution_boundaries.py.

## Next
Root performs combined suite and integrates documentation; no commits or Actions run by specialist.
