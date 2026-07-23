#!/usr/bin/env bash
set -euo pipefail
INSTALL_DIR="${CODEX_TRACE_SAVER_DIR:-$HOME/.local/share/codex-trace-saver}"
BIN_DIR="${CODEX_TRACE_SAVER_BIN:-$HOME/.local/bin}"
rm -f "$BIN_DIR/codex-save-trace"
rm -rf "$INSTALL_DIR"
echo "==> codex-trace-saver removed."
