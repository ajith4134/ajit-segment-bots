#!/usr/bin/env bash
# Same harness as verify_board_renders.sh, for dashboard/wiring-explorer.html.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPS="$HOME/.local/pwdeps/root/usr/lib/x86_64-linux-gnu"
PAGE="${1:-file://$(cd "$HERE/.." && pwd)/wiring-explorer.html}"
SHOT="${2:-/tmp/wiring-render.png}"
[ -d "$DEPS" ] || { echo "browser libraries missing: $DEPS" >&2; exit 1; }
[ -n "$(find "$HOME/.local/share/fonts" -name '*.ttf' 2>/dev/null | head -1)" ] || { echo "no fonts installed" >&2; exit 1; }
export LD_LIBRARY_PATH="$DEPS:$DEPS/nss${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
node "$HERE/wiring_render_check.mjs" "$PAGE" "$SHOT"
echo "screenshot: $SHOT"
