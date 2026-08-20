#!/usr/bin/env bash
# Prove the board actually draws, rather than assuming it does because it built.
#
#     dashboard/web/verify_board_renders.sh [page] [screenshot]
#
# Mounts the page in a real Chromium, fails on any console error or empty root,
# asserts the blocks/parts/cells/legend are on the page, clicks a cell to check the
# proof appears, and writes a screenshot to look at. A build that succeeds and a
# page that renders are different claims; this checks the second one.
#
# This server has no browser libraries and no fonts, so both were installed into
# ~/.local/pwdeps (user-space, no root). Without the library path Chromium exits
# 127 on libnspr4.so; without the fonts it paints every glyph invisible and the
# page looks empty while every DOM assertion still passes.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPS="$HOME/.local/pwdeps/root/usr/lib/x86_64-linux-gnu"
PAGE="${1:-file://$(cd "$HERE/.." && pwd)/board.html}"
SHOT="${2:-/tmp/board-render.png}"

if [ ! -d "$DEPS" ]; then
  echo "browser libraries missing: $DEPS" >&2
  echo "re-create with: apt-get download <libs> into ~/.local/pwdeps/debs, then dpkg-deb -x each into ~/.local/pwdeps/root" >&2
  exit 1
fi
if [ -z "$(find "$HOME/.local/share/fonts" -name '*.ttf' 2>/dev/null | head -1)" ]; then
  echo "no fonts installed - the page will render with invisible text" >&2
  exit 1
fi

export LD_LIBRARY_PATH="$DEPS:$DEPS/nss${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
node "$HERE/render_check.mjs" "$PAGE" "$SHOT"
echo "screenshot: $SHOT"
