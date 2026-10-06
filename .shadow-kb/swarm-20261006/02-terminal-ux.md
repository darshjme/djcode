# 02 · DJcode terminal interaction and visual UX

## Done
- Reworked the header and agent strip to fit narrow terminals, preserve mode/model identity, and count executing agents separately from configured profiles.
- Made unknown context usage and missing pricing explicit. Zero pricing is supported when it is actually provided; missing rates no longer display a misleading $0 estimate.
- Replaced the MCP panel's nonfunctional switches with read-only enabled states and the valid extension command. Removed misleading empty safety/monitoring claims.
- Made detail panels scrollable, preserved literal names/alerts instead of interpreting Rich markup, wrapped numbered todos, and fixed count/summary resets after removing the last item.
- Produced real Textual SVG captures at 80×28 and 160×40 and visually reviewed rendered PNG previews. Captures deliberately use a disconnected fixture; they do not represent a live provider session.

## Found
- Header mode did not follow Ctrl+P; root fixed the app callback and the new keyboard test now verifies PLAN/ACT changes.
- Profile readiness, unmeasured context, unknown rates, and an empty alert list previously implied runtime state that the application did not know.
- MCP enabled switches did not change extension configuration. A zero-item refresh left old summaries behind.

## Decisions
- Preserve existing layout/component structure and use compact responsive text rather than replacing the TUI.
- Keep MCP state display truthful; extension configuration remains through existing commands.
- Keep unknown measurements visibly unknown until reported by actual state updates.

## Evidence and verification
- `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_terminal_ux.py tests/test_compact_tui.py`: **13 passed**.
- Focused fatal Ruff checks (`E9,F63,F7,F82`) for changed TUI modules and new tests passed.
- Headless Textual checks cover widths 40/60/80/120, real Ctrl+P actions, detail-panel scrolling, literal bracket text, cost calculation with supplied rates, and empty resets.
- SVG artifacts: `docs/assets/terminal-compact.svg`, `docs/assets/terminal-workspace.svg`.
- Preview inspection: `/tmp/djcode-terminal-preview-20261006/terminal-compact.svg.png` and `/tmp/djcode-terminal-preview-20261006/terminal-workspace.svg.png`; generated with macOS Quick Look, both visually inspected.
- Both SVGs were regenerated and both rendered previews visually re-inspected after the final Ctrl+E/F6 discovery improvements. The final combined targeted suite passed **86 tests**; fatal Ruff checks passed for all owned source and tests.

## Residuals
- No live provider/model call or physical terminal acceptance in this assignment. Captures exercise the actual app layout with initialization mocked.
- Pricing remains unavailable until a caller supplies model-specific rates; the panel now communicates that limitation.
- Wide sidebar tabs extend beyond their visible row. Assignment 15 provides F6 plus native Left/Right navigation, verified to reach all seven remaining tabs at 40/60/80 columns.

## Next
- Root to integrate the SVGs into documentation and run the combined suite with other specialists.
- Assignments 11, 14, 15 and 21 are complete; their numbered handoffs record the final behavior and verification.
