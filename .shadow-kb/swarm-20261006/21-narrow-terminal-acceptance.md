# 21 · Narrow terminal acceptance

## Done
- Verified real Textual screens and keyboard actions at 40×18, 60×20 and 80×24: Help, model picker, connection setup, command palette, agent roster and Project Studio.
- Made the roster scrollable so its full content remains reachable.
- Rebalanced approval details and actions: long tool arguments scroll while Deny and Allow once remain on screen. Deny receives initial focus.
- Made ConnectScreen responsive so provider options, key/endpoint/model input and Cancel remain on screen in compact terminals.
- Verified the prompt remains on screen, Studio opens without the navigation rail, and all sidebar tabs can be selected through F6 and native arrows.

## Found
- Fixed-height connection options and stacked margins could push Cancel below short viewports.
- The roster used a non-scrolling container. Approval content could consume space required by the action row.

## Decisions
- CSS and container corrections preserve existing provider transaction logic, denial semantics and tool approval callback.
- Fixtures suppress network/provider initialization and supply one model for picker geometry. The composed screens, keyboard dispatch and approval result use actual app behavior.

## Evidence and tests
- New `tests/test_narrow_terminal_acceptance.py`: **3 parameterized cases** covering the stated viewports. Shared keyboard checks also provide assignment 15 evidence.
- Narrow/Studio/compact run: **11 passed**. Final owned aggregate: **86 passed** in 69.10 seconds.
- Fatal Ruff checks passed for all owned modules and tests. Both SVGs refreshed after final changes and both rendered previews visually inspected.
- Post-final help/welcome wording, the narrow keyboard file passed **3 cases** in 21.85 seconds.

## Residuals and next
- Headless Textual acceptance, not a physical terminal or live authenticated provider session. No inference, browser login or external tool side effects were executed.
- Root should include these scoped results in combined verification and the final memory checkpoint; source edits are frozen.
