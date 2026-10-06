# 02 — Model selection and terminal operations

Owner: terminal/model specialist. Date: 2026-10-06. **Source frozen for root integration.**

## Done

- Added a nonmutating, secret-free provider/model catalog in `model_selection.py` and a searchable responsive `ModelPicker` in `model_picker.py`. Search preserves exact case and slash-containing model IDs. Arrow keys move the highlighted choice; Enter selects that choice; Shift+Tab reaches the provider selector; Escape closes without a write.
- Model/provider selection now stages and probes a candidate before persistence. Failure, busy execution, rejected authentication, absent model, unsupported discovery and malformed configuration preserve the active runtime and existing configuration. A provider change clears the previous provider's selected model/base override, while preserving unrelated auth/settings.
- Live discovery distinguishes missing credentials, rejected credentials, unavailable transport, empty catalogs and unsupported discovery. It follows the documented Anthropic/Google cursor parameters on the original API origin, bounds request/body/page/time limits and rejects repeated cursors. Provider bodies and exceptions never become raw error messages.
- Added explicit provider-reported capability metadata where present. Missing tools, vision, reasoning, context or output metadata is unknown; a model name or registry default does not establish capability.
- Unified startup/provider key resolution with the auth owner's frozen `get_key_source`. Named custom providers retain their identity independently of the protocol adapter. Invalid saved credential types no longer suppress valid environment values. No environment key is copied into persisted runtime settings during a model switch.
- Runtime switching preserves the conversation and synchronizes the context manager, compressor, operator, orchestrator, shadow worker, session metadata, header and status. Old provider resources close after transfer. A failed candidate/save retains the old provider. Context pins and injected context survive the switch.
- Added `ContextManager.reconfigure(...)` and `sessions.set_session_model(...)` to make synchronization explicit. Startup uses the exact author spelling **Darshankumar Joshi**.
- TUI Ctrl+P now opens its searchable command palette, with F4 fallback; Ctrl+G toggles Plan/Act. The classic REPL's Ctrl+P starts slash completion at an empty/slash prompt and preserves other drafts; Ctrl+G toggles Plan/Act. Disabled Textual's conflicting default palette. Added `/hotkeys`, TUI `/studio`, and discoverable operation descriptions.
- Added shared `terminal_operations.run_operation` for real read-only auth status, update status/check, bounded Ruff lint and source/runtime checks. Update installation occurs only for the explicit `install` argument and is blocked in Plan mode.
- Applied `load_config_for_write()` before setup, connect, TUI and REPL persistence. A damaged settings file cannot be replaced by read-only recovered defaults during these mutations.
- Refreshed four actual headless Textual SVG captures from the final source. Captures use disconnected fixtures; the model catalog uses explicitly synthetic `fixture/...` IDs and `fixture` source. No live model availability is asserted by a screenshot.

## Found

- Previous model switching could attach a new model to an old provider, persist before validation, replace the context manager and lose conversation/context state, or leave the header/session/compressor out of sync.
- A global saved base override and model were previously inherited when browsing another provider. Named custom provider keys/runtime identities differed from startup readiness.
- The old model picker did not provide a coherent cross-provider search flow and selected the first filtered item rather than the highlighted item in keyboard paths.
- The old Ctrl+P Plan shortcut conflicted with the requested command-palette convention and Textual's built-in palette. Displayed help and actual bindings needed to change together.
- Successful catalog membership establishes discovery/auth access, not successful generation, tool execution or any general-intelligence claim.

## Decisions

- Keep discovery separate from mutation. CLI, TUI and REPL share the same candidate/catalog/selection functions rather than duplicating provider rules.
- Preserve exact provider model IDs and use provider-scoped recent selections. Only Ollama's existing implicit `:latest` alias is canonicalized.
- Require a `ready` probe for runtime model switching and the Connect screen. Existing startup setup retains its explicit user-confirmed offline/unverified save-for-later path; root documentation distinguishes these behaviors.
- Keep saved base overrides scoped to the active provider; an explicit `DJCODE_BASE_URL` environment override remains global. Preserve the auth owner's account-origin restriction and explicit account/API-key method selection.
- Display only provider-reported capability fields. Browser-visible defaults and examples are not live discovery.
- Palette selection fills the command into the prompt, including a trailing space when it takes arguments. The user then submits it with Enter. Escape retains the original draft; selecting a palette item does not itself install, authenticate or run a tool.
- Auth status does not sign in or reconfigure the active provider. Auth mutation remains in the auth owner's helpers and root CLI; `/connect` uses the existing explicit setup flow.

## Frozen integration API

```python
from djcode.model_selection import (
    ModelSelectionError, provider_choices, candidate_config,
    model_catalog, select_model, capability_summary,
)
```

- `provider_choices(config=None) -> list[dict]`: registry and valid named custom IDs, plus configured legacy `remote`. IDs are bounded ASCII letters/digits/underscore/dot/hyphen. URL-shaped legacy provider inputs are excluded from public option labels.
- `candidate_config(config, provider, model=None) -> dict`: deep copied provider/model pairing, no persistence or credential output.
- `model_catalog(config=None, provider=None) -> dict`: `provider`, `status`, `source`, `error`, `message`, `models`, `selected`; each model has `id`, `name`, `capabilities`. Statuses retain startup `ready`, `missing`, `offline`, `unverified`, with `available` for a usable catalog lacking a selected model. CLI computes success from `ready`/`available`; no `ok` field is claimed here.
- `select_model(config, reference, provider=None) -> dict`: exact model ID, or `PROVIDER/MODEL` when the prefix is a known provider; explicit provider allows slash-containing model IDs. Returns a verified unsaved candidate or raises `ModelSelectionError`. Updates at most ten provider/model recent selections.
- `capability_summary(model) -> str`: reported fields or `Capabilities not reported by this provider`.
- `ProviderConfig.from_mapping(cfg, provider_override=None, model_override=None)` builds runtime config from the candidate without rereading disk. `provider_id` retains logical provider identity; adapter `name` retains existing protocol behavior.
- `ContextManager.reconfigure(model, provider, max_context=None)` synchronizes the internal model/provider/compressor and budget, preserving accumulated context.
- `sessions.set_session_model(session_id, provider, model)` updates model metadata without changing transcript content.
- `terminal_operations.run_operation(command, argument="") -> list[str]`: allowlisted printable status/diagnostic rows; TUI/REPL run it off the UI event loop.

## Frozen operations and shortcuts

| Operation | TUI | Classic REPL |
| --- | --- | --- |
| Commands | Ctrl+P, F4, `/commands` | Ctrl+P slash completion, `/commands` |
| Plan/Act | Ctrl+G, `/mode` | Ctrl+G, `/mode` |
| Models | F2, `/models`, `/model` | `/models [PROVIDER]`, `/model [PROVIDER/MODEL]` |
| Connect | F3, `/connect`, `/provider` | `/connect`, `/provider` |
| Help | F1, `/help`, `/hotkeys`, `/shortcuts` | `/help`, `/hotkeys`, `/shortcuts` |
| Agents/panels | F5 roster, F6 focus panels | `/agents` |
| Project Studio | Ctrl+E, `/studio` | Not a REPL command |
| Sidebar | Ctrl+B | Not applicable |

- `/auth` or `/auth list`: secret-free local readiness list. `/auth status [PROVIDER]`: local readiness detail. It does not perform live login or switch providers.
- `/update` or `/update status`: local cached/installation status, no automatic installation or network check. `/update check`: explicitly query latest release. `/update install`: explicit install, blocked in Plan mode.
- `/lint [PATH]`: actual bounded fatal Ruff diagnostics with filename/line/column/code. `/check`: bounded existing source/runtime checks.
- Modal Escape closes/cancels. The provider connect flow retains cancellation/restart rather than adding a separate Back step in this scoped pass.

## Tests and evidence

All commands ran from this repository using the existing `.venv` and isolated settings/mocked HTTP providers. No live provider login/inference or paid API request was run.

1. `PYTHONPATH=src .venv/bin/python -m pytest tests/test_startup.py tests/test_colibri_provider.py tests/test_runtime_protocol.py tests/test_terminal_ux.py tests/test_narrow_terminal_acceptance.py tests/test_input_flow.py -q` — **87 passed in 46.76s**.
2. `PYTHONPATH=src .venv/bin/python -m pytest tests/test_model_selection.py tests/test_model_ui.py tests/test_compact_tui.py tests/test_classic_auth.py tests/test_maintenance_cli.py tests/test_maintenance_cli_integration.py -q` — **48 passed in 9.02s**.
3. After the final model footer correction, `PYTHONPATH=src .venv/bin/python -m pytest tests/test_model_ui.py tests/test_narrow_terminal_acceptance.py -q` — **14 passed in 23.55s**. This repeats cases in the two suites above: **135 unique targeted cases**, not 149 unique cases.
4. Fatal Ruff `--select E9,F63,F7,F82` over all owned changed source and changed/new tests — **All checks passed** after the final source change. Full project acceptance/distribution verification belongs to root and is not claimed here.

New acceptance files are `tests/test_model_selection.py` and `tests/test_model_ui.py`. They cover deep-copy pairing, exact IDs, provider-specific key resolution, sanitized failures, cursor bounds, authentic metadata versus unknown capabilities, actual mounted model/palette keyboard paths at 40 and 80 columns, provider selector catalog replacement, empty/auth-failed selection, Escape preservation, runtime/context/session synchronization, save/damaged-config failure and busy guards. Existing tests were updated for Ctrl+G and validated selection semantics.

Actual final SVGs:

- `docs/assets/terminal-compact.svg` — 80×28 disconnected TUI.
- `docs/assets/terminal-workspace.svg` — 160×40 disconnected TUI.
- `docs/assets/terminal-commands.svg` — 100×30 actual Ctrl+P palette, filtered to `model`.
- `docs/assets/terminal-models.svg` — 80×28 actual model picker with explicitly mocked catalog/capabilities.

Captured using `work/capture-model-terminal.py` in the parent project workspace, not a product source module. SVGs were rendered through Quick Look and visually inspected; the model picker was inspected again after the final Shift+Tab footer update. The wide native tab strip can clip later labels; existing F6/arrow-key mounted acceptance proves all panel tabs remain reachable. The palette has no extra bottom key hint; source remained frozen per root priority.

## Primary references reviewed

- [OpenCode model selection](https://opencode.ai/docs/models/) and [OpenCode keybindings](https://opencode.ai/v2/docs/cli/keybinds): model/provider and searchable-command interaction conventions.
- [Pi keybindings](https://pi.dev/docs/latest/keybindings): discoverable keyboard operations.
- [Anthropic model listing](https://platform.claude.com/docs/en/api/models/list): cursor pagination and provider model fields.
- [OpenAI model listing](https://developers.openai.com/api/reference/resources/models/methods/list): model identifiers, without invented context/tool guarantees.
- [Google model listing](https://ai.google.dev/api/models): page tokens and reported model limits.
- [OpenRouter model metadata](https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties): explicit supported parameters/modalities/context metadata.
- Auth handoff `01-auth.md` and prior `.shadow-kb/swarm-20261006/` terminal, session, Studio and narrow-screen handoffs were read for integration.

## Open questions and residuals

- Physical terminal/PTY behavior and live inference with real credentials were not exercised in this task. Mounted headless Textual and PromptToolkit binding tests are the actual evidence.
- Provider catalog membership verifies discovery, not generation, latency, tool correctness, or AGI/ASI capability. Account eligibility/registration remains the auth owner's documented external requirement.
- Endpoints without supported discovery cannot complete a verified model switch; the explicitly confirmed offline setup path remains available for later connection.
- Legacy URL-shaped provider inputs remain supported by runtime resolution but are excluded from public catalogs to avoid leaking embedded credentials. A named custom provider is the supported picker representation.
- Discovery is intentionally bounded and not cached; very large/slow catalogs may fail with an actionable discovery error rather than run indefinitely.
- Separate configuration writers still lack a cross-process transaction; scoped in-process switching is serialized and malformed-config writes are guarded. No cross-process race-free guarantee is made.

## Next steps

- Root run the final combined Python acceptance and wheel/sdist content verification after this source freeze.
- Root retain the documented differences between verified runtime model selection and explicitly confirmed offline setup, plus TUI versus REPL Ctrl+P behavior.
- A later authorized live acceptance should test one provider's discovery, inference and tools separately from local credential readiness.

No Actions, commits, releases or live credential requests were performed. Capture/test processes started for this pass have completed; no test process remains running.
