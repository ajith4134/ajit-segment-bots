"""Retire every learned checkpoint fitted to the crypto market.

The operator's standing instruction is that every crypto feature is converted to
its NSE equivalent. `bull-feature-builder` was converted and stopped *feeding*
`funding_rate` -- but a converted builder does not unlearn. The models kept
restoring their crypto state on every start, and nothing was counting it:
`dashboard/measure_objectives.py` measured source files and settings, and a
model's own saved state is neither.

What that state actually was, measured 2026-09-12:

    bull-conviction-model      funding_rate its 3rd-largest weight (+0.1795),
                               44,812 observations, against 65 for
                               open_interest_change and 80 for
                               order_flow_imbalance -- the two Indian features
                               meant to replace it. 98,862 labels, all crypto era
    bear-conviction-model      46,513 labels, same shape
    bull/bear-outlier-rejector the same feature names
    signal-excursion-profiler  1,492 of 2,098 symbol keys binance-usdm and
                               bybit-linear; 606 upstox

**Two different treatments, because the two kinds of state differ.**

A conviction model's weights were *co-fitted*: dropping `funding_rate` alone
leaves twelve weights that were learned alongside it, on crypto-scaled
normalisation moments. There is no honest surgical fix, so its checkpoint is
removed and the part cold-starts. `runtime/learned_estimator.py` already states
what that means -- "starts unfitted and says so" -- and an unfitted model
reporting `is_fitted` false is the correct rendering of a model that knows
nothing about this market yet (Rule 8).

An excursion profile is *keyed by symbol*, so the two markets never mixed inside
one number. Those keys are filtered: the 1,492 crypto ones go and the 606 upstox
ones stay, because they are real Indian observations and discarding them would
throw away learning this project actually wants.

Its `claims_recorded` / `claims_that_came_right` / `claims_that_came_wrong`
counters are aggregates with no per-venue split, so they mix two markets and
cannot be apportioned. They are reset rather than kept: a count whose
denominator spans a market this project no longer trades means nothing, and
carrying it forward would make a crypto hit rate look like an Indian one.

**Refuses to run while the spine is up.** Every one of these parts holds its
state in memory and checkpoints periodically, so a purge underneath a running
spine is overwritten within seconds -- and would look like it had worked.

Re-runnable. A second run finds nothing to do and says so.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import subprocess
import sys
import time

LEARNED_ROOT = pathlib.Path.home() / ".local/share/ajit-segment-bots/learned"

# The venue prefix every Indian key carries. Kept as an allowlist rather than a
# crypto blocklist: a blocklist admits whatever venue nobody thought to name,
# which is the failure mode this whole exercise is about.
THE_MARKET_THIS_PROJECT_TRADES = "upstox"

# Checkpoints whose learned state cannot be honestly repaired in place, because
# every weight in them was fitted alongside the crypto ones.
COLD_START_THESE = (
    "bull-conviction-model.conviction.json",
    "bear-conviction-model.conviction.json",
    "bull-outlier-rejector.normals.json",
    "bear-outlier-rejector.normals.json",
)

# Checkpoints keyed by symbol, where the two markets never mixed inside a single
# number and the Indian half is worth keeping.
FILTER_THESE_BY_VENUE = ("signal-excursion-profiler.excursions.json",)

# Counters in a filtered checkpoint that aggregate across venues and therefore
# cannot be apportioned. Reset rather than carried: see the module docstring.
AGGREGATE_COUNTERS_THAT_MIX_MARKETS = (
    "claims_recorded", "claims_that_came_right", "claims_that_came_wrong",
)

CRYPTO_FEATURE_NAMES = re.compile(r"funding_rate|funding_forecast", re.IGNORECASE)


def the_spine_is_running() -> bool:
    """Whether `ajit-spine` is up, asked of systemd rather than guessed."""
    try:
        done = subprocess.run(
            ["systemctl", "--user", "is-active", "ajit-spine"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.stdout.strip() == "active"


def back_up(root: pathlib.Path) -> pathlib.Path:
    """A dated copy beside the live directory, before anything is written."""
    destination = root.with_name(f"{root.name}-before-crypto-retirement-{time.strftime('%Y-%m-%dT%H%M%S')}")
    shutil.copytree(root, destination)
    return destination


def venue_of(key: str) -> str:
    return key.split("|", 1)[0]


def filter_state_by_venue(node, kept: list[int], dropped: list[int]):
    """Every symbol-keyed dictionary, keeping only this market's keys.

    Walks structurally rather than at a fixed path: each part checkpoints its
    own shape, and a filter that knew one layout would silently keep a crypto
    key living under any other.
    """
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if isinstance(key, str) and "|" in key:
                if venue_of(key) != THE_MARKET_THIS_PROJECT_TRADES:
                    dropped.append(1)
                    continue
                kept.append(1)
            out[key] = filter_state_by_venue(value, kept, dropped)
        return out
    if isinstance(node, list):
        return [filter_state_by_venue(item, kept, dropped) for item in node]
    return node


def retire(root: pathlib.Path, dry_run: bool) -> int:
    if not root.is_dir():
        print(f"no learned state at {root}; nothing to retire")
        return 0

    changed = 0
    for name in COLD_START_THESE:
        path = root / name
        if not path.exists():
            print(f"  already cold      {name}")
            continue
        state = json.loads(path.read_text(encoding="utf-8")).get("state", {})
        labels = state.get("labels_trained_on")
        detail = f"{labels:,} label(s)" if isinstance(labels, int) else "fitted state"
        print(f"  COLD-START        {name}  discarding {detail}")
        if not dry_run:
            path.unlink()
        changed += 1

    for name in FILTER_THESE_BY_VENUE:
        path = root / name
        if not path.exists():
            print(f"  not present       {name}")
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        kept: list[int] = []
        dropped: list[int] = []
        document["state"] = filter_state_by_venue(document.get("state", {}), kept, dropped)
        for counter in AGGREGATE_COUNTERS_THAT_MIX_MARKETS:
            if counter in document["state"]:
                document["state"][counter] = 0
        if not dropped:
            print(f"  already clean     {name}  {len(kept)} key(s), all {THE_MARKET_THIS_PROJECT_TRADES}")
            continue
        print(
            f"  FILTER            {name}  dropping {len(dropped)} crypto key(s), "
            f"keeping {len(kept)} {THE_MARKET_THIS_PROJECT_TRADES} key(s); "
            f"aggregate claim counters reset"
        )
        if not dry_run:
            path.write_text(json.dumps(document), encoding="utf-8")
        changed += 1

    # Anything left that still names crypto is a checkpoint neither list covers,
    # which is a finding rather than a pass: it means a new part has started
    # checkpointing crypto-shaped state and nobody has decided what to do.
    for path in sorted(root.glob("*.json")):
        if path.name in COLD_START_THESE or path.name in FILTER_THESE_BY_VENUE:
            continue
        text = path.read_text(encoding="utf-8")
        if CRYPTO_FEATURE_NAMES.search(text) or "binance" in text or "bybit" in text:
            print(
                f"  UNHANDLED         {path.name} still names crypto and is on neither "
                f"list; decide what it should be and add it here rather than leaving it"
            )
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="actually write. Without it this reports what it would do and changes nothing.",
    )
    arguments = parser.parse_args()

    if arguments.apply and the_spine_is_running():
        print(
            "REFUSED: ajit-spine is running. Every one of these parts holds its state in\n"
            "memory and checkpoints periodically, so a purge underneath it is overwritten\n"
            "within seconds -- and would look like it had worked.\n\n"
            "    systemctl --user stop ajit-spine\n"
            "    python3 operate/retire_crypto_learned_state.py --apply\n"
            "    systemctl --user start ajit-spine",
            file=sys.stderr,
        )
        return 2

    print(f"learned state: {LEARNED_ROOT}")
    if arguments.apply:
        print(f"backed up to:  {back_up(LEARNED_ROOT)}")
    else:
        print("DRY RUN -- nothing will be written. Pass --apply to do it.")
    changed = retire(LEARNED_ROOT, dry_run=not arguments.apply)
    print(
        f"\n{changed} checkpoint(s) {'changed' if arguments.apply else 'would change'}.\n"
        "The bots cold-start on the next spine start and stand aside until conviction is\n"
        "refitted -- which is the honest state of a model that knows nothing about this\n"
        "market yet, not a fault. Watch `champion.observations` climb on the part board."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
