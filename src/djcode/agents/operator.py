"""Operator agent — the main execution agent that uses tools to complete tasks.

This is the primary agent that receives user messages, reasons about them,
calls tools, and produces results. It manages the tool-calling loop.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import sys
from typing import Any, AsyncIterator, Awaitable, Callable

import questionary
from rich.console import Console
from rich.panel import Panel

from djcode.provider import Message, Provider
from djcode.prompt import build_system_prompt
from djcode.tools import dispatch_tool

console = Console()

# ── Thinking block detection ───────────────────────────────────────────────
# Models like qwen3, deepseek, gemma4 emit <think>...</think> tags.
# We detect these and render them as dimmed verbose thinking output,
# separate from the actual response — like Claude Code's thinking blocks.

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

# Dimmed styling for thinking output
THINK_PREFIX = "\033[2m\033[3m"   # dim + italic
THINK_RESET = "\033[0m"
THINK_LABEL = "\033[2m\033[33m"   # dim yellow


class ThinkingStreamProcessor:
    """Processes a stream of tokens, separating thinking from response.

    Detects <think>...</think> blocks and renders them as dimmed output.
    Everything outside thinking blocks is yielded as normal response text.
    """

    def __init__(self, show_thinking: bool = True, raw: bool = False) -> None:
        self.show_thinking = show_thinking
        self.raw = raw
        self._in_think = False
        self._buffer = ""
        self._think_started = False  # Track if we printed the thinking header
        self._response_text = ""     # Accumulated non-thinking response

    def process_token(self, token: str) -> str | None:
        """Handle tags even when a complete thinking block arrives in one chunk."""
        self._buffer += token
        output = []
        while self._buffer:
            marker = THINK_CLOSE if self._in_think else THINK_OPEN
            index = self._buffer.find(marker)
            if index >= 0:
                text, self._buffer = self._buffer[:index], self._buffer[index + len(marker):]
                if not self._in_think:
                    output.append(text)
                    self._think_started = True
                elif self.show_thinking and not self.raw:
                    sys.stderr.write(f"{THINK_PREFIX}{text}{THINK_RESET}")
                self._in_think = not self._in_think
                continue
            hold = max((n for n in range(1, len(marker)) if self._buffer.endswith(marker[:n])), default=0)
            safe = self._buffer[:-hold] if hold else self._buffer
            self._buffer = self._buffer[-hold:] if hold else ""
            if not self._in_think:
                output.append(safe)
            elif self.show_thinking and not self.raw:
                sys.stderr.write(f"{THINK_PREFIX}{safe}{THINK_RESET}")
            break
        result = "".join(output)
        self._response_text += result
        return result or None

    def flush(self) -> str | None:
        """Flush any remaining buffer content."""
        if self._buffer:
            if self._in_think:
                # Unclosed thinking block — print it
                if self.show_thinking and not self.raw:
                    sys.stderr.write(f"{THINK_PREFIX}  {self._buffer}{THINK_RESET}\n")
                    sys.stderr.flush()
                self._buffer = ""
                return None
            result = self._buffer
            self._buffer = ""
            self._response_text += result
            return result
        return None

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
        raw: bool = False,
        model: str = "",
        auto_accept: bool = False,
        show_thinking: bool = True,
        approval_callback: Callable[[str, dict[str, Any]], Awaitable[bool]] | None = None,
    ) -> None:
        self.provider = provider
        self.bypass_rlhf = bypass_rlhf
        self.raw = raw
        self.auto_accept = auto_accept
        self.show_thinking = show_thinking
        self.approval_callback = approval_callback
        self.plan_mode = False
        self.on_checkpoint = None
        from djcode.context.manager import ContextWindowManager
        self.context_manager = ContextWindowManager(model=provider.config.model, provider=provider)
        self.messages: list[Message] = [
            Message(role="system", content=build_system_prompt(
                bypass_rlhf=bypass_rlhf, model=model or provider.config.model
            ))
        ]
        self.max_tool_rounds = 20  # Safety limit on tool-calling loops
        self.last_had_thinking = False  # Track if last response had thinking
        self.last_had_tool_calls = False  # Track if last response used native tool calling

    async def send(self, user_input: str) -> AsyncIterator[str]:
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
            user_input += "\n\nSaved context (lexical matches; verify relevance):\n" + "\n".join(recalled)[:4000]
        self.messages.append(Message(role="user", content=user_input))
        extracted_seen = set()
        self.last_had_tool_calls = False

        for _round in range(self.max_tool_rounds):
            self.context_manager.replace_messages(self.messages)
            if self.context_manager.needs_compression():
                await self.context_manager.auto_compress()
                self.messages = self.context_manager.get_messages()
            full_response = ""
            tool_calls: list[dict[str, Any]] = []
            thinker = ThinkingStreamProcessor(
                show_thinking=self.show_thinking, raw=self.raw
            )

            from djcode.streaming import stream_turn
            async for text, calls in stream_turn(self.provider, self.messages):
                if text:
                    response_part = thinker.process_token(text)
                    if response_part:
                        full_response += response_part
                        yield response_part
                if calls:
                    tool_calls.extend(calls)

            # Flush remaining buffer
            remainder = thinker.flush()
            if remainder:
                full_response += remainder
                yield remainder

            self.last_had_thinking = thinker.had_thinking

            # If there are tool calls, execute them and loop
            if tool_calls:
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
                        self.messages.append(Message(role="tool", content="Error: tool arguments must be a JSON object", tool_call_id=tc["id"], name=name))
                        continue

                    # Display tool call
                    if not self.raw:
                        self._display_tool_call(name, args)

                    if not await self._approve_tool(name, args):
                        self.messages.append(Message(role="tool", content="Error: User denied tool execution", tool_call_id=tc["id"], name=name))
                        continue

                    # Execute tool
                    from djcode.tools.agent_spawn import agent_context
                    with agent_context(self.provider, self.auto_accept, self.approval_callback):
                        result = await dispatch_tool(name, args)

                    # Display result
                    if not self.raw:
                        self._display_tool_result(name, result)

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

                if self.on_checkpoint:
                    self.on_checkpoint(self.messages)

                # Continue the loop to get next LLM response
                continue

            # Text-only models still receive execution results, rather than a UI-only side effect.
            if full_response and not self.plan_mode:
                from djcode.tool_router import ToolExtractionRouter
                router = ToolExtractionRouter()
                intents = router.extract_intents(full_response)
                pending = []
                for intent in intents:
                    signature = (intent.action, intent.path, intent.content, intent.old_string, intent.new_string)
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
                        self.messages.append(Message(role="user", content=router.format_results_for_context(results)))
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
            raise RuntimeError(f"Tool round limit ({self.max_tool_rounds}) reached; task is incomplete.")

    async def _approve_tool(self, name: str, args: dict[str, Any]) -> bool:
        if self.plan_mode:
            return False
        if self.auto_accept:
            return True
        if self.approval_callback:
            return await self.approval_callback(name, args)
        if not sys.stdin.isatty():
            raise PermissionError("Tool execution needs approval; use --auto-accept for an authorized unattended task.")
        console.print(Panel(f"Tool: {name}\n{json.dumps(args, indent=2)[:1000]}", title="Approve tool"))
        return bool(await asyncio.to_thread(lambda: questionary.confirm("Execute this tool?", default=False).ask()))

    async def _stream_ollama(self) -> AsyncIterator[tuple[str, list[dict]]]:
        """Stream from Ollama, yielding (text_chunk, tool_calls)."""
        tool_calls: list[dict[str, Any]] = []

        async for chunk in self.provider.chat_ollama(self.messages, stream=True):
            msg = chunk.get("message", {})

            # Text content
            content = msg.get("content", "")
            if content:
                yield (content, [])

            # Tool calls
            if "tool_calls" in msg:
                for tc in msg["tool_calls"]:
                    tool_calls.append(tc)

            # End of stream
            if chunk.get("done", False):
                if tool_calls:
                    yield ("", tool_calls)
                break

    async def _stream_openai(self) -> AsyncIterator[tuple[str, list[dict]]]:
        """Stream from OpenAI-compatible endpoint."""
        tool_calls_acc: dict[int, dict] = {}

        async for chunk in self.provider.chat_openai_compat(self.messages, stream=True):
            choices = chunk.get("choices", [])
            if not choices:
                continue

            delta = choices[0].get("delta", {})

            # Text content
            content = delta.get("content", "")
            if content:
                yield (content, [])

            # Tool calls (streamed incrementally)
            if "tool_calls" in delta:
                for tc_delta in delta["tool_calls"]:
                    idx = tc_delta.get("index", 0)
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {
                            "id": tc_delta.get("id", ""),
                            "function": {"name": "", "arguments": ""},
                        }
                    if "function" in tc_delta:
                        fn = tc_delta["function"]
                        if "name" in fn:
                            tool_calls_acc[idx]["function"]["name"] = fn["name"]
                        if "arguments" in fn:
                            tool_calls_acc[idx]["function"]["arguments"] += fn["arguments"]

            # Finish reason
            finish = choices[0].get("finish_reason", "")
            if finish == "tool_calls" and tool_calls_acc:
                yield ("", list(tool_calls_acc.values()))
            elif finish == "stop":
                break

    # ── Clean tool display (Claude Code style) ─────────────────────────

    # Canonical display names for tools
    TOOL_DISPLAY: dict[str, str] = {
        "bash": "Bash",
        "file_read": "Read",
        "file_write": "Write",
        "file_edit": "Edit",
        "grep": "Grep",
        "glob": "Glob",
        "git": "Git",
        "web_fetch": "WebFetch",
    }

    def _display_tool_call(self, name: str, args: dict) -> None:
        """Render a tool call as a clean one-liner: ⏺ ToolName(arg)"""
        display_name = self.TOOL_DISPLAY.get(name, name.title())

        if name == "bash":
            cmd = args.get("command", "")
            arg_display = cmd if len(cmd) < 72 else cmd[:69] + "..."
        elif name in ("file_read", "file_write", "file_edit"):
            arg_display = args.get("path", "")
        elif name == "grep":
            pattern = args.get("pattern", "")
            path = args.get("path", ".")
            arg_display = f'"{pattern}" in {path}'
        elif name == "glob":
            arg_display = args.get("pattern", "")
        elif name == "git":
            sub = args.get("subcommand", "")
            git_args = args.get("args", "")
            arg_display = f"{sub} {git_args}".strip()
        else:
            # Generic: show first meaningful arg value
            vals = [str(v) for v in args.values() if v]
            arg_display = vals[0][:72] if vals else ""

        console.print(f"[#FFD700]\u23fa[/] [bold white]{display_name}[/][dim]({arg_display})[/]")

    def _display_tool_result(self, name: str, result: str) -> None:
        """Render a tool result — indented dim summary."""
        lines = result.strip().splitlines()
        if not lines:
            return

        total = len(lines)

        # Check for errors
        first = lines[0].strip().lower()
        if first.startswith("error") or first.startswith("traceback"):
            # Show error in red
            err_msg = lines[0][:120]
            console.print(f"  [dim]\u21b3[/] [red]Error: {err_msg}[/]")
            return

        # Short output: just show line count
        if total <= 3:
            for line in lines:
                truncated = line[:120] + "..." if len(line) > 120 else line
                console.print(f"  [dim]  {truncated}[/]")
        else:
            # Show first 3 lines + summary
            for line in lines[:3]:
                truncated = line[:120] + "..." if len(line) > 120 else line
                console.print(f"  [dim]  {truncated}[/]")
            remaining = total - 3
            console.print(f"  [dim]  ... ({remaining} more lines)[/]")

    def reset(self) -> None:
        """Clear conversation history, keeping system prompt."""
        system = self.messages[0] if self.messages else None
        self.messages.clear()
        if system:
            self.messages.append(system)
