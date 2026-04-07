"""Operator agent — the main execution agent that uses tools to complete tasks.

This is the primary agent that receives user messages, reasons about them,
calls tools, and produces results. It manages the tool-calling loop.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import sys
from typing import Any, AsyncIterator

import questionary
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

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
        """Process a single streamed token.

        Returns the text to yield as response (or None if it's thinking content).
        Side effect: prints thinking output directly to stderr if show_thinking.
        """
        self._buffer += token

        # Check for think open tag
        if not self._in_think:
            if THINK_OPEN in self._buffer:
                # Split: everything before the tag is response, after is thinking
                before, _, after = self._buffer.partition(THINK_OPEN)
                self._buffer = after
                self._in_think = True

                # Print thinking header
                if self.show_thinking and not self.raw:
                    if not self._think_started:
                        sys.stderr.write(f"\n{THINK_LABEL}  \u2728 thinking...{THINK_RESET}\n")
                        self._think_started = True
                    # Flush any buffered thinking content
                    if self._buffer:
                        sys.stderr.write(f"{THINK_PREFIX}  {self._buffer}{THINK_RESET}")
                        sys.stderr.flush()
                        self._buffer = ""

                if before.strip():
                    self._response_text += before
                    return before
                return None

            # Check for partial tag at end of buffer (could be start of <think>)
            for i in range(1, len(THINK_OPEN)):
                if self._buffer.endswith(THINK_OPEN[:i]):
                    # Hold back the potential partial tag
                    safe = self._buffer[:-i]
                    self._buffer = self._buffer[-i:]
                    if safe:
                        self._response_text += safe
                        return safe
                    return None

            # No tag detected — flush buffer as response
            result = self._buffer
            self._buffer = ""
            if result:
                self._response_text += result
                return result
            return None

        # Inside thinking block
        if THINK_CLOSE in self._buffer:
            # End of thinking
            before, _, after = self._buffer.partition(THINK_CLOSE)
            self._buffer = ""
            self._in_think = False

            # Print remaining thinking content
            if self.show_thinking and not self.raw and before:
                sys.stderr.write(f"{THINK_PREFIX}  {before}{THINK_RESET}\n")
            if self.show_thinking and not self.raw:
                sys.stderr.write(f"{THINK_LABEL}  \u2728 done thinking{THINK_RESET}\n\n")
                sys.stderr.flush()

            # Anything after </think> is response
            if after.strip():
                self._response_text += after
                return after
            return None

        # Check for partial </think> at end of buffer — hold it back
        for i in range(1, len(THINK_CLOSE)):
            if self._buffer.endswith(THINK_CLOSE[:i]):
                safe = self._buffer[:-i]
                self._buffer = self._buffer[-i:]
                if self.show_thinking and not self.raw and safe:
                    sys.stderr.write(f"{THINK_PREFIX}  {safe}{THINK_RESET}")
                    sys.stderr.flush()
                return None

        # Still inside thinking — print and consume
        if self.show_thinking and not self.raw and self._buffer:
            sys.stderr.write(f"{THINK_PREFIX}  {self._buffer}{THINK_RESET}")
            sys.stderr.flush()
        self._buffer = ""
        return None

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
    ) -> None:
        self.provider = provider
        self.bypass_rlhf = bypass_rlhf
        self.raw = raw
        self.auto_accept = auto_accept
        self.show_thinking = show_thinking
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
        self.messages.append(Message(role="user", content=user_input))

        for _round in range(self.max_tool_rounds):
            full_response = ""
            tool_calls: list[dict[str, Any]] = []
            thinker = ThinkingStreamProcessor(
                show_thinking=self.show_thinking, raw=self.raw
            )

            # Stream the response
            if self.provider.is_ollama:
                async for chunk in self._stream_ollama():
                    text, calls = chunk
                    if text:
                        response_part = thinker.process_token(text)
                        if response_part:
                            full_response += response_part
                            yield response_part
                    if calls:
                        tool_calls.extend(calls)
            else:
                async for chunk in self._stream_openai():
                    text, calls = chunk
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
                            args = {"command": args_raw} if name == "bash" else {}
                    else:
                        args = args_raw

                    # Display tool call
                    if not self.raw:
                        self._display_tool_call(name, args)

                    # Tool confirmation gate — require user approval unless auto_accept
                    if not self.auto_accept:
                        console.print(Panel(
                            f"[bold]Tool:[/] {name}\n[bold]Args:[/] {json.dumps(args, indent=2)[:500]}",
                            title="[yellow]Tool Call[/]",
                            border_style="yellow",
                        ))
                        # Run questionary in thread to avoid asyncio.run() conflict
                        with concurrent.futures.ThreadPoolExecutor() as pool:
                            confirm = await asyncio.get_event_loop().run_in_executor(
                                pool,
                                lambda: questionary.confirm(
                                    "Execute this tool?", default=True
                                ).ask(),
                            )
                        if not confirm:
                            self.messages.append(
                                Message(
                                    role="tool",
                                    content="Error: User denied tool execution",
                                    tool_call_id=tc.get("id", f"call_{name}"),
                                    name=name,
                                )
                            )
                            continue

                    # Execute tool
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

                # Continue the loop to get next LLM response
                continue

            # No tool calls — final response
            # Only mark as no-tool-calls if we never saw any in this entire send()
            if not tool_calls and _round == 0:
                self.last_had_tool_calls = False
            if full_response:
                self.messages.append(Message(role="assistant", content=full_response))
            break

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

    # Tool icons for compact display
    TOOL_ICONS: dict[str, str] = {
        "bash": "\u2699",       # gear
        "file_read": "\U0001f4c4",   # page
        "file_write": "\u270f",  # pencil
        "file_edit": "\u2702",   # scissors
        "grep": "\U0001f50d",    # magnifier
        "glob": "\U0001f4c2",    # folder
        "git": "\U0001f500",     # shuffle arrows
        "web_fetch": "\U0001f310",  # globe
    }

    def _display_tool_call(self, name: str, args: dict) -> None:
        """Render a tool call as a compact one-line indicator."""
        icon = self.TOOL_ICONS.get(name, "\u26a1")

        if name == "bash":
            cmd = args.get("command", "")
            # Show command inline, truncated
            display = cmd if len(cmd) < 80 else cmd[:77] + "..."
            console.print(f"  {icon} [bold yellow]{name}[/] [dim]{display}[/]")
        elif name in ("file_read", "file_write", "file_edit"):
            path = args.get("path", "")
            console.print(f"  {icon} [bold cyan]{name}[/] [white]{path}[/]")
        elif name == "grep":
            pattern = args.get("pattern", "")
            path = args.get("path", ".")
            console.print(f"  {icon} [bold cyan]grep[/] [white]{pattern}[/] [dim]in {path}[/]")
        elif name == "glob":
            pattern = args.get("pattern", "")
            console.print(f"  {icon} [bold cyan]glob[/] [white]{pattern}[/]")
        elif name == "git":
            sub = args.get("subcommand", "")
            git_args = args.get("args", "")
            console.print(f"  {icon} [bold cyan]git {sub}[/] [dim]{git_args}[/]")
        else:
            brief = json.dumps(args)[:100]
            console.print(f"  {icon} [bold cyan]{name}[/] [dim]{brief}[/]")

    def _display_tool_result(self, name: str, result: str) -> None:
        """Render a tool result — compact, dimmed, truncated."""
        lines = result.strip().splitlines()
        if not lines:
            return

        # Show first few lines dimmed and indented
        max_show = 8
        for line in lines[:max_show]:
            truncated = line[:120] + "..." if len(line) > 120 else line
            console.print(f"    [dim]{truncated}[/]")

        remaining = len(lines) - max_show
        if remaining > 0:
            console.print(f"    [dim italic]... {remaining} more lines[/]")

    def reset(self) -> None:
        """Clear conversation history, keeping system prompt."""
        system = self.messages[0] if self.messages else None
        self.messages.clear()
        if system:
            self.messages.append(system)
