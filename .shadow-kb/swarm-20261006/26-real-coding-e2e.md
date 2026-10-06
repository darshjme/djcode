# 26 — Real coding tool acceptance

## Done
Verified the public coding loop through the actual provider serializer and local
tool router. A deterministic HTTP transport proposes a file write, a correction
with `file_edit`, and a shell test against the resulting file.

## Found
The acceptance exercises filesystem and subprocess effects rather than asserting
an invented model answer. Invalid stream indices and workflow admission now fail
before arbitrary tool effects. Denied or inherited specialist approvals and
cancelled/dependency-blocked work are covered by the combined suite.

## Decisions
Use deterministic provider responses to reproduce the tool protocol. A fixture
transport proves tool execution behavior; it does not establish hosted inference
quality, model reasoning or autonomous intelligence.

## Tests
`tests/test_runtime_protocol.py::test_actual_http_tool_loop_writes_edits_and_runs_test`
passed in the frozen combined suite: **692 passed and 14 subtests passed** in
138.76 seconds. Evidence: `/tmp/djcode-auth-terminal-final-tests.log`.
Fatal Ruff over `src/djcode`, `tests` and `scripts` also passed.

## Open questions / limits
Live provider inference and optional voice/computer integrations were not tested.

## Next steps
No required implementation work remains for this acceptance. Live-provider
acceptance is a separate integration exercise with provider credentials.
