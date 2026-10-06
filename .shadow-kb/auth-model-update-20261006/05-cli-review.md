# 05 — Independent maintenance CLI integration review

## Done
Read root maintenance_cli.py/cli.py and actual auth_management/model_selection/update/lint APIs. Added only tests/test_maintenance_cli_integration.py. No live network, account, installation or source mutation in review fixtures.

## Found
- Unknown model catalog JSON contained provider_unknown but CLI returned success (0). Regression initially failed; root now maps ready/available to successful catalog and errors to failure, including text.
- lint nonexistent-path --json was rejected by Click before structured helper, producing plaintext. Regression initially failed; root now defers existence check to run_lint.
- Offline disabled update status preserves config bytes, performs no client/install/cache writes. Verified with forbidden client/install callbacks.

## Decisions
Root owns source integration; reviewer did not edit maintenance_cli.py or cli.py. New integration tests exercise actual public main dispatch, actual catalog unknown-provider gate and actual missing lint path.

## Tests
Initial isolated run: 2 failed, 1 passed, establishing both regressions. After root repair: `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_maintenance_cli_integration.py tests/test_maintenance_cli.py`: 12 passed in 0.20s. New test fatal Ruff and git diff --check passed. Parent root tests additionally cover auth status/login/logout, help without inference, prompt escape, update conflict flags/cache bypass, actual lint syntax and rejected model selection preserving config.

## Open questions
Root added strict config.load_config_for_write and uses it for mode/model mutations. Added two isolated regressions proving malformed config is preserved and model probing is forbidden before validation. Root acceptance log `/tmp/djcode-maintenance-cli-safe-config-tests.log` confirms configuration-recovery/rootCLI/integration tests passed. Offline config.load_config may create configuration directories on pristine installation via ensure_dirs; read-only guarantee is no credential/config/cache content modification or network, not zero filesystem metadata changes. CLI argument-usage errors generally remain Click text even under --json; the missing lint path is now an operational machine-readable result.

## Next
Strict configuration write guard implemented and verified; root final acceptance and update checkpoint. Review source/test frozen after these checks; no repeated audit loops.

## Final bounded documentation/API observations
Reviewed AUTH-MODELS-MAINTENANCE.md and TERMINAL-INTERACTION.md against public auth/model/update/lint dispatch and current keyboard bindings. No credential-value exposure found in these reviewed public status paths; no live credential flow exercised. Documented full-screen Ctrl+G/Ctrl+P/F6/Ctrl+E match current app bindings. One actionable text UX gap reported: lint operational failures include helper `detail` but CLI text emission drops it and shows only "Lint could not run." JSON contains remediation; default text should also display detail to satisfy documented actionable path/dependency failure behavior. No source edits or full-suite rerun during this final bounded review.

## Frozen combined acceptance review
Read final focused auth/model/UI/integration handoffs and final documentation after root reported 692 tests plus 14 subtests passed. Independently traced palette callback (fills input, does not dispatch), Ctrl+P/Ctrl+G bindings, shared terminal operation dispatch, both TUI/REPL Plan admission guards for `/update install`, offline/check split, and text/JSON lint detail. These documented behaviors match frozen source. Previous lint text remediation gap is repaired. No additional release blocker found in this bounded read-only review; combined counts are root-run evidence, not a duplicate reviewer test run. Live provider authentication and physical-terminal acceptance remain outside this review.
