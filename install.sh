#!/usr/bin/env bash
# One-click install for codex-trace-saver.
#
# Local (after `git clone`):
#   bash install.sh
#
# Remote one-liner (no clone needed):
#   curl -fsSL https://raw.githubusercontent.com/MichaelYang-lyx/codex-trace-saver/main/install.sh | bash
#
# What it does (no root required):
#   * Copies the three source files into $CODEX_TRACE_SAVER_DIR (default
#     ~/.local/share/codex-trace-saver).
#   * Places an executable `codex-save-trace` on your PATH via
#     ~/.local/bin/codex-save-trace (a small wrapper that runs the
#     bundled script with your current Python).
#   * Warns if ~/.local/bin isn't on your PATH.
set -euo pipefail

REPO_URL="${CODEX_TRACE_SAVER_REPO:-https://github.com/MichaelYang-lyx/codex-trace-saver.git}"
INSTALL_DIR="${CODEX_TRACE_SAVER_DIR:-$HOME/.local/share/codex-trace-saver}"
BIN_DIR="${CODEX_TRACE_SAVER_BIN:-$HOME/.local/bin}"

SRC_DIR=""
_self="${BASH_SOURCE[0]:-}"
if [ -n "$_self" ] && [ -f "$(dirname "$_self")/uploader.py" ] 2>/dev/null; then
  SRC_DIR="$(cd "$(dirname "$_self")" && pwd)"
fi

_TMP_CLONE=""
if [ -z "$SRC_DIR" ]; then
  echo "==> Fetching from $REPO_URL"
  command -v git >/dev/null 2>&1 || { echo "!! git is required for remote install"; exit 1; }
  _TMP_CLONE="$(mktemp -d)"
  git clone --depth 1 "$REPO_URL" "$_TMP_CLONE" >/dev/null 2>&1 \
    || { echo "!! git clone failed: $REPO_URL"; exit 1; }
  SRC_DIR="$_TMP_CLONE"
  trap 'rm -rf "$_TMP_CLONE"' EXIT
fi

echo "==> Installing codex-trace-saver to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR" "$BIN_DIR"
for f in uploader.py filepicker.py codex-save-trace README.md QUICKSTART.md config.example.env uninstall.sh; do
  [ -f "$SRC_DIR/$f" ] && cp -f "$SRC_DIR/$f" "$INSTALL_DIR/$f"
done
chmod +x "$INSTALL_DIR/codex-save-trace"

# Install a tiny wrapper on PATH that runs the bundled script.
cat > "$BIN_DIR/codex-save-trace" <<EOF
#!/usr/bin/env bash
exec python3 "$INSTALL_DIR/codex-save-trace" "\$@"
EOF
chmod +x "$BIN_DIR/codex-save-trace"

echo "    files:  $(ls "$INSTALL_DIR" | tr '\n' ' ')"
echo "    binary: $BIN_DIR/codex-save-trace"

# PATH check
case ":$PATH:" in
  *":$BIN_DIR:"*) : ;;
  *)
    echo
    echo "!! Warning: $BIN_DIR is not in your PATH."
    echo "   Add this to your shell rc (~/.bashrc, ~/.zshrc):"
    echo "     export PATH=\"\$HOME/.local/bin:\$PATH\""
    ;;
esac

cat <<EOF

==> Done. Try:
  codex-save-trace                    # scan + preview
  codex-save-trace --yes              # scan + upload (+1)
  codex-save-trace --yes --local      # save the zip locally
  codex-save-trace list               # show recent rollouts
  codex-save-trace --help             # full help

Configure (optional):
  export TRACE_LEADERBOARD_NAME="your-name"
  export TRACE_LEADERBOARD_URL="http://10.9.66.12:8848"

Leaderboard: ${TRACE_LEADERBOARD_URL:-http://10.9.66.12:8848}
EOF
