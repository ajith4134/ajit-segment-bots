#!/usr/bin/env python3
"""Make bull, bear and tailgater wiring private to each bot (RL-048).

The three-bots edit stamped bull and bear from one template with identical data
type ids (side-candidate, feature-vector, raw-conviction, ...). Edges are derived
from types, so the derivation wired bull's setup filter into bear's feature
builder, the tailgater's conviction into bull's entry timer, and so on: 46 edges
between bots that RL-048 says are separate at runtime.

Fix: every type that flows *inside* one bot becomes that bot's own type
(bull-feature-vector, bear-feature-vector, tail-calibrated-conviction, ...).
Types that cross the bot boundary on purpose are untouched: directional-opinion
(the merge point every bot writes and the arbiter reads) and
forecast-out-of-distribution-flag from the prediction block (a genuine input).
Parts outside the bots that read an internal type now read all three variants.

Also stamps the three bot blocks with peer_group "segment-bots" so R-03 can
refuse any future wire between them.

Idempotent: re-running changes nothing once applied.
"""
import json, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
d = json.loads(REG.read_text())
TODAY = "2026-08-20"
feats = {f["id"]: f for f in d["features"]}
types = {t["id"]: t for t in d["data_types"]}
cats = {c["id"]: c for c in d["categories"]}

BOTS = {"bull": "bull-bot", "bear": "bear-bot", "tail": "profit-tailgating-bot"}
LABEL = {"bull": "Bull bot", "bear": "Bear bot", "tail": "Tailgater"}
# internal types, and which bots use them
INTERNAL = {
    "side-candidate": ("bull", "bear"),
    "feature-vector": ("bull", "bear"),
    "raw-conviction": ("bull", "bear"),
    "calibrated-conviction": ("bull", "bear", "tail"),
    "entry-timing": ("bull", "bear"),
    "exit-plan": ("bull", "bear", "tail"),
    "setup-weight": ("bull", "bear", "tail"),
}
# the outlier rejector's own flag, distinct from prediction's forecast-level flag
OWN_FLAG = {"bull": "bull-feature-out-of-distribution-flag", "bear": "bear-feature-out-of-distribution-flag"}

def add_type(tid, name, desc):
    if tid not in types:
        t = {"id": tid, "name": name, "description": desc, "origin": "proposed", "added": TODAY}
        d["data_types"].append(t); types[tid] = t

def swap(lst, old, new):
    return [new if x == old else x for x in lst]

# 1. per-bot variants of every internal type
BASE_DESC = {  # copied from the generic types, which step 4 removes
    "side-candidate": ("side candidate", "An entry candidate one side's bot has kept, with the setup weight it carried."),
    "feature-vector": ("feature vector", "One bot's own features for one candidate: the numbers its model reads."),
    "raw-conviction": ("raw conviction", "A learned model's probability that the move goes the bot's way by more than cost within the horizon, before calibration."),
    "calibrated-conviction": ("calibrated conviction", "A conviction mapped onto the bot's own measured hit-rate, so 0.7 means 70% for this bot."),
    "entry-timing": ("entry timing", "Now, or wait for the pullback the playbook says comes, with a give-up time."),
    "exit-plan": ("exit plan", "A bot's proposed target, stop and time-stop for its own candidate. Proposed; the risk gate decides."),
    "setup-weight": ("setup weight", "How much one bot trusts each setup class on each time-frame, learned from its own scorecard, applied inside the bot. The scanner never sees it (RL-046)."),
}
for base, sides in INTERNAL.items():
    name, desc = BASE_DESC[base]
    for s in sides:
        add_type(f"{s}-{base}", f"{LABEL[s]} {name}", f"{LABEL[s]}'s own. {desc}")
for s, tid in OWN_FLAG.items():
    add_type(tid, f"{LABEL[s]} feature out-of-distribution flag",
             f"{LABEL[s]}'s outlier rejector found the feature vector far from anything its model has seen; its conviction model abstains. Distinct from the prediction block's forecast-level flag.")

# 2. bot parts read and write their own variants
for f in d["features"]:
    side = next((s for s, c in BOTS.items() if f["category"] == c), None)
    if not side: continue
    for base, sides in INTERNAL.items():
        if side in sides:
            f["consumes"] = swap(f["consumes"], base, f"{side}-{base}")
            f["produces"] = swap(f["produces"], base, f"{side}-{base}")
    if side in OWN_FLAG:
        if f["id"] == f"{side}-outlier-rejector":
            f["produces"] = swap(f["produces"], "forecast-out-of-distribution-flag", OWN_FLAG[side])
        if f["id"] == f"{side}-conviction-model" and OWN_FLAG[side] not in f["consumes"]:
            f["consumes"].append(OWN_FLAG[side])   # keeps prediction's flag too: a real input
    f.setdefault("wiring_privatised", {"on": TODAY, "why": "RL-048: bots never wire into each other"})

# 3. outside readers of an internal type read every bot's variant
bot_cats = set(BOTS.values())
for f in d["features"]:
    if f["category"] in bot_cats: continue
    for base, sides in INTERNAL.items():
        if base in f["consumes"]:
            f["consumes"] = [x for x in f["consumes"] if x != base] + [f"{s}-{base}" for s in sides if f"{s}-{base}" not in f["consumes"]]
        if base in f["produces"]:
            raise SystemExit(f"{f['id']} produces {base} from outside the bots — decide by hand")

# 4. the generic types are now unused: drop them
used = {x for f in d["features"] for x in f["consumes"] + f["produces"]}
d["data_types"] = [t for t in d["data_types"] if not (t["id"] in INTERNAL and t["id"] not in used)]

# 5. peer group, so R-03 refuses any future wire between the bots
for c in bot_cats:
    cats[c]["peer_group"] = "segment-bots"

# 6. recompute block contracts from parts
for c in d["categories"]:
    parts = [f for f in d["features"] if f["category"] == c["id"]]
    if not parts: continue
    c["consumes"] = sorted({x for f in parts for x in f["consumes"]})
    c["produces"] = sorted({x for f in parts for x in f["produces"]})
    c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print("applied: side-private types;", len(d["features"]), "features,", len(d["data_types"]), "types")
