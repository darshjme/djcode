# Changelog

## 4.2.1 — original design references

- Bundle seven original design-context packs with accessible SVG examples, responsive states and implementation guidance; no design-service account is required.
- Add offline `--design-packs`, `--design-pack ID`, and `--design-export DIR` commands. A selected pack can guide a one-shot prompt.
- Add `/design ID` and `/design off` in both terminal interfaces, preserving operational rules and tool history while selecting or clearing a reference.
- Keep examples and guidance original and MIT-licensed; cite public standards as references and include no proprietary library assets.


## 4.2.0 — 2026-09-09

- Remove the ASCII Buddy companion and `/buddy` commands; retain existing user data and the separate Mitra legal specialist.
- Add a compact responsive TUI, usable keyboard command selection, visible shortcuts and scrollable tool approvals.
- Add canonical CI-verified managed updates, atomic release switching, rollback, and automatic/manual/disabled update modes.
- Add `--check`/`--lint` and TUI maintenance commands, plus provider discovery and configuration-preserving startup selection.
- Preserve account authentication across provider switching and specialist permission policy in classic commands. Account sign-in availability remains provider-specific; see docs/ACCOUNT-AUTH.md.
- Distinguish 15 model-callable tool schemas from 2 additional internal dispatch helpers.

project by Darshan Kumar Joshi

## 4.1.0 — 2026-09-09

- Add opt-in Colibri provider and `djcode-colibri` resource planning, preflight and foreground serving for existing model files. No engine or weights are downloaded.
- Require native tool capability and explicit memory/context budgets; preserve provider errors, approval and cancellation semantics.
- Include both CLI entrypoints in the installer. See docs/LOW-MEMORY-COLIBRI.md for verified scope, requirements and limits.

## 4.0.2 — 2026-09-09

- Stop heuristic text-command extraction after native tool use, preventing final summaries from replaying shell commands or overwriting files.
- Close OpenAI-compatible SSE responses immediately at `[DONE]`, without waiting for the server to close the connection.
- Derive TUI profile counts and version from the runtime registry, and fix modal overlay styling.

## 4.0.1 — 2026-09-09

- Repair provider selection, native routing, streamed tool IDs and terminal failure handling.
- Carry approvals and provider selection into specialist/subagent execution; enforce deadlines, limits and cancellation.
- Add native TUI approvals, working slash-command dispatch and line-oriented `--repl` entrypoint.
- Preserve tool-call/result protocol across session recovery and context compression.
- Make facts/context durable without implicit embedding-model downloads.
- Repair the installer and update commands, retain previous releases and add Featherless provider configuration.
- Correct unsupported marketing claims and document acceptance coverage and remaining limits.
