# 27 — Malformed frame and subprocess fault acceptance

## Done
- Added identical Rust wire-contract tests in source/bundled DDAL: fixed golden encode/decode; routing/flag/payload checksum corruption; truncated EOF; malformed version/type/oversized headers rejected while a TCP peer holds the connection open; byte-fragmented TCP success before EOF.
- Added disposable host subprocess tests for malformed/unknown events, unhashable IDs/states, altered arguments, dependency request ordering, false completion claims, duplicate/missing wire outcomes, oversized replies, host exit after one tool, cancellation/reaping, concurrent dispatch limits, approved-request snapshots and verbose stderr.
- Host wire metadata must belong to a completed requested tool, match its outcome, carry bounded integer byte counts, and occur once per ID. Completion must contain each expected unique terminal state exactly once, agree with success outcomes, and account for one wire event per requested tool.
- Events are filtered to permitted metadata fields before callback/storage. With at most 32 tools and one wire event each, retained normal-run events are bounded at 66 (preparing + tools + wires + complete), plus a failure/cancellation event on interruption.

## Found
Repeated wire events previously allowed unbounded retained history/database rewrites until the deadline. Loose state/argument comparisons could accept contradictory host behavior. Awaiting pending handlers before checking premature completion/EOF could stall fault handling behind unfinished work.

## Decisions
- Validate completion before awaiting handler tasks; invalid or disconnected host cleanup cancels Python handlers and reaps its process.
- Do not retry an uncertain or oversized-result side effect. Missing wire confirmation fails completion verification.
- Store no argument objects, output content, stderr diagnostics or arbitrary host fields in workflow records.

## Actual verification
- `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_workflow_integrity.py`: **44 passed**, final version includes duplicate/missing wire tests.
- Real-host targeted `tests/test_capabilities.py -k 'real_daf or daf_rejects'` with debug host override: **3 passed, 10 deselected**, after final Python boundary changes.
- `cargo test --locked -p daf-ddal --test wire_contract`: **5 passed in source, 5 passed in bundle**, actual loopback TCP; no fake socket substituted.
- Source strict all-target DDAL Clippy and bundled strict all-target DDAL Clippy passed. Changed Python/tests full Ruff and scoped diff checks passed.

## Open questions
This is bounded local acceptance; no adversarial external service, network reconnect, model/provider invocation, physical terminal or power-loss test. Persisted tool-request metadata diagnoses interrupted work, not proof of external commit/rollback. Typed tool success remains an architectural improvement.

## Next steps
Root run combined acceptance once after all owners freeze and keep precise claims in docs. No commits, Actions, live provider calls or test host processes left running.
