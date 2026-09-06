#!/usr/bin/env python3
"""Drop five declared inputs that no part ever bound a reader for.

docs/proposals/a-declared-input-must-actually-be-read.md: each of these types
appears exactly once in its part's source -- in the `PART_DECLARATION` consumes
tuple -- and nowhere else. R-01 computes every edge from consumes/produces, so a
declared-and-unread input is a wire on every diagram and a `NOT CARRYING` line
on the audit board, reported against a producer that is doing its job perfectly:
`broker-price-level-sampler` was publishing 5,868 price frames while
`expiry-day-zero-to-hero-detector` read as starved of them.

Resolved by asking what each part actually needs, never by deleting a line to
quiet a checker -- the reasoning per part is in the proposal.

`opinion-arbiter`'s `bot-maturity` is deliberately NOT touched here: it records a
real design intent (how proven a bot is, is what an arbiter should weigh), it is
dead at the source (`edge-graduation-gate` has published nothing), and four other
parts consume it. That one is the operator's call.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-06"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}

PROPOSAL = "docs/proposals/a-declared-input-must-actually-be-read.md"

# part id -> the consumes tuple it really binds, in the exact order its own
# PART_DECLARATION literal states. Set explicitly rather than by removing an
# element, because PartDeclaration equality compares the tuple in order and each
# part's blueprint test checks exactly that.
REAL_CONSUMES = {
    # Judges how far OTM a strike is from Upstox's own delta, never from spot.
    "expiry-day-zero-to-hero-detector": [
        "broker-subscribed-instrument-listing", "broker-market-data",
        "broker-option-greeks", "training-label",
    ],
    # One vector per candidate; the universe is what the scanner sweeps.
    "bull-feature-builder": [
        "bull-side-candidate", "broker-subscribed-instrument-listing",
        "broker-open-interest", "order-book-snapshot", "symbol-price-frame",
        "symbol-profile",
    ],
    "bear-feature-builder": [
        "bear-side-candidate", "broker-subscribed-instrument-listing",
        "broker-open-interest", "order-book-snapshot", "symbol-price-frame",
        "symbol-profile",
    ],
    # Crypto venue parts, off, and being retired by the Indian conversion.
    "venue-trade-stream-reader": ["stream-plan"],
    "venue-quote-stream-reader": ["stream-plan"],
}

touched_categories = set()
for part_id, consumes in REAL_CONSUMES.items():
    part = feats[part_id]
    part["consumes"] = list(consumes)
    rewiring = {"on": TODAY, "why": PROPOSAL}
    if rewiring not in part.setdefault("rewired", []):
        part["rewired"].append(rewiring)
    touched_categories.add(part["category"])

for cid in sorted(touched_categories):
    c = next(cat for cat in d["categories"] if cat["id"] == cid)
    parts = [feature for feature in d["features"] if feature["category"] == cid]
    c["consumes"] = sorted({x for feature in parts for x in feature["consumes"]})
    c["produces"] = sorted({x for feature in parts for x in feature["produces"]})
    c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d["_proposal_2026-09-06_declared_inputs_that_were_never_read"] = {
    "what": (
        "Five parts declared a `consumes` type and never bound a reader for it, so "
        "R-01 drew a wire on every diagram that no message could ever travel and the "
        "audit board reported it as NOT CARRYING against a healthy producer. Found by "
        "scanning every launchable part's PART_DECLARATION against every literal "
        "handed to context.bus.reader(). expiry-day-zero-to-hero-detector judged "
        "moneyness from Upstox's own delta and never needed broker-price-frame; the "
        "bull and bear feature builders build one vector per candidate and never "
        "needed symbol-universe; the two crypto venue stream readers are off and "
        "being retired, and their venue-standing producer (ban-signal-detector) is "
        "off too. opinion-arbiter's bot-maturity is deliberately left alone -- it is "
        "a real design intent, dead at the source, and changing it changes how trades "
        "are arbitrated. dashboard/check_declared_inputs.py stops the class "
        "recurring; it is not in the pre-commit hook until the arbiter is answered."
    ),
    "origin": (
        "found by Claude walking opportunity-scanner, feature 5 of the 29 under the "
        "audit temporary goal of 2026-09-05 (docs/feature-audit.md); item 1 of that "
        "goal is measured data flow rather than a declared contract, and RL-067 says "
        "a part's real consumes equal what the blueprint declares"
    ),
    "applied_by": (
        "dashboard/blueprint_edits/apply_2026-09-06_declared_inputs_that_were_never_read.py"
    ),
    "proposal": [PROPOSAL],
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
for part_id in REAL_CONSUMES:
    print(f"{part_id}: {feats[part_id]['consumes']}")
print(
    f"total: {len(d['features'])} features, {len(d['categories'])} categories, "
    f"{len(d['data_types'])} data types"
)
