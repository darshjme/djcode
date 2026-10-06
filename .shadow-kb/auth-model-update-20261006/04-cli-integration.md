# 04 — Public command integration

## Done
Added `djcode auth list/status/login/logout`, `models`, `update`, `lint` and
`doctor` without starting inference. Preserved legacy flags and quoted one-shot
prompts; `djcode -- auth` escapes reserved command names. Login keys use hidden
prompts or stdin. JSON commands omit Rich formatting and propagate failure exits.
Update check/status do not install. Mode/model writes reject damaged configuration
before discovery or mutation, through `config.load_config_for_write`.

## Found
Independent review exposed successful exit codes for unavailable catalogs,
Click's missing-path check bypassing lint JSON, and mode writes replacing damaged
settings with defaults. All three were repaired and independently verified.
Auth status/login/logout now handle unknown provider errors as structured failures.
Local login chooses its offered method rather than incorrectly asking for a key.
Final text-output review found lint omitted remediation details. The CLI now
prints missing-path/dependency details in text mode; actual missing-path invocation
reported the path and exited 1. JSON retains its structured report.

## Decisions
Dispatch reserved verbs only in the first position; ordinary prompt invocation
remains compatible. Validate complete model selection before saving. Preserve
provider-scoped credentials and existing update installation verification.
No live accounts, actual managed upgrade, release or deployment was requested.

## Tests
`tests/test_config_recovery.py`, `tests/test_maintenance_cli.py` and independent
`tests/test_maintenance_cli_integration.py`: 20 passed in 0.25s. Checks cover
inference-free help, reserved prompts, offline/read-only update semantics,
real Ruff syntax diagnostics, scoped secret-free credential roundtrip, safe
failed selection, unknown providers, and preserved damaged config bytes.
Evidence: `/tmp/djcode-maintenance-cli-safe-config-tests.log`.

## Open questions / limits
Hidden interactive prompts still require a terminal; automation should provide
stdin. Model discovery verifies availability rather than inference quality.
Account/PKCE acceptance uses mocks; provider-supported live account login is not
established by a successful local command.

## Next steps
Primary: full frozen-source acceptance and rebuilt-wheel real-process verification,
then publish source PR. Those results are recorded in assignments 26/29/30.
