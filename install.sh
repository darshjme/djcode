#!/usr/bin/env bash
# DJcode installer. Never downloads models or modifies shell startup files.
set -euo pipefail
REPO="${DJCODE_REPO_URL:-https://github.com/darshjme/djcode.git}"
REF="${DJCODE_REF:-main}"
PREFIX="${DJCODE_INSTALL_DIR:-$HOME/.local/share/djcode}"
BIN_DIR="${DJCODE_BIN_DIR:-$HOME/.local/bin}"
if [[ "${1:-}" == "--help" ]]; then
  printf '%s\n' 'Install: bash install.sh' 'Requires git and Python 3.12+ (or an existing uv-managed Python).' 'Overrides: DJCODE_INSTALL_DIR, DJCODE_BIN_DIR, DJCODE_REF, DJCODE_REPO_URL.' 'Models and API subscriptions are configured separately; no models are downloaded.'
  exit 0
fi
if [[ -n "${1:-}" ]]; then printf 'Unknown option: %s\n' "$1" >&2; exit 2; fi
command -v git >/dev/null || { printf 'git is required.\n' >&2; exit 1; }
mkdir -p "$PREFIX" "$BIN_DIR"
RELEASE=$(mktemp -d "$PREFIX/release.XXXXXXXX")
SUCCEEDED=0
cleanup() { if [[ "$SUCCEEDED" != 1 ]]; then rm -rf -- "$RELEASE"; fi; }
trap cleanup EXIT
printf 'Installing DJcode from %s (%s)\n' "$REPO" "$REF"
git clone --quiet --depth 1 --branch "$REF" "$REPO" "$RELEASE/source"
if command -v uv >/dev/null 2>&1; then
  uv venv --python 3.12 --no-python-downloads "$RELEASE/venv"
  uv pip install --python "$RELEASE/venv/bin/python" "$RELEASE/source"
else
  PYTHON="${DJCODE_PYTHON:-python3}"
  "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else "Python 3.12+ is required")'
  "$PYTHON" -m venv "$RELEASE/venv"
  "$RELEASE/venv/bin/python" -m pip install "$RELEASE/source"
fi
"$RELEASE/venv/bin/djcode" --version
# Keep earlier releases for rollback; refuse to replace an unrelated executable.
for ENTRY in djcode djcode-colibri; do
  if [[ -e "$RELEASE/venv/bin/$ENTRY" && -e "$BIN_DIR/$ENTRY" && ! -L "$BIN_DIR/$ENTRY" ]]; then
    printf 'Existing %s/%s is not a symlink; choose DJCODE_BIN_DIR or move it first.\n' "$BIN_DIR" "$ENTRY" >&2
    exit 1
  fi
done
for ENTRY in djcode djcode-colibri; do
  if [[ -e "$RELEASE/venv/bin/$ENTRY" ]]; then
    ln -sfn "$RELEASE/venv/bin/$ENTRY" "$BIN_DIR/$ENTRY"
  fi
done
SUCCEEDED=1
printf '\nInstalled: %s/djcode\n' "$BIN_DIR"
case ":$PATH:" in
  *":$BIN_DIR:"*) printf 'Run djcode --help to get started.\n' ;;
  *) printf 'Add this directory to PATH: %s\nOr launch using the full path above.\n' "$BIN_DIR" ;;
esac
printf 'Use an existing Ollama model or configure a hosted provider. No models were downloaded.\n'
