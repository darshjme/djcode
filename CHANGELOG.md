# Changelog

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
