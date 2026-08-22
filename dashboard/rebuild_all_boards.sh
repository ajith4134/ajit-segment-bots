#!/usr/bin/env bash
# Rebuild every board from probes that run, in the order they depend on each other.
#
#   dashboard/rebuild_all_boards.sh
#
# This exists because the boards went stale and nobody noticed. Four generators,
# each run by hand, each easy to forget after the one part you happened to be
# working on -- so the part monitor was current and the other three were two days
# old, all of them showing a project with nothing built while five parts were
# built and a million records were on the tape.
#
# A stale board is worse than no board. It is convincing.
#
# **Publishing is deliberately not in here.** The boards are re-published to their
# Artifact URLs by Claude, and those URLs live outside this repository; a script
# that pretended to publish would be the same failure one layer along. What this
# guarantees is that every file on disk was made from a probe that ran just now.

set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PROJECT}/.venv/bin/python"

cd "${PROJECT}"

echo "== contracts =="
"${PYTHON}" dashboard/check_contracts.py

echo
echo "== status board =="
"${PYTHON}" dashboard/build_status_board.py | tail -5

echo
echo "== part monitor =="
"${PYTHON}" dashboard/build_part_monitor.py | tail -3

echo
echo "== wiring explorer =="
"${PYTHON}" dashboard/build_wiring_explorer.py | tail -2

echo
echo "== trade board =="
"${PYTHON}" dashboard/build_trade_board.py

echo
echo "== part board (React: build, then freeze one measured payload into it) =="
# The frontend is rebuilt first because board.html inlines the built bundle. A
# snapshot taken over a stale bundle would be a current payload rendered by old
# code, which is the hardest kind of wrong board to spot.
(cd dashboard/web && npm run build >/dev/null)
"${PYTHON}" dashboard/build_board_snapshot.py | tail -3

echo
echo "== what was written =="
ls -la dashboard/*.html

cat <<'NEXT'

Now re-publish these to their existing Artifact URLs, or the shared links still
show the versions above's predecessors:

  part-monitor.html    https://claude.ai/code/artifact/9c2b45b7-6978-4626-b17b-e11c3872246e
  status-board.html    https://claude.ai/code/artifact/7aace43b-f4d8-4ff1-a4f3-cc448ba850fb
  board.html           https://claude.ai/code/artifact/8a2fa262-34eb-498a-a8f7-246b3dc927cf
  wiring-explorer.html https://claude.ai/code/artifact/190da915-dc97-4c42-a951-907bd65c3644
  trade-board.html     https://claude.ai/code/artifact/f6b77448-f535-4629-9647-383035214c0c
NEXT
