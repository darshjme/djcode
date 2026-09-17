# Development timeline

| Date | Evidence | Change |
|---|---|---|
| 20 March 2026 | `94a1fce` | Initial Python CLI and local-first coding loop. |
| 7 April 2026 | `ecf61f7` | Version 1.0: ChromaDB, onboarding and status bar. |
| 14 September 2026 | `4af7db9` | Published DAF workflows and terminal redesign. |
| 16 September 2026 | `77a842b` | Version 4.3 source release used below. |
| 17 September 2026 | [Provenance manifest](development-provenance.json) | Older DJcode provider runtime contributed the milestone validator to 4.4. |

The dates above come from Git author timestamps. Runtime project events use UTC
and record the executing DJcode version. Roadmap entries remain user assertions.

For the 4.4 milestone, an unmodified 4.3 checkout at the recorded commit ran
`Provider.chat_ollama` against local `qwen2.5:1.5b`, with tools disabled. The prompt
requested a function accepting the four roadmap statuses and raising `ValueError`
for invalid strings and other types. Its returned function became
`src/djcode/milestones.py` after review, type annotations and formatting. Ten
parameterized cases test that function. The manifest records hashes and scope.

The installed 3.0 CLI and a 4.3 CLI attempt both returned empty output, so neither
is treated as a successful contribution. The provider API attempt succeeded. This
is evidence that older DJcode helped implement one part of newer DJcode, not that
it autonomously built the entire release. No historical self-build claims were
added to existing commits.

The website capture comes from a real initialized 4.4 Textual application with a
local Ollama provider. It shows idle workspace state and the studio template;
no fake completed workflow is presented as an actual run.
