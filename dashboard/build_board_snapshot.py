#!/usr/bin/env python3
"""Freeze the board into one self-contained page.

    python3 dashboard/build_board_snapshot.py

The board is a live thing: served by `part_health_api.py`, it polls and moves. But
only port 22 is open on this server, so the live board cannot reach the user at all.
This build is how it does — the same React app, everything inlined, with one measured
payload frozen into it, published as a page.

The page is told it is frozen and says so: it renders `SNAPSHOT · frozen` beside the
timestamp of the probes rather than `LIVE · polling`, and it never tries to fetch.
A snapshot presented as live is a lie with a timestamp available, so the mode travels
inside the payload rather than being guessed at by the page.

Requires the frontend's dependencies:  cd dashboard/web && npm install
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from part_health_api import build_board_payload  # noqa: E402

WEB = HERE / "web"
SINGLE = WEB / "dist-single" / "index.html"
OUT = HERE / "board.html"

TITLE = "<title>Segment Bots — Part Board</title>"


def run_single_file_build() -> None:
    result = subprocess.run(
        ["npm", "run", "build:single"], cwd=WEB, capture_output=True, text=True, timeout=600
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout + result.stderr)
        raise SystemExit("the single-file build failed - is `npm install` done in dashboard/web?")


def write_board_snapshot() -> Path:
    if not (WEB / "node_modules").exists():
        raise SystemExit("dashboard/web/node_modules missing - run: cd dashboard/web && npm install")

    run_single_file_build()
    if not SINGLE.exists():
        raise SystemExit(f"expected {SINGLE} after the build, and it is not there")

    payload = build_board_payload("snapshot")
    # `</script>` inside JSON would close the tag early and break the page silently.
    frozen = json.dumps(payload).replace("</", "<\\/")
    injection = f'<script>window.__BOARD_SNAPSHOT__ = {frozen};</script>'

    html = SINGLE.read_text()
    if TITLE not in html:
        raise SystemExit("the built page lost its <title> - the injection anchor is gone")
    # Before the app's own script, so the snapshot exists by the time React mounts.
    html = html.replace(TITLE, TITLE + "\n" + injection, 1)

    OUT.write_text(html)
    return OUT


if __name__ == "__main__":
    path = write_board_snapshot()
    payload = build_board_payload("snapshot")
    size_kb = round(path.stat().st_size / 1024)
    print(f"{payload['totals']['blocks']} blocks, {payload['totals']['parts']} parts, "
          f"{payload['totals']['built']} built")
    print(f"counts: {({k: v for k, v in payload['counts'].items() if v})}")
    print(f"wrote {path} ({size_kb} KB, self-contained)")
