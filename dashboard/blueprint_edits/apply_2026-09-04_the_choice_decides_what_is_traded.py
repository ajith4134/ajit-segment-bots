#!/usr/bin/env python3
"""`instrument-choice` decides the traded contract and the side it is traded on.

docs/proposals/the-choice-decides-what-is-traded.md: position-sizer read the
choice only for its reference price and built every order from the intent's own
symbol and side, so the selector's decision never reached the venue -- and a
bearish intent on a call became a sell-to-open on that call, which is writing a
naked option in a segment whose settings say buy-only.

No part's consumes or produces changes. What changes is which part's decision
reaches the venue, and the two data types' descriptions are what record it.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-04"

d = json.loads(REG.read_text())

CHOICE = (
    "Which listed contract carries a trade intent, the side it must be traded on "
    "to open that view, what it costs, and what was rejected. Binding since "
    "2026-09-04: an open is built from this choice -- its contract and its "
    "order_side -- and an open with no actionable choice is refused rather than "
    "falling back to the symbol the intent happened to name. Before that the only "
    "field anything read was the reference price, so the selector chose and "
    "nothing listened. While the segment is buy-only an option's order_side is "
    "always buy: a bearish view is expressed by buying a put, never by selling a "
    "call. `reduce` and `close` do not come through here -- those act on the "
    "contract actually held."
)
INTENT = (
    "What the brain wants to be true of a position: an asset, a direction, a "
    "horizon and the conviction behind it. Deliberately not an order -- it names "
    "no contract, no price and no order side, because those belong to parts that "
    "know things the brain does not. Where its symbol names a specific contract "
    "rather than an asset, instrument-selector resolves it to that contract's "
    "underlying and re-expresses the direction (2026-09-04): with the segment "
    "buy-only, a short view on a call is a bearish view on the underlying and is "
    "carried by buying a put, so the belief is converted rather than discarded."
)

for type_id, description in (("instrument-choice", CHOICE), ("trade-intent", INTENT)):
    entry = next(t for t in d["data_types"] if t["id"] == type_id)
    entry["description"] = description

d["_proposal_2026-09-04_the_choice_decides_what_is_traded"] = {
    "what": (
        "InstrumentChoice gains order_side, and position-sizer builds an open "
        "from the choice's contract and that side instead of from the intent's "
        "symbol and side. An open with no actionable choice is refused by name. "
        "instrument-selector resolves a contract-named intent to its underlying "
        "and converts the view while the segment is buy-only -- short a call "
        "becomes buy a put, short a put becomes buy a call -- rather than "
        "refusing it. An underlying with no chain in this segment is refused by "
        "its own name. Neither part's consumes or produces changes."
    ),
    "origin": (
        "the conversion rule is the user's decision, 2026-09-04; the choice "
        "being binding follows from finding that nothing read it"
    ),
    "applied_by": (
        "dashboard/blueprint_edits/apply_2026-09-04_the_choice_decides_what_is_traded.py"
    ),
    "proposal": "docs/proposals/the-choice-decides-what-is-traded.md",
    "recorded_on": TODAY,
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"instrument-choice: {CHOICE[:70]}...")
print(f"trade-intent:      {INTENT[:70]}...")
print(f"total: {len(d['features'])} features, {len(d['data_types'])} data types")
