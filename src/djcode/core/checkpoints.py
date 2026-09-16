"""Checkpoints and undo (P0-1) -- the engine half.

WHAT THIS MODULE PROMISES, AND WHAT IT REFUSES TO PROMISE
=========================================================
``/undo`` only has value if the user can trust it. So the design rule here is
the same one W3 applied to ``ToolOutcome.ok``: **state the coverage, never
imply it.** Everything this store cannot restore is recorded as a machine-
readable notice (:meth:`CheckpointStore.coverage_notes`) that the surface
prints at the moment it matters -- not buried in a docstring.

COVERED (pre-image captured from disk bytes, restore verified by hash)
---------------------------------------------------------------------
* ``file_write``  -- whole-file create/overwrite, plus every parent directory
  the tool creates via ``p.parent.mkdir(parents=True)``.
* ``file_edit``   -- in-place substring replacement.
* ``notebook_edit`` -- whole-file rewrite of the ``.ipynb`` JSON.
* ``bash`` and ``git`` -- via a content ledger of the cwd subtree, but ONLY
  while :attr:`_Ledger.enabled`. It disables itself, loudly and for the rest
  of the session, whenever it cannot do the job honestly (see below).

NOT COVERED, EVER
-----------------
* ``process(action="start"|"schedule")`` and the ``schedule`` tool: the command
  runs in a detached task (or in a separate ``djcode --scheduler`` process)
  minutes to hours after ``dispatch_tool`` returned. There is no capture
  window to put around it.
* ``mcp(action="call")``, ``browser``, ``computer``: an arbitrary server may
  write arbitrary paths outside any root we walk.
* A shell command that leaves the walk root (``cd ../other && rm -rf x``) or
  writes through an absolute path outside it. The model cannot set ``cwd``
  (``provider.py``'s ``bash`` schema exposes only ``command``/``timeout``), so
  the root is unambiguous -- but ``cd`` inside the command string still escapes
  it and there is no cheap fix.
* A command that rewrites a file and restores its original mtime -- ``tar -x``,
  ``unzip``, ``cp -p``, ``rsync -t``, ``touch -r``. Measured on this machine:
  restoring the mtime after a content change makes the write 100% invisible to
  a ``(mtime, size)`` diff. Closing it would mean re-hashing the whole tree on
  every shell call, which is the 4.7s-cold number in the W5 audit.
* Symlinked files: the walk uses ``is_file(follow_symlinks=False)``, so a
  symlink is skipped rather than followed. That is the safe direction (we never
  clobber a link target) but it means those files are unprotected.

WHY THE BLUEPRINT'S BASH SCHEME IS NOT WHAT IS IMPLEMENTED
----------------------------------------------------------
Blueprint W5-2 says to "store pre-images ... from a pre-walk content snapshot
taken lazily -- i.e. snapshot content only for files whose mtime changed".
That is impossible: you only learn which files changed *after* the command has
run, and by then the pre-image is gone. For ``rm -rf build/`` -- the exact case
SSOT P0-1 cites -- there would be nothing to restore.

What is implemented instead is a **session-scoped content ledger**:
``key -> (mtime_ns, size, sha256)``, with every sha's bytes already in the
content-addressed ``blobs`` table. Before each shell call we stat-walk (1.6-3.6
ms on this repo, measured) and re-read only the entries whose stat tuple moved
since the last walk; normally that is zero files. The command then runs, and
the post-walk needs only stats plus hashes of the movers -- and a deleted
file's bytes are already in the store. The first shell call in a session pays
one full read of the tree (27 ms warm here); that read is budgeted, and if it
blows the budget the ledger turns itself off rather than half-covering.

``st_mtime_ns`` is not nanosecond-accurate. Measured on this box: ~0.6 ms
effective tick, and 153 of 200 back-to-back same-size writes were
indistinguishable by mtime. So the ledger also carries git's "racily clean"
rule -- any entry whose recorded mtime is within :data:`RACY_TICK_NS` of the
walk that recorded it is re-hashed on the next walk regardless of its stat
tuple.

TERMINAL-FREE
-------------
Nothing here imports rich/questionary/prompt_toolkit/textual/click and nothing
prints. All rendering, all confirmation, all picker UI is the surface's job.
``tests/test_headless_purity.py`` enforces it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sqlite3
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from djcode.config import CONFIG_DIR

logger = logging.getLogger(__name__)

#: Tools whose single ``path`` argument is the whole mutation surface.
FILE_TOOLS: frozenset[str] = frozenset({"file_write", "file_edit", "notebook_edit"})

#: Tools that mutate the filesystem through a shell and therefore need the
#: ledger. ``git`` is here because ``tools/git.py`` explicitly admits
#: ``checkout/switch/restore/merge/rebase/pull/cherry-pick/stash``, every one of
#: which rewrites the working tree; the blueprint's W5-2 list omits it.
LEDGER_TOOLS: frozenset[str] = frozenset({"bash", "git"})

#: Only ``file_write`` creates directories (``p.parent.mkdir(parents=True)``).
DIR_CREATING_TOOLS: frozenset[str] = frozenset({"file_write"})

DEFAULT_IGNORE_DIRS: tuple[str, ...] = (
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "target",
    "dist",
    ".ruff_cache",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
    ".idea",
)

#: Filesystem timestamp granularity guard, in nanoseconds. See the module
#: docstring: a file written within one tick of the walk that stat'ed it can
#: carry an unchanged ``(mtime, size)`` tuple, so it is re-hashed unconditionally.
RACY_TICK_NS = 2_000_000

#: ``tool_name`` for the checkpoint a restore takes of the pre-restore tree.
#: These are excluded from ``/undo``'s and ``/rewind``'s turn lists -- otherwise
#: undoing an undo would be the first thing ``/undo`` offered.
RESTORE_TOOL = "__restore__"

_IS_WINDOWS = os.name == "nt"


def _key(path: Any) -> str:
    """A stable identity for a path.

    ``Path.resolve()`` normalises an EXISTING file's case to the on-disk
    spelling but leaves a not-yet-created leaf as the caller typed it, so
    ``New.txt`` then ``new.txt`` would otherwise be two rows for one file on
    Windows. ``normcase`` closes that.
    """
    return os.path.normcase(str(path))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FileChange:
    """One path a checkpoint can put back.

    ``pre_sha is None and existed is False`` encodes "this did not exist" so
    restore deletes it. ``post_sha is None and existed is True`` encodes "the
    tool deleted it" so restore recreates it -- the blueprint spells out only
    the first of those two.
    """

    path: str
    pre_sha: str | None
    post_sha: str | None
    existed: bool
    kind: str = "file"  # "file" | "dir"
    mode: int | None = None

    @property
    def created(self) -> bool:
        return not self.existed

    @property
    def removed(self) -> bool:
        return self.existed and self.post_sha is None and self.kind == "file"


@dataclass(slots=True)
class Checkpoint:
    """One capture window: everything one tool call changed."""

    id: str
    session_id: str
    turn_id: str
    seq: int
    created_at: str
    tool_name: str
    label: str
    coverage: str = ""
    entry_id: int | None = None
    files: list[FileChange] = field(default_factory=list)

    # ``core/events.checkpoint_event`` duck-types this name first.
    @property
    def checkpoint_id(self) -> str:
        return self.id


@dataclass(slots=True)
class PlannedAction:
    """What a restore intends to do to one path, before it does anything."""

    action: str  # restore | delete | skip | keep-dir | noop
    path: str
    reason: str = ""


@dataclass(slots=True)
class RestoreReport:
    """The outcome of a restore.

    Four buckets, not three. ``skipped`` is "I chose not to touch this" (the
    file moved outside DJcode since the checkpoint). ``failed`` is "I tried and
    the OS said no" -- on Windows an editor or a dev server holding a handle
    makes delete and overwrite raise ``WinError 32``. Collapsing the two lets
    ``/undo`` report a clean restore over a half-restored tree.
    """

    restored: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    kept_dirs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    redo_id: str | None = None
    checkpoint_ids: list[str] = field(default_factory=list)

    @property
    def touched(self) -> int:
        return len(self.restored) + len(self.deleted)

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable form, for ``conversations.checkpoint_blob``."""
        return {
            "restored": list(self.restored),
            "deleted": list(self.deleted),
            "skipped": [list(pair) for pair in self.skipped],
            "failed": [list(pair) for pair in self.failed],
            "kept_dirs": list(self.kept_dirs),
            "notes": list(self.notes),
            "redo_id": self.redo_id,
            "checkpoint_ids": list(self.checkpoint_ids),
        }


@dataclass(slots=True)
class _Entry:
    path: str
    mtime_ns: int
    size: int
    sha: str


@dataclass(slots=True)
class _Ledger:
    """Per-root content ledger for the shell tools."""

    root: str
    entries: dict[str, _Entry] = field(default_factory=dict)
    stamp_ns: int = 0
    enabled: bool = True
    reason: str = ""
    baseline_done: bool = False


@dataclass(slots=True)
class Pending:
    """State carried across the handler call: what step 3 saw, for step 5."""

    tool_name: str
    session_id: str
    turn_id: str
    label: str
    kind: str  # "file" | "ledger" | "none"
    covered: bool = True
    reason: str = ""
    # file capture
    path: Path | None = None
    pre_sha: str | None = None
    existed: bool = False
    mode: int | None = None
    created_dirs: list[str] = field(default_factory=list)
    # ledger capture
    root: Path | None = None
    pre_entries: dict[str, _Entry] = field(default_factory=dict)
    lock_held: bool = False


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class CheckpointStore:
    """Content-addressed pre-image store living inside ``sessions.db``.

    The three tables are created by ``djcode.sessions``'s ``_BASE_SCHEMA_SQL``
    -- there is exactly one DDL writer for this file and it is not this module.
    Constructing a ``CheckpointStore`` therefore requires a ``SessionDB`` (or
    anything exposing ``db_path``) that has already run its migration.
    """

    def __init__(
        self,
        db: Any,
        *,
        cwd: str | Path | None = None,
        budget_mb: float = 256.0,
        max_file_mb: float = 8.0,
        bash_enabled: bool = True,
        walk_budget_ms: float = 400.0,
        baseline_budget_ms: float = 1500.0,
        max_entries: int = 20_000,
        ignore_dirs: tuple[str, ...] | frozenset[str] = DEFAULT_IGNORE_DIRS,
    ) -> None:
        self.db_path = Path(getattr(db, "db_path", db))
        self.default_cwd = str(cwd or os.getcwd())
        self.budget_bytes = int(budget_mb * 1024 * 1024)
        self.max_file_bytes = int(max_file_mb * 1024 * 1024)
        self.bash_enabled = bool(bash_enabled)
        self.walk_budget_ms = float(walk_budget_ms)
        self.baseline_budget_ms = float(baseline_budget_ms)
        self.max_entries = int(max_entries)
        self.ignore_dirs = frozenset(ignore_dirs)
        self._ledgers: dict[str, _Ledger] = {}
        self._lock = asyncio.Lock()
        self._turn_id = ""
        self._turn_label = ""
        self._notices: list[str] = []
        self._seen_notices: set[str] = set()

    # -- connection ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    # -- notices ------------------------------------------------------------

    def _notice(self, text: str) -> None:
        """Record a one-time user-facing fact. The surface drains these."""
        if text in self._seen_notices:
            return
        self._seen_notices.add(text)
        self._notices.append(text)

    def pop_notices(self) -> list[str]:
        """Take the notices raised since the last call. Never repeats one."""
        out, self._notices = self._notices, []
        return out

    def coverage_notes(self, cwd: str | Path | None = None) -> list[str]:
        """What ``/undo`` must say out loud, every single time it runs."""
        root = _key(Path(cwd or self.default_cwd))
        ledger = self._ledgers.get(root)
        notes: list[str] = []
        if not self.bash_enabled:
            notes.append(
                "bash/git changes are NOT covered (checkpoint_bash is off in config)."
            )
        elif ledger is None:
            notes.append(
                "bash/git changes: no shell command has run yet this session, so "
                "there is nothing recorded either way."
            )
        elif not ledger.enabled:
            notes.append(f"bash/git changes are NOT covered: {ledger.reason}")
        else:
            notes.append(
                f"bash/git changes are covered for {ledger.root} "
                f"({len(ledger.entries)} tracked files)."
            )
        notes.append(
            "Never covered: background processes (process start/schedule), the "
            "schedule tool, and MCP/browser/computer tool calls."
        )
        notes.append(
            "Never covered: shell writes outside the tracked directory (a command "
            "that does `cd ..` first, or writes an absolute path elsewhere)."
        )
        notes.append(
            "Never covered: a command that restores a file's original timestamp "
            "(tar -x, unzip, cp -p, rsync -t) -- the ledger cannot see it."
        )
        return notes

    # -- turns --------------------------------------------------------------

    def begin_turn(self, label: str = "") -> str:
        """Open a new turn. Checkpoints captured after this share a ``turn_id``.

        ``/undo`` works on whole turns: a turn that edits five files must revert
        as one action, or the fourth ``/undo`` leaves the tree half-rolled-back.
        """
        self._turn_id = uuid.uuid4().hex
        self._turn_label = " ".join(str(label or "").split())[:160]
        return self._turn_id

    def _ensure_turn(self) -> str:
        if not self._turn_id:
            self.begin_turn("")
        return self._turn_id

    # -- blob storage -------------------------------------------------------

    def _store_blobs(self, conn: sqlite3.Connection, blobs: Mapping[str, bytes]) -> None:
        if not blobs:
            return
        conn.executemany(
            "INSERT OR IGNORE INTO blobs (sha256, body, size) VALUES (?, ?, ?)",
            [(sha, sqlite3.Binary(body), len(body)) for sha, body in blobs.items()],
        )

    def _load_blob(self, conn: sqlite3.Connection, sha: str) -> bytes | None:
        row = conn.execute("SELECT body FROM blobs WHERE sha256 = ?", (sha,)).fetchone()
        return bytes(row[0]) if row is not None else None

    def total_blob_bytes(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COALESCE(SUM(size), 0) FROM blobs").fetchone()
            return int(row[0])

    def enforce_budget(self) -> int:
        """Evict oldest checkpoints until the blob store fits the budget.

        Returns the number of checkpoints evicted. SQLite does not hand pages
        back to the OS without ``VACUUM``, so the file on disk will not shrink;
        ``total_blob_bytes()`` is the number that moves, and that is the number
        the surface reports.
        """
        evicted = 0
        conn = self._connect()
        try:
            while self._blob_bytes(conn) > self.budget_bytes:
                row = conn.execute(
                    "SELECT id FROM checkpoints ORDER BY created_at ASC, rowid ASC LIMIT 1"
                ).fetchone()
                if row is None:
                    break
                conn.execute("DELETE FROM checkpoints WHERE id = ?", (row[0],))
                conn.execute(
                    "DELETE FROM blobs WHERE sha256 NOT IN "
                    "(SELECT pre_sha FROM checkpoint_files WHERE pre_sha IS NOT NULL)"
                )
                conn.commit()
                evicted += 1
            if evicted:
                self._notice(
                    f"Checkpoint storage passed its {self.budget_bytes // (1024 * 1024)} MB "
                    f"budget; the {evicted} oldest restore point(s) were discarded."
                )
        finally:
            conn.close()
        return evicted

    @staticmethod
    def _blob_bytes(conn: sqlite3.Connection) -> int:
        return int(conn.execute("SELECT COALESCE(SUM(size), 0) FROM blobs").fetchone()[0])

    # -- capture: step 3 ----------------------------------------------------

    async def before(
        self, tool_name: str, arguments: Mapping[str, Any], *, session_id: str, cwd: str | None
    ) -> Pending | None:
        """Pre-image capture. Returns state :meth:`after` needs, or ``None``.

        Must never raise: a checkpoint failure has to degrade to "not covered",
        never to "the tool did not run".
        """
        try:
            if tool_name in FILE_TOOLS:
                return await self._before_file(tool_name, arguments, session_id)
            if tool_name in LEDGER_TOOLS:
                return await self._before_ledger(tool_name, session_id, cwd)
        except Exception:  # pragma: no cover - defensive
            logger.debug("checkpoint pre-capture failed for %s", tool_name, exc_info=True)
        return None

    async def _before_file(
        self, tool_name: str, arguments: Mapping[str, Any], session_id: str
    ) -> Pending | None:
        raw_path = arguments.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        # The same expression all three tools use, so the checkpoint keys the
        # path the tool will actually touch.
        target = Path(raw_path).expanduser().resolve()
        pending = Pending(
            tool_name=tool_name,
            session_id=session_id,
            turn_id=self._ensure_turn(),
            label=self._turn_label,
            kind="file",
            path=target,
        )
        if tool_name in DIR_CREATING_TOOLS:
            pending.created_dirs = _missing_ancestors(target)

        result = await asyncio.to_thread(self._read_pre_image, target)
        if result is None:
            pending.existed = False
            return pending
        sha, body, mode, oversize = result
        pending.existed = True
        pending.mode = mode
        pending.pre_sha = sha
        if oversize:
            pending.covered = False
            pending.reason = (
                f"{target} is larger than the {self.max_file_bytes // (1024 * 1024)} MB "
                "per-file checkpoint limit; its previous contents were not saved."
            )
            self._notice(pending.reason)
            return pending
        if body is not None:
            conn = self._connect()
            try:
                self._store_blobs(conn, {sha: body})
                conn.commit()
            finally:
                conn.close()
        return pending

    def _read_pre_image(self, target: Path) -> tuple[str, bytes | None, int | None, bool] | None:
        """``(sha, body, mode, oversize)`` or ``None`` when the file is absent.

        Bytes, never text. ``file_write`` and ``notebook_edit`` both call
        ``write_text`` with ``newline=None``, so on Windows every ``\\n`` they
        write becomes ``\\r\\n`` on disk -- measured, a 208-byte LF notebook
        came back 222 bytes. A pre-image round-tripped through ``read_text``/
        ``write_text`` would corrupt line endings and destroy any file that is
        not valid UTF-8.
        """
        try:
            st = target.stat()
        except OSError:
            return None
        if not target.is_file():
            return None
        mode = st.st_mode & 0o777 if not _IS_WINDOWS else None
        if st.st_size > self.max_file_bytes:
            # Hash it anyway so post-comparison still works; just do not keep
            # the body, and say so.
            digest = hashlib.sha256()
            try:
                with open(target, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b""):
                        digest.update(chunk)
            except OSError:
                return None
            return digest.hexdigest(), None, mode, True
        try:
            body = target.read_bytes()
        except OSError:
            return None
        return _sha(body), body, mode, False

    async def _before_ledger(
        self, tool_name: str, session_id: str, cwd: str | None
    ) -> Pending | None:
        root = Path(cwd or self.default_cwd).resolve()
        pending = Pending(
            tool_name=tool_name,
            session_id=session_id,
            turn_id=self._ensure_turn(),
            label=self._turn_label,
            kind="ledger",
            root=root,
        )
        if not self.bash_enabled:
            pending.covered = False
            pending.reason = "checkpoint_bash is disabled in config"
            return pending

        # One capture window at a time. parallel_execute, workflow(concurrency>1)
        # and background spawn_agent all reach this chokepoint concurrently
        # through the SAME DispatchContext; overlapping walks would credit each
        # command with the other's writes, which makes /undo lie.
        await self._lock.acquire()
        pending.lock_held = True

        ledger = self._ledgers.get(_key(root))
        if ledger is None:
            ledger = _Ledger(root=str(root))
            self._ledgers[_key(root)] = ledger
        if not ledger.enabled:
            pending.covered = False
            pending.reason = ledger.reason
            return pending

        try:
            await asyncio.to_thread(self._refresh_ledger, ledger, root, not ledger.baseline_done)
        except Exception:  # pragma: no cover - defensive
            logger.debug("ledger refresh failed", exc_info=True)
            ledger.enabled = False
            ledger.reason = "the directory walk failed"
        if not ledger.enabled:
            pending.covered = False
            pending.reason = ledger.reason
            self._notice(
                f"Shell commands are NOT being checkpointed: {ledger.reason}. "
                "/undo cannot revert what bash or git changes in this session."
            )
            return pending
        pending.pre_entries = dict(ledger.entries)
        return pending

    def _refresh_ledger(self, ledger: _Ledger, root: Path, baseline: bool) -> None:
        """Bring ``ledger.entries`` up to date with the tree, content included.

        Runs in a worker thread. Budgeted: the baseline pass gets
        ``baseline_budget_ms``, later passes ``walk_budget_ms``. Blowing either
        turns the ledger OFF for the rest of the session rather than leaving it
        silently partial -- "the first 20 000 files, whichever scandir returned
        first" is not a sentence a user can act on.
        """
        budget = self.baseline_budget_ms if baseline else self.walk_budget_ms
        started = time.monotonic()
        stamp = time.time_ns()
        stats, truncated = _walk(root, self.ignore_dirs, self.max_entries, budget)
        if truncated:
            ledger.enabled = False
            ledger.reason = (
                f"{root} is too large to track within the "
                f"{int(budget)} ms / {self.max_entries} file budget"
            )
            return

        fresh: dict[str, _Entry] = {}
        blobs: dict[str, bytes] = {}
        read_bytes = 0
        for key, (path_str, mtime_ns, size) in stats.items():
            old = ledger.entries.get(key)
            racy = old is not None and old.mtime_ns >= ledger.stamp_ns - RACY_TICK_NS
            if old is not None and not racy and old.mtime_ns == mtime_ns and old.size == size:
                fresh[key] = _Entry(path_str, mtime_ns, size, old.sha)
                continue
            if size > self.max_file_bytes:
                # Hash without keeping the body: it stays detectable, just not
                # restorable, and _restore reports that honestly.
                digest = hashlib.sha256()
                try:
                    with open(path_str, "rb") as handle:
                        for chunk in iter(lambda: handle.read(1 << 20), b""):
                            digest.update(chunk)
                except OSError:
                    continue
                fresh[key] = _Entry(path_str, mtime_ns, size, digest.hexdigest())
                continue
            try:
                body = Path(path_str).read_bytes()
            except OSError:
                continue
            sha = _sha(body)
            fresh[key] = _Entry(path_str, mtime_ns, size, sha)
            blobs[sha] = body
            read_bytes += len(body)
            if (time.monotonic() - started) * 1000 > budget:
                ledger.enabled = False
                ledger.reason = (
                    f"reading {root} took longer than the {int(budget)} ms checkpoint budget"
                )
                return

        if blobs:
            conn = self._connect()
            try:
                self._store_blobs(conn, blobs)
                conn.commit()
            finally:
                conn.close()
        ledger.entries = fresh
        ledger.stamp_ns = stamp
        ledger.baseline_done = True

    # -- capture: step 5 ----------------------------------------------------

    async def after(self, pending: Pending) -> Checkpoint | None:
        """Post-image + row write. Must never raise; always releases the lock."""
        try:
            if pending.kind == "file":
                return await self._after_file(pending)
            if pending.kind == "ledger":
                return await self._after_ledger(pending)
        except Exception:  # pragma: no cover - defensive
            logger.debug("checkpoint post-capture failed", exc_info=True)
        finally:
            if pending.lock_held:
                pending.lock_held = False
                self._lock.release()
        return None

    async def _after_file(self, pending: Pending) -> Checkpoint | None:
        target = pending.path
        if target is None:
            return None
        post = await asyncio.to_thread(_hash_file, target)
        created_dirs = [d for d in pending.created_dirs if Path(d).is_dir()]

        # The ONLY honest mutation test. `outcome.ok` is "unverified" for all
        # three file tools, and file_edit has three no-op return paths, one of
        # which ("Already applied: ...") does not even say "Error". Sniffing
        # either would be the exact regression W3-1 deleted.
        if post == pending.pre_sha and not created_dirs:
            return None

        files = [
            FileChange(
                path=str(target),
                pre_sha=pending.pre_sha,
                post_sha=post,
                existed=pending.existed,
                kind="file",
                mode=pending.mode,
            )
        ]
        files.extend(
            FileChange(path=d, pre_sha=None, post_sha=None, existed=False, kind="dir")
            for d in created_dirs
        )
        return await asyncio.to_thread(self._write_checkpoint, pending, files)

    async def _after_ledger(self, pending: Pending) -> Checkpoint | None:
        root = pending.root
        if root is None or not pending.covered:
            return None
        ledger = self._ledgers.get(_key(root))
        if ledger is None or not ledger.enabled:
            return None
        files = await asyncio.to_thread(self._diff_ledger, ledger, root, pending.pre_entries)
        if not files:
            return None
        return await asyncio.to_thread(self._write_checkpoint, pending, files)

    def _diff_ledger(
        self, ledger: _Ledger, root: Path, pre: dict[str, _Entry]
    ) -> list[FileChange]:
        stats, truncated = _walk(root, self.ignore_dirs, self.max_entries, self.walk_budget_ms)
        if truncated:
            ledger.enabled = False
            ledger.reason = f"{root} grew past the checkpoint walk budget"
            return []

        changes: list[FileChange] = []
        blobs: dict[str, bytes] = {}
        fresh: dict[str, _Entry] = {}
        stamp = time.time_ns()

        for key, (path_str, mtime_ns, size) in stats.items():
            old = pre.get(key)
            racy = old is not None and old.mtime_ns >= ledger.stamp_ns - RACY_TICK_NS
            if old is not None and not racy and old.mtime_ns == mtime_ns and old.size == size:
                fresh[key] = _Entry(path_str, mtime_ns, size, old.sha)
                continue
            try:
                body = Path(path_str).read_bytes() if size <= self.max_file_bytes else None
            except OSError:
                continue
            if body is None:
                digest = hashlib.sha256()
                try:
                    with open(path_str, "rb") as handle:
                        for chunk in iter(lambda: handle.read(1 << 20), b""):
                            digest.update(chunk)
                except OSError:
                    continue
                sha = digest.hexdigest()
            else:
                sha = _sha(body)
                blobs[sha] = body
            fresh[key] = _Entry(path_str, mtime_ns, size, sha)
            if old is None:
                changes.append(
                    FileChange(path=path_str, pre_sha=None, post_sha=sha, existed=False)
                )
            elif old.sha != sha:
                changes.append(
                    FileChange(path=path_str, pre_sha=old.sha, post_sha=sha, existed=True)
                )

        for key, old in pre.items():
            if key not in stats:
                changes.append(
                    FileChange(path=old.path, pre_sha=old.sha, post_sha=None, existed=True)
                )

        if blobs:
            conn = self._connect()
            try:
                self._store_blobs(conn, blobs)
                conn.commit()
            finally:
                conn.close()
        ledger.entries = fresh
        ledger.stamp_ns = stamp
        return changes

    # -- row writing --------------------------------------------------------

    def _write_checkpoint(
        self,
        pending: Pending,
        files: list[FileChange],
        *,
        tool_name: str | None = None,
        label: str | None = None,
    ) -> Checkpoint:
        cp_id = uuid.uuid4().hex
        coverage = "" if pending.covered else pending.reason
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM checkpoints WHERE session_id = ?",
                (pending.session_id,),
            ).fetchone()
            seq = int(row[0]) + 1
            checkpoint = Checkpoint(
                id=cp_id,
                session_id=pending.session_id,
                turn_id=pending.turn_id,
                seq=seq,
                created_at=_now(),
                tool_name=tool_name or pending.tool_name,
                label=label if label is not None else pending.label,
                coverage=coverage,
                files=files,
            )
            conn.execute(
                "INSERT INTO checkpoints "
                "(id, session_id, turn_id, seq, entry_id, created_at, tool_name, label, coverage) "
                "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                (
                    checkpoint.id,
                    checkpoint.session_id,
                    checkpoint.turn_id,
                    checkpoint.seq,
                    checkpoint.created_at,
                    checkpoint.tool_name,
                    checkpoint.label,
                    checkpoint.coverage,
                ),
            )
            conn.executemany(
                "INSERT INTO checkpoint_files "
                "(checkpoint_id, path, pre_sha, post_sha, existed, kind, mode) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        cp_id,
                        f.path,
                        f.pre_sha,
                        f.post_sha,
                        1 if f.existed else 0,
                        f.kind,
                        f.mode,
                    )
                    for f in files
                ],
            )
            conn.commit()
        finally:
            conn.close()
        self.enforce_budget()
        return checkpoint

    # -- reading ------------------------------------------------------------

    def _session_chain(self, session_id: str) -> list[str]:
        """``session_id`` plus every ancestor it was forked from.

        ``SessionDB.fork_session`` copies conversation rows but not checkpoint
        rows -- those tables did not exist when that SQL was written, and they
        are not per-session copies anyway. Without walking the chain, ``/undo``
        in a freshly forked session finds nothing and reports "nothing to undo"
        over a tree it could perfectly well restore.
        """
        chain = [session_id]
        seen = {session_id}
        conn = self._connect()
        try:
            current = session_id
            for _ in range(64):
                try:
                    row = conn.execute(
                        "SELECT parent_session_id FROM sessions WHERE id = ?", (current,)
                    ).fetchone()
                except sqlite3.Error:
                    break
                if row is None or not row[0] or row[0] in seen:
                    break
                current = str(row[0])
                seen.add(current)
                chain.append(current)
        finally:
            conn.close()
        return chain

    def checkpoints(
        self, session_id: str, *, limit: int = 200, include_restores: bool = False
    ) -> list[Checkpoint]:
        """Checkpoints for a session and its fork ancestry, newest first."""
        chain = self._session_chain(session_id)
        placeholders = ",".join("?" * len(chain))
        sql = (
            "SELECT id, session_id, turn_id, seq, entry_id, created_at, tool_name, label, coverage "
            f"FROM checkpoints WHERE session_id IN ({placeholders})"
        )
        params: list[Any] = list(chain)
        if not include_restores:
            sql += " AND tool_name != ?"
            params.append(RESTORE_TOOL)
        sql += " ORDER BY created_at DESC, seq DESC LIMIT ?"
        params.append(limit)
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            out = [self._row_to_checkpoint(conn, row) for row in rows]
        finally:
            conn.close()
        return out

    def _row_to_checkpoint(self, conn: sqlite3.Connection, row: sqlite3.Row) -> Checkpoint:
        files = [
            FileChange(
                path=f["path"],
                pre_sha=f["pre_sha"],
                post_sha=f["post_sha"],
                existed=bool(f["existed"]),
                kind=f["kind"] or "file",
                mode=f["mode"],
            )
            for f in conn.execute(
                "SELECT path, pre_sha, post_sha, existed, kind, mode "
                "FROM checkpoint_files WHERE checkpoint_id = ? ORDER BY rowid",
                (row["id"],),
            )
        ]
        return Checkpoint(
            id=row["id"],
            session_id=row["session_id"],
            turn_id=row["turn_id"] or "",
            seq=int(row["seq"] or 0),
            created_at=row["created_at"],
            tool_name=row["tool_name"] or "",
            label=row["label"] or "",
            coverage=row["coverage"] or "",
            entry_id=row["entry_id"],
            files=files,
        )

    def turns(self, session_id: str, *, limit: int = 40) -> list[list[Checkpoint]]:
        """Checkpoints grouped by turn, newest turn first."""
        grouped: dict[str, list[Checkpoint]] = {}
        order: list[str] = []
        for cp in self.checkpoints(session_id, limit=limit * 20):
            bucket = cp.turn_id or cp.id
            if bucket not in grouped:
                grouped[bucket] = []
                order.append(bucket)
            grouped[bucket].append(cp)
        return [grouped[b] for b in order[:limit]]

    def last_turn(self, session_id: str) -> list[Checkpoint]:
        """The newest turn that actually touched the filesystem."""
        turns = self.turns(session_id, limit=1)
        return turns[0] if turns else []

    def resolve_seq(self, session_id: str, seq: int) -> list[Checkpoint]:
        """Every checkpoint from the newest down to and including ``#seq``."""
        out = [cp for cp in self.checkpoints(session_id, limit=5000) if cp.seq >= seq]
        return out

    def pending_redo(self, session_id: str) -> Checkpoint | None:
        """The newest unconsumed restore checkpoint, if any."""
        conn = self._connect()
        try:
            chain = self._session_chain(session_id)
            placeholders = ",".join("?" * len(chain))
            row = conn.execute(
                "SELECT id, session_id, turn_id, seq, entry_id, created_at, tool_name, "
                f"label, coverage FROM checkpoints WHERE session_id IN ({placeholders}) "
                "AND tool_name = ? AND label != 'consumed' "
                "ORDER BY created_at DESC, seq DESC LIMIT 1",
                (*chain, RESTORE_TOOL),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_checkpoint(conn, row)
        finally:
            conn.close()

    def _mark_consumed(self, checkpoint_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE checkpoints SET label = 'consumed' WHERE id = ?", (checkpoint_id,)
            )
            conn.commit()
        finally:
            conn.close()

    # -- restore ------------------------------------------------------------

    def plan_restore(self, checkpoints: list[Checkpoint]) -> list[PlannedAction]:
        """What a restore of ``checkpoints`` would do, without doing any of it.

        Nothing is written until the surface has shown this and the user has
        said yes. There is no flag, config key or approval mode that skips the
        confirmation: unlike a tool call, an undo is not something the model
        asked for, and a mis-fired one destroys work this store does not hold.
        """
        targets, dirs = _collapse(checkpoints)
        actions: list[PlannedAction] = []
        conn = self._connect()
        try:
            for key in sorted(targets):
                target_pre_sha, target_existed, latest_post_sha, path = targets[key]
                current = _hash_file(Path(path))

                # 1. Already at the target state.
                if target_existed and current is not None and current == target_pre_sha:
                    actions.append(PlannedAction("noop", path, "already at the restored content"))
                    continue
                if not target_existed and current is None:
                    actions.append(PlannedAction("noop", path, "already absent"))
                    continue

                # 2. Did anything touch it since DJcode last wrote it? Compared
                #    by HASH, never by (mtime, size). `current is None and
                #    latest_post_sha is None` is the tool-deleted-it case and
                #    matches, which is what makes `rm -rf build/` restorable.
                if current != latest_post_sha:
                    if current is None:
                        reason = "deleted it since — not restored"
                    elif latest_post_sha is None:
                        reason = "recreated outside DJcode since — not touched"
                    else:
                        reason = "changed outside DJcode since — not touched"
                    actions.append(PlannedAction("skip", path, reason))
                    continue

                # 3. Apply.
                if not target_existed:
                    actions.append(PlannedAction("delete", path, "created this turn"))
                    continue
                if target_pre_sha is None or self._load_blob(conn, target_pre_sha) is None:
                    actions.append(
                        PlannedAction(
                            "skip", path, "its previous contents were never saved (size/budget)"
                        )
                    )
                    continue
                actions.append(PlannedAction("restore", path, ""))
        finally:
            conn.close()
        for d in sorted(dirs, key=len, reverse=True):
            if Path(d).is_dir():
                actions.append(PlannedAction("keep-dir", d, "created this turn"))
        return actions

    def restore(
        self, checkpoints: list[Checkpoint], *, session_id: str, make_redo: bool = True
    ) -> RestoreReport:
        """Apply the plan. Per-file atomic, bytes not text, skips never clobbered."""
        report = RestoreReport(checkpoint_ids=[cp.id for cp in checkpoints])
        if not checkpoints:
            report.notes.append("Nothing to restore.")
            return report
        actions = self.plan_restore(checkpoints)
        targets, dirs = _collapse(checkpoints)

        redo_files: list[FileChange] = []
        conn = self._connect()
        try:
            for action in actions:
                if action.action == "skip":
                    report.skipped.append((action.path, action.reason))
                    continue
                if action.action in {"noop", "keep-dir"}:
                    if action.action == "keep-dir":
                        report.kept_dirs.append(action.path)
                    continue
                key = _key(action.path)
                target_pre_sha, target_existed, _latest, path = targets[key]
                current_sha = _hash_file(Path(path))
                current_body = None
                if current_sha is not None:
                    try:
                        current_body = Path(path).read_bytes()
                    except OSError:
                        current_body = None
                if action.action == "delete":
                    try:
                        Path(path).unlink()
                    except OSError as exc:
                        report.failed.append((path, str(exc)))
                        continue
                    report.deleted.append(path)
                else:
                    body = self._load_blob(conn, target_pre_sha or "")
                    if body is None:
                        report.skipped.append((path, "pre-image no longer in the store"))
                        continue
                    mode = None
                    for cp in checkpoints:
                        for f in cp.files:
                            if _key(f.path) == key and f.mode is not None:
                                mode = f.mode
                    try:
                        _atomic_write(Path(path), body, mode)
                    except OSError as exc:
                        report.failed.append((path, str(exc)))
                        continue
                    report.restored.append(path)
                if make_redo:
                    if current_body is not None and current_sha is not None:
                        self._store_blobs(conn, {current_sha: current_body})
                    redo_files.append(
                        FileChange(
                            path=path,
                            pre_sha=current_sha,
                            post_sha=target_pre_sha if target_existed else None,
                            existed=current_sha is not None,
                        )
                    )
            conn.commit()
        finally:
            conn.close()

        # Empty directories the turn created are pruned only when they are
        # empty, deepest first, and never recursively -- a directory the user
        # has since put something in is theirs, not ours.
        for d in sorted(dirs, key=len, reverse=True):
            p = Path(d)
            if p.is_dir():
                try:
                    p.rmdir()
                    report.deleted.append(d)
                    if d in report.kept_dirs:
                        report.kept_dirs.remove(d)
                except OSError:
                    pass

        if make_redo and redo_files:
            pending = Pending(
                tool_name=RESTORE_TOOL,
                session_id=session_id,
                turn_id="",
                label="",
                kind="file",
            )
            redo = self._write_checkpoint(
                pending, redo_files, tool_name=RESTORE_TOOL, label=""
            )
            report.redo_id = redo.id
        report.notes.extend(self.coverage_notes())
        for cp in checkpoints:
            if cp.coverage:
                report.notes.append(f"checkpoint #{cp.seq}: {cp.coverage}")
        return report

    def restore_ids(
        self, checkpoint_ids: list[str], *, session_id: str, make_redo: bool = True
    ) -> RestoreReport:
        conn = self._connect()
        try:
            found: list[Checkpoint] = []
            for cid in checkpoint_ids:
                row = conn.execute(
                    "SELECT id, session_id, turn_id, seq, entry_id, created_at, tool_name, "
                    "label, coverage FROM checkpoints WHERE id = ?",
                    (cid,),
                ).fetchone()
                if row is not None:
                    found.append(self._row_to_checkpoint(conn, row))
        finally:
            conn.close()
        found.sort(key=lambda c: (c.created_at, c.seq), reverse=True)
        return self.restore(found, session_id=session_id, make_redo=make_redo)

    def redo(self, session_id: str) -> RestoreReport:
        """Re-apply the most recent undo. Single level, by design."""
        target = self.pending_redo(session_id)
        if target is None:
            report = RestoreReport()
            report.notes.append("Nothing to redo.")
            return report
        self._mark_consumed(target.id)
        report = self.restore([target], session_id=session_id, make_redo=True)
        if report.redo_id:
            # The redo's own restore-point is consumed immediately: /redo is
            # one level deep and saying so beats an undo/redo ping-pong that
            # silently flips the tree back and forth.
            self._mark_consumed(report.redo_id)
            report.redo_id = None
        return report


# ---------------------------------------------------------------------------
# Free functions
# ---------------------------------------------------------------------------


def _collapse(
    checkpoints: list[Checkpoint],
) -> tuple[dict[str, tuple[str | None, bool, str | None, str]], list[str]]:
    """Fold a group of checkpoints into one target state per path.

    The OLDEST checkpoint touching a path owns the content to restore; the
    NEWEST owns the ``post_sha`` we compare the file on disk against. Undoing
    three successive edits to one file must land on the state before the first,
    while still refusing to clobber an edit the user made after the third.
    """
    ordered = sorted(checkpoints, key=lambda c: (c.created_at, c.seq))
    targets: dict[str, tuple[str | None, bool, str | None, str]] = {}
    dirs: list[str] = []
    for cp in ordered:
        for f in cp.files:
            if f.kind == "dir":
                if f.path not in dirs:
                    dirs.append(f.path)
                continue
            key = _key(f.path)
            if key in targets:
                pre, existed, _post, path = targets[key]
                targets[key] = (pre, existed, f.post_sha, path)
            else:
                targets[key] = (f.pre_sha, f.existed, f.post_sha, f.path)
    return targets, dirs


def _hash_file(path: Path) -> str | None:
    """sha256 of the bytes on disk, or ``None`` when there is no regular file."""
    try:
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _atomic_write(path: Path, body: bytes, mode: int | None) -> None:
    """Write ``body`` to ``path`` via a sibling temp file and ``os.replace``.

    ``os.replace`` is atomic on both platforms, so a failure mid-restore leaves
    the old file intact rather than a truncated one. Bytes, never text.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.djcode-undo-{uuid.uuid4().hex[:8]}")
    try:
        with open(tmp, "wb") as handle:
            handle.write(body)
        if mode is not None and not _IS_WINDOWS:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _missing_ancestors(target: Path) -> list[str]:
    """Directories ``file_write``'s ``mkdir(parents=True)`` is about to create.

    Without these, undoing the creation of ``deep/nested/w.txt`` deletes the
    file and leaves two empty directories behind; the blueprint's
    ``checkpoint_files`` schema has no directory concept at all.
    """
    out: list[str] = []
    current = target.parent
    while True:
        if current.exists() or current.parent == current:
            break
        out.append(str(current))
        current = current.parent
    out.reverse()
    return out


def _walk(
    root: Path, skip: frozenset[str], max_entries: int, budget_ms: float
) -> tuple[dict[str, tuple[str, int, int]], bool]:
    """Stat-only walk of ``root``. ``key -> (path, mtime_ns, size)``.

    Excludes ``CONFIG_DIR`` unconditionally: it holds ``sessions.db`` itself,
    the spill directory and the cached Rust DAF build tree, and this harness
    puts it *inside* a working directory. Walking it would checkpoint DJcode's
    own scratch files on every shell command.

    ``truncated`` is ``True`` when the entry cap or the time budget stopped the
    walk early. The caller turns the ledger off rather than shipping a partial
    one, because "the first 20 000 files scandir happened to return" is not a
    set the user can reason about and is not the same set twice.
    """
    out: dict[str, tuple[str, int, int]] = {}
    started = time.monotonic()
    seen_dirs: set[tuple[int, int]] = set()
    config_key = _key(CONFIG_DIR.resolve()) if CONFIG_DIR else ""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in skip:
                        continue
                    if _key(entry.path) == config_key:
                        continue
                    st = entry.stat(follow_symlinks=False)
                    # Windows junctions report is_dir(follow_symlinks=False)
                    # True and is_symlink() False, so a naive walk descends into
                    # them -- duplicate entries, and with a cycle, unbounded
                    # recursion. st_ino is 0 on some filesystems; only dedupe
                    # when it is real.
                    ident = (st.st_dev, st.st_ino)
                    if ident[1]:
                        if ident in seen_dirs:
                            continue
                        seen_dirs.add(ident)
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    out[_key(entry.path)] = (entry.path, st.st_mtime_ns, st.st_size)
                    if len(out) > max_entries:
                        return out, True
            except OSError:
                continue
        if (time.monotonic() - started) * 1000 > budget_ms:
            return out, True
    return out, False


def store_from_config(db: Any, *, cwd: str | Path | None = None) -> CheckpointStore:
    """Build a store from the user's config. Kept out of ``__init__`` so tests
    can construct a store with explicit budgets and no config file."""
    from djcode.config import load_config

    cfg = load_config()
    ignore = cfg.get("checkpoint_ignore_dirs") or DEFAULT_IGNORE_DIRS
    if isinstance(ignore, str):
        ignore = tuple(part for part in ignore.split(",") if part.strip())
    return CheckpointStore(
        db,
        cwd=cwd,
        budget_mb=float(cfg.get("checkpoint_budget_mb", 256)),
        max_file_mb=float(cfg.get("checkpoint_max_file_mb", 8)),
        bash_enabled=bool(cfg.get("checkpoint_bash", True)),
        walk_budget_ms=float(cfg.get("checkpoint_walk_budget_ms", 400)),
        baseline_budget_ms=float(cfg.get("checkpoint_baseline_budget_ms", 1500)),
        max_entries=int(cfg.get("checkpoint_max_entries", 20000)),
        ignore_dirs=tuple(ignore),
    )
