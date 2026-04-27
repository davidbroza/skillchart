#!/usr/bin/env bash
# skillchart installer — one-command install from GitHub.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/davidbroza/skillchart/main/install.sh | bash
#
# Env overrides:
#   SKILLCHART_DIR   install directory          (default: ~/.skillchart)
#   SKILLCHART_BIN   binary symlink target dir  (default: ~/.local/bin)
#   SKILLCHART_REF   git ref to install         (default: latest tag, else main)

set -euo pipefail

INSTALL_DIR="${SKILLCHART_DIR:-$HOME/.skillchart}"
BIN_DIR="${SKILLCHART_BIN:-$HOME/.local/bin}"
REPO_URL="https://github.com/davidbroza/skillchart"
REF="${SKILLCHART_REF:-}"

c_blue=$'\033[0;34m'
c_green=$'\033[0;32m'
c_yellow=$'\033[0;33m'
c_red=$'\033[0;31m'
c_reset=$'\033[0m'

say() { printf "%s\n" "$*"; }
ok() { printf "%s✓%s %s\n" "$c_green" "$c_reset" "$*"; }
note() { printf "%s→%s %s\n" "$c_blue" "$c_reset" "$*"; }
warn() { printf "%s!%s %s\n" "$c_yellow" "$c_reset" "$*"; }
die() { printf "%s✗%s %s\n" "$c_red" "$c_reset" "$*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "python3 is required (3.9+). Install it and re-run."

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(printf '%s' "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(printf '%s' "$PY_VERSION" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 9 ]; }; then
  die "python3 >= 3.9 required (you have $PY_VERSION)."
fi

mkdir -p "$INSTALL_DIR" "$BIN_DIR"

if command -v git >/dev/null 2>&1; then
  if [ -d "$INSTALL_DIR/.git" ]; then
    note "Updating existing checkout at $INSTALL_DIR"
    git -C "$INSTALL_DIR" fetch -q --tags origin
    if [ -z "$REF" ]; then
      REF=$(git -C "$INSTALL_DIR" tag --list 'v*' --sort=-v:refname | head -1)
      [ -z "$REF" ] && REF=main
    fi
    git -C "$INSTALL_DIR" checkout -q "$REF"
  else
    note "Cloning $REPO_URL into $INSTALL_DIR"
    git clone -q "$REPO_URL" "$INSTALL_DIR"
    if [ -z "$REF" ]; then
      REF=$(git -C "$INSTALL_DIR" tag --list 'v*' --sort=-v:refname | head -1)
      [ -z "$REF" ] && REF=main
    fi
    git -C "$INSTALL_DIR" checkout -q "$REF"
  fi
else
  note "git not found — falling back to tarball"
  TARBALL_REF="${REF:-main}"
  curl -fsSL "$REPO_URL/archive/refs/heads/$TARBALL_REF.tar.gz" \
    | tar xz -C "$INSTALL_DIR" --strip-components=1
fi

chmod +x "$INSTALL_DIR/bin/skillchart"
ln -sf "$INSTALL_DIR/bin/skillchart" "$BIN_DIR/skillchart"

ok "skillchart installed at $INSTALL_DIR ($REF)"
ok "Binary symlinked to $BIN_DIR/skillchart"

if ! printf '%s\n' "$PATH" | tr ':' '\n' | grep -qx "$BIN_DIR"; then
  echo
  warn "$BIN_DIR is not on your PATH."
  warn "Add this to your ~/.zshrc or ~/.bashrc:"
  echo
  echo "    export PATH=\"\$PATH:$BIN_DIR\""
fi

if ! python3 -c 'import tiktoken' 2>/dev/null; then
  echo
  note "Optional: install tiktoken for ~10x more accurate token counts"
  note "    pip install --user tiktoken"
fi

echo
ok "Run: skillchart"
