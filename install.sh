#!/bin/bash
set -euo pipefail

# DJcode Installer — https://cli.darshj.ai
# Usage: curl -fsSL https://cli.darshj.ai/install.sh | bash
#
# Installs DJcode CLI via pip/uv into ~/.djcode/
# Zero sudo. Works on macOS and Linux.

VERSION="1.3.0"
REPO="https://github.com/darshjme/djcode.git"
DJCODE_DIR="$HOME/.djcode"

BOLD='\033[1m'
GOLD='\033[38;5;220m'
GREEN='\033[32m'
RED='\033[31m'
DIM='\033[2m'
RESET='\033[0m'

info()  { printf "${GOLD}${BOLD}djcode${RESET} ${DIM}→${RESET} %s\n" "$1"; }
ok()    { printf "${GREEN}✓${RESET} %s\n" "$1"; }
warn()  { printf "${GOLD}⚠${RESET} %s\n" "$1"; }
err()   { printf "${RED}✗ %s${RESET}\n" "$1" >&2; exit 1; }

# ── Uninstall ───────────────────────────────────────────────────────────────
if [ "${1:-}" = "--uninstall" ]; then
  info "Uninstalling DJcode..."
  pip uninstall djcode -y 2>/dev/null || true
  uv tool uninstall djcode 2>/dev/null || true
  rm -rf "$DJCODE_DIR/src"
  for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile" "$HOME/.bash_profile"; do
    if [ -f "$rc" ] && grep -q '\.djcode' "$rc" 2>/dev/null; then
      TMP=$(mktemp)
      grep -v '\.djcode' "$rc" > "$TMP" && mv "$TMP" "$rc"
    fi
  done
  printf "${GREEN}DJcode uninstalled.${RESET}\n"
  exit 0
fi

# ── Banner ──────────────────────────────────────────────────────────────────
printf "\n${GOLD}${BOLD}"
cat << 'ASCII'
  ██████╗      ██╗ ██████╗ ██████╗ ██████╗ ███████╗
  ██╔══██╗     ██║██╔════╝██╔═══██╗██╔══██╗██╔════╝
  ██║  ██║     ██║██║     ██║   ██║██║  ██║█████╗
  ██║  ██║██   ██║██║     ██║   ██║██║  ██║██╔══╝
  ██████╔╝╚█████╔╝╚██████╗╚██████╔╝██████╔╝███████╗
  ╚═════╝  ╚════╝  ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝
ASCII
printf "${RESET}\n"
info "Installer v${VERSION} — zero sudo, Python-native"
printf "\n"

# ── Preflight ───────────────────────────────────────────────────────────────

# Check Python 3.12+
if ! command -v python3 >/dev/null 2>&1; then
  err "Python 3.12+ required. Install from https://python.org"
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 12 ]); then
  err "Python 3.12+ required (found $PY_VERSION). Update: https://python.org"
fi
ok "Python $PY_VERSION"

# Check git
if ! command -v git >/dev/null 2>&1; then
  err "git is required. Install from https://git-scm.com"
fi
ok "git $(git --version | awk '{print $3}')"

# ── Detect best install method ──────────────────────────────────────────────

INSTALL_METHOD=""

if command -v uv >/dev/null 2>&1; then
  INSTALL_METHOD="uv"
  ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"
elif command -v pipx >/dev/null 2>&1; then
  INSTALL_METHOD="pipx"
  ok "pipx detected"
else
  INSTALL_METHOD="pip"
  ok "pip (fallback)"
fi

# ── Clone & Install ─────────────────────────────────────────────────────────

info "Downloading DJcode..."
rm -rf "$DJCODE_DIR/src"
mkdir -p "$DJCODE_DIR"

if ! git clone --depth 1 "$REPO" "$DJCODE_DIR/src" 2>&1 | tail -2; then
  err "Failed to clone. Check your internet connection."
fi
ok "Source downloaded"

info "Installing DJcode via ${INSTALL_METHOD}..."

cd "$DJCODE_DIR/src"

case "$INSTALL_METHOD" in
  uv)
    uv tool install --force . 2>&1 | tail -3
    ;;
  pipx)
    pipx install --force . 2>&1 | tail -3
    ;;
  pip)
    python3 -m pip install --user --break-system-packages . 2>&1 | tail -3 || \
    python3 -m pip install --user . 2>&1 | tail -3
    ;;
esac

ok "DJcode installed"

# ── Check Ollama ────────────────────────────────────────────────────────────
if command -v ollama >/dev/null 2>&1; then
  ok "Ollama detected"
  if ! ollama list 2>/dev/null | grep -q "gemma4"; then
    info "Pulling default model (gemma4)..."
    ollama pull gemma4 2>/dev/null && ok "gemma4 ready" || warn "Run 'ollama pull gemma4' later"
  else
    ok "gemma4 model ready"
  fi
else
  warn "Ollama not found — install from https://ollama.com for local inference"
fi

# ── Create default config ──────────────────────────────────────────────────
if [ ! -f "$DJCODE_DIR/config.json" ]; then
  cat > "$DJCODE_DIR/config.json" << 'CONF'
{"provider":"ollama","model":"gemma4","ollama_url":"http://localhost:11434","temperature":0.7,"max_tokens":8192,"telemetry":false}
CONF
fi

# ── Verify ──────────────────────────────────────────────────────────────────
printf "\n"

if command -v djcode >/dev/null 2>&1; then
  INSTALLED_V=$(djcode --version 2>&1 || echo "installed")
  printf "${GREEN}${BOLD}  DJcode installed successfully!${RESET}\n"
  printf "  ${DIM}%s${RESET}\n" "$INSTALLED_V"
  printf "\n  Run ${GOLD}${BOLD}djcode${RESET} to start coding.\n\n"
else
  printf "${GREEN}${BOLD}  DJcode installed!${RESET}\n"
  printf "\n  ${DIM}Open a new terminal, then run:${RESET}\n"
  printf "  ${GOLD}${BOLD}djcode${RESET}\n\n"
fi
