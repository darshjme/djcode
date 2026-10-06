# 15 · Keyboard discovery and panel reachability

## Done
- Added Ctrl+E to open Project Studio from the prompt, including layouts where the navigation rail is hidden.
- Added F6 to reveal the sidebar and focus its native tabs; Left/Right reaches every tab even when labels extend beyond the visible row.
- Help documents Ctrl+B, Ctrl+E and F6. F3 correctly describes the real connection setup. Welcome text exposes help, Studio and panels; connected Ready text includes Studio.
- Escape returns input focus after panel navigation; Ctrl+B toggles the same sidebar state.

## Found
- Studio was reachable through the wide navigation rail but lacked a direct key when that rail disappeared.
- Sidebar tab labels overflow the visible row, making rightmost panels hard to discover.

## Decisions
- Reuse Textual tab behavior and existing Studio callback rather than creating a second interaction path.
- Keep the normal prompt shortcuts unchanged and show the new actions in existing help/welcome surfaces.

## Evidence and tests
- Shared real keyboard acceptance in `tests/test_narrow_terminal_acceptance.py` exercises all actions at **40×18, 60×20 and 80×24**, including all seven remaining tabs and return to input. Its 3 cases are shared with assignment 21 and must not be counted twice.
- Combined narrow/Studio/compact run: **11 passed**. Final aggregate across the owned areas: **86 passed**.
- Fatal Ruff checks passed. Both SVGs were regenerated after final discovery text and visually reviewed through Quick Look PNG previews.
- Post-final help/welcome wording, the narrow keyboard file passed **3 cases** in 21.85 seconds.

## Residuals and next
- Tabs remain horizontally clipped until focused/navigation changes the visible selection; all are keyboard-reachable.
- Root owns README and documentation prose; document Ctrl+E Studio, F6 panels and Ctrl+B sidebar consistently.
