#!/usr/bin/env bash
# Rebuild the board, prove it draws, and push it to its public URL.
#
#     dashboard/publish_board_to_pages.sh
#
# The board lives at https://ajith4134.github.io/segment-bots-board/ because only
# port 22 listens on this server -- a served board has no route to anyone. The page
# repo is public and holds nothing but the generated page; the source stays here,
# in the private repo.
#
# Nothing is pushed until the page has been mounted in a real browser and rendered
# without a console error. A build that succeeded and a page that draws are two
# different claims, and only the second one is worth publishing.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAGE_REPO="https://github.com/ajith4134/segment-bots-board.git"
CHECKOUT="${TMPDIR:-/tmp}/segment-bots-board-publish"
SHOT="${TMPDIR:-/tmp}/board-render.png"

echo "== rebuilding the snapshot from probes that run =="
python3 "$HERE/build_board_snapshot.py"

echo
echo "== proving it renders =="
"$HERE/web/verify_board_renders.sh" "file://$HERE/board.html" "$SHOT" >/dev/null
echo "renders clean, screenshot at $SHOT"

echo
echo "== publishing =="
rm -rf "$CHECKOUT"
git clone -q "$PAGE_REPO" "$CHECKOUT"
# This box sets no global git identity, and the page repo is a throwaway clone,
# so the author is named here rather than by changing anything global.
git -C "$CHECKOUT" config user.name "Ajith D"
git -C "$CHECKOUT" config user.email "ajithd747@gmail.com"
cp "$HERE/board.html" "$CHECKOUT/index.html"

cd "$CHECKOUT"
if git diff --quiet -- index.html; then
  echo "the page is byte-identical to what is already live - nothing to push"
  exit 0
fi

STAMP="$(python3 -c "import re,sys; print(re.search(r'\"generated_at\": ?\"([^\"]+)\"', open('index.html').read()).group(1))")"
git add index.html
git commit -q -m "Board as measured at ${STAMP}"
git push -q origin main

echo "pushed. live within a minute: https://ajith4134.github.io/segment-bots-board/"
echo "probes ran at: ${STAMP}"
