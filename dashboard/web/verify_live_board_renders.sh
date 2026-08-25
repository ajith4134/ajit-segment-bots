#!/usr/bin/env bash
# Prove the live board draws and actually moves, rather than assuming it from a build.
#
#     dashboard/web/verify_live_board_renders.sh [url] [screenshot]
#
# Needs part_health_api.py running, because the thing being checked is the live
# column and there is nothing live about a file:// page. Waits for a second poll so
# a rate exists, opens a block, opens a part, and asserts its counters are on screen.
#
# Same library and font requirement as verify_board_renders.sh, and for the same
# reason: without the fonts Chromium paints every glyph invisible while every DOM
# assertion still passes, which is the failure a screenshot catches and code does not.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPS="$HOME/.local/pwdeps/root/usr/lib/x86_64-linux-gnu"
URL="${1:-http://127.0.0.1:8787/}"
SHOT="${2:-/tmp/live-board-render.png}"

if [ ! -d "$DEPS" ]; then
  echo "browser libraries missing: $DEPS" >&2
  exit 1
fi
if [ -z "$(find "$HOME/.local/share/fonts" -name '*.ttf' 2>/dev/null | head -1)" ]; then
  echo "no fonts installed - the page will render with invisible text" >&2
  exit 1
fi
if ! curl -fsS -o /dev/null "${URL%/}/api/activity"; then
  echo "no API at ${URL%/}/api/activity - start it with: python3 dashboard/part_health_api.py" >&2
  exit 1
fi

export LD_LIBRARY_PATH="$DEPS:$DEPS/nss${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
node "$HERE/live_render_check.mjs" "$URL" "$SHOT"
echo "screenshot: $SHOT"
