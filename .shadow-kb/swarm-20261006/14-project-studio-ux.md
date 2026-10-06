# 14 · Project Studio editing and draft behavior

## Done
- Compact kind/name fields and responsive action rows keep every action visible at 40×18, 60×20 and 100×30. The editor retains at least two content lines after Textual borders.
- Save, Template, Load and Close have working keyboard actions: Ctrl+S, Ctrl+N, Ctrl+R and Escape. Focus starts in the editor.
- Switching kind updates an untouched template but retains an edited draft and explains how to replace it. Saved definitions load into the same editor.
- Invalid JSON shape remains in the dialog with literal error text. Run flow is enabled only for Flow with a supplied saved name.

## Found
- Stacked fields and a single five-button row crowded the editor and clipped actions in narrow terminals.
- Kind changes left the wrong template in the editor; template replacement could erase an edited draft without explaining the behavior.
- Run flow appeared actionable for every definition kind.

## Decisions
- Preserve the existing Studio storage and command pipeline. The modal validates basic JSON shape; domain validation remains in the common Studio handlers.
- Retain modified drafts when kind changes. Explicit Template performs replacement.

## Evidence and tests
- New `tests/test_studio_screen_ux.py`: **4 passed** in its final focused run; real headless keyboard input covers responsive geometry, invalid save recovery, template save, draft retention and temporary-store loading.
- Final aggregate includes the 4 UX cases and existing 15 Studio domain cases: **86 passed** across all owned acceptance areas.
- Fatal Ruff checks passed for `studio_screen.py`, `app.py` and the new tests.

## Residuals and next
- No live workflow inference was triggered. Flow execution validation is covered by the workflow specialist.
- The compact helper line hides below 22 rows to retain editor space; keyboard shortcuts remain functional. Root to integrate documentation and combined validation.
