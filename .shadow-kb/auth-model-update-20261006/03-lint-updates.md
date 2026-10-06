# 03 — Lint and update status

## Done
- Added `updater.get_update_status(check=False, force=False)` non-installing status API; offline status/doctor never contact network or write cache. Explicit checks use bounded canonical metadata reads. Manual mode permits checks; disabled/environment opt-out overrides force. Error states stay unavailable and cache for 24 hours; force bypasses cache. Managed cache binds active commit.
- Reports managed/pip/uv/editable source ownership, suggested manual command, mode, active managed version/commit and receipt-validated rollback availability. Studio doctor includes offline status.
- Added `maintenance.run_lint(path=None)` fatal Ruff checks, structured diagnostics, actionable missing/config/dependency failure, exit codes 0/1/2. Does not change source or install Ruff.
- Managed activation exceptions restore both current and prior rollback pointers. Rollback verifies target executable version before pointer mutation and restores mode/current if activation fails.

## Found
Previous legacy update check conflated network failure with no update and did not expose ownership/mode clearly. Previous failed activation could overwrite rollback history. Previous rollback trusted receipt without testing target executable.

## Decisions
Preserved canonical manifest/checksum/tag verification and staged checks. No release or real installation changed. Borrowed visible ownership/status semantics from official Claude Code setup documentation: https://code.claude.com/docs/en/setup . Fatal lint checks only (E9,F63,F7,F82); no broad formatting policy. Parent owns CLI wiring.

## Tests
`PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_update_status_lint.py tests/test_managed_update.py tests/test_updates.py tests/test_studio.py`: 70 passed in 16.40s. Actual Ruff fixture detects syntax location and then passes after repair; invalid/missing path and tool failure remain nonzero. HTTP mock verifies stable metadata only, offline/disabled no network, cached sanitized error, malformed cache and active-pointer invalidation. Managed tempdir tests verify rejected rollback and preserved current/prior previous after failed activation. Scoped fatal Ruff and git diff --check passed.

## Open questions / boundaries
Individual pointer replacement is atomic; exception recovery is tested, but a process kill between multiple pointer/config operations is not a cross-file transaction. Legacy `check_for_updates` behavior remains for compatibility; new CLI must use new explicit status API. Cache is local non-authoritative UI state; install always re-verifies canonical artifacts. No live update server tested. Ruff checks bounded to 30 seconds; startup installation checks and project lint are distinct APIs. Editable checkout guidance requires reviewing source changes before syncing.

## Next
Parent integrate `update --check/--status/--rollback/--mode` and `lint [PATH] --json`, map `ok` and `exit_code` to process failure, then full acceptance. Preserve disabled setting rather than silently bypassing it.
