"""W1-8 / GAP B16: file_edit tells the caller how to succeed on the next try.

A failing edit used to return "old_string not found" and nothing else, which
costs a round trip and usually a second guess. These tests pin the recovery
ladder: already-applied, whitespace-normalised match, nearest candidate with a
line number and the exact differing bytes, and a distinguishable report when
the match is ambiguous.
"""

import asyncio

from djcode.tools.file_edit import execute_file_edit


def edit(path, old, new) -> str:
    # W7-2 changed the contract: the handler returns a ToolOutcome, not a str.
    # `ToolOutcome.__str__` is `content`, so every assertion below is unchanged
    # -- only this one conversion moved. `test_diff.py` pins the new `ok` flag
    # on all eight return paths.
    return str(asyncio.run(execute_file_edit(str(path), old, new)))


def write(path, text: str) -> None:
    """Write bytes literally: no newline translation in either direction."""
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def read(path) -> str:
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


# -- The happy path still behaves ---------------------------------------------


def test_unique_match_is_replaced(tmp_path):
    target = tmp_path / "unique.py"
    write(target, "alpha\nbeta\ngamma\n")

    result = edit(target, "beta", "delta")

    assert "replaced 1 occurrence" in result
    assert not result.startswith("Error")
    assert read(target) == "alpha\ndelta\ngamma\n"


def test_missing_file_is_named(tmp_path):
    result = edit(tmp_path / "nope.py", "a", "b")
    assert result.startswith("Error: File not found")


# -- Rung 1: already applied ---------------------------------------------------


def test_already_applied_reads_as_success(tmp_path):
    target = tmp_path / "applied.py"
    write(target, "goodbye world\n")

    result = edit(target, "hello", "goodbye")

    assert result.startswith("Already applied:")
    assert "already contains new_string" in result
    # A success must not look like a failure to a caller that checks the prefix.
    assert not result.startswith("Error")
    assert read(target) == "goodbye world\n"


def test_absent_old_string_is_not_called_applied_when_new_string_is_empty(tmp_path):
    target = tmp_path / "empty-new.py"
    write(target, "alpha\n")

    result = edit(target, "zeta", "")

    # "" is in every file; that must not be read as "already applied".
    assert not result.startswith("Already applied")
    assert result.startswith("Error: old_string not found")


# -- Rung 2: whitespace- and line-ending-normalised match ----------------------


def test_tabs_versus_spaces_still_applies(tmp_path):
    target = tmp_path / "tabs.py"
    write(target, "def f():\n\tif x:\n\t\treturn 1\n")

    result = edit(target, "    if x:\n        return 1", "    if y:\n        return 2")

    assert "replaced 1 occurrence at line 2" in result
    assert "normalising whitespace" in result
    assert read(target) == "def f():\n    if y:\n        return 2\n"


def test_lf_old_string_matches_a_crlf_file(tmp_path):
    target = tmp_path / "crlf.txt"
    write(target, "line one\r\nline two\r\n")

    result = edit(target, "line one\nline two", "line one\nline TWO")

    assert "replaced 1 occurrence at line 1" in result
    assert "line endings" in result
    # The edit lands, and the line ending the file already had is untouched.
    assert read(target) == "line one\nline TWO\r\n"


def test_ambiguous_after_normalising_refuses_to_guess(tmp_path):
    target = tmp_path / "ambiguous-ws.py"
    write(target, "a\n\tvalue = 1\nb\n    value = 1\nc\n")

    result = edit(target, "\t\tvalue = 1", "value = 2")

    assert result.startswith("Error:")
    assert "more than one place" in result
    # Nothing was written on a guess.
    assert read(target) == "a\n\tvalue = 1\nb\n    value = 1\nc\n"


# -- Rung 3: nearest candidate, with a line number and the differing bytes -----


def test_nearest_candidate_carries_a_line_number_and_the_bytes(tmp_path):
    target = tmp_path / "near.py"
    write(
        target,
        "def alpha():\n    return 1\n\ndef beta(a, b):\n    return a + b\n",
    )

    result = edit(target, "def beta(a,b):", "def beta(a, b, c):")

    assert result.startswith("Error: old_string not found")
    assert "Nearest match at line 4" in result
    assert "similar" in result
    # The exact bytes, not a rendered diff that hides whitespace.
    assert "column" in result
    assert repr(" ") in result or repr(", ") in result
    assert "def beta(a, b):" in result


def test_nearest_candidate_reports_a_multiline_window(tmp_path):
    target = tmp_path / "multi.py"
    write(
        target,
        "header\n\ndef gamma(value):\n    total = value * 2\n    return total\n",
    )

    result = edit(
        target,
        "def gamma(value):\n    total = value * 3\n    return total",
        "def gamma(value):\n    return value * 4",
    )

    assert result.startswith("Error: old_string not found")
    assert "Nearest match at line 3" in result
    # The differing line inside the window is pinpointed, not just the window.
    assert "line 4, column" in result
    assert repr("3") in result and repr("2") in result


def test_crlf_mismatch_is_named_when_no_region_matches(tmp_path):
    target = tmp_path / "hint.txt"
    write(target, "alpha alpha\r\nbeta beta\r\nzzzz\r\n")

    result = edit(target, "nothing like this\nat all here", "x")

    assert result.startswith("Error: old_string not found")
    assert "CRLF" in result


# -- count > 1: distinct context per occurrence --------------------------------


def test_ambiguous_match_lists_each_occurrence_with_context(tmp_path):
    target = tmp_path / "dupes.py"
    write(
        target,
        "def a():\n"
        "    value = compute()\n"
        "    return value\n"
        "\n"
        "def b():\n"
        "    value = compute()\n"
        "    return value\n",
    )

    result = edit(target, "    value = compute()", "    value = compute(2)")

    assert result.startswith("Error: old_string found 2 times")
    assert "occurrence 1 at line 2" in result
    assert "occurrence 2 at line 6" in result
    # Two lines of context per occurrence, and they are what tells them apart.
    assert "def a():" in result
    assert "def b():" in result
    # Nothing was edited.
    assert read(target).count("compute()") == 2


def test_many_occurrences_are_summarised(tmp_path):
    target = tmp_path / "many.py"
    write(target, "".join(f"x = 1  # {i}\n" for i in range(9)))

    result = edit(target, "x = 1", "x = 2")

    assert result.startswith("Error: old_string found 9 times")
    assert "occurrence 5 at line" in result
    assert "and 4 more occurrences" in result


# -- Line endings are the file's business, not the platform's ------------------


def test_plain_edit_does_not_rewrite_line_endings(tmp_path):
    crlf = tmp_path / "keep-crlf.txt"
    write(crlf, "one\r\ntwo\r\nthree\r\n")
    assert edit(crlf, "two", "TWO").startswith("Edited")
    assert crlf.read_bytes() == b"one\r\nTWO\r\nthree\r\n"

    lf = tmp_path / "keep-lf.txt"
    write(lf, "one\ntwo\nthree\n")
    assert edit(lf, "two", "TWO").startswith("Edited")
    assert lf.read_bytes() == b"one\nTWO\nthree\n"
