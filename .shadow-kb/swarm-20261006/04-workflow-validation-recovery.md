# 04 — Workflow preflight and interruption metadata

## Done
- Native workflows reject the entire dependency-bearing batch before any dispatch. Previously a later dependent node could reject only after earlier nodes had executed.
- Validate dependency element types before hashing, unique bounded lists, nonempty bounded IDs/tool names, finite serializable JSON, a 1 MiB batch, and integer concurrency 1..4 (booleans rejected).
- Snapshot the approved batch before the first await so caller mutations during build/preparation cannot alter tool arguments.
- Match host requests to approved name/ID/canonical JSON arguments; booleans cannot substitute for integers through Python equality. Gate dependency requests on successful predecessors and enforce concurrency in Python.
- Checkpoint sanitized lifecycle/tool metadata before dispatch and on host/start/cancellation failures. Process shutdown terminates, waits three seconds, then kills and reaps if necessary; stderr is drained fully without retaining diagnostics.
- Bound replies to 1 MiB; oversized results report a bounded failure without retrying their already performed effects.

## Found
Invalid unhashable dependencies escaped as TypeError; native preflight was incremental; host stdout arguments were compared with loose Python equality; completion state was accepted without sufficient evidence. A single stderr read could leave the child blocked after verbose diagnostics. The prior metadata save happened only in finally and therefore did not checkpoint before effects.

## Decisions
- Preserve the explicit sequential native engine and existing Rust scheduler; Python remains the side-effect/approval boundary.
- IDs and names are at most 128 characters. JSON and transport bounds count encoded bytes.
- Never automatically replay an interrupted or uncertain side effect. Metadata records that a tool was requested, not whether an external effect committed.
- SQLite saving failure logs a constant warning and preserves existing execution availability; this is not a durable exactly-once transaction protocol.

## Actual verification
- `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_workflow_integrity.py`: **44 passed**, final run after wire duplicate/completion gates.
- `DJCODE_DAF_ENGINE=/Users/darshjme/Documents/Codex/2026-10-06/ch/work/djcode/target/daf/debug/djcode-daf-engine PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_capabilities.py -k 'real_daf or daf_rejects'`: **3 passed, 10 deselected**, final run after workflow changes; real bundled Rust host exercises concurrent roots, dependency order, failure skip and cancellation.
- `CARGO_TARGET_DIR=/Users/darshjme/Documents/Codex/2026-10-06/ch/work/djcode/target/daf cargo build --locked --manifest-path src/djcode/daf_engine/Cargo.toml`: passed, producing the debug host used above.
- Full Ruff checks on `src/djcode/workflow.py` and `tests/test_workflow_integrity.py`: passed. Scoped `git diff --check`: passed.

## Open questions
Crash metadata is for diagnosis; no resume/replay UI was added. Tool success still uses the existing textual error-prefix convention, which should eventually become a typed dispatch result. No hard process-crash/power-loss durability claim, rollback, or external exactly-once claim. Native execution remains sequential.

## Next steps
Root: combine acceptance and packaging once after integration; document the limits and uncertain-effect handling. No provider calls, Actions, commits, persistent background processes or credentials were used by this assignment.
