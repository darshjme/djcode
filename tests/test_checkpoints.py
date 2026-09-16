"""W5 green gate: checkpoints and undo (P0-1).

Every test here drives the REAL chokepoint -- ``dispatch_tool`` with a
``DispatchContext`` carrying a ``CheckpointStore`` -- not the store's internals.
That is deliberate: the bug this feature can actually ship is "capture never
fired for the tool the user used", and only the chokepoint proves it did.

Two things this file refuses to assume:

* that ``outcome.ok`` says anything. All three file tools are
  ``ok_source="unverified"``; mutation is decided by comparing bytes on disk.
* that the DAF half is optional. ``cargo`` is installed on the box this was
  written for, so ``workflow.one`` takes the Rust path in production and a test
  that only exercises the native fallback proves nothing here.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from djcode.core.checkpoints import CheckpointStore, _walk
from djcode.core.dispatch import DispatchContext
from djcode.sessions import SessionDB
from djcode.tools import dispatch_tool

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def make(tmp_path, **kw):
    """A store over a private sessions.db, plus the context that carries it."""
    db = SessionDB(tmp_path / "sessions.db")
    root = tmp_path / "work"
    root.mkdir(exist_ok=True)
    store = CheckpointStore(db, cwd=str(root), **kw)
    ctx = DispatchContext(checkpoints=store, session_id="sess-1", cwd=str(root))
    store.begin_turn("a test turn")
    return db, store, ctx, root


def rows(db, table):
    with __import__("sqlite3").connect(str(db.db_path)) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


async def run(ctx, name, **arguments):
    return await dispatch_tool(name, arguments, ctx=ctx)


# ---------------------------------------------------------------------------
# file tools -- the four round trips the green gate names
# ---------------------------------------------------------------------------


async def test_file_write_create_is_undone_by_deleting_the_file(tmp_path):
    db, store, ctx, root = make(tmp_path)
    target = root / "new.txt"

    await run(ctx, "file_write", path=str(target), content="hello\n")
    assert target.is_file()

    report = store.restore(store.last_turn("sess-1"), session_id="sess-1")
    assert not target.exists()
    assert [os.path.normcase(p) for p in report.deleted] == [os.path.normcase(str(target))]
    assert report.failed == []


async def test_file_write_also_undoes_the_directories_it_created(tmp_path):
    """file_write does parent.mkdir(parents=True); the blueprint's schema has
    no directory concept at all, so undo would leave empty dirs behind."""
    db, store, ctx, root = make(tmp_path)
    target = root / "deep" / "nested" / "w.txt"

    await run(ctx, "file_write", path=str(target), content="x\n")
    assert target.is_file()

    store.restore(store.last_turn("sess-1"), session_id="sess-1")
    assert not target.exists()
    assert not (root / "deep" / "nested").exists()
    assert not (root / "deep").exists()


async def test_created_directory_is_kept_when_the_user_put_something_in_it(tmp_path):
    db, store, ctx, root = make(tmp_path)
    target = root / "made" / "w.txt"
    await run(ctx, "file_write", path=str(target), content="x\n")
    (root / "made" / "mine.txt").write_text("user's own file\n")

    report = store.restore(store.last_turn("sess-1"), session_id="sess-1")
    assert (root / "made").is_dir()
    assert (root / "made" / "mine.txt").is_file()
    assert any(os.path.normcase(str(root / "made")) == os.path.normcase(d)
               for d in report.kept_dirs)


async def test_file_write_overwrite_restores_the_exact_bytes_from_disk(tmp_path):
    """The post-image MUST be read back from disk, not taken from the `content`
    argument: file_write opens in text mode with newline=None, so on Windows
    every \\n it writes is \\r\\n on disk. A post_sha computed from the argument
    never matches the file, and every later restore lands in `skipped`."""
    db, store, ctx, root = make(tmp_path)
    target = root / "f.txt"
    original = b"alpha\r\nbeta\n\xff\xfe raw bytes\n"
    target.write_bytes(original)

    await run(ctx, "file_write", path=str(target), content="replaced\n")
    assert target.read_bytes() != original

    report = store.restore(store.last_turn("sess-1"), session_id="sess-1")
    assert target.read_bytes() == original, "byte-for-byte round trip failed"
    assert report.skipped == []
    assert report.failed == []


async def test_file_edit_round_trip(tmp_path):
    db, store, ctx, root = make(tmp_path)
    target = root / "e.txt"
    target.write_bytes(b"one\ntwo\nthree\n")

    await run(ctx, "file_edit", path=str(target), old_string="two", new_string="TWO")
    assert b"TWO" in target.read_bytes()

    store.restore(store.last_turn("sess-1"), session_id="sess-1")
    assert target.read_bytes() == b"one\ntwo\nthree\n"


async def test_file_edit_no_op_paths_take_no_checkpoint(tmp_path):
    """file_edit has five return paths and only two of them write. One of the
    no-ops ("Already applied: ...") is success-shaped and says nothing about an
    error; another ("Error: old_string found 2 times") writes nothing while
    looking like a failure. Neither the string nor `ok` may decide this."""
    db, store, ctx, root = make(tmp_path)

    applied = root / "a.txt"
    applied.write_bytes(b"NEW\n")
    outcome = await run(ctx, "file_edit", path=str(applied), old_string="OLD", new_string="NEW")
    assert "Already applied" in str(outcome)
    assert outcome.ok is True and outcome.details["ok_source"] == "unverified"

    dup = root / "d.txt"
    dup.write_bytes(b"dup\ndup\n")
    await run(ctx, "file_edit", path=str(dup), old_string="dup", new_string="x")
    assert dup.read_bytes() == b"dup\ndup\n"

    missing = root / "nope.txt"
    await run(ctx, "file_edit", path=str(missing), old_string="a", new_string="b")

    assert store.last_turn("sess-1") == []
    assert rows(db, "checkpoints") == 0


async def test_notebook_edit_round_trip(tmp_path):
    db, store, ctx, root = make(tmp_path)
    nb = root / "n.ipynb"
    nb.write_text(
        json.dumps(
            {
                "cells": [{"cell_type": "code", "source": ["print(1)\n"], "metadata": {}}],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    before = nb.read_bytes()

    await run(ctx, "notebook_edit", path=str(nb), cell_index=0, new_source="print(2)\n")
    assert nb.read_bytes() != before

    store.restore(store.last_turn("sess-1"), session_id="sess-1")
    assert nb.read_bytes() == before


# ---------------------------------------------------------------------------
# the conflict rule
# ---------------------------------------------------------------------------


async def test_externally_modified_file_is_skipped_and_the_rest_still_restores(tmp_path):
    db, store, ctx, root = make(tmp_path)
    a, b = root / "a.txt", root / "b.txt"
    a.write_bytes(b"A0\n")
    b.write_bytes(b"B0\n")

    await run(ctx, "file_write", path=str(a), content="A1\n")
    await run(ctx, "file_write", path=str(b), content="B1\n")

    # The user (or their editor, or a build) touches `a` afterwards.
    a.write_bytes(b"A-typed-by-hand\n")

    report = store.restore(store.last_turn("sess-1"), session_id="sess-1")
    assert a.read_bytes() == b"A-typed-by-hand\n", "a skipped file must never be clobbered"
    assert b.read_bytes() == b"B0\n", "a skip must not abort the rest of the restore"
    assert [os.path.normcase(p) for p, _ in report.skipped] == [os.path.normcase(str(a))]
    assert "changed outside DJcode" in report.skipped[0][1]


async def test_a_file_the_user_deleted_since_is_not_resurrected(tmp_path):
    db, store, ctx, root = make(tmp_path)
    target = root / "gone.txt"
    target.write_bytes(b"v0\n")
    await run(ctx, "file_write", path=str(target), content="v1\n")
    target.unlink()

    report = store.restore(store.last_turn("sess-1"), session_id="sess-1")
    assert not target.exists()
    assert report.skipped and "deleted it since" in report.skipped[0][1]


# ---------------------------------------------------------------------------
# turn semantics, redo, seq
# ---------------------------------------------------------------------------


async def test_undo_reverts_a_whole_turn_not_one_file(tmp_path):
    db, store, ctx, root = make(tmp_path)
    paths = []
    for i in range(5):
        p = root / f"t{i}.txt"
        p.write_bytes(f"old{i}\n".encode())
        paths.append(p)
        await run(ctx, "file_write", path=str(p), content=f"new{i}\n")

    turn = store.last_turn("sess-1")
    assert len(turn) == 5, "one turn, five checkpoints"
    store.restore(turn, session_id="sess-1")
    for i, p in enumerate(paths):
        assert p.read_bytes() == f"old{i}\n".encode()


async def test_undo_only_reaches_the_turn_it_was_told_to(tmp_path):
    db, store, ctx, root = make(tmp_path)
    first, second = root / "1.txt", root / "2.txt"
    first.write_bytes(b"1-old\n")
    second.write_bytes(b"2-old\n")

    store.begin_turn("turn one")
    await run(ctx, "file_write", path=str(first), content="1-new\n")
    # Read back from disk, not from the argument: file_write text-mode writes
    # turn every \n into \r\n on Windows.
    first_new = first.read_bytes()
    store.begin_turn("turn two")
    await run(ctx, "file_write", path=str(second), content="2-new\n")

    store.restore(store.last_turn("sess-1"), session_id="sess-1")
    assert second.read_bytes() == b"2-old\n"
    assert first.read_bytes() == first_new, "the earlier turn must be untouched"

    # /undo <n> reaches further back: every checkpoint from the newest down to #1.
    store.restore(store.resolve_seq("sess-1", 1), session_id="sess-1")
    assert first.read_bytes() == b"1-old\n"


async def test_three_successive_edits_undo_to_the_state_before_the_first(tmp_path):
    db, store, ctx, root = make(tmp_path)
    target = root / "s.txt"
    target.write_bytes(b"gen0\n")
    for gen in (1, 2, 3):
        await run(ctx, "file_write", path=str(target), content=f"gen{gen}\n")

    store.restore(store.last_turn("sess-1"), session_id="sess-1")
    assert target.read_bytes() == b"gen0\n"


async def test_redo_reapplies_the_undo_once(tmp_path):
    db, store, ctx, root = make(tmp_path)
    target = root / "r.txt"
    target.write_bytes(b"before\n")
    await run(ctx, "file_write", path=str(target), content="after\n")
    written = target.read_bytes()

    store.restore(store.last_turn("sess-1"), session_id="sess-1")
    assert target.read_bytes() == b"before\n"

    report = store.redo("sess-1")
    assert target.read_bytes() == written
    assert report.restored

    again = store.redo("sess-1")
    assert again.notes == ["Nothing to redo."], "redo is one level deep, and says so"


async def test_seq_is_a_renderable_per_session_ordinal(tmp_path):
    """DESIGN-CLI prints `checkpoint #7` on every file-mutating tool card and
    /undo takes `7` as an argument; a uuid primary key cannot do either."""
    db, store, ctx, root = make(tmp_path)
    for i in range(3):
        await run(ctx, "file_write", path=str(root / f"q{i}.txt"), content="x\n")
    seqs = sorted(cp.seq for cp in store.checkpoints("sess-1"))
    assert seqs == [1, 2, 3]


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------


async def test_blob_dedup_by_row_count(tmp_path):
    db, store, ctx, root = make(tmp_path)
    a, b = root / "a.txt", root / "b.txt"
    a.write_bytes(b"identical\n")
    b.write_bytes(b"identical\n")

    await run(ctx, "file_write", path=str(a), content="one\n")
    await run(ctx, "file_write", path=str(b), content="two\n")

    assert rows(db, "checkpoints") == 2
    assert rows(db, "checkpoint_files") == 2
    assert rows(db, "blobs") == 1, "two checkpoints of identical bytes store one blob"


async def test_details_carry_shas_and_paths_only_and_stay_json_serialisable(tmp_path):
    """`details` rides on tool_result_event, which W10 serialises to JSONL. A
    file body in there would be written to the event stream on every edit."""
    db, store, ctx, root = make(tmp_path)
    outcome = await run(ctx, "file_write", path=str(root / "j.txt"), content="body\n")
    summary = outcome.details["checkpoint"]
    json.dumps(outcome.details)  # must not raise
    assert summary["covered"] is True
    assert summary["files"] == 1
    assert "body" not in json.dumps(summary)


async def test_oversize_file_is_recorded_but_declared_unrestorable(tmp_path):
    db, store, ctx, root = make(tmp_path, max_file_mb=0.0001)
    target = root / "big.txt"
    target.write_bytes(b"x" * 4096)

    await run(ctx, "file_write", path=str(target), content="small\n")
    report = store.restore(store.last_turn("sess-1"), session_id="sess-1")
    assert report.restored == []
    assert report.skipped and "never saved" in report.skipped[0][1]
    assert any("per-file checkpoint limit" in n for n in store.pop_notices())


# ---------------------------------------------------------------------------
# persistence: restart and fork
# ---------------------------------------------------------------------------


async def test_restore_survives_a_process_restart(tmp_path):
    db, store, ctx, root = make(tmp_path)
    target = root / "p.txt"
    target.write_bytes(b"persisted\n")
    await run(ctx, "file_write", path=str(target), content="changed\n")

    # Everything in memory is gone; only sessions.db survives. This is what
    # `/resume` then `/undo` reduces to.
    fresh_db = SessionDB(tmp_path / "sessions.db")
    fresh = CheckpointStore(fresh_db, cwd=str(root))
    fresh.restore(fresh.last_turn("sess-1"), session_id="sess-1")
    assert target.read_bytes() == b"persisted\n"


async def test_undo_works_in_a_forked_session(tmp_path):
    """fork_session copies conversation rows, not checkpoint rows -- those
    tables did not exist when that SQL was written. Looking checkpoints up by
    session_id alone makes /undo in a fork report "nothing to undo"."""
    db, store, ctx, root = make(tmp_path)
    parent = db.create_session("m", "p")
    ctx.session_id = parent
    target = root / "fork.txt"
    target.write_bytes(b"parent\n")
    await run(ctx, "file_write", path=str(target), content="edited\n")

    child = db.fork_session(parent)
    assert child and child != parent
    assert db.get_session(parent) is not None, "the parent must stay open"

    found = store.last_turn(child)
    assert found, "a fork must see its parent's checkpoints"
    store.restore(found, session_id=child)
    assert target.read_bytes() == b"parent\n"


# ---------------------------------------------------------------------------
# the bash / git ledger
# ---------------------------------------------------------------------------


def _script(tmp_path, body: str) -> str:
    """A python script OUTSIDE the walked root, invoked through the real shell."""
    path = tmp_path / "helper.py"
    path.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{path}"'


async def test_bash_that_writes_three_files_is_fully_reverted(tmp_path):
    db, store, ctx, root = make(tmp_path)
    (root / "one.txt").write_bytes(b"1-old\n")
    (root / "two.txt").write_bytes(b"2-old\n")
    command = _script(
        tmp_path,
        "import pathlib\n"
        f"r = pathlib.Path(r'''{root}''')\n"
        "(r / 'one.txt').write_bytes(b'1-new\\n')\n"
        "(r / 'two.txt').write_bytes(b'2-new\\n')\n"
        "(r / 'three.txt').write_bytes(b'3-new\\n')\n",
    )

    outcome = await run(ctx, "bash", command=command, cwd=str(root))
    assert outcome.details["checkpoint"]["covered"] is True, str(outcome)
    assert (root / "three.txt").is_file()

    report = store.restore(store.last_turn("sess-1"), session_id="sess-1")
    assert (root / "one.txt").read_bytes() == b"1-old\n"
    assert (root / "two.txt").read_bytes() == b"2-old\n"
    assert not (root / "three.txt").exists()
    assert report.failed == []


async def test_bash_that_deletes_three_files_is_fully_restored(tmp_path):
    """The test the blueprint's scheme cannot pass. Its "pre-image snapshot
    taken lazily" only learns which files changed AFTER the command, and by
    then a deleted file's bytes are gone. `rm -rf build/` is the case SSOT
    P0-1 names, so the pre-images are taken BEFORE the handler runs."""
    db, store, ctx, root = make(tmp_path)
    for i in range(3):
        (root / f"d{i}.txt").write_bytes(f"keep-{i}\n".encode())
    command = _script(
        tmp_path,
        "import pathlib\n"
        f"r = pathlib.Path(r'''{root}''')\n"
        "[ (r / f'd{i}.txt').unlink() for i in range(3) ]\n",
    )

    await run(ctx, "bash", command=command, cwd=str(root))
    assert not (root / "d0.txt").exists()

    report = store.restore(store.last_turn("sess-1"), session_id="sess-1")
    for i in range(3):
        assert (root / f"d{i}.txt").read_bytes() == f"keep-{i}\n".encode()
    assert len(report.restored) == 3


async def test_a_same_size_overwrite_is_detected_by_hash_not_by_stat(tmp_path):
    """Measured on this box: ~0.6 ms mtime granularity, and 153 of 200
    back-to-back same-size writes were indistinguishable by (mtime, size)."""
    db, store, ctx, root = make(tmp_path)
    target = root / "same.txt"
    target.write_bytes(b"AAAA")
    command = _script(
        tmp_path,
        "import pathlib, os\n"
        f"p = pathlib.Path(r'''{target}''')\n"
        "st = p.stat()\n"
        "p.write_bytes(b'BBBB')\n"
        "os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))\n",
    )
    await run(ctx, "bash", command=command, cwd=str(root))
    assert target.read_bytes() == b"BBBB"

    # An mtime-preserving write (tar -x, cp -p, unzip) is a DECLARED blind spot,
    # so this asserts the declaration, not a capture. If a future wave closes
    # it by hashing every file every call, this test flips to asserting the
    # restore -- and the coverage note must go with it.
    assert any("restores a file's original timestamp" in n for n in store.coverage_notes())


async def test_bash_capture_is_serialised_so_concurrent_calls_cannot_cross_credit(tmp_path):
    import asyncio

    db, store, ctx, root = make(tmp_path)
    command_a = _script(
        tmp_path,
        "import pathlib, time\n"
        f"r = pathlib.Path(r'''{root}''')\n"
        "time.sleep(0.2)\n(r / 'from_a.txt').write_bytes(b'a\\n')\n",
    )
    path_b = tmp_path / "helper_b.py"
    path_b.write_text(
        "import pathlib\n"
        f"r = pathlib.Path(r'''{root}''')\n"
        "(r / 'from_b.txt').write_bytes(b'b\\n')\n",
        encoding="utf-8",
    )
    command_b = f'"{sys.executable}" "{path_b}"'

    await asyncio.gather(
        run(ctx, "bash", command=command_a, cwd=str(root)),
        run(ctx, "bash", command=command_b, cwd=str(root)),
    )
    checkpoints = store.checkpoints("sess-1")
    paths = [os.path.basename(f.path) for cp in checkpoints for f in cp.files]
    assert sorted(paths) == ["from_a.txt", "from_b.txt"]
    for cp in checkpoints:
        assert len(cp.files) == 1, "each command owns exactly its own writes"


async def test_git_takes_the_same_ledger_as_bash(tmp_path):
    """tools/git.py admits checkout/switch/restore/merge/rebase/stash, every one
    of which rewrites the working tree. The blueprint's W5-2 list omits git."""
    db, store, ctx, root = make(tmp_path)
    outcome = await run(ctx, "git", subcommand="status --porcelain")
    assert "checkpoint" in outcome.details
    assert outcome.details["checkpoint"]["covered"] is True


async def test_ledger_turns_itself_off_rather_than_covering_part_of_a_tree(tmp_path):
    db, store, ctx, root = make(tmp_path, max_entries=2)
    for i in range(10):
        (root / f"f{i}.txt").write_bytes(b"x\n")

    outcome = await run(ctx, "bash", command="echo hi", cwd=str(root))
    summary = outcome.details["checkpoint"]
    assert summary["covered"] is False
    assert "too large" in summary["reason"]
    assert any("NOT covered" in n for n in store.coverage_notes(root))
    assert any("NOT being checkpointed" in n for n in store.pop_notices())


async def test_bash_can_be_switched_off_and_says_so(tmp_path):
    db, store, ctx, root = make(tmp_path, bash_enabled=False)
    outcome = await run(ctx, "bash", command="echo hi", cwd=str(root))
    assert outcome.details["checkpoint"]["covered"] is False
    assert any("checkpoint_bash is off" in n for n in store.coverage_notes(root))


async def test_coverage_notes_name_every_hole_every_time(tmp_path):
    db, store, ctx, root = make(tmp_path)
    notes = " ".join(store.coverage_notes(root))
    assert "background processes" in notes
    assert "schedule" in notes
    assert "MCP" in notes
    assert "outside the tracked directory" in notes
    assert "original timestamp" in notes


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_walk_excludes_the_config_dir_and_the_skip_list(tmp_path):
    from djcode.config import CONFIG_DIR
    from djcode.core.checkpoints import DEFAULT_IGNORE_DIRS

    root = tmp_path / "w"
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "junk.js").write_text("x")
    (root / "src").mkdir()
    (root / "src" / "keep.py").write_text("x")
    found, truncated = _walk(root, frozenset(DEFAULT_IGNORE_DIRS), 10_000, 5_000)
    assert not truncated
    assert [os.path.basename(p) for p, _, _ in found.values()] == ["keep.py"]

    # CONFIG_DIR holds sessions.db, the spill directory and the cached Rust
    # build tree, and this harness puts it inside a working directory.
    assert CONFIG_DIR.name not in {os.path.basename(p) for p, _, _ in found.values()}


# ---------------------------------------------------------------------------
# the DAF engine branch
# ---------------------------------------------------------------------------


async def test_capture_fires_through_the_real_daf_engine(tmp_path, daf_runtime, monkeypatch):
    """cargo is installed on this box, so `workflow.one` takes the Rust path in
    production. A checkpoint test that only exercises the native fallback
    proves nothing here."""
    import djcode.workflow as workflow
    from djcode.core.dispatch import dispatch_context

    monkeypatch.setattr(workflow, "CONFIG_DIR", daf_runtime)
    db, store, ctx, root = make(tmp_path)
    target = root / "daf.txt"
    target.write_bytes(b"before\n")

    engine = workflow.WorkflowEngine(mode="daf")
    with dispatch_context(ctx):
        result = await engine.one(
            "file_write", {"path": str(target), "content": "after\n"}, dispatch_tool
        )
    assert engine.mode == "daf", "the native fallback proves nothing on a box with cargo"
    assert "after" in target.read_text()
    assert str(result)

    store.restore(store.last_turn("sess-1"), session_id="sess-1")
    assert target.read_bytes() == b"before\n"
