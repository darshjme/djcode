"""ToolOutcome — one structured result type for every tool call.

Today (W2) every dispatch path collapses a tool result to ``str`` three separate
times before the model ever sees it::

    tools/__init__.py:58    return str(result)                      # the handler's result
    workflow.py:234         result = str(await dispatch(...))       # the DAF wire path
    workflow.py:197         results[ident] = await dispatch(...)    # NATIVE path: no str() at all

so a tool can say "I truncated 40k of output to 2k and spilled the rest to
/tmp/x" only by writing that sentence into the text the model reads. There is no
channel for anything a UI would render differently from what a model reads.

W3 makes ``dispatch_tool`` return one of these instead. This class ships in W2,
ahead of that rewiring, for one reason: ``__str__``.

``__str__`` IS THE COMPATIBILITY SHIM AND IT IS LOAD-BEARING. ``workflow.py``
stringifies the dispatch result and ``operator.py`` does ``Message(content=result)``.
Returning a ``ToolOutcome`` from ``dispatch_tool`` on day one therefore changes
nothing observable on the DAF wire path -- the wire keeps carrying
``str(outcome)`` == ``outcome.content``, byte for byte what it carried before.

The asymmetry to watch, and the reason W3 must not assume the shim saves it:
``WorkflowEngine._execute_native`` (``workflow.py:197``) never stringified. It
stores the dispatch return value raw. That is the branch that runs on every
machine WITHOUT a Rust toolchain, so a regression there is invisible to anyone
testing on a box where DAF is live. W3 owns that; W2 only supplies the type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ToolOutcome:
    """The result of one tool call: what the model reads, plus what a UI renders.

    Attributes:
        content: The text the model sees. This is what goes into
            ``Message(role="tool", content=...)`` and onto the DAF wire.
        details: Structured payload for front-ends -- a diff, a file list, a row
            count, an exit code. Never shown to the model, never parsed back out
            of ``content``. Must stay JSON-serialisable: it rides on
            ``tool_result_event``, which W10 serialises to JSONL.
        ok: Whether the call succeeded, decided by the *handler*, not by sniffing
            whether ``content`` starts with "error". That sniff is the bug W3-1
            removes (it lives at ``workflow.py:48`` and ``operator.py:514``
            today); nothing new may reintroduce it.
        spill_path: Where the full output was written when ``content`` had to be
            truncated (P1-4). ``None`` when nothing was truncated.
        duration_ms: Wall-clock time the handler took.
    """

    content: str
    details: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    spill_path: str | None = None
    duration_ms: int = 0

    def __str__(self) -> str:
        """The model-facing text.

        Load-bearing. See the module docstring: this is what keeps every existing
        ``str(await dispatch(...))`` call site working unchanged on day one.
        ``@dataclass`` generates ``__repr__`` but never ``__str__``, so this
        definition is what ``str()``, ``f"{outcome}"`` and ``"%s" % outcome`` all
        reach.
        """
        return self.content
