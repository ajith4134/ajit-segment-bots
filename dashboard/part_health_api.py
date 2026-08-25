#!/usr/bin/env python3
"""The board's API. One payload describing every block and every part, measured.

    python3 dashboard/part_health_api.py [--port 8787]

This is the spine the dashboard renders. It exists so that a part appears on the
board the moment it is written, with no per-part dashboard work: every part already
declares `produces: part-health` (T-1), so the board reads a contract the blueprint
already guarantees rather than one wired panel at a time.

Honesty is per part, never global -- the lesson from the user's own board, where a
`demo` flag rides on every endpoint's payload and a value is never shown as live
unless it is. Here each part carries its own `rung` and the `proof` that produced it,
and `mode` says whether the whole payload came from a live probe or a frozen snapshot.

    GET /api/board     the whole measured state: every part's rung and its proof
    GET /api/activity  what every reporting part is doing now, and how fast
    GET /api/machine   what this server is spending: cpu, memory, disk, per part
    GET /api/trades    what the bot holds and what it has closed
    GET /api/settings  the capital settings, and when each last changed (RL-055)
    GET /api/blueprint the design only, no measurement
    GET /            the built frontend from web/dist, when it exists
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from build_part_monitor import measure_parts  # noqa: E402
from completion import (  # noqa: E402
    DECLARED,
    FAILING,
    IMPLEMENTED,
    RUNNING,
    TESTED,
    UNMEASURED,
    block_completion,
    part_is_measured_complete,
)
from capital_settings_view import build_capital_settings_view  # noqa: E402
from machine_load import MachineLoadReader  # noqa: E402
from measured_cache import MeasuredCache  # noqa: E402
from part_activity import ActivityReader, summarise_block_activity  # noqa: E402
from trade_activity import build_trade_activity  # noqa: E402
from render_blueprint import find_contract_violations, load_feature_registry  # noqa: E402

DIST = HERE / "web" / "dist"
BUILT_RUNGS = (IMPLEMENTED, TESTED, RUNNING)

# One reader for the process, because a rate needs two observations and the second
# has to be compared against something this process actually saw. Per-request
# readers would each hold one sample and no rate would ever exist.
ACTIVITY_READER = ActivityReader()

# How long a measurement of the *code* may be reused. The board payload scans the
# filesystem for all 327 parts and takes about eleven seconds; the shape it
# describes changes on deploy, not between two polls a second apart. Without this
# the frontend's five-second poll started a new eleven-second scan before the last
# had finished, the server saturated, and Cloudflare answered the operator HTTP 524
# while every part underneath was healthy.
BOARD_FRESH_FOR_SECONDS = 30.0
# The trades payload marks every held symbol against the tape and reads the
# attribution journal, so its cost grows with open positions rather than with the
# universe -- measured at about four seconds with fifteen positions held.
#
# The window has to sit above that cost. At five seconds it was refreshing almost
# continuously: each refresh took four, so a fresh answer was stale a second after
# it arrived and the next caller paid the full four again. An interval below a
# measurement's own cost is not a fast board, it is a board permanently mid-scan.
TRADES_FRESH_FOR_SECONDS = 20.0
# Same reason: a CPU percentage is a difference between two /proc/stat readings,
# and the second one needs a first one this process actually took.
MACHINE_READER = MachineLoadReader()


def summarise_block_state(rungs: list[str]) -> str:
    """One badge for a block, derived from its parts. Never flatters upward.

    A block is only LIVE when every part is running, and any failing part makes the
    whole block read FAILING -- a block with one broken part is not a working block.
    """
    if not rungs:
        return UNMEASURED
    if FAILING in rungs:
        return FAILING
    if all(r == RUNNING for r in rungs):
        return RUNNING
    if all(r == DECLARED for r in rungs):
        return DECLARED
    return "PARTIAL"


def build_board_payload(mode: str = "live") -> dict:
    registry = load_feature_registry()
    states = measure_parts()
    by_id = {s.part_id: s for s in states}
    violations = find_contract_violations(registry)

    parts = []
    for feature in registry.features:
        state = by_id[feature["id"]]
        parts.append(
            {
                "id": feature["id"],
                "name": feature.get("name", feature["id"]),
                "role": feature.get("role", ""),
                "block": feature.get("category", ""),
                "rung": state.rung,
                "proof": state.proof,
                # RL-070's dot: green only when part_is_measured_complete() says so
                # (dashboard/completion.py, the one place that predicate is decided).
                # dot_proof mirrors state.proof here -- for a part the rung IS the
                # completeness evidence -- kept as its own field for symmetry with
                # the block-level rollup below, whose dot_proof is a different fact.
                "is_complete": part_is_measured_complete(state),
                "dot_proof": state.proof,
                "consumes": feature.get("consumes", []),
                "produces": feature.get("produces", []),
                "states": feature.get("states", []),
                "origin": feature.get("origin", ""),
                "note": feature.get("note", ""),
            }
        )

    parts_by_block: dict[str, list[dict]] = {}
    for part in parts:
        parts_by_block.setdefault(part["block"], []).append(part)

    blocks = []
    for category in registry.categories:
        owned = parts_by_block.get(category["id"], [])
        owned_states = [by_id[p["id"]] for p in owned]
        # RL-070: the block's dot is green only when every part in it is green
        # -- block_completion() is the same rollup build_part_monitor.py's block
        # header dots and build_status_board.py's block tiles use, so this board
        # can never silently disagree with those about what "complete" means.
        block_is_complete, block_dot_proof = block_completion(owned_states)
        blocks.append(
            {
                "id": category["id"],
                "name": category.get("name", category["id"]),
                "summary": category.get("summary", ""),
                "scope": category.get("scope", ""),
                "origin": category.get("origin", ""),
                "consumes": category.get("consumes", []),
                "produces": category.get("produces", []),
                "part_ids": [p["id"] for p in owned],
                "state": summarise_block_state([p["rung"] for p in owned]),
                "n_parts": len(owned),
                "n_built": len([p for p in owned if p["rung"] in BUILT_RUNGS]),
                "is_complete": block_is_complete,
                "dot_proof": block_dot_proof,
            }
        )

    rungs = [DECLARED, IMPLEMENTED, TESTED, RUNNING, FAILING, UNMEASURED]
    return {
        "mode": mode,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "contract": {"ok": not violations, "violations": violations},
        "blocks": blocks,
        "parts": parts,
        "counts": {r: len([p for p in parts if p["rung"] == r]) for r in rungs},
        "totals": {
            "blocks": len(blocks),
            "parts": len(parts),
            "built": len([p for p in parts if p["rung"] in BUILT_RUNGS]),
            "data_types": len(registry.data_types),
        },
    }


def build_activity_payload() -> dict:
    """What every reporting part is doing, and how fast, with nothing else in it.

    Split from the board payload deliberately. The board measures the filesystem
    for every part's rung, which is far too expensive to poll at the rate live
    behaviour changes; this reads one small file. So the shape is polled slowly
    and the behaviour is polled quickly, and neither has to pay for the other.
    """
    activities, provenance = ACTIVITY_READER.read_activity()
    registry = load_feature_registry()
    block_of = {f["id"]: f.get("category", "") for f in registry.features}

    by_block: dict[str, list] = {}
    for activity in activities:
        by_block.setdefault(block_of.get(activity.part_id, ""), []).append(activity)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provenance": provenance,
        "parts": {a.part_id: a.as_dict() for a in activities},
        "blocks": {
            category["id"]: summarise_block_activity(by_block.get(category["id"], []))
            for category in registry.categories
        },
    }


def build_machine_payload() -> dict:
    """What this server is spending right now, measured on every call."""
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **MACHINE_READER.read_machine_load(),
    }


def build_trade_payload() -> dict:
    """What the bot holds and what it has closed.

    Heavier than the other routes -- it marks each held symbol against the tape --
    so it is meant to be polled slowly. The cost is the number of open positions,
    not the size of the universe.
    """
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **build_trade_activity(),
    }


def build_blueprint_payload() -> dict:
    """The design with no measurement in it, for anything that only needs the shape."""
    registry = load_feature_registry()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "categories": registry.categories,
        "features": registry.features,
        "data_types": registry.data_types,
        "state_vocabulary": registry.state_vocabulary,
    }


# Defined here, after the builders they wrap. Module level so the measurement is
# shared by every request this process serves rather than per connection.
BOARD_CACHE = MeasuredCache(
    refresh=lambda: build_board_payload("live"), fresh_for_seconds=BOARD_FRESH_FOR_SECONDS
)
TRADES_CACHE = MeasuredCache(
    refresh=build_trade_payload, fresh_for_seconds=TRADES_FRESH_FOR_SECONDS
)
# The settings change when the operator changes them, which is rarely, and the
# journal behind the 'last changed' answer only grows. Short enough that an edit
# shows up while the operator is still looking at the page.
SETTINGS_CACHE = MeasuredCache(refresh=build_capital_settings_view, fresh_for_seconds=5.0)


class BoardHandler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict) -> None:
        self._send(200, json.dumps(payload).encode(), "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 - http.server's required name
        route = self.path.split("?", 1)[0]

        if route == "/api/board":
            self._send_json(BOARD_CACHE.read())
            return
        if route == "/api/activity":
            self._send_json(build_activity_payload())
            return
        if route == "/api/machine":
            self._send_json(build_machine_payload())
            return
        if route == "/api/trades":
            self._send_json(TRADES_CACHE.read())
            return
        if route == "/api/settings":
            self._send_json(SETTINGS_CACHE.read())
            return
        if route == "/api/blueprint":
            self._send_json(build_blueprint_payload())
            return

        # Static frontend. Missing dist is a real state and says so, rather than 404.
        relative = "index.html" if route in ("/", "") else route.lstrip("/")
        candidate = (DIST / relative).resolve()
        if DIST.exists() and str(candidate).startswith(str(DIST.resolve())) and candidate.is_file():
            suffix = candidate.suffix
            kinds = {
                ".html": "text/html; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".json": "application/json; charset=utf-8",
                ".svg": "image/svg+xml",
            }
            self._send(200, candidate.read_bytes(), kinds.get(suffix, "application/octet-stream"))
            return

        if not DIST.exists():
            self._send(
                503,
                b"frontend not built - run: cd dashboard/web && npm install && npm run build",
                "text/plain; charset=utf-8",
            )
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def log_message(self, fmt: str, *args) -> None:
        """Quiet by default. The board is watched, not tailed."""
        return


def warm_caches() -> None:
    """Take the expensive measurements once before the first request arrives.

    Without this the first visitor pays the full scan -- about ten seconds of
    filesystem work while they look at a blank page and decide the board is
    broken. Warming is done in the foreground on purpose: a server that is
    listening before it can answer is a server that returns 524 to whoever knocks
    first, which is exactly the failure this whole path was fixed for.
    """
    for name, cache in (("board", BOARD_CACHE), ("trades", TRADES_CACHE)):
        started = time.monotonic()
        try:
            cache.read()
            print(f"warmed {name} in {time.monotonic() - started:.1f}s")
        except Exception as failure:
            # A cold cache is recoverable; refusing to start is not. The route will
            # measure on demand and the failure will be visible there.
            print(f"could not warm {name}: {failure}")


def serve_board(port: int) -> None:
    warm_caches()
    server = ThreadingHTTPServer(("127.0.0.1", port), BoardHandler)
    print(f"board API on http://127.0.0.1:{port}/api/board")
    print(f"frontend {'from ' + str(DIST) if DIST.exists() else 'NOT BUILT - see README'}")
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--print", action="store_true", help="print one payload and exit")
    args = parser.parse_args()
    if args.print:
        print(json.dumps(build_board_payload("live"), indent=2)[:2000])
    else:
        serve_board(args.port)
