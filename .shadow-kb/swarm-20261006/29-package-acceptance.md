# 29 — Package and installed-wheel acceptance

## Done
Built the final 4.4.0 wheel and source archive locally with `uv build`. Installed
the wheel into an isolated staging directory, without replacing the user's
installation, and verified public commands and the deep doctor from that copy.

## Found
The initial source archive included 398 MB of local Rust build output. Explicit
source selection and target/cache exclusions fixed packaging without deleting
build data. The final archive is 709,718 bytes; the wheel is 511,043 bytes.

## Decisions
Compare every current packaged Python/Rust/TOML/lock source byte and the bundled
notice against the checkout. Check documentation and captures in the source
archive. Verify an installed wheel rather than relying on editable imports.
Existing locked dependencies supplied the staging runtime; a fresh dependency
resolution on every platform was not tested.

## Tests
- Final checker verified **137 source files in each artifact** and **18 Markdown/SVG
  documentation files in the source archive**, plus the lock, notice and root
  project documents. Neither artifact contains target or virtualenv directories.
- Staged wheel deep doctor passed: 102 module syntax checks, runtime registries
  (13 providers / 22 tools), fatal lint, memory restart and real DAF/DDAL roundtrip.
- Six real-process CLI groups passed: help, offline update ownership/opt-out,
  secret-free scoped key roundtrip, unknown model provider failure, actual lint
  diagnostics with no edits, and missing-path text/JSON failure exits.

Evidence: `/tmp/djcode-swarm-final-package-check.json`,
`/tmp/djcode-swarm-final-wheel-doctor.json`,
`/tmp/djcode-swarm-final-wheel-cli.json`.

| Artifact | SHA-256 |
| --- | --- |
| `djcode-4.4.0-py3-none-any.whl` | `e81f76fa5358784cbeb7a55b26f3104c1eca730a984653f185f59b1b38c9ff09` |
| `djcode-4.4.0.tar.gz` | `81e4c60c832bb8f8aba4838b052a4c9a5677ef9d432c84e94b704df77fdb4fc8` |

## Open questions / limits
No managed installer release or production update was published. Live hosted
authentication/inference and physical terminal behavior remain separate checks.
The staged wheel reused existing locked dependencies on macOS arm64.

## Next steps
No required package repair remains. Source publication belongs to assignment 30.
