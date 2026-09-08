# DJcode 4.1.0 validation

This release repairs execution paths in the existing agent, rather than measuring a new model. There are 19 engineering profiles, 12 content profiles, 17 built-in tools and 48 registered TUI commands. Profiles are system prompts and tool policies, not people or academic credentials.

## Acceptance coverage

| Path | Evidence |
| --- | --- |
| Main loop | Real HTTP serialization fixture reads, writes, edits and runs a Python test through multiple model/tool turns. |
| Hosted inference | Existing NVIDIA account, `nvidia/nemotron-3-super-120b-a12b`: actual file reads, surgical edit and shell test; independent test rerun. A transient provider 503 was also observed and reported as failure. |
| Transport | Native OpenAI/Anthropic/Google adapter routing; distinct simultaneous tool IDs; split argument and thinking chunks; malformed arguments, truncated output and incomplete stream failures. Provider protocol tests use fixtures, not live accounts for every provider. |
| Permissions | Main and nested specialist approvals, native Textual modal, read-only roles, explicit unattended auto-accept and plan-mode tool denial. These are application controls, not an OS sandbox. |
| Subagents | Parent provider/approval inheritance, finite child rounds/deadlines, nesting/concurrency caps, actual file execution, failed background state, pipeline failure and cancellation. |
| Sessions/context | Atomic SQLite transcript replacement and schema migration preserve tool IDs/names. Compression preserves complete call/result groups. Atomic concurrent fact writes and restart retrieval tested. |
| Text-only tools | Native tool use disables heuristic extraction for the remainder of that run, so final summaries cannot replay commands. Extracted actions execute through tools and their results return to the next model turn. Heuristic extraction can misunderstand prose; review approvals. It is not a guarantee that every model is a reliable coding agent. |
| Local processes | Shell output bounded while reading; timeout/cancellation kills process groups. Git arguments passed without shell interpolation. |
| TUI | Real Textual headless startup/render, modal deny/allow, slash dispatch, history/context and cancellation checks. |
| Install/update | Installer uses isolated release environments, preserves previous releases and config, checks runtime version, exposes prefix/bin overrides and never downloads models. Update commands target the official repository. |
| Memory routing | Offline lexical retrieval by default. Semantic search requires explicit embeddings/configuration; no default embedding model download. Existing Chroma data is retained. |
| Featherless | Named provider and `FEATHERLESS_API_KEY`, official OpenAI-compatible endpoint. No live Featherless account test or invented referral/partnership. |

Current release gate: **286 tests passed**, and the 4.1.0 wheel/source distribution built. The earlier 4.0.2 hosted runtime evidence below remains explicitly tied to that commit. The clean hosted rerun completed the read/edit/test fixture with exit 0 and no async-generator shutdown warning. An earlier overloaded-provider run correctly returned nonzero; retry now occurs only before any stream data is emitted.

Image/video commands draft prompts rather than directly calling media-generation services. Campaign executes its director; launch builds and drafts campaign content, without a guaranteed production deployment. MCP extension servers require separate configuration and were not live-tested.

The automated suite is reproducible with `uv run --with pytest pytest -q`. Tests isolate DJcode configuration and disable update checks. Live hosted evidence is separate from deterministic fixtures. Local Ollama/MLX inference, microphone/voice hardware, all third-party extensions, and every provider/model combination were not exercised. Model context limits and pricing in the bundled registry are estimates; confirm current provider documentation.

A real hosted delegation acceptance on `04fef3d` completed in 39.82 seconds: the parent called `spawn_agent`, the specialist repaired the function and ran tests, the parent returned its result, and an independent rerun passed. The test file was byte-for-byte unchanged. Before the correction, the specialist succeeded but the text fallback replayed summary code blocks and overwrote the disposable fixture test; this was reproduced and fixed with regression coverage. OpenAI-compatible SSE now also closes immediately at `[DONE]`, with held-open connection regression tests.

## Colibri integration acceptance

The opt-in `colibri` provider and `djcode-colibri` helper use an existing external Colibri installation. Nineteen provider tests and ten helper tests cover exact model discovery, optional auth, context/output budgets, native tool/result feedback, unsupported-tool errors, preflight rejection, argv boundaries, dry-run behavior and planning timeout. No weights or engine were downloaded or built.

The actual upstream launcher at `fd93c41aa6ae2c7d1cc1a1e2d6b79dbe6d341708` successfully planned a synthetic 1,273-byte safetensors fixture. Actual doctor metadata recognized its GLM family and rejected inference readiness because the fixture lacks a tokenizer and built engine. This validates inspection and refusal paths, not large-model inference, performance, quality or hardware fit. See [requirements and scope](LOW-MEMORY-COLIBRI.md).

## Source-informed changes

All implementation changes were written for DJcode. No external engine code was copied.

- [Hermes delegation](https://github.com/NousResearch/hermes-agent/blob/b1f003e18633298d549668b8e186af84cca45b76/tools/delegate_tool.py), MIT at `b1f003e`: resolved provider/model inheritance, child budgets and cleanup informed scoped subagent fixes.
- [Pi compaction](https://github.com/earendil-works/pi/blob/6160683a4a8012f0d1cd30c145df18b4ca6f5176/packages/coding-agent/src/core/compaction/compaction.ts), MIT at `6160683a`: valid cut boundaries avoid tool results; typed session entries and migrations informed protocol-preserving recovery.
- [Codex parallel tool runtime](https://github.com/openai/codex/blob/1032738aa09b0f0d0d45f7e24e4a96fff219926a/codex-rs/core/src/tools/parallel.rs), Apache-2.0 at `1032738a`: explicit cancellation and execution admission informed child-process cleanup and bounded agent work. DJcode does not claim Codex's OS sandbox.
- [Claude Code agent loop documentation](https://code.claude.com/docs/en/agent-sdk/agent-loop): tool results return to the model until a final response. The [published repository license](https://github.com/anthropics/claude-code/blob/8e02f6ddce21f4c2585d2be501651c1d6b16246a/LICENSE.md) was reviewed as commercial/all-rights-reserved, not a permissive engine source grant.

No cross-product quality, speed or cost winner is established by these checks.
