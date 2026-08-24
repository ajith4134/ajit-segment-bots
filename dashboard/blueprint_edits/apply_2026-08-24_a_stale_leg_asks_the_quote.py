#!/usr/bin/env python3
"""Let the parts that refuse a stale price ask the quote before refusing.

Proposed by Claude 2026-08-24 after the user asked how other projects solve the
"prices too old" problem that keeps recurring here.
Rationale: docs/proposals/an-all-market-quote-is-the-price-a-quiet-symbol-has.md.
Idempotent.

The answer from the corpus, verified against source rather than a summary:
nautilus_trader names the price source explicitly instead of assuming one --
`crates/model/src/enums.rs:PriceType` is `Bid, Ask, Mid, Last, Mark`, and `Mark`
is documented there as "a reference price reflecting an instrument's fair value,
often used for portfolio calculations and risk management". Every part here was
hardcoded to `Last`, which is the single source that goes stale precisely when
nobody is trading -- which is when an illiquid symbol needs pricing most.

Measured on the live run of 2026-08-24, that cost:

    spread-reversion-detector   stale_leg 1,438,376 of 4,199,062 tests -- 34%
    signal-outcome-labeller     refused_for_a_stale_price 44

instrument-selector already reads `symbol-quote-frame` and had by then priced 3
decisions from a quote that it would otherwise have refused. This edit gives the
other two the same fact to ask.

signal-outcome-labeller has the same defect and is deliberately NOT included. It
builds the training labels the model learns from, and a label anchored on a mid
while the detector that fired it was reading trades would teach the model from a
different price than the one it acted on. That is a risk to ground truth, and it
buys 44 refusals against the detector's 1,438,376. Worth doing one day, on
purpose, with the basis question answered first -- not as a side effect of this.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"
PROPOSAL = "docs/proposals/an-all-market-quote-is-the-price-a-quiet-symbol-has.md"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}

# Both keep symbol-price-frame. A traded price is the better fact when there is a
# fresh one -- it is what somebody actually paid, where a mid is what two people
# are asking for. The quote is the fallback, never the replacement.
GAINS_THE_QUOTE_FRAME = ["spread-reversion-detector"]

changed = []
for part_id in GAINS_THE_QUOTE_FRAME:
    feature = features.get(part_id)
    if feature is None:
        raise SystemExit(f"{part_id} is not in the registry; this edit is out of date")
    if "symbol-price-frame" not in feature["consumes"]:
        raise SystemExit(
            f"{part_id} no longer consumes symbol-price-frame, so the quote would be its "
            f"only price rather than its fallback. That is a different design; amend "
            f"{PROPOSAL} rather than letting this edit make it silently."
        )
    if "symbol-quote-frame" in feature["consumes"]:
        continue
    # Appended rather than sorted: a part's declaration in code carries the order
    # its author wrote, and the two are compared for equality.
    feature["consumes"] = list(feature["consumes"]) + ["symbol-quote-frame"]
    changed.append(f"{part_id}: consumes symbol-quote-frame")

if changed:
    REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(changed)} change(s) written to {REGISTRY}:")
    for line in changed:
        print(f"  {line}")
else:
    print("nothing to do; the registry already carries this edit")
