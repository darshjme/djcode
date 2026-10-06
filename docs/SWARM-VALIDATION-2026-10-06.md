# Coding workflow and terminal verification — 2026-10-06

Created by Darshankumar Joshi. This milestone repairs workflow admission,
approvals, cancellation, streaming, session recovery and terminal navigation,
then adds provider-scoped authentication, validated model selection, update
status and fatal lint commands.
The [operating guide](SWARM-AND-TERMINAL.md) explains the user controls and
the distinction between the bundled engine and standalone DAF.

## Executed checks

Local verification used macOS arm64, uv-managed Python 3.12.11 and
Rust/Cargo 1.94.1. Test fixtures use temporary configuration and workspaces.

| Check | Actual result |
| --- | --- |
| `PYTHONPATH=src .venv/bin/python -m pytest -q` | 692 passed; 14 subtests passed; zero failures (138.76 seconds) |
| Fatal Ruff across `src/djcode`, `tests` and `scripts` | Passed (`E9,F63,F7,F82`) |
| Locked bundled Rust `--workspace --all-targets` tests | 260 passed; zero failures or ignored tests |
| Bundled workspace doctests | 1 passed; 1 ignored |
| Strict bundled workspace/all-target Clippy | Passed with `-D warnings` |
| Offline release-helper tests | 12 passed |
| Source/bundled DDAL contract parity | Codec, protocol, channel and golden-wire fixture match |

The Rust count includes 79 core, 113 DDAL, five wire-contract and 63 graph
tests. The host binary and two benchmark targets compiled but contain no tests;
their zero counts are not added to the total. Formatting checks cover changed
Rust files, not unrelated preexisting formatting differences.

The coding acceptance uses a deterministic HTTP provider fixture that proposes
a real file write, corrects the file with `file_edit`, then runs it through the
shell tool. Other tests verify denied writes, inherited specialist approvals,
blocked dependencies, real DAF/DDAL events and cancellation of pending handlers.
Malformed stream arguments never become tool effects.

Headless Textual tests exercise actual mounted screens at 40×18, 60×20 and
80×24, including provider connection, studio fields, sidebar keyboard access,
approval buttons and session resume. The committed SVGs capture the real
disconnected interface at 80×28 and 160×40, the command palette at 100×30 and
the model picker at 80×28 with an explicitly labeled fixture catalog.
No hosted model activity or usage is invented. Keyboard tests cover Ctrl+P
command search, Ctrl+G Plan/Act, F2 model selection, Escape preservation and
sidebar/studio operations. Runtime model changes preserve conversation and
context; failed selections preserve the previous runtime and settings.

Authentication checks cover secret-free status, hidden/stdin key entry,
provider-scoped logout, PKCE and account refresh/logout races. Model checks
cover exact IDs, provider pairing, catalog readiness and unknown capabilities.
Update/lint checks cover offline ownership, disabled checks, cache failures,
activation recovery, rollback validation and real diagnostic locations.

## Packaging and recovery

Wheel/source-archive acceptance checks the bundled `Cargo.lock`, Rust sources,
wire fixture and third-party notice, while excluding build directories and the
virtual environment. Explicit source selection prevents local Rust build output
from entering the source archive; the first archive exposed this defect at
398 MB. The source archive includes the guides and SVG captures.
A staged wheel is imported from its installed directory, with existing locked
dependencies, before running the deep doctor. Checks cover syntax, registries,
fatal lint, persistent memory restart and an actual built DAF/DDAL roundtrip.
Six real-process acceptance groups also exercise the staged wheel's help,
offline update status, saved-key login/status/logout, unknown-provider failure,
actual lint diagnostics and missing-path text/JSON failures. The final artifact
outcome is recorded in assignment 29 under
`.shadow-kb/swarm-20261006/`.

After `uv build`, verify the artifacts with:

```sh
python3 scripts/check_distribution.py dist/djcode-4.4.0-py3-none-any.whl dist/djcode-4.4.0.tar.gz
```

The checker compares packaged Python/Rust sources and documentation to the
checkout and rejects build/virtual-environment directories in both artifacts.

The first combined run exposed a fork-session compatibility regression for
minimal operator implementations. Optional context/checkpoint access was fixed;
52 recovery tests then passed. Another run timed out in two cancellation tests
while bundled Rust files were still changing. The source hash determines the
engine build cache, so the initial frozen full run passed all 575 tests. After
the authentication/model/maintenance work, a fresh frozen run passed all 692
tests. An editable-install collection problem caused by a macOS hidden
`.pth` flag was bypassed explicitly with `PYTHONPATH=src`; packaged imports are
verified separately.

## Scope

Thirty numbered assignments across DAF and DJcode used three specialists and
the primary agent in waves within four active slots. A focused follow-up improved
authentication, model selection, keyboard operations and update/lint behavior.
Existing persistent-memory recovery checks continued to pass. Handoffs preserve
findings, decisions, executed checks and remaining limits.

Live provider quality, every model/provider, a distributed fleet and physical
terminal hardware were not tested. Optional computer/voice integrations were
not exercised. These results do not establish AGI or ASI. Source changes do not
publish a managed installer release. GitHub Actions was not enabled or used.
