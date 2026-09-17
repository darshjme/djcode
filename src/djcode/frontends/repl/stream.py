"""Split a token stream into blocks that are safe to render, and a tail that is not.

The REPL writes the model's answer to ``sys.stdout`` one token at a time, raw.
So a reply containing::

    ## Plan

    1. **Read** `operator.py`
    2. Run:

    ```python
    print("hi")
    ```

arrives on screen as exactly those characters -- hashes, asterisks, backticks
and all. Every other surface DJcode has renders markdown; the one people
actually use does not.

The obvious fix -- buffer the whole answer and render it at the end -- trades a
real problem for a worse one: nothing appears until the model stops talking.
The fix that works is to decide, continuously, which prefix of the answer can
never change again, render that as markdown, and hold the rest back.

**What "can never change" means here.** A markdown block is only safe to render
once it is closed, because rendering half of one produces something that is not
merely ugly but *wrong*:

* an unclosed ``` fence renders as a paragraph of code text, and then the
  closing fence arrives and the same lines want to be a syntax-highlighted
  block -- but they have already been committed to scrollback;
* a table without its separator row renders as a paragraph of pipes;
* a list split in the middle renders as two lists, renumbered from 1.

So this class holds back fences until they close, tables until they end, and
lists until a blank line ends them. Everything else flushes at a blank line,
which is markdown's own block separator and therefore exactly the right seam.

**The long-paragraph escape hatch.** A single 900-word paragraph has no blank
line in it, and holding it back to the end would reintroduce the very problem
this class exists to avoid. So a paragraph that has grown past
:attr:`SOFT_FLUSH_CHARS` is allowed to flush at a sentence boundary. Prose is
the only thing that gets this: inside a fence, a table or a list, a sentence
end means nothing structural and flushing there would break the block.

This module is pure. It has no console, no timers and no terminal; it turns
``str`` into ``list[str]``. That is what makes it testable, and it is why the
composer can adopt it later without changing a line of it.
"""

from __future__ import annotations

import re

__all__ = ["StreamSplitter"]

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_TABLE_ROW = re.compile(r"^\s{0,3}\|")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
#: A sentence end followed by a space. Kept deliberately dumb -- a false
#: positive costs one extra paragraph break, never a corrupted block.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


class StreamSplitter:
    """Feed tokens, get back the part of the answer that is safe to render.

    ``feed`` returns zero or more complete markdown blocks. ``tail`` is
    whatever is still in flight. ``finish`` returns the rest, closed or not --
    a stream can end mid-fence if the model is cut off, and the user still has
    to see what arrived.
    """

    #: A paragraph longer than this may flush at a sentence boundary rather
    #: than waiting for a blank line.
    SOFT_FLUSH_CHARS = 280

    def __init__(self, soft_flush_chars: int | None = None) -> None:
        self._buffer = ""
        self.soft_flush_chars = (
            self.SOFT_FLUSH_CHARS if soft_flush_chars is None else soft_flush_chars
        )

    # -- state ------------------------------------------------------------

    @property
    def tail(self) -> str:
        """What is held back right now."""
        return self._buffer

    @property
    def in_fence(self) -> bool:
        """True while a ``` block is open, so nothing may flush.

        Computed from the buffer, not remembered. That is sound because of one
        invariant: a fence is only ever consumed WHOLE, so the buffer always
        begins outside any fence. An earlier version cached the fence marker on
        the instance and it went wrong in both directions -- stale True after a
        block was emitted, and a committed marker turning the opener line into
        its own closer on the next call.
        """
        return self._open_fence() is not None

    # -- the stream -------------------------------------------------------

    def feed(self, text: str) -> list[str]:
        """Add text; return the blocks that are now safe to render."""
        if not text:
            return []
        self._buffer += text
        return self._drain()

    def finish(self) -> list[str]:
        """End of stream. Return whatever is left, complete or not.

        A stream can end mid-fence -- the model was cut off, the connection
        dropped -- and the user still has to see what did arrive.
        """
        blocks = self._drain()
        remainder = self._buffer.strip("\n")
        self._buffer = ""
        if remainder:
            blocks.append(remainder)
        return blocks

    # -- internals --------------------------------------------------------

    def _drain(self) -> list[str]:
        blocks: list[str] = []
        while True:
            block = self._take_block()
            if block is None:
                break
            if block.strip():
                blocks.append(block)
        soft = self._soft_flush()
        if soft:
            blocks.append(soft)
        return blocks

    def _open_fence(self) -> str | None:
        """The marker of a fence left open by the buffer's COMPLETE lines.

        The trailing partial line is excluded: "``" is not a fence until the
        newline proves it was not going to be "```".
        """
        fence: str | None = None
        lines = self._buffer.split("\n")
        for line in lines[:-1]:
            match = _FENCE.match(line)
            if not match:
                continue
            if fence is None:
                fence = match.group(1)
            elif line.strip()[0] == fence[0] and len(match.group(1)) >= len(fence):
                fence = None
        return fence

    def _take_block(self) -> str | None:
        """The longest prefix of the buffer that is a finished markdown block.

        Returns None when nothing is safe to emit yet. Only ever consumes whole
        lines: a partial line could still turn out to be a fence opener, a
        table row or a list item, and deciding before the newline arrives is
        how a renderer corrupts a block.
        """
        newline = self._buffer.find("\n")
        if newline == -1:
            return None

        consumed = 0
        # The buffer always begins outside a fence (a fence is only consumed
        # whole), so the scan starts from a known state every time and nothing
        # has to be remembered between calls.
        fence: str | None = None
        lines = self._buffer.split("\n")
        # The final element is a partial line unless the buffer ends in \n; it
        # is never eligible, so stop before it.
        for index, line in enumerate(lines[:-1]):
            before = consumed
            consumed += len(line) + 1
            match = _FENCE.match(line)
            if fence is not None:
                # Inside a fence, the ONLY thing that matters is the closer.
                if (
                    match
                    and line.strip()[0] == fence[0]
                    and len(match.group(1)) >= len(fence)
                ):
                    return self._consume(consumed)
                continue
            if match:
                fence = match.group(1)
                continue
            if line.strip() == "":
                # A blank line outside a fence is markdown's block separator,
                # and therefore the one seam that is always safe.
                return self._consume(consumed)
            if _HEADING.match(line):
                # A heading is its own block, so the paragraph after it is not
                # swallowed into it -- and a heading that starts a new block
                # ends the previous one.
                return self._consume(consumed if index == 0 else before)
        return None

    def _consume(self, count: int) -> str | None:
        if count <= 0:
            return None
        block = self._buffer[:count]
        self._buffer = self._buffer[count:]
        return block.strip("\n")

    def _soft_flush(self) -> str | None:
        """Let a long plain paragraph out at a sentence boundary.

        Refused inside a fence, a table or a list, where a full stop carries no
        structural meaning and cutting there would split the block.
        """
        if self.in_fence or len(self._buffer) <= self.soft_flush_chars:
            return None
        lines = self._buffer.split("\n")
        for line in lines:
            # `in_fence` above only reports COMMITTED state. An opener still
            # sitting unconsumed in the buffer has to veto the soft flush too,
            # or a long comment inside a code block would be cut in half.
            if (
                _FENCE.match(line)
                or _TABLE_ROW.match(line)
                or _LIST_ITEM.match(line)
                or _HEADING.match(line)
            ):
                return None
        # Split at the LAST completed sentence boundary. Whatever follows it is
        # still being written, so it stays in the buffer; when the boundary is
        # the end of the buffer, the whole paragraph so far is complete and all
        # of it goes.
        matches = list(_SENTENCE_END.finditer(self._buffer))
        if not matches:
            return None
        cut = matches[-1].end()
        block = self._buffer[:cut].strip()
        self._buffer = self._buffer[cut:]
        return block or None
