#!/usr/bin/env python3
"""exposure-limiter reads the stop, so it can measure risk rather than notional.

Proposed by Claude 2026-08-26, decided by the operator the same day.
Rationale: docs/proposals/the-exposure-cap-measured-notional-and-judged-it-as-risk.md
Idempotent.

Measured 2026-08-26 at 15:16: 12 open positions read as 199% of the allotment
against a 5% cap, and position-sizer refused 62,935 of 71,233 actionable intents
with `refused_no_risk_allowed`. The caps are the operator's risk numbers -- "the
most one position may risk" -- and the limiter was summing notional. A position
sized to risk 1% of the allotment at a 0.5% stop is about 200% of it in notional,
so the two numbers are not close enough to be a tuning question.

Risk needs the stop, and `Position` does not carry one. `stop-adjustment` does:
venue, symbol, entry, previous stop and new stop for an open position, produced by
profit-lock and exit-order-chainer in this same block.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"

LIMITER = "exposure-limiter"
STOP_ADJUSTMENT = "stop-adjustment"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}

if LIMITER not in features:
    raise SystemExit(f"{LIMITER} is not in the registry; this edit is out of date")

producers = [
    feature["id"]
    for feature in registry["features"]
    if STOP_ADJUSTMENT in feature.get("produces", ())
]
if not producers:
    raise SystemExit(
        f"nothing produces {STOP_ADJUSTMENT!r}; this edit would create a dangling edge (R-01)"
    )

consumes = features[LIMITER].setdefault("consumes", [])
if STOP_ADJUSTMENT in consumes:
    print(f"{LIMITER} already consumes {STOP_ADJUSTMENT!r}; nothing to do")
else:
    consumes.append(STOP_ADJUSTMENT)
    consumes.sort()
    REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
    print(f"{LIMITER} now consumes {STOP_ADJUSTMENT!r}, produced by {', '.join(producers)}")

print(
    "A position whose stop nobody can name is counted at its full notional: a "
    "position with no stop resting can lose all of it, so its notional is its risk."
)
