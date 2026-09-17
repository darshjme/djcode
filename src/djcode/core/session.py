"""``CoreSession`` — the entire surface a front-end needs (SSOT section 2.3, W8-1).

``CoreSession`` **owns** an :class:`~djcode.agents.operator.Operator`; it does not
replace, subclass or reimplement one. The tool-round loop, the round budget, the
compaction trigger, the denial-note flush and — above all — the cancellation
bookkeeping that repairs ``tool_call``/``tool_result`` pairing are correct today
and stay exactly where they are. What this class adds is everything *around* the
loop: the event stream, the steer and queue slots, the checkpoint/diff/fork
surface, and a lifecycle with a real ``close()``.

Three ownership decisions, each of which had a wrong answer available:

* **The ``PermissionEngine`` is read from the Operator, never constructed here.**
  ``Operator.__init__`` builds it deliberately (see the W6 comment there): W5
  attached its checkpoint store from ``repl.py`` and the Textual TUI silently
  ended up with no checkpoints at all. Two engines would be two rule sets, and
  the front-end holding the wrong one would be enforcing nothing.
* **The ``CheckpointStore`` IS attached here.** W5 left it surface-attached and
  only ``repl.py`` ever did it, so ``app.py`` has had no undo since W5 shipped.
  Attaching it in the constructor means every front-end that holds a
  ``CoreSession`` gets checkpoints, and no front-end has to know it needed to.
* **Steer text lives on the Operator; queued text lives here.** The drain point
  is inside ``Operator._send``, so the steer slot must be reachable from there.
  The queue is the opposite: it is "after this turn", which is a concept the
  Operator does not have and should not gain — and a second queue on the
  Operator would immediately diverge from ``app.py``'s ``_followups``.

The ``ok_source`` discipline from W3 applies here too: ``ToolOutcome.ok`` is
meaningful only when ``details["ok_source"]`` is ``handler``/``raised``/
``dispatch``, and 17 of 24 tools report ``unverified``. So the turn-level verdict
this class emits — ``COMPLETE`` or ``ERROR`` — is derived from whether
``Operator.send`` raised, never from any tool's ``ok`` flag.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from djcode.core.checkpoints import Checkpoint, CheckpointStore, RestoreReport
from djcode.core.diff import FileDiff, diff_paths
from djcode.core.events import (
    CoreEvent,
    EventBus,
    EventType,
    checkpoint_event,
    complete_event,
    diff_event,
    error_event,
    permission_decided_event,
    permission_request_event,
    queue_event,
    steer_event,
)
from djcode.core.permissions import Decision, DecisionAction, Mode, ToolRequest
from djcode.errors import classify_error
from djcode.provider import Message, Provider
from djcode.sessions import SessionDB

logger = logging.getLogger(__name__)

#: Fields of :class:`djcode.provider.Message`. ``SessionDB._row_to_message``
#: also returns ``timestamp``, which ``Message`` has no room for.
_MESSAGE_FIELDS = ("role", "content", "tool_calls", "tool_call_id", "name", "images")


@dataclass(slots=True)
class ContextItem:
    """One line of the context bill of materials: what is filling the window.

    SSOT section 2.3 asks for ``bill_of_materials() -> list[ContextItem]`` and no
    such type existed anywhere in the tree -- ``ContextWindowManager`` reports
    totals (``ContextStats``) and per-injection records (``InjectedContext``) but
    never a per-source breakdown. This is that breakdown, aggregated from what
    the manager already holds. Rendering it (``/context``, P1-8) is W9's.
    """

    source: str
    tokens: int
    items: int = 1
    share_pct: float = 0.0
    detail: str = ""


@dataclass(slots=True)
class SessionOptions:
    """Everything about a session that is a choice rather than a dependency."""

    model: str = ""
    bypass_rlhf: bool = False
    auto_accept: bool = False
    show_thinking: bool = True
    #: ``None`` leaves the mode the Operator's own policy chose for ``auto_accept``.
    permission_mode: Mode | None = None
    #: Resume an existing session row instead of creating one.
    session_id: str | None = None
    cwd: Path = field(default_factory=Path.cwd)
    #: A fork shares its parent's provider and must not close it.
    owns_provider: bool = True


class CoreSession:
    """One conversation: an Operator, a bus, a checkpoint store and a session row."""

    def __init__(
        self,
        config: dict | None = None,
        *,
        provider: Provider,
        event_bus: EventBus | None = None,
        approval: Callable[[ToolRequest], Awaitable[Decision | bool]] | None = None,
        options: SessionOptions | None = None,
        session_db: SessionDB | None = None,
        operator: Any = None,
    ) -> None:
        from djcode.agents.operator import Operator

        self._config = dict(config or {})
        self._options = options or SessionOptions()
        self._provider = provider
        self._bus = event_bus if event_bus is not None else EventBus()
        self._approval = approval
        self._cwd = Path(self._options.cwd)

        self._operator = operator or Operator(
            provider,
            bypass_rlhf=self._options.bypass_rlhf,
            model=self._options.model or provider.config.model,
            auto_accept=self._options.auto_accept,
            show_thinking=self._options.show_thinking,
            approval_callback=self._approve if approval is not None else None,
            event_bus=self._bus,
        )
        if self._options.permission_mode is not None:
            self._operator.permissions.mode = Mode(self._options.permission_mode)

        self._db = session_db if session_db is not None else SessionDB()
        self._session_id = self._options.session_id or self._db.create_session(
            provider.config.model, provider.config.name
        )
        # Grafted onto the Operator rather than kept only here: `Operator.send`
        # reads `getattr(self, "session_id", None)` at CALL time to key the
        # spill writer and the checkpoint store, and `/resume` reassigns it in
        # place. A CoreSession-only copy would leave both keyed to None.
        self._operator.session_id = self._session_id
        self._operator.session_db = self._db
        self._operator.on_checkpoint = lambda messages: self._db.append_messages(
            self._session_id, messages
        )
        self._store: CheckpointStore | None = None
        try:
            from djcode.core.checkpoints import store_from_config

            self._store = store_from_config(self._db, cwd=str(self._cwd))
            self._operator.dispatch_ctx.checkpoints = self._store
        except Exception:  # pragma: no cover - a missing store must not kill a session
            logger.warning("checkpoints unavailable for this session", exc_info=True)

        # W8-2 (P0-5). `_queued` is text for after the turn; `_returned` is the
        # ONLY place cancelled text goes and `take_queued()` the ONLY way it
        # leaves. `app.py:2916`'s `if self._followups and not self._cancel_requested`
        # could neither deliver nor return it -- and because `_followups` was
        # never cleared and `_cancel_requested` was reset at the top of the NEXT
        # turn, the stranded text fired later as a spontaneous third turn. There
        # is no predicate here to get backwards.
        self._queued: list[str] = []
        self._returned: list[str] = []
        self._inflight: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    # ── identity ─────────────────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def event_bus(self) -> EventBus:
        return self._bus

    @property
    def operator(self) -> Any:
        """The owned Operator. Exposed so a front-end can read state, not swap it."""
        return self._operator

    @property
    def permissions(self) -> Any:
        """The Operator's engine. There is exactly one, and this is it."""
        return self._operator.permissions

    @property
    def checkpoint_store(self) -> CheckpointStore | None:
        return self._store

    # ── approval ─────────────────────────────────────────────────────────

    async def _approve(self, name: str, args: dict[str, Any]) -> Decision:
        """Adapt the SSOT-shaped approval to the arity ``Operator`` calls.

        SSOT section 2.3 specifies ``approval: (ToolRequest) -> Awaitable[Decision]``.
        ``Operator.approval_callback`` is ``(name, args) -> Awaitable[bool]`` and
        its arity must NOT change: ``_approve_repeated_call`` is registered as
        the three-argument ``dispatch_ctx.approval_callback`` and forwards to
        the two-argument one. So the adapter lives here, builds the request, and
        hands the ``Decision`` straight back -- ``Operator._approve_tool``
        already honours one, including ``grant_session``/``grant_always``.
        """
        request = ToolRequest(tool=name, arguments=dict(args or {}), cwd=str(self._cwd))
        self._bus.emit_nowait(permission_request_event(request))
        answer = await self._approval(request)  # type: ignore[misc]
        decision = (
            answer
            if isinstance(answer, Decision)
            else Decision(DecisionAction.ALLOW if answer else DecisionAction.DENY)
        )
        self._bus.emit_nowait(permission_decided_event(request, decision))
        return decision

    # ── steering and queueing (W8-2, P0-5) ───────────────────────────────

    def steer(self, text: str) -> None:
        """Correct the model mid-turn. Delivered at the next tool-round boundary.

        Delegates straight to ``Operator.steer``, which only appends to a list.
        The text becomes a ``user`` message in exactly one place --
        ``Operator._drain_steer``, at the top of the tool-round loop -- and that
        is what keeps it from landing between an ``assistant(tool_calls)``
        message and the ``tool`` messages answering it.
        """
        text = str(text or "").strip()
        if not text:
            return
        self._operator.steer(text)
        self._bus.emit_nowait(steer_event(text, delivered=False))

    def queue(self, text: str) -> None:
        """Send this after the current turn finishes, as its own turn."""
        text = str(text or "").strip()
        if not text:
            return
        self._queued.append(text)
        self._bus.emit_nowait(queue_event(text, action="queued", depth=len(self._queued)))

    @property
    def queued(self) -> list[str]:
        """Text waiting for after the turn. Read-only view."""
        return list(self._queued)

    def take_queued(self) -> list[str]:
        """Take back everything a cancel returned. Idempotent: twice gives ``[]``.

        This is the whole of P0-5. ``cancel()`` is the only thing that fills
        ``_returned`` and this is the only thing that empties it, so text the
        user typed can be delivered or handed back but never quietly dropped.
        """
        out, self._returned = self._returned, []
        return out

    def _abort_slots(self) -> None:
        """Move every undelivered word into ``_returned``. Safe to call twice.

        Both slots move, not just the queue: a steer typed at round 3 and
        cancelled at round 4 is text the user wrote and never saw acted on.
        """
        for text in self._operator.take_steer():
            self._returned.append(text)
            self._bus.emit_nowait(queue_event(text, action="returned", depth=0))
        for text in self._queued:
            self._returned.append(text)
            self._bus.emit_nowait(queue_event(text, action="returned", depth=0))
        self._queued = []
        # A queued item popped for delivery but cancelled before the Operator
        # appended it is neither delivered nor queued; it belongs to the user.
        if self._inflight is not None and not self._was_appended(self._inflight):
            self._returned.append(self._inflight)
        self._inflight = None

    def _was_appended(self, text: str) -> bool:
        for message in reversed(self._operator.messages):
            if message.role == "user" and text in (message.content or ""):
                return True
        return False

    def cancel(self) -> None:
        """Stop the running turn and hand every undelivered word back.

        The turn's own cancellation protocol is ``Operator.send``'s
        ``except asyncio.CancelledError`` block, which synthesises the missing
        ``role="tool"`` answers so the transcript stays valid. This only cancels
        the task and drains the slots.
        """
        self._abort_slots()
        task = self._task
        if task is not None and not task.done():
            task.cancel()

    # ── the turn ─────────────────────────────────────────────────────────

    async def send(self, text: str) -> AsyncIterator[CoreEvent]:
        """Run a turn and yield its events; then run whatever was queued.

        Queued text is delivered *here*, inside the same generator, immediately
        after ``COMPLETE`` -- not popped into a local by the caller and passed
        back in. The pop and the turn that consumes it are one statement, so a
        cancel in between cannot drop the text on the floor.
        """
        self._require_open()
        while True:
            async for event in self._turn(text):
                yield event
            if not self._queued:
                return
            text = self._queued.pop(0)
            self._inflight = text
            # Emitted AND yielded: between turns no per-turn sink is subscribed,
            # so a caller iterating `send()` would never see the handoff.
            handoff = queue_event(text, action="delivered", depth=len(self._queued))
            self._bus.emit_nowait(handoff)
            yield handoff

    async def _turn(self, text: str) -> AsyncIterator[CoreEvent]:
        started = time.monotonic()
        response: list[str] = []
        queue: asyncio.Queue[Any] = asyncio.Queue()
        sentinel = object()

        async def sink(event: CoreEvent) -> None:
            queue.put_nowait(event)

        async def drive() -> None:
            try:
                async for token in self._operator.send(text):
                    response.append(token)
            finally:
                queue.put_nowait(sentinel)

        # W3-5's doom-loop window is per-turn by intent -- `forget_calls`'s own
        # docstring says "e.g. at a turn boundary" -- and nothing has ever
        # called it, so a legitimate second ask for the same file counted as a
        # repeat forever.
        with contextlib.suppress(Exception):
            self._operator.dispatch_ctx.forget_calls()

        self._bus.subscribe(sink)
        task = asyncio.create_task(drive())
        self._task = task
        failure: BaseException | None = None
        try:
            while True:
                item = await queue.get()
                if item is sentinel:
                    break
                for event in self._expand(item):
                    yield event
            # `emit_nowait` schedules each subscriber as its own task, so events
            # emitted just before the generator finished may not have reached
            # the sink yet. Yield to the loop, then take whatever landed.
            for _ in range(3):
                await asyncio.sleep(0)
            while not queue.empty():
                item = queue.get_nowait()
                if item is not sentinel:
                    for event in self._expand(item):
                        yield event
            try:
                await task
            except Exception as error:  # noqa: BLE001 - re-raised below as ERROR
                # `Exception`, not `BaseException`: a CancelledError must reach
                # the handler below and be re-raised as a cancellation, not
                # reported as a turn that errored.
                failure = error
        except (asyncio.CancelledError, KeyboardInterrupt, GeneratorExit):
            # Both paths matter on Windows: `repl_runtime` cannot install a
            # SIGINT handler on the Proactor loop, so Ctrl+C during a turn
            # arrives as KeyboardInterrupt rather than CancelledError.
            self._abort_slots()
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
            raise
        finally:
            self._bus.unsubscribe(sink)
            self._task = None
            if self._inflight is not None and self._was_appended(self._inflight):
                self._inflight = None

        elapsed = time.monotonic() - started
        if failure is not None:
            classified = classify_error(failure)
            event = error_event(
                str(failure) or type(failure).__name__,
                kind=str(getattr(classified, "category", "") or type(failure).__name__),
                recoverable=bool(getattr(classified, "recoverable", False)),
            )
            self._bus.emit_nowait(event)
            yield event
            raise failure
        # The verdict comes from "did the turn raise", never from any tool's
        # `ok` flag: 17 of 24 tools report `ok_source="unverified"`, which means
        # only that dispatch saw no failure signal.
        event = complete_event(
            "".join(response),
            tool_rounds=int(getattr(self._operator, "last_tool_rounds", 0) or 0),
            elapsed_s=elapsed,
        )
        self._bus.emit_nowait(event)
        yield event

    def _expand(self, event: CoreEvent) -> list[CoreEvent]:
        """Pass an event through, deriving DIFF/CHECKPOINT from a tool result.

        ``dispatch_tool``'s ``_finish_checkpoint`` already put ``checkpoint`` and
        ``diffs`` on every ``tool_result_event``'s ``details``, bounded and
        body-free. Deriving the two event types from that costs no second
        capture and no second ``git`` call.
        """
        if event.event_type is not EventType.TOOL_RESULT:
            return [event]
        out = [event]
        details = event.data.get("details") or {}
        summary = details.get("checkpoint") or {}
        if summary.get("id"):
            derived = checkpoint_event(_CheckpointRef(summary))
            self._bus.emit_nowait(derived)
            out.append(derived)
        for raw in self._diff_dicts(details):
            try:
                restored = FileDiff.from_dict(raw)
            except Exception:  # pragma: no cover - defensive
                continue
            derived = diff_event(restored)
            self._bus.emit_nowait(derived)
            out.append(derived)
        return out

    @staticmethod
    def _diff_dicts(details: dict[str, Any]) -> list[dict[str, Any]]:
        """Every diff on a tool result, under whichever key put it there.

        W10 bug fix, found by ``tests/test_core_contract.py``. There are two
        producers and they do not agree on a key:

        * ``tools/__init__.py::_finish_checkpoint`` sets ``details["diffs"]``
          (a list) for the tools that cannot build their own -- ``notebook_edit``
          and every path a ``bash``/``git`` command touched -- and ALSO mirrors
          it to ``details["diff"]`` when there is exactly one.
        * ``file_edit`` and ``file_write`` build a more accurate diff from the
          text they already held and return it on ``details["diff"]`` alone
          (``OUTCOME_TOOLS`` is skipped by ``_finish_checkpoint`` for exactly
          that reason). Singular. No list.

        Reading only ``diffs`` meant the two tools a user edits with ALL DAY
        emitted no ``DIFF`` event at all, while `bash` did -- so the proof
        obligation's DIFF was being satisfied by the wrong tools. Reading both
        keys naively would double-emit whenever ``_finish_checkpoint`` mirrored
        a single diff, so the list wins when it is present and the singular is
        the fallback, never an addition.
        """
        listed = details.get("diffs")
        if isinstance(listed, list) and listed:
            return [raw for raw in listed if isinstance(raw, dict)]
        single = details.get("diff")
        return [single] if isinstance(single, dict) else []

    # ── undo, diffs, context ─────────────────────────────────────────────

    async def checkpoints(self, *, limit: int = 200) -> list[Checkpoint]:
        if self._store is None:
            return []
        return self._store.checkpoints(self._session_id, limit=limit)

    async def restore(self, checkpoint_id: str) -> RestoreReport:
        if self._store is None:
            report = RestoreReport()
            report.notes.append("Checkpoints are not active in this session.")
            return report
        return self._store.restore_ids(
            [checkpoint_id], session_id=self._session_id, make_redo=True
        )

    async def diff(self, base: str = "session", *, ref: str = "") -> list[FileDiff]:
        """``session`` | ``uncommitted`` | ``branch``.

        ``async`` because two of the three bases spawn ``git``; SSOT section 2.3
        sketches it synchronous and reality wins.
        """
        return await diff_paths(
            base,
            self._store,
            session_id=self._session_id,
            cwd=str(self._cwd),
            ref=ref,
        )

    def context_stats(self) -> Any:
        # `stats` is a property on ContextWindowManager, not a method.
        return self._operator.context_manager.stats

    def bill_of_materials(self) -> list[ContextItem]:
        """What is filling the context window, by source, largest first."""
        manager = self._operator.context_manager
        totals: dict[str, list[int]] = {}
        for injected in getattr(manager, "_injected", []):
            if getattr(injected, "is_expired", False):
                continue
            bucket = totals.setdefault(f"injected:{injected.source}", [0, 0])
            bucket[0] += int(injected.tokens or 0)
            bucket[1] += 1
        estimate = getattr(manager, "count_tokens", None)
        for message in self._operator.messages:
            key = f"message:{message.role}"
            bucket = totals.setdefault(key, [0, 0])
            text = message.content or ""
            bucket[0] += int(estimate(text)) if callable(estimate) else max(1, len(text) // 4)
            bucket[1] += 1
        grand = sum(value[0] for value in totals.values()) or 1
        items = [
            ContextItem(
                source=source,
                tokens=tokens,
                items=count,
                share_pct=round(100.0 * tokens / grand, 1),
            )
            for source, (tokens, count) in totals.items()
        ]
        items.sort(key=lambda item: item.tokens, reverse=True)
        return items

    # ── lifecycle ────────────────────────────────────────────────────────

    async def fork(self, at_message_id: int | None = None) -> CoreSession:
        """Branch this session at a transcript row; the parent keeps every entry.

        ``at_message_id`` is a ``conversations`` row id (``ConversationEntry.id``),
        NOT an index into ``messages``: ``djcode.provider.Message`` has no ``id``
        field, and ``SessionDB.fork_session`` keys on the row. ``None`` forks at
        the end.

        The child gets its own ``EventBus`` -- section 2.3 says "independent",
        and a shared bus would render both conversations into one transcript --
        and does NOT own the provider, so closing the fork leaves the parent
        connected.
        """
        self._db.append_messages(self._session_id, self._operator.messages)
        fork_id = self._db.fork_session(self._session_id, at_message_id)
        if not fork_id:
            raise RuntimeError(f"could not fork session {self._session_id}")
        child = CoreSession(
            self._config,
            provider=self._provider,
            event_bus=EventBus(),
            approval=self._approval,
            options=replace(self._options, session_id=fork_id, owns_provider=False),
            session_db=self._db,
        )
        system = self._operator.messages[0] if self._operator.messages else None
        rebuilt: list[Message] = [system] if system is not None else []
        for row in self._db.load_conversation(fork_id, view="model"):
            if row.get("role") == "system":
                continue
            rebuilt.append(Message(**{k: row[k] for k in _MESSAGE_FIELDS if k in row}))
        child._operator.messages = rebuilt
        child._operator.context_manager.replace_messages(rebuilt)
        return child

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("this CoreSession is closed")

    async def close(self) -> None:
        """Persist, end the session row, and release the provider. Idempotent."""
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self._db.append_messages(self._session_id, self._operator.messages)
        with contextlib.suppress(Exception):
            self._db.end_session(self._session_id)
        if self._options.owns_provider:
            with contextlib.suppress(Exception):
                await self._provider.close()


class _CheckpointRef:
    """The bounded checkpoint summary ``dispatch_tool`` already put on ``details``.

    ``checkpoint_event`` duck-types its argument on ``checkpoint_id``/``id``, so
    a summary is enough and the store is never re-read to build an event.
    """

    __slots__ = ("id", "seq", "files", "paths", "covered", "reason")

    def __init__(self, summary: dict[str, Any]) -> None:
        self.id = str(summary.get("id", "") or "")
        self.seq = int(summary.get("seq", 0) or 0)
        self.files = int(summary.get("files", 0) or 0)
        self.paths = list(summary.get("paths") or [])
        self.covered = bool(summary.get("covered", True))
        self.reason = str(summary.get("reason", "") or "")


__all__ = ["ContextItem", "CoreSession", "SessionOptions"]
