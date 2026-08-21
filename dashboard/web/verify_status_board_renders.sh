#!/usr/bin/env bash
# Same harness as verify_board_renders.sh, for dashboard/status-board.html.
# Asserts the substrate tile group (Task 14, RL-069) is present with every
# chip in it carrying a state from the board's own vocabulary -- not just
# that some page loaded.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPS="$HOME/.local/pwdeps/root/usr/lib/x86_64-linux-gnu"
PAGE="${1:-file://$(cd "$HERE/.." && pwd)/status-board.html}"
SHOT="${2:-/tmp/status-board-render.png}"
[ -d "$DEPS" ] || { echo "browser libraries missing: $DEPS" >&2; exit 1; }
[ -n "$(find "$HOME/.local/share/fonts" -name '*.ttf' 2>/dev/null | head -1)" ] || { echo "no fonts installed" >&2; exit 1; }
export LD_LIBRARY_PATH="$DEPS:$DEPS/nss${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
node "$HERE/status_board_render_check.mjs" "$PAGE" "$SHOT"
echo "screenshot: $SHOT"
