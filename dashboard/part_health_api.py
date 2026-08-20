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

    GET /api/board     the whole measured state
    GET /api/blueprint the design only, no measurement
    GET /            the built frontend from web/dist, when it exists
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from build_part_monitor import (  # noqa: E402
    DECLARED,
    FAILING,
    IMPLEMENTED,
    RUNNING,
    TESTED,
    UNMEASURED,
    measure_parts,
)
from render_blueprint import find_contract_violations, load_feature_registry  # noqa: E402

DIST = HERE / "web" / "dist"
BUILT_RUNGS = (IMPLEMENTED, TESTED, RUNNING)


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
            self._send_json(build_board_payload("live"))
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


def serve_board(port: int) -> None:
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
