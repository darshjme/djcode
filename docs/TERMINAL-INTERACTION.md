# Terminal interaction

Run `djcode` for the full-screen TUI or `djcode --repl` for the line-oriented interface.
Both interfaces discover commands from the same catalog. Each shows only the commands
it supports. Provider setup and managed updates keep their existing behavior.

## Full-screen TUI

- Type `/` to browse suggestions. Tab completes the highlighted command; Enter
  completes a partial name or submits an exact command. Arguments are preserved.
- F4 opens the searchable command palette. F1 shows the full command and shortcut list.
  F2 selects a model, F3 selects a provider, and F5 shows agents.
- Up/Down browse this application's submitted prompts when suggestions are hidden.
  Moving past the newest entry restores the draft you were editing.
- While a response runs, submitting another prompt keeps the draft intact. Ctrl+K
  or `/cancel` cancels the active response. Model/provider switching and clearing the
  conversation wait until the active response is cancelled or finished.
- Ctrl+B toggles the sidebar. It starts hidden below 110 columns. The footer and
  approval status occupy separate rows; command suggestions do not cover the input.

TUI conversations are checkpointed to SQLite and saved on exit. `/history` lists
sessions and `/resume ID` restores their conversation.

## Line-oriented REPL

- Tab completes slash command names. Up/Down browse persisted prompt history.
- Ctrl+C clears an input or cancels a running response on macOS/Linux and returns
  to the prompt. Ctrl+D at an empty prompt, or `/exit`, closes the session.
- Ctrl+R recalls the last message for editing. Ctrl+K deletes text after the input
  cursor. These input bindings are active while the REPL is accepting input.
- `/plan` or Ctrl+P switches Plan/Act. Plan mode blocks operator tool execution,
  including when auto-accept is enabled. Specialist execution and running recipes
  require returning to Act; read-only scout/architect commands remain available.
- `/thinking` toggles thinking output. `/auto` or Ctrl+T toggles the effective
  approval state for both the operator and orchestration.
- `/check` and `/lint` run the existing runtime checks; `/update` uses the existing
  managed updater. `/docs` opens the built-in index, `/docs overview` opens a topic,
  and `/docs <project task>` dispatches documentation work in Act mode.

The full operator conversation is checkpointed after tool rounds and responses,
preserving tool-call/result associations. Session cleanup also runs on interruption
or command failure. Terminal cancellation is not an undo operation for completed tools.

## Validation

`tests/test_input_flow.py` covers actual Textual keyboard events, preservation of a
draft during active work, command completion, prompt history, Plan-mode tool denial,
real TUI initialization and SQLite recovery, cancellation, and 60/80/120-column layout.

The September 14 acceptance run used the actual CLI in a PTY with an isolated
loopback SSE fixture. It verified completion, Plan/Act, docs, streaming, cancellation
of a stalled response, a successful next prompt, and clean exit. It did not exercise
real hosted credentials, model inference, or Windows signal handling.
