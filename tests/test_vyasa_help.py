"""W1-10: `--vyasa` help must disclaim, not assert an unverifiable roster size.

PARITY-MATRIX records the marketing claim of 29 specialist personas as
server-side and unverifiable from the CLI: `vyasa.py` holds no roster and
`request_fleet()` returns whatever the fleet host sends. The help text
therefore states where the roster comes from instead of quoting a number.
"""

from __future__ import annotations

import re

from click.testing import CliRunner

from djcode.cli import main

DISCLAIMER = "The roster comes from your fleet server; this CLI ships no persona list of its own."


def _normalised_help() -> str:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0, result.output
    return " ".join(result.output.split())


def test_vyasa_help_carries_the_roster_disclaimer() -> None:
    assert DISCLAIMER in _normalised_help()


def test_vyasa_help_still_documents_what_the_flag_does() -> None:
    assert "Send the prompt to your Vyasa fleet" in _normalised_help()
    assert "list employees when no prompt is given" in _normalised_help()


def test_cli_help_claims_no_persona_count() -> None:
    """No `29 personas` / `29 specialists` style claim anywhere in the CLI help."""
    match = re.search(r"\b\d+\s+(?:\w+\s+)?(?:persona|specialist|employee)s?\b", _normalised_help())
    assert match is None, f"CLI help asserts an unverifiable roster size: {match.group(0)!r}"
