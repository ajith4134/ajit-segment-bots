#!/usr/bin/env python3
"""Correct three parts the shape-based classifier wrongly left throttleable.

apply_2026-08-20_part_declarations.py classified 321 parts from their id tokens
alone. Three of the 58 it put in "model, scorer, search, learning"
(compute-bound / latency-only / delays, throttleable) make a live decision in
the trade path rather than recording or judging one after the fact, and an
independent full audit of all 101 throttleable parts (not a keyword grep) found
them:

  - leverage-selector: chooses leverage per trade from volatility plus funding.
    Stale volatility sizes leverage against out-of-date risk -- that changes the
    answer, not merely when it arrives. It is the recursive-indicator failure
    docs/proposals/part-declarations.md already uses as its worked example,
    applied to this part's own inputs rather than to an indicator.
  - tail-trailing-exit-planner: trails a stop below the last higher low. A stop
    left un-tightened through a fast move is a corrupted stop, not a late one.
  - forecast-distribution-gate: flags a forecast whose features sit far from
    anything the model trained on. Throttling a safety gate widens the window an
    out-of-distribution forecast passes through unchecked -- the gate's whole
    job is to not let that through, on time.

The rule this makes explicit, and that docs/proposals/part-declarations.md now
states: a part making a live decision in the trade path is not throttleable; a
part recording or auditing after the fact is. Four parts that matched the same
shape of the classifier and were checked against this rule stay throttleable on
purpose -- control-recorder, model-registry, abstention-coverage-auditor,
stop-placement-auditor -- because each one records or judges after the fact,
where a lower rate costs latency and nothing else.

Unlike its sibling, which sets a field only if absent, this script OVERWRITES
rate_risk and skipped_tick_effect on exactly these three ids, unconditionally to
the target value -- resource_class is untouched, since the correction is about
throttle safety, not allocation. This is deliberate and it is the process the
proposal already describes as the intended one: a default is corrected part by
part as it is actually reviewed, by a one-line edit to that part's fields, never
by re-running the classifier. Overwriting here is that one-line edit, done as a
script rather than by hand so the change is idempotent, printed, and reviewable
-- the same discipline as every other blueprint edit in this directory.

Idempotent: once these three ids hold the target values, re-running changes
nothing.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"

# part id -> (rate_risk, skipped_tick_effect). resource_class is deliberately
# absent from this table: the correction is about throttle safety, not about
# what kind of work the governor thinks it is allocating.
CORRECTIONS = {
    "leverage-selector": ("changes-the-answer", "corrupts"),
    "tail-trailing-exit-planner": ("changes-the-answer", "corrupts"),
    "forecast-distribution-gate": ("changes-the-answer", "corrupts"),
}

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}

missing = sorted(set(CORRECTIONS) - set(feats))
if missing:
    raise SystemExit(f"not in the blueprint, refusing to guess: {missing}")

changed = []
left_alone = []
for part_id, (rate_risk, skipped_tick_effect) in CORRECTIONS.items():
    feature = feats[part_id]
    before = (feature.get("rate_risk"), feature.get("skipped_tick_effect"))
    after = (rate_risk, skipped_tick_effect)
    if before == after:
        left_alone.append(part_id)
        continue
    feature["rate_risk"] = rate_risk
    feature["skipped_tick_effect"] = skipped_tick_effect
    changed.append((part_id, before, after))

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")

print(f"{len(changed)} parts corrected, {len(left_alone)} already at the target value")
for part_id, before, after in changed:
    print(f"  {part_id}: {before} -> {after}")
for part_id in left_alone:
    print(f"  {part_id}: already {CORRECTIONS[part_id]}")
