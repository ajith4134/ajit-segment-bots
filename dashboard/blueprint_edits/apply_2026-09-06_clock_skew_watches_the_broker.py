#!/usr/bin/env python3
"""Let clock-skew-monitor read the broker's own timestamps.

docs/proposals/clock-skew-monitor-watches-the-broker-clock.md: the part consumed
only `raw-venue-order-status`, whose producers are both crypto and both off, so
the one thing that would notice this machine's clock drifting has never run.
`observe_venue_time` is already venue-agnostic and `broker-market-data` carries
Upstox's own `broker_time_ns` at thousands a second.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-06"
PROPOSAL = "docs/proposals/clock-skew-monitor-watches-the-broker-clock.md"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}
part = feats["clock-skew-monitor"]
part["consumes"] = ["broker-market-data", "raw-venue-order-status"]
rewiring = {"on": TODAY, "why": PROPOSAL}
if rewiring not in part.setdefault("rewired", []):
    part["rewired"].append(rewiring)

cid = part["category"]
c = next(cat for cat in d["categories"] if cat["id"] == cid)
parts = [f for f in d["features"] if f["category"] == cid]
c["consumes"] = sorted({x for f in parts for x in f["consumes"]})
c["produces"] = sorted({x for f in parts for x in f["produces"]})
c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d["_proposal_2026-09-06_clock_skew_watches_the_broker"] = {
    "what": (
        "clock-skew-monitor reads broker-market-data's own broker_time_ns beside the "
        "crypto order-status stream it had, so the offset between this machine and the "
        "broker is measured continuously instead of never. Two timestamp traps were "
        "found by hand on this one day -- Upstox's +05:30 historical rows and NSE's "
        "IST-as-epoch intraday chart -- and nothing running would have caught either. "
        "The rejection half is kept rather than deleted: a venue saying 'your timestamp "
        "is wrong' is the sharpest signal there is, the day a real order path exists."
    ),
    "origin": (
        "found by Claude walking observability, feature 15 of the 29 under the audit "
        "temporary goal of 2026-09-05 (docs/feature-audit.md); item 3 is convert or "
        "replace, never leave in place"
    ),
    "applied_by": "dashboard/blueprint_edits/apply_2026-09-06_clock_skew_watches_the_broker.py",
    "proposal": [PROPOSAL],
}
REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print("clock-skew-monitor consumes:", part["consumes"])
