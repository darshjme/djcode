# 16 — Bundled DDAL compatibility

## Done
- Ported only verified compatible `protocol.rs`, `codec.rs` and `channel.rs` from source DAF into bundled `src/djcode/daf_engine/crates/daf-ddal`, without replacing the workspace or changing DDAL 0.1.0 wire layout.
- Added the identical golden-wire and actual loopback TCP acceptance fixture to both crates. Verified all four files with `cmp`.
- Incoming version/type/size validation now happens from the complete 21-byte header before awaiting advertised payload; stalled peers do not trigger eager payload allocation.
- Outgoing codec validation checks version, actual length, declared length and checksum before changing the destination. Low-level raw serialization allocates for actual payload and remains explicitly unchecked.
- Preserve shared-close/backpressure/admission fixes from task 07. Replaced a bundled router manual Default with the equivalent derived default for strict Clippy; root made the matching small core Priority derive repair.

## Found
Bundled DDAL lagged source fixes and retained old decoder/allocation/encoder behavior. Complete workspace replacement would introduce unrelated changes; limited compatible files were sufficient. Initial strict Clippy exposed pre-existing manual enum Default warnings in bundled core/router; these were repaired without changing default variants.

## Decisions
- Golden fixture uses DDAL 0.1.0 Data, stream 0x01020304, seven-byte `fixture` payload, checksum e7266272. Checksum was independently computed directly over the specified header and payload using BLAKE3 before freezing the literal fixture.
- Keep existing flags, control types and compatibility policy; chunking/compression/priority flags are not automatically implemented by their presence.
- BLAKE3's truncated checksum detects corruption, not peer identity or authorization.

## Actual verification
- Bundled `CARGO_TARGET_DIR=/Users/darshjme/Documents/Codex/2026-10-06/ch/work/djcode/target/daf cargo test --locked -p daf-ddal --lib --manifest-path src/djcode/daf_engine/Cargo.toml`: **113 passed, zero failed/ignored**.
- Bundled same command with `--test wire_contract` instead of `--lib`: **5 passed**.
- Source equivalents: **114 library + 5 integration tests passed**. The library count differs because unchanged handshake test coverage differs between source and bundle.
- Bundled `cargo build --locked --manifest-path src/djcode/daf_engine/Cargo.toml` with the same target directory: passed. The resulting real host passed **3 targeted Python DAF ordering/failure/cancellation tests**.
- Final bundled strict `cargo clippy --locked -p daf-ddal --all-targets --manifest-path src/djcode/daf_engine/Cargo.toml -- -D warnings` with the same target directory: passed. Initial failures were the pre-existing derives noted above, not hidden/allowed lints.
- Scoped diff checks, rustfmt, and byte comparisons passed. Target directory stayed outside packaged engine sources.

## Open questions
No arbitrary external remote peer, cross-language implementation, TLS/authentication, compression, streaming reassembly or distributed scheduler acceptance was performed. The fixture proves shared byte compatibility and local TCP framing only. Wheel packaging and release build acceptance belong to root's integration.

## Next steps
Root retain the fixture and document source/bundle parity during future protocol changes. No Cargo manifest/lockfile replacement, dependency upgrade, provider call, Actions or commit performed.

## Final whole-workspace integration supplement
- Root strict Clippy found graph manual Default/redundant closure; repaired equivalent derived `Always` default and direct predicate. Subsequent whole-workspace lint also found two core test default-field reassignments and host nested-if; replaced equivalent initializers and stable let-else (preserving declared Rust 1.85 syntax).
- `CARGO_TARGET_DIR=.../djcode/target/daf cargo test --locked --workspace --all-targets --manifest-path src/djcode/daf_engine/Cargo.toml`: **260 passed**, zero failed/ignored: core79, DDAL113, wire-contract5, graph63. Two benchmark targets and host binary compiled with zero tests. Log `/tmp/djcode-bundled-final-tests.log`.
- Separate locked whole-workspace `--doc`: **1 passed, 1 ignored**, zero failed; ignored DDAL documentation remains unexecuted. Log `/tmp/djcode-bundled-final-doc-tests.log`.
- Whole-workspace locked all-target strict Clippy passed with no lint suppressions. Log `/tmp/djcode-bundled-final-clippy.log`.
- Initial whole-workspace format normalization was narrowed afterward: restored 18 purely formatted unrelated files only after comparing their exact contents with rustfmt(HEAD), preserving existing substantive changes. Equivalent whitespace cleanup does not alter tested semantics. No claim of clean preexisting whole-workspace format baseline.
- Final scoped rustfmt checks on config.rs/edge.rs/node.rs/main.rs and bundled diff check passed. Exact protocol.rs/codec.rs/channel.rs source-to-bundle comparisons passed.
- Source frozen after removing unrelated formatting; no DAF source edits, dependencies or lock changes, Actions, live provider calls or commits. Root retains final integration ownership.
