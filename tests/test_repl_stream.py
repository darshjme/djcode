"""The stream splitter: what may be rendered, and the promise that nothing is lost.

The REPL used to `sys.stdout.write(token)` the model's answer one raw token at
a time, so `## Plan`, `**bold**` and ``` fences reached the user as literal
punctuation. Rendering markdown instead means deciding, mid-stream, which
prefix of the reply can never change again -- and getting that wrong is worse
than not rendering at all, because a half-rendered fence is committed to
scrollback before the closing fence arrives to say what those lines were.

The most important test in this file is
`test_no_token_is_ever_lost`: whatever else the splitter does, every character
the model produced has to reach the screen.
"""

from __future__ import annotations

import pytest

from djcode.frontends.repl.stream import StreamSplitter

FENCED = """Here is the plan.

```python
def f():
    # a comment with a full stop. And another sentence.

    return 1
```

Done."""

TABLE = """Results:

| tool | ok |
|------|----|
| bash | y  |

That is all."""


def _stream(text: str, chunk: int = 1) -> list[str]:
    """Feed `text` in `chunk`-sized pieces, as a provider would."""
    splitter = StreamSplitter()
    blocks: list[str] = []
    for i in range(0, len(text), chunk):
        blocks.extend(splitter.feed(text[i : i + chunk]))
    blocks.extend(splitter.finish())
    return blocks


# -- the invariant that matters most ---------------------------------------


@pytest.mark.parametrize("text", [FENCED, TABLE, "one\n\ntwo\n\nthree", "no newline at all"])
@pytest.mark.parametrize("chunk", [1, 3, 17, 10_000])
def test_no_token_is_ever_lost(text, chunk):
    """Blank lines are block separators and may be normalised away; every other
    character has to come out."""
    joined = "\n".join(_stream(text, chunk))
    assert "".join(joined.split()) == "".join(text.split())


@pytest.mark.parametrize("chunk", [1, 2, 5, 50])
def test_chunking_does_not_change_the_blocks(chunk):
    """A provider's chunk boundaries are arbitrary. They must not be visible in
    the output."""
    assert _stream(FENCED, chunk) == _stream(FENCED, 10_000)


# -- fences ----------------------------------------------------------------


def test_a_fence_is_held_back_until_it_closes():
    splitter = StreamSplitter()
    blocks = splitter.feed("```python\nprint(1)\n")
    assert blocks == [], "an open fence must never be rendered"
    assert splitter.in_fence
    blocks = splitter.feed("```\n")
    assert len(blocks) == 1
    assert blocks[0].startswith("```python")
    assert blocks[0].endswith("```")
    assert not splitter.in_fence


def test_a_blank_line_inside_a_fence_does_not_split_it():
    """The blank line is markdown's block separator everywhere EXCEPT inside a
    fence, where it is just an empty line of code."""
    blocks = _stream(FENCED)
    fence = [b for b in blocks if b.startswith("```")]
    assert len(fence) == 1, f"the fence was split into {len(fence)} pieces"
    assert "return 1" in fence[0]


def test_the_fence_opener_is_not_emitted_on_its_own():
    """Regression: `self._fence` was committed while the opener was still in
    the buffer, so the next call read the opener as its own closer and emitted
    "```python" as a block, leaving the code after it unfenced."""
    for block in _stream(FENCED):
        assert block.strip() != "```python"


def test_a_tilde_fence_is_not_closed_by_backticks():
    splitter = StreamSplitter()
    assert splitter.feed("~~~\ncode\n```\nmore\n") == []
    assert splitter.in_fence
    assert splitter.feed("~~~\n")


def test_a_longer_closer_still_closes():
    splitter = StreamSplitter()
    splitter.feed("```\nx\n")
    assert splitter.feed("`````\n")


def test_an_unclosed_fence_is_still_shown_at_the_end():
    """A cut-off stream must not swallow what did arrive."""
    splitter = StreamSplitter()
    splitter.feed("```python\nprint(1)\n")
    rest = splitter.finish()
    assert rest and "print(1)" in rest[0]


# -- tables and lists ------------------------------------------------------


def test_a_table_is_emitted_whole():
    blocks = _stream(TABLE)
    table = [b for b in blocks if "| tool" in b]
    assert len(table) == 1
    assert "|------|----|" in table[0]
    assert "| bash | y  |" in table[0]


def test_a_list_is_not_split_mid_item():
    text = "1. first\n2. second\n3. third\n\nafter"
    blocks = _stream(text)
    listing = [b for b in blocks if "1. first" in b]
    assert len(listing) == 1
    assert "3. third" in listing[0]


# -- headings --------------------------------------------------------------


def test_a_heading_is_its_own_block():
    """Otherwise the paragraph under it is swallowed into the heading's block
    and Rich renders the prose as part of the heading rule."""
    blocks = _stream("# Title\nbody text\n\nmore")
    assert blocks[0].strip() == "# Title"
    assert "body text" in blocks[1]


def test_a_heading_ends_the_paragraph_before_it():
    blocks = _stream("some prose\n## Next\nmore\n\n")
    assert blocks[0].strip() == "some prose"
    assert blocks[1].strip() == "## Next"


# -- the long-paragraph escape hatch ---------------------------------------


def test_a_long_paragraph_flushes_at_a_sentence_boundary():
    """A 900-word paragraph has no blank line in it. Holding it to the end
    would put the user back where they started: staring at a spinner."""
    splitter = StreamSplitter(soft_flush_chars=40)
    blocks = splitter.feed("First sentence here. Second sentence here. Third one now. ")
    assert blocks, "a long paragraph should have flushed"
    assert blocks[0].startswith("First sentence here.")
    assert not blocks[0].endswith("Third one now.") or splitter.tail == ""


def test_the_soft_flush_never_cuts_inside_a_fence():
    splitter = StreamSplitter(soft_flush_chars=10)
    text = "```\nA sentence. Another sentence. A third one. And more text here.\n"
    assert splitter.feed(text) == []
    assert splitter.in_fence


def test_the_soft_flush_never_cuts_inside_a_list():
    splitter = StreamSplitter(soft_flush_chars=10)
    assert splitter.feed("- One thing. Two things. Three things. Four things here.\n") == []


def test_the_soft_flush_never_cuts_inside_a_table():
    splitter = StreamSplitter(soft_flush_chars=10)
    assert splitter.feed("| a. b. c. d. e. f. g. h. i. j. |\n") == []


def test_a_short_paragraph_is_not_soft_flushed():
    splitter = StreamSplitter()
    assert splitter.feed("Short. Text. ") == []
    assert splitter.tail == "Short. Text. "


# -- degenerate input ------------------------------------------------------


def test_empty_input_produces_nothing():
    splitter = StreamSplitter()
    assert splitter.feed("") == []
    assert splitter.finish() == []


def test_whitespace_only_produces_nothing():
    assert _stream("\n\n   \n\n") == []


def test_a_single_line_with_no_newline_survives_to_finish():
    splitter = StreamSplitter()
    assert splitter.feed("just one line") == []
    assert splitter.finish() == ["just one line"]


def test_the_splitter_is_reusable_after_finish():
    splitter = StreamSplitter()
    splitter.feed("a\n\n")
    splitter.finish()
    assert splitter.tail == ""
    assert not splitter.in_fence
    assert splitter.feed("b\n\n") == ["b"]


def test_crlf_input_does_not_break_block_detection():
    blocks = _stream("one\r\n\r\ntwo\r\n\r\n")
    assert len(blocks) == 2


# -- what the REPL does with the blocks ------------------------------------


def test_repl_renders_blocks_as_markdown_not_raw_tokens():
    """Pin the wiring: the response path must go through the splitter and Rich
    Markdown, not `sys.stdout.write(token)`."""
    import inspect

    from djcode import repl

    source = inspect.getsource(repl.run_repl)
    assert "StreamSplitter()" in source
    assert "Markdown(block" in source
    assert "sys.stdout.write(token)" not in source, "raw token writing is back"
