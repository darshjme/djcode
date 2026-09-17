"""Operator agent — the main execution agent that uses tools to complete tasks.

This is the primary agent that receives user messages, reasons about them,
calls tools, and produces results. It manages the tool-calling loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from djcode.core.dispatch import DispatchContext, dispatch_context
from djcode.core.events import (
    EventBus,
    steer_event,
    thinking_event,
    token_event,
    tool_call_event,
    tool_result_event,
)
from djcode.core.hooks import HookBus
from djcode.prompt import build_system_prompt
from djcode.provider import Message, Provider
from djcode.tools import dispatch_tool

# ── Thinking block detection ───────────────────────────────────────────────
# Models like qwen3, deepseek, gemma4 emit <think>...</think> tags.
# We detect these and render them as dimmed verbose thinking output,
# separate from the actual response — like Claude Code's thinking blocks.

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

# Dimmed styling for thinking output
THINK_PREFIX = "\033[2m\033[3m"  # dim + italic
THINK_RESET = "\033[0m"
THINK_LABEL = "\033[2m\033[33m"  # dim yellow


class ThinkingStreamProcessor:
    """Processes a stream of tokens, separating thinking from response.

    Detects <think>...</think> blocks and renders them as dimmed output.
    Everything outside thinking blocks is yielded as normal response text.
    """

    def __init__(self, show_thinking: bool = True) -> None:
        self.show_thinking = show_thinking
        self._in_think = False
        self._buffer = ""
        self._think_started = False  # Track if a thinking block has been seen
        self._response_text = ""  # Accumulated non-thinking response

    def process_token(self, token: str) -> tuple[str | None, str | None]:
        """Split one streamed token into (response_text, thinking_text).

        Handles tags even when a complete thinking block arrives in one chunk,
        and holds back a trailing partial marker so a ``<think>`` split across
        two chunks is still recognised.

        Returns ``(response, thinking)``; either element is ``None`` when this
        token contributed nothing of that kind. Thinking is returned as data --
        W2 removed the raw-ANSI write to stderr so a GUI can render it too.
        """
        self._buffer += token
        output = []
        thinking = []
        while self._buffer:
            marker = THINK_CLOSE if self._in_think else THINK_OPEN
            index = self._buffer.find(marker)
            if index >= 0:
                text, self._buffer = self._buffer[:index], self._buffer[index + len(marker) :]
                if not self._in_think:
                    output.append(text)
                    self._think_started = True
                elif self.show_thinking:
                    thinking.append(text)
                self._in_think = not self._in_think
                continue
            hold = max(
                (n for n in range(1, len(marker)) if self._buffer.endswith(marker[:n])), default=0
            )
            safe = self._buffer[:-hold] if hold else self._buffer
            self._buffer = self._buffer[-hold:] if hold else ""
            if not self._in_think:
                output.append(safe)
            elif self.show_thinking:
                thinking.append(safe)
            break
        result = "".join(output)
        self._response_text += result
        return (result or None, "".join(thinking) or None)

    def flush(self) -> tuple[str | None, str | None]:
        """Flush any remaining buffer content as ``(response, thinking)``."""
        if self._buffer:
            if self._in_think:
                # Unclosed thinking block — surface it rather than dropping it.
                pending = self._buffer
                self._buffer = ""
                return (None, pending if self.show_thinking else None)
            result = self._buffer
            self._buffer = ""
            self._response_text += result
            return (result, None)
        return (None, None)

    @property
    def had_thinking(self) -> bool:
        return self._think_started


class Operator:
    """Main execution agent with tool-calling loop."""

    def __init__(
        self,
        provider: Provider,
        *,
        bypass_rlhf: bool = False,
        model: str = "",
        auto_accept: bool = False,
        show_thinking: bool = True,
        approval_callback: Callable[[str, dict[str, Any]], Awaitable[bool]] | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self.provider = provider
        self.bypass_rlhf = bypass_rlhf
        self.auto_accept = auto_accept
        self.show_thinking = show_thinking
        self.approval_callback = approval_callback
        # The engine emits; it never prints. A front-end that wants to show
        # thinking, tool cards or tokens subscribes to this bus. When no bus is
        # attached the engine simply runs silent -- which is what a headless
        # caller wants and what `import djcode.core` guarantees.
        self.event_bus = event_bus
        # W3: the chokepoint's view of this session. Installed for the whole of
        # `send` with `dispatch_context` rather than passed as an argument,
        # because the production path is `self.workflow.one(name, args,
        # dispatch_tool)` -- a positional call that tests monkeypatch with
        # `async def hang(*args)`. A ContextVar reaches `dispatch_tool` without
        # changing that call, so nothing downstream had to move.
        #
        # `hooks` is an empty bus today (W3-4 shipped the seam, P1-1 ships the
        # handlers). W5 fills `checkpoints` and W6 fills `permissions`.
        self.hooks = HookBus()
        # W6 (P0-2). The ENGINE is built here, in the engine, and NOT attached
        # by a surface. W5 attached its checkpoint store from `repl.py` and the
        # Textual TUI silently ended up with no checkpoints at all; doing the
        # same for permissions would recreate F3 in the TUI -- the exact bug
        # this wave exists to close. A front-end supplies `approval_callback`
        # and nothing else.
        from djcode.core.permissions import PermissionEngine

        self.permissions = PermissionEngine(mode=self._initial_mode(auto_accept), load=True)
        self.dispatch_ctx = DispatchContext(
            hooks=self.hooks,
            permissions=self.permissions,
            cwd=os.getcwd(),
            approval_callback=self._approve_repeated_call,
        )
        from djcode.workflow import WorkflowEngine

        self.workflow = WorkflowEngine()
        from djcode.capabilities import Capabilities

        self.capabilities = Capabilities(self)
        if not hasattr(provider, "_session_runtimes"):
            provider._session_runtimes = []
        provider._session_runtimes.append(self.capabilities)
        self.plan_mode = False
        self.on_checkpoint = None
        from djcode.context.manager import ContextWindowManager

        self.context_manager = ContextWindowManager(
            model=provider.config.model,
            provider=provider,
            max_context=getattr(provider.config, "context_window", None),
        )
        self.messages: list[Message] = [
            Message(
                role="system",
                content=build_system_prompt(
                    bypass_rlhf=bypass_rlhf, model=model or provider.config.model
                ),
            )
        ]
        # W1-4: configurable, and the limit no longer destroys completed work.
        from djcode.config import load_config as _load_config

        self.max_tool_rounds = int(_load_config().get("max_tool_rounds", 200))
        self.last_had_thinking = False  # Track if last response had thinking
        self.last_had_tool_calls = False  # Track if last response used native tool calling
        self.last_tool_rounds = 0  # W8: rounds the last turn actually ran
        # W8 (P0-5). Steer text the user typed WHILE this turn is running. It is
        # a plain list and nothing else may touch `self.messages` on its behalf:
        # `steer()` appends here and returns, and the ONLY place the text
        # becomes a message is `_drain_steer()` at the top of the tool-round
        # loop. That separation is the whole safety property -- see the comment
        # on `_drain_steer`.
        self._steer: list[str] = []

    def _emit(self, event: Any) -> None:
        """Publish an event if a front-end attached a bus. Never blocks.

        ``emit_nowait`` is deliberate: a slow subscriber must not backpressure
        the model stream. With no bus this is a no-op and the engine is silent.
        """
        if self.event_bus is not None:
            self.event_bus.emit_nowait(event)

    async def send(self, user_input: str) -> AsyncIterator[str]:
        from djcode.capabilities import capability_context

        # `session_id` is grafted onto the Operator from outside (repl.py,
        # app.py, session_commands.py), so it is read here rather than in
        # __init__ where it is reliably absent. The spill writer (W3-2) uses it
        # to name its per-session directory.
        self.dispatch_ctx.session_id = getattr(self, "session_id", None)
        self.dispatch_ctx.cwd = os.getcwd()
        # W5: one turn = one undo. A turn that edits five files must revert as
        # a single action or the fourth `/undo` leaves the tree half rolled
        # back, so the checkpoint store needs to know where a turn starts. The
        # store is attached by the surface (repl.py); with none attached this
        # is a no-op and every capture falls into one anonymous turn.
        _checkpoints = getattr(self.dispatch_ctx, "checkpoints", None)
        if _checkpoints is not None:
            try:
                _checkpoints.begin_turn(user_input)
            except Exception:  # pragma: no cover - never fail a turn over this
                pass
        with capability_context(self.capabilities), dispatch_context(self.dispatch_ctx):
            try:
                async for token in self._send(user_input):
                    yield token
            except asyncio.CancelledError:
                # Complete protocol bookkeeping without claiming rollback of effects.
                for index in range(len(self.messages) - 1, -1, -1):
                    message = self.messages[index]
                    if message.role == "assistant" and message.tool_calls:
                        answered = {
                            m.tool_call_id for m in self.messages[index + 1 :] if m.role == "tool"
                        }
                        for call in message.tool_calls:
                            if call.get("id") not in answered:
                                self.messages.append(
                                    Message(
                                        role="tool",
                                        tool_call_id=call.get("id"),
                                        name=call.get("function", {}).get("name"),
                                        content="Error: Execution cancelled before a result was "
                                        "available. Inspect state before retrying; effects "
                                        "may have occurred.",
                                    )
                                )
                        break
                if self.on_checkpoint:
                    self.on_checkpoint(self.messages)
                raise

    async def _send(self, user_input: str) -> AsyncIterator[str]:
        """Send a user message and yield streamed response tokens.

        Handles the full tool-calling loop: if the LLM requests tools,
        we execute them and feed results back until the LLM produces
        a final text response.

        Thinking blocks (<think>...</think>) are detected and rendered
        as dimmed verbose output to stderr, not included in the response.
        """
        from djcode.memory.manager import MemoryManager

        memory = MemoryManager()
        recalled = []
        for key, score in memory.search(user_input, top_k=3):
            entry = memory.recall(key)
            if entry:
                recalled.append(f"{key}: {entry}")
        if recalled:
            user_input += (
                "\n\nSaved context (lexical matches; verify relevance):\n"
                + "\n".join(recalled)[:4000]
            )
        pending_images = list(self.capabilities.computer.images)
        self.capabilities.computer.images.clear()
        self.messages.append(Message(role="user", content=user_input, images=pending_images))
        extracted_seen = set()
        native_tools_used = False
        self.last_had_tool_calls = False
        self.last_tool_rounds = 0

        for _round in range(self.max_tool_rounds):
            self.last_tool_rounds = _round + 1
            # W8 (P0-5): the ONE safe drain point for steer text. See
            # `_drain_steer` for why it is here and nowhere else.
            self._drain_steer()
            self.context_manager.replace_messages(self.messages)
            if self.context_manager.needs_compression():
                # W4-3: auto-compaction fires here, without any user command, so
                # this path has to record the compaction entry too — otherwise a
                # resumed session replays the whole transcript instead of the
                # elided view.
                _db = getattr(self, "session_db", None)
                _sid = getattr(self, "session_id", None)
                if _db is not None and _sid:
                    _db.append_messages(_sid, self.messages)
                result = await self.context_manager.auto_compress()
                self.messages = self.context_manager.get_messages()
                if _db is not None and _sid:
                    _db.record_compaction(
                        _sid,
                        summary=getattr(result, "summary_text", "") or "",
                        kept_messages=self.messages,
                        strategy=getattr(getattr(result, "strategy_used", None), "value", ""),
                        messages_removed=getattr(result, "messages_removed", 0),
                    )
            full_response = ""
            tool_calls: list[dict[str, Any]] = []
            thinker = ThinkingStreamProcessor(show_thinking=self.show_thinking)

            from djcode.streaming import stream_turn

            async for text, calls in stream_turn(self.provider, self.messages):
                if text:
                    response_part, thinking_part = thinker.process_token(text)
                    if thinking_part:
                        self._emit(thinking_event(thinking_part))
                    if response_part:
                        full_response += response_part
                        self._emit(token_event(response_part))
                        yield response_part
                if calls:
                    tool_calls.extend(calls)

            # Flush remaining buffer
            remainder, thinking_remainder = thinker.flush()
            if thinking_remainder:
                self._emit(thinking_event(thinking_remainder))
            if remainder:
                full_response += remainder
                self._emit(token_event(remainder))
                yield remainder

            self.last_had_thinking = thinker.had_thinking

            # If there are tool calls, execute them and loop
            if tool_calls:
                native_tools_used = True
                self.last_had_tool_calls = True
                # Record assistant message with tool calls
                self.messages.append(
                    Message(role="assistant", content=full_response, tool_calls=tool_calls)
                )

                denial_notes: list[str] = []
                for tc in tool_calls:
                    func = tc.get("function", {})
                    name = func.get("name", "unknown")
                    args_raw = func.get("arguments", "{}")

                    # Parse arguments
                    if isinstance(args_raw, str):
                        try:
                            args = json.loads(args_raw)
                        except json.JSONDecodeError:
                            args = None
                    else:
                        args = args_raw

                    if not isinstance(args, dict):
                        self.messages.append(
                            Message(
                                role="tool",
                                content="Error: tool arguments must be a JSON object",
                                tool_call_id=tc["id"],
                                name=name,
                            )
                        )
                        continue

                    # Announce the call the moment it is parsed -- before
                    # approval and before execution -- so a UI can show a
                    # pending card while the user decides.
                    self._display_tool_call(name, args, call_id=tc.get("id") or f"call_{name}")

                    decision = await self._approve_tool(name, args)
                    if not decision:
                        # TWO appends, and the order matters. The `role="tool"`
                        # message is mandatory: every tool_call id must be
                        # answered or provider role-alternation breaks -- the
                        # same invariant `send`'s cancellation bookkeeping
                        # protects. The comment rides along here so the model
                        # sees the reason even if it reads no further, and is
                        # ALSO queued as a `user` message flushed after the
                        # whole loop: appending it BETWEEN two tool messages
                        # would produce assistant(tool_calls) -> tool -> user ->
                        # tool, which several providers reject outright.
                        note = decision.comment.strip()
                        self.messages.append(
                            Message(
                                role="tool",
                                content=(
                                    f"Error: User denied tool execution: {note}"
                                    if note
                                    else "Error: User denied tool execution"
                                ),
                                tool_call_id=tc["id"],
                                name=name,
                            )
                        )
                        if note:
                            denial_notes.append(note)
                        continue

                    # Execute tool
                    from djcode.tools.agent_spawn import agent_context

                    with agent_context(self.provider, self.auto_accept, self.approval_callback):
                        result = await self.workflow.one(name, args, dispatch_tool)

                    # Publish the result
                    self._display_tool_result(name, result, call_id=tc.get("id") or f"call_{name}")

                    # Feed result back to LLM. The EVENT above carries the
                    # whole ToolOutcome (that is what W3-3 preserves); the
                    # message carries only the model-facing text, because
                    # Message.content is serialised onto the wire.
                    tool_call_id = tc.get("id", f"call_{name}")
                    self.messages.append(
                        Message(
                            role="tool",
                            content=str(result),
                            tool_call_id=tool_call_id,
                            name=name,
                        )
                    )

                if denial_notes:
                    # "no, use uv not pip" becomes the next instruction rather
                    # than killing the turn (W6-4).
                    self.messages.append(Message(role="user", content="\n".join(denial_notes)))

                images = self.capabilities.computer.images
                if images:
                    self.messages.append(
                        Message(
                            role="user",
                            content="Screenshot from the preceding tool. Inspect it before acting.",
                            images=list(images),
                        )
                    )
                    images.clear()
                if self.on_checkpoint:
                    self.on_checkpoint(self.messages)

                # Continue the loop to get next LLM response
                continue

            # Once this run uses native tools, later code blocks are summaries,
            # not fallback commands. Never execute them a second time.
            if full_response and not self.plan_mode and not native_tools_used:
                from djcode.tool_router import ToolExtractionRouter

                router = ToolExtractionRouter(
                    dispatcher=lambda name, args: self.workflow.one(name, args, dispatch_tool)
                )
                intents = router.extract_intents(full_response)
                pending = []
                for intent in intents:
                    signature = (
                        intent.action,
                        intent.path,
                        intent.content,
                        intent.old_string,
                        intent.new_string,
                    )
                    if signature not in extracted_seen:
                        pending.append(intent)
                        extracted_seen.add(signature)
                if pending:
                    self.messages.append(Message(role="assistant", content=full_response))
                    results = []
                    for intent in pending:
                        # The shape presented for approval must be the shape
                        # that is dispatched. A `bash` intent carries its
                        # command in `intent.content` and has no `path` at all,
                        # so the old `{"path", "content"}` dict handed a rule
                        # matcher no `command` key; and a `mkdir` intent is
                        # dispatched as `bash` with a synthesised `mkdir -p`,
                        # so it was judged under a tool name that never runs.
                        if intent.action in ("bash", "mkdir"):
                            tool_name = "bash"
                            args = {"command": intent.content or f"mkdir -p {intent.path}"}
                        else:
                            tool_name = intent.action
                            args = {"path": intent.path, "content": intent.content}
                        if await self._approve_tool(tool_name, args):
                            results.append(await router._execute_intent(intent))
                    if results:
                        self.last_had_tool_calls = True
                        self.messages.append(
                            Message(role="user", content=router.format_results_for_context(results))
                        )
                        if self.on_checkpoint:
                            self.on_checkpoint(self.messages)
                        continue

            # No tool calls — final response
            # Only mark as no-tool-calls if we never saw any in this entire send()
            if full_response:
                self.messages.append(Message(role="assistant", content=full_response))
            self.context_manager.replace_messages(self.messages)
            if self.on_checkpoint:
                self.on_checkpoint(self.messages)
            break
        else:
            # W1-4: the limit used to raise, throwing away every completed tool
            # round. Instead, tell the model to stop and summarise, run one more
            # non-tool round, and return normally.
            self.messages.append(
                Message(
                    role="user",
                    content=(
                        "<round-limit>You reached the tool-round limit for this turn. "
                        "Summarise what you completed, what remains, and stop calling "
                        "tools.</round-limit>"
                    ),
                )
            )
            summary = ""
            from djcode.streaming import stream_turn as _stream_turn

            async for text, _calls in _stream_turn(self.provider, self.messages):
                if text:
                    summary += text
                    self._emit(token_event(text))
                    yield text
            if summary:
                self.messages.append(Message(role="assistant", content=summary))
            self.context_manager.replace_messages(self.messages)
            if self.on_checkpoint:
                self.on_checkpoint(self.messages)

    @staticmethod
    def _initial_mode(auto_accept: bool):
        """The mode a session starts in. A FLAG must not rewrite the config.

        `djcode --auto-accept` used to call `set_value("auto_accept", True)`, so
        every later interactive session started in auto-accept with no expiry
        and nothing saying so. The flag now selects the mode for THIS process
        only; `permission_mode` in config.json is the default it overrides, and
        the two legacy booleans stay readable for one release.
        """
        from djcode.config import load_config
        from djcode.core.permissions import Mode

        if auto_accept:
            return Mode.AUTO
        config = load_config()
        raw = config.get("permission_mode")
        if isinstance(raw, str):
            try:
                return Mode(raw)
            except ValueError:
                pass
        if config.get("auto_accept") or config.get("auto_approve_tools"):
            return Mode.AUTO
        return Mode.MANUAL

    async def _approve_tool(self, name: str, args: dict[str, Any]):
        """Returns a ``Decision``, not a ``bool`` (W6-4).

        ``Decision.__bool__`` is ``action in {ALLOW, ALWAYS, SESSION}``, so the
        six other call sites that still write ``if not await
        self._approve_tool(...)`` keep the SAME meaning -- and any site that is
        ever missed fails closed on a deny rather than silently allowing it,
        which a plain slotted dataclass (unconditionally truthy) would not.

        The ORDER below is the whole point of the wave: the policy is consulted
        before plan mode and before auto-accept. The old body returned ``True``
        on ``self.auto_accept`` before any rule ran at all. That is F3.
        """
        from djcode.core.permissions import (
            Decision,
            DecisionAction,
            Mode,
            Resolution,
            ToolRequest,
            Verdict,
        )

        request = ToolRequest(tool=name, arguments=dict(args), cwd=os.getcwd())
        verdict = self.permissions.evaluate(request)
        mode = Mode.AUTO if self.auto_accept else self.permissions.mode
        # Keep the engine in step with a runtime toggle (`/auto`, Ctrl+T), so
        # that the chokepoint's own `resolve` -- which has no view of
        # `self.auto_accept` -- reaches the same answer this method does.
        self.permissions.mode = mode

        if verdict is Verdict.HARDLINE_BLOCK:
            return Decision(DecisionAction.DENY, comment=self.permissions.explain(request))
        if self.plan_mode:
            # Plan mode is not a permission verdict, and the old refusal text
            # ("User denied tool execution") lied to the model about who
            # refused and why.
            return Decision(
                DecisionAction.DENY,
                comment=f"Plan mode is on, so '{name}' was not run. Present the plan first.",
            )
        resolution = self.permissions.resolve(verdict, mode, tool=name)
        if resolution is Resolution.ALLOW:
            return Decision(DecisionAction.ALLOW)
        if resolution is Resolution.DENY:
            return Decision(DecisionAction.DENY, comment=self.permissions.explain(request))
        if self.approval_callback:
            answer = await self.approval_callback(name, args)
            if isinstance(answer, Decision):
                if answer.action is DecisionAction.SESSION:
                    self.permissions.grant_session(request, answer.rule)
                elif answer.action is DecisionAction.ALWAYS:
                    self.permissions.grant_always(request, answer.rule)
                return answer
            return Decision(
                DecisionAction.ALLOW if answer else DecisionAction.DENY,
                rule=self.permissions.rule_for(request),
            )
        if not sys.stdin.isatty():
            raise PermissionError(
                "Tool execution needs approval; use --auto-accept for an authorized unattended "
                "task."
            )
        # No approval_callback and no policy permit means DENY. The engine does
        # not prompt -- a front-end owns every interaction with the user. An
        # engine that prompts cannot be driven by a GUI, a test, or a CI run.
        return Decision(DecisionAction.DENY)

    async def _approve_repeated_call(self, name: str, args: dict[str, Any], reason: str) -> bool:
        """Approve a call the doom-loop breaker stopped (P1-9, W3-5).

        Deliberately does NOT honour ``auto_accept``. An unattended run is
        precisely where a model repeating one byte-identical tool call burns
        real money with nobody watching, so the breaker is the one gate
        ``--auto-accept`` does not open. With no front-end callback attached the
        answer is no; the engine never prompts on its own.
        """
        if self.approval_callback is not None:
            return await self.approval_callback(name, args)
        return False

    def _display_tool_call(self, name: str, args: dict, *, call_id: str = "") -> None:
        """Announce a tool call as an event. Rendering belongs to the front-end.

        Fired the moment the call is parsed out of the stream -- before approval
        and before execution -- so a UI can show a pending card while the user
        is still deciding. The display name and argument formatting that used to
        live here now live in ``djcode.frontends.repl.render``.
        """
        self._emit(tool_call_event(name, args, call_id))

    def _display_tool_result(self, name: str, result: Any, *, call_id: str = "") -> None:
        """Publish a tool result as an event."""
        self._emit(tool_result_event(call_id, result, name=name))

    # ── Steering (W8, P0-5) ────────────────────────────────────────────────

    def steer(self, text: str) -> None:
        """Queue a correction for the running turn. Appends to a list, nothing else.

        This method must never touch ``self.messages``. It is called from a
        front-end's input handler on the SAME event loop the tool round is
        running on, which means it fires while ``_send`` is parked on one of its
        two long awaits -- ``_approve_tool`` (the approval prompt, the single
        most likely moment a human types a correction) or ``workflow.one``
        (which on a Rust-equipped box is a DAF subprocess that can run for an
        hour). Appending a ``user`` message from here would land it *between*
        an ``assistant(tool_calls)`` message and the ``tool`` messages that
        answer it, which OpenAI and Anthropic reject outright and which Ollama
        -- whose pairing is positional, ``provider.py::_msg_to_ollama`` never
        sends ``tool_call_id`` -- silently mis-associates instead.
        """
        text = str(text or "").strip()
        if text:
            self._steer.append(text)

    @property
    def pending_steer(self) -> list[str]:
        """Steer text typed but not yet delivered to the model."""
        return list(self._steer)

    def take_steer(self) -> list[str]:
        """Remove and return undelivered steer text. Idempotent: twice gives []."""
        pending, self._steer = self._steer, []
        return pending

    def _drain_steer(self) -> None:
        """Turn queued steer text into a ``user`` message. The one safe point.

        Called as the first statement of the tool-round loop body, before
        ``context_manager.replace_messages``. Four properties hold here and
        nowhere else:

        1. **Every path returns here.** The ``if tool_calls:`` branch, the
           extraction-router branch and the first round of a turn all re-enter
           the loop at this line. Draining after the per-call loop instead would
           strand a steer for as long as the model happens not to call a tool --
           which, when the user is steering *because* the model is going wrong,
           can be forever.
        2. **Pairing is intact.** Whatever the previous round did, every
           ``tool_call`` id it opened has already been answered by a ``tool``
           message, and W6's ``denial_notes`` has already been flushed as its
           own ``user`` message. ``assistant(tool_calls) -> tool -> tool ->
           user`` is a shape this codebase already ships.
        3. **It is inside the token count.** Line ``replace_messages`` +
           ``needs_compression`` runs immediately after, so the round that
           carries the steer is budgeted with it.
        4. **Compaction stays safe.** ``context/compressor.py::_partition``
           attaches a ``tool`` message to the previous group only when that
           group's FIRST message has ``tool_calls``. A ``user`` message drained
           here opens its own group and can never split an assistant/tool pair;
           one injected mid-round permanently would.

        There is no ``await`` between the pop and the append, so a cancellation
        racing the drain can neither lose the text nor deliver it twice.
        """
        if not self._steer:
            return
        pending, self._steer = self._steer, []
        text = "\n\n".join(pending)
        last = self.messages[-1] if self.messages else None
        if last is not None and last.role == "user":
            # Never emit two consecutive `user` messages: round 0 would produce
            # user(prompt) -> user(steer), and after a denial note the previous
            # message is already a `user`. Merging keeps the transcript in a
            # shape every provider adapter in `providers/` already handles.
            last.content = f"{last.content}\n\n{text}" if last.content else text
        else:
            self.messages.append(Message(role="user", content=text))
        self._emit(steer_event(text, delivered=True))

    def reset(self) -> None:
        """Clear conversation history, keeping system prompt."""
        system = self.messages[0] if self.messages else None
        self.messages.clear()
        if system:
            self.messages.append(system)
