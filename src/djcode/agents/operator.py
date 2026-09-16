"""Operator agent — the main execution agent that uses tools to complete tasks.

This is the primary agent that receives user messages, reasons about them,
calls tools, and produces results. It manages the tool-calling loop.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from djcode.core.events import (
    EventBus,
    thinking_event,
    token_event,
    tool_call_event,
    tool_result_event,
)
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

    def _emit(self, event: Any) -> None:
        """Publish an event if a front-end attached a bus. Never blocks.

        ``emit_nowait`` is deliberate: a slow subscriber must not backpressure
        the model stream. With no bus this is a no-op and the engine is silent.
        """
        if self.event_bus is not None:
            self.event_bus.emit_nowait(event)

    async def send(self, user_input: str) -> AsyncIterator[str]:
        from djcode.capabilities import capability_context

        with capability_context(self.capabilities):
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

        for _round in range(self.max_tool_rounds):
            self.context_manager.replace_messages(self.messages)
            if self.context_manager.needs_compression():
                await self.context_manager.auto_compress()
                self.messages = self.context_manager.get_messages()
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

                    if not await self._approve_tool(name, args):
                        self.messages.append(
                            Message(
                                role="tool",
                                content="Error: User denied tool execution",
                                tool_call_id=tc["id"],
                                name=name,
                            )
                        )
                        continue

                    # Execute tool
                    from djcode.tools.agent_spawn import agent_context

                    with agent_context(self.provider, self.auto_accept, self.approval_callback):
                        result = await self.workflow.one(name, args, dispatch_tool)

                    # Publish the result
                    self._display_tool_result(name, result, call_id=tc.get("id") or f"call_{name}")

                    # Feed result back to LLM
                    tool_call_id = tc.get("id", f"call_{name}")
                    self.messages.append(
                        Message(
                            role="tool",
                            content=result,
                            tool_call_id=tool_call_id,
                            name=name,
                        )
                    )

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
                        args = {"path": intent.path, "content": intent.content}
                        if await self._approve_tool(intent.action, args):
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

    async def _approve_tool(self, name: str, args: dict[str, Any]) -> bool:
        if self.plan_mode:
            return False
        if self.auto_accept:
            return True
        if self.approval_callback:
            return await self.approval_callback(name, args)
        if not sys.stdin.isatty():
            raise PermissionError(
                "Tool execution needs approval; use --auto-accept for an authorized unattended "
                "task."
            )
        # No approval_callback and no policy permit means DENY. The engine does
        # not prompt -- a front-end owns every interaction with the user. An
        # engine that prompts cannot be driven by a GUI, a test, or a CI run.
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

    def reset(self) -> None:
        """Clear conversation history, keeping system prompt."""
        system = self.messages[0] if self.messages else None
        self.messages.clear()
        if system:
            self.messages.append(system)
