# ruff: noqa: E501
"""Keep the standalone curl-to-bash installer aligned with its tested Python helper."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEADER = '''#!/usr/bin/env bash
# DJcode installer: verified canonical CI wheel by default. No model/Python downloads.
set -euo pipefail
if [[ "${1:-}" == "--help" ]]; then
  printf '%s\\n' \\
    'Install: bash install.sh' 'Requires existing Python 3.12+ or an existing uv-managed Python.' 'Default: verified canonical main CI release; automatic updates use managed release pointers.' 'Overrides: DJCODE_INSTALL_DIR, DJCODE_BIN_DIR, DJCODE_PYTHON.' 'Explicit DJCODE_REPO_URL or DJCODE_REF selects manual/unmanaged source installation.' 'No models are downloaded; existing config and earlier releases are preserved.'
  exit 0
fi
if [[ -n "${1:-}" ]]; then printf 'Unknown option: %s\\n' "$1" >&2; exit 2; fi
PYTHON="${DJCODE_PYTHON:-python3}"
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)' 2>/dev/null; then
  if command -v uv >/dev/null 2>&1; then
    PYTHON=$(uv python find --no-python-downloads 3.12) || { printf 'Python 3.12+ is required.\\n' >&2; exit 1; }
  else
    printf 'Python 3.12+ is required. No Python was downloaded.\\n' >&2; exit 1
  fi
fi
"$PYTHON" - <<'DJCODE_BOOTSTRAP_PY'
'''

if __name__ == "__main__":
    (ROOT / "install.sh").write_text(
        HEADER + (ROOT / "scripts/managed_install.py").read_text() + "DJCODE_BOOTSTRAP_PY\n")
