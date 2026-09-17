# Website capabilities and terminal workspace

The reference is the live [DJcode website](https://cli.darshj.ai), inspected September 17, 2026. The full-screen Textual application is the app/TUI; `djcode --repl` is the line-oriented interface. Both use charcoal surfaces, quiet gray borders, white text and cobalt focus accents. The application provides the website's workspace navigation and Explore/Plan/Build/Verify guide. These are command entry points, not simulated successful task runs.

At 110 columns the workspace navigation appears. At 150 columns the existing context sidebar also opens. Smaller terminals keep the conversation and input visible; F4 and slash completion expose the same commands. Ctrl+B toggles the context sidebar. Tab moves focus, Escape returns to the input, Ctrl+P toggles Plan mode and Ctrl+K cancels. `NO_COLOR` remains respected. Browser typography, gradients, illustration, animation and rounded pixel geometry cannot be reproduced identically in terminal cells.

## Feature access

| Website capability | Working entry point and implementation | Verification scope |
| --- | --- | --- |
| Read, edit, run and inspect code | Natural-language tasks dispatch scoped file, shell and git tools; `/project` inspects actual git state | `test_execution.py`, `test_runtime_protocol.py`; new real git workspace test |
| Eight intent modes and project context | `prompt_enhancer.py`, called by both app and REPL before task dispatch | Existing prompt/context fixtures; source wiring inspected |
| Specialist team and shared context | `/scout`, `/architect`, `/build`, `/orchestra`, `/test`, `/agents`; local registry and orchestration engine | Tool execution, inherited approvals, deadlines and cancellation in `test_execution.py` |
| Session memory and persistent facts | `/memory`, `/remember key=value`, `/recall key`, `/history`, `/resume ID` | Restart, concurrent facts, malformed data and protocol recovery in `test_memory_recovery.py` |
| Semantic retrieval | Explicit embeddings through memory/context APIs; offline lexical fallback | `test_context_no_download.py` checks explicit vectors and fallback; no model download or live embedding service test |
| Seven original design packs | `/design`, `/design ID`, `/design off`; CLI also reads/exports Markdown and SVG without provider auth | `test_design_packs.py`, `test_design_commands.py`; app navigation selection tested |
| Local and hosted providers | `/connect`, `/provider`, `/models`; existing Ollama/MLX and hosted provider integrations | Protocol, discovery and authentication fixtures; no new live inference or Apple GPU acceptance |
| Reviewable changes | Approval prompts, Plan mode, file/git tools and `/project` diff summary | Deny/allow, keyboard focus and actual git fixture coverage |
| Local storage, no usage analytics | Session/fact stores on disk; Chroma analytics disabled; hosted calls send chosen task context | Configuration and Chroma source inspected; no network packet audit |
| Custom project teams and dynamic flows | `/agent`, `/organisation`, `/flow`, or the Project studio editor; persistent definitions and validated dependency graphs | `test_studio.py`; actual engine transport verified by doctor |
| Roadmap and verified completion | `/roadmap`, `/timeline`, `/doctor`, `/finish`; Git history, events and approval-gated checks | `test_studio.py`; user milestones remain separate from verified completion |
| DAF/DDAL | `/workflow`; bundled Rust graph executor and binary transport | Real engine tests, including cancellation after cold-build setup |
| Self-hosted Vyasa fleet | Existing `--vyasa`; new `/fleet` in both interfaces lists specialists or sends an explicit named-session request | Auth, endpoint, redirect and request fixtures in `test_vyasa.py`; new shared command dispatch test |

The website's 29-person fleet refers to the connected Vyasa server directory, not the built-in local specialist count. `/fleet` reports that server's actual response. Set `VYASA_URL` and `VYASA_TOKEN` using the existing fleet setup; credentials are not accepted in slash-command JSON. Remote access requires HTTPS. Example: `/fleet {"text":"Review this plan","employee":"prometheus","session":"release"}`. Plan mode permits listing the directory and blocks remote task dispatch.

`/features` exposes the capability guide from either interface. Project inspection, the feature guide and the fleet client do not require a local model connection; fleet access still requires its own authentication. The existing CLI can list/read/export design packs without provider setup. Semantic retrieval needs explicitly supplied embeddings; the application never silently installs embedding or inference models.

## Repairs and validation

The lockfile now matches package version 4.4.0. Scheduler ownership uses per-job OS locks; abandoned runs become interrupted without replay. Legacy lockless jobs require explicit recovery after their old worker stops. See [installation and recovery](INSTALLATION-AND-RECOVERY.md). Cancellation tests build the Rust runtime before starting their five-second handler deadline, so compilation is no longer confused with cancellation responsiveness.

Final validation: 494 tests and 14 subtests passed; 12 release-helper tests passed. All five doctor checks passed, including persistent memory restart and a real DAF/DDAL round trip. Wheel and source distribution builds, lockfile validation and fatal Ruff checks passed. The website passed ESLint, its webpack production build and desktop/mobile visual review. Real screenshots use an initialized local Ollama session. The full suite uses isolated configuration and fixture providers. Visual exports cover 60, 120 and 160 columns, design-pack navigation and the REPL banner. This work does not claim a live test of every provider, hardware backend or external fleet, or literal browser/terminal pixel equality.
