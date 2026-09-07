#!/usr/bin/env python3
"""Per part: what real data went in, what came out, and whether the output is work.

    python3 dashboard/audit_part_purpose.py                  every part
    python3 dashboard/audit_part_purpose.py stock-market-news-data   one block
    python3 dashboard/audit_part_purpose.py --json           machine-readable
    python3 dashboard/audit_part_purpose.py --ledger         rows for the audit ledger

The instrument for the **third temporary goal** (2026-09-06, `docs/goal.md`):
prove for each of the 373 parts that it is *"really providing or working with the
data according to its intended purpose, or if it is just a skeleton providing or
inputting or outputing rubbish data which is not useful just decorating data"*.

**Why the three instruments already here cannot answer it.**

    check_contracts.py           the declaration is coherent
    audit_feature_dataflow.py    a message travelled on a declared wire
    audit_part_data_reality.py   the universe a part holds is real and fresh

The first two can be perfectly green while a part publishes a number that means
nothing -- a climbing counter proves a message moved, never that it was right.
The third opens the state a part restores from, which is the sharpest evidence
there is, but only 21 parts checkpoint anything. This one covers every part, by
reading the one thing they all publish: their own standing.

**What it measures, and why that is evidence rather than decoration.**

A part's `standing` is its own account of what it did -- candidates raised, plans
made, refusals by reason. It is published on health beside the bus counters, so
for one measured window this probe can put three facts side by side:

    input      messages received, by type, as a delta this process observed
    output     messages published, by type, same delta
    work       which of the part's own standing counters moved, and to what

The three together separate the cases that matter. A part receiving thousands of
messages whose work counters are all still is not idle -- it is *refusing
everything*, which is what `momentum-burst-detector` was doing with 7,654,732
observations and 0 candidates, and what `arxiv-feed-reader` does with 352,432
fetches attempted and 0 papers fetched (every one `refused_no_search_installed`).
Neither shows up as anything but healthy on a dataflow audit.

**Four states, named for what this probe can prove and not for the ledger's
vocabulary (Rule 8).** A probe that reads counters cannot read whether a number
is *right*, so none of these is `SERVING ITS PURPOSE` -- that verdict costs a
person looking at the actual output, and a state that claimed it would be the
green tile this whole goal exists to prevent.

    PRODUCES REAL OUTPUT      real input has reached it, it has published, and
                              its own work counters are above zero. It is doing
                              something with real data. Whether the something is
                              *correct* is the next question, not this one.
    FED BUT PRODUCES NOTHING  real input has reached it and nothing has ever come
                              of it. The strong negative finding -- and still not
                              proof of a skeleton, because a part that acts on an
                              event is silent until the event happens. It says
                              exactly where to aim a targeted test.
    ONLY REFUSALS             input arrived and the only counters above zero are
                              refusals. A gate correctly refusing everything and a
                              part that cannot do its job look identical here, and
                              telling them apart needs someone who knows what the
                              gate is for.
    NOT MEASURED              nothing has ever reached it, or it is not running,
                              or its standing carries nothing readable as work

**The window is observed, never assumed.** Two readings of the heartbeat table
this process took itself, with the real elapsed time between them. A counter
that is large but still is a counter from before this probe started, and calling
that activity is the mistake `audit_part_data_reality.py` was written to stop.

RL-063 binds: the input is whatever the live spine is actually receiving, which
on a trading day is the real feed. A part fed nothing is reported as fed
nothing -- never as passing.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

BLUEPRINT = PROJECT / "docs" / "features.json"
HEARTBEAT = pathlib.Path.home() / ".local/share/ajit-segment-bots/heartbeat-table.json"

# What this probe can actually prove, named for that and not for the ledger's
# vocabulary. A whole-system probe reads counters; it cannot read whether a
# number is *right*, and a state called SERVING ITS PURPOSE would claim it did.
PRODUCES = "PRODUCES REAL OUTPUT"
PRODUCES_NOTHING = "FED BUT PRODUCES NOTHING"
STARVED = "WAITING FOR AN INPUT IT HAS NEVER SEEN"
ONLY_REFUSALS = "ONLY REFUSALS"
NOT_MEASURED = "NOT MEASURED"

# A standing counter whose name says it counted something the part declined to
# do. Matched by prefix on the last path segment so `refused_by_reason.x` and
# `by_refusal.x` are both caught, and so a part inventing its own refusal name
# is caught without this list being edited.
REFUSAL_WORDS = (
    "refused", "refusal", "skipped", "skip", "rejected", "dropped", "stood_aside",
    "stood_down", "not_", "no_", "missing_", "failed", "failures", "unmeasurable",
    "incomplete", "cannot", "waiting", "held_", "deferred", "unchanged",
)

# A counter that says how much the part is holding rather than how much it did.
# A held universe is state, not work: `symbols_tracked` climbing says the part
# was handed more symbols, not that it did anything with them.
HOLDING_WORDS = (
    "_held", "held_", "_known", "known_", "tracked", "_seen", "seen_",
    "observations", "symbols_with", "instruments_", "largest_", "worst_",
    "deepest_", "widest_", "strongest_", "highest_", "last_", "cycle_position",
)

SECONDS_BETWEEN_READINGS = 8.0


def read_heartbeats() -> dict:
    """One reading of the table `heartbeat-collector` writes, by part."""
    try:
        table = json.loads(HEARTBEAT.read_text())
    except (OSError, ValueError):
        return {}
    return {entry["part_id"]: entry for entry in table.get("heartbeats", [])}


def blueprint_parts() -> list[tuple[str, str]]:
    """Every part in the blueprint as (block_id, part_id), in blueprint order.

    Parts live in `features`, each naming the `category` it belongs to -- the
    categories list carries the blocks themselves and no parts, which is R-01's
    shape: a block's consumes/produces are computed from its parts, so the parts
    are the record and the block is the derived thing.
    """
    document = json.loads(BLUEPRINT.read_text())
    order = {category["id"]: index for index, category in enumerate(document["categories"])}
    parts = [(part["category"], part["id"]) for part in document["features"]]
    parts.sort(key=lambda entry: (order.get(entry[0], len(order)), entry[1]))
    return parts


def declared_inputs() -> dict[str, tuple[str, ...]]:
    """What each part declares it consumes, `part-health` excluded.

    Compared against what actually arrived, this splits the parts that produce
    nothing into two very different findings: one that has never seen an input it
    declares is waiting for something upstream, and one that has received every
    input it asked for and still publishes nothing is a defect. Only the second
    is worth a person's time first.
    """
    document = json.loads(BLUEPRINT.read_text())
    return {
        part["id"]: tuple(t for t in part.get("consumes", []) if t != "part-health")
        for part in document["features"]
    }


def declared_outputs() -> dict[str, tuple[str, ...]]:
    """What each part declares it produces, `part-health` excluded.

    Every part publishes health; a part whose only declared output is health is a
    genuine sink and is judged on its own work counters. A part that declares a
    real output and never sends it is not a sink, it is silent.
    """
    document = json.loads(BLUEPRINT.read_text())
    return {
        part["id"]: tuple(t for t in part.get("produces", []) if t != "part-health")
        for part in document["features"]
    }


def flatten(standing: dict) -> dict[str, float]:
    """The numeric leaves of a standing, by dotted name."""
    out: dict[str, float] = {}
    for name, value in (standing or {}).items():
        if isinstance(value, dict):
            for inner, number in value.items():
                if isinstance(number, (int, float)) and not isinstance(number, bool):
                    out[f"{name}.{inner}"] = float(number)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            out[name] = float(value)
    return out


def is_refusal(name: str) -> bool:
    leaf = name.split(".")[0]
    return any(word in leaf for word in REFUSAL_WORDS)


def is_holding(name: str) -> bool:
    leaf = name.split(".")[0]
    return any(word in leaf for word in HOLDING_WORDS)


def total(counter: dict | None) -> float:
    if not isinstance(counter, dict):
        return 0.0
    return float(sum(v for v in counter.values() if isinstance(v, (int, float))))


def judge(before: dict | None, after: dict | None, declares_output: bool = True,
          consumes: tuple[str, ...] = ()) -> dict:
    """One part's verdict, with the evidence that produced it.

    **The verdict is cumulative and the window is only liveness.** An early
    version judged on an eight-second delta and called `book-walk-fill-pricer` a
    skeleton: it had 1,517 order-book snapshots in and nothing out, because it
    prices a fill only when there is a fill to price, and there were no orders in
    those eight seconds. Its standing carried 6,312 estimates. A part that acts on
    an event is silent between events, and silence between events is not
    decoration -- so the question "has this part ever done its job while being
    fed" is answered from the counters since the spine started, and the delta
    answers the different question of whether it is doing it right now.
    """
    if after is None or after.get("reported_state") != "on":
        return {
            "verdict": NOT_MEASURED, "live": "not running",
            "why": "the part is not running, so nothing was fed to it and nothing was observed",
            "input": {}, "output": {}, "work": {},
        }

    # `part-health` is what every part publishes about itself once a second. It is
    # not the part doing its job, so it never counts as input or as output here --
    # counting it would make every running part look fed and productive.
    def by_type(entry, key):
        return {k: v for k, v in (entry or {}).get(key, {}).items() if k != "part-health"}

    inputs_now, inputs_then = by_type(after, "messages_received"), by_type(before, "messages_received")
    outputs_now, outputs_then = by_type(after, "messages_published"), by_type(before, "messages_published")
    input_delta = {k: v - inputs_then.get(k, 0) for k, v in inputs_now.items() if v - inputs_then.get(k, 0) > 0}
    output_delta = {k: v - outputs_then.get(k, 0) for k, v in outputs_now.items() if v - outputs_then.get(k, 0) > 0}

    standing = flatten(after.get("standing"))
    work = {k: v for k, v in standing.items()
            if v > 0 and not is_refusal(k) and not is_holding(k)}
    refusals = {k: v for k, v in standing.items() if v > 0 and is_refusal(k)}

    previous = flatten((before or {}).get("standing"))
    moved = {k for k, v in standing.items() if v != previous.get(k, 0.0)}
    live = "working" if (moved or output_delta) else "idle in the window"

    if not inputs_now:
        return {
            "verdict": NOT_MEASURED, "live": live,
            "why": "nothing has ever reached this part, so its purpose has not been exercised",
            "input": {}, "output": outputs_now, "work": work,
        }

    # **Publishing nothing is disqualifying, whatever the internal counters say.**
    # `arxiv-feed-reader` has `fetches_attempted` at 60,780 and publishes not one
    # paper -- every fetch is `refused_no_search_installed`. Attempting is not
    # producing, and a counter that climbs while nothing leaves the part is the
    # exact shape of decoration this ledger exists to catch. A part with no
    # `produces` in the blueprint is a genuine sink and is judged on its work
    # alone; one that declares an output and never sends it is not.
    if work and (outputs_now or not declares_output):
        return {
            "verdict": PRODUCES, "live": live,
            "why": (
                f"{sum(inputs_now.values()):,.0f} message(s) in, {sum(outputs_now.values()):,.0f} out, "
                f"and {len(work)} of its own work counter(s) are above zero"
            ),
            "input": inputs_now, "output": outputs_now, "work": work,
        }

    if refusals:
        return {
            "verdict": ONLY_REFUSALS, "live": live,
            "why": (
                f"{sum(inputs_now.values()):,.0f} message(s) in and not one counter of its own work "
                f"has ever left zero -- the only counters above zero are refusals"
            ),
            "input": inputs_now, "output": outputs_now, "work": refusals,
        }

    if outputs_now:
        return {
            "verdict": NOT_MEASURED, "live": live,
            "why": (
                f"{sum(outputs_now.values()):,.0f} message(s) published and its standing carries no "
                f"counter this probe can read as work -- pure transport, or a standing that does "
                f"not account for what it did"
            ),
            "input": inputs_now, "output": outputs_now, "work": {},
        }

    never_seen = tuple(sorted(set(consumes) - set(inputs_now)))
    if never_seen:
        return {
            "verdict": STARVED, "live": live,
            "why": (
                f"{sum(inputs_now.values()):,.0f} message(s) in and nothing out, and it has never "
                f"seen {len(never_seen)} of the input(s) it declares: {', '.join(never_seen[:4])}"
                + (f" (+{len(never_seen) - 4} more)" if len(never_seen) > 4 else "")
            ),
            "input": inputs_now, "output": outputs_now, "work": {},
        }

    return {
        "verdict": PRODUCES_NOTHING, "live": live,
        "why": (
            f"{sum(inputs_now.values()):,.0f} message(s) have reached it on EVERY input it "
            f"declares and nothing has ever come of them: nothing published, and no counter of "
            f"its own work above zero"
        ),
        "input": inputs_now, "output": outputs_now, "work": {},
    }


def short(mapping: dict, limit: int = 3) -> str:
    items = sorted(mapping.items(), key=lambda kv: -abs(kv[1] if isinstance(kv[1], (int, float)) else 0))
    rendered = []
    for name, value in items[:limit]:
        if isinstance(value, tuple):
            rendered.append(f"{name} {value[0]:,.0f}->{value[1]:,.0f}")
        else:
            rendered.append(f"{name} {value:,.0f}")
    if len(items) > limit:
        rendered.append(f"+{len(items) - limit} more")
    return ", ".join(rendered) if rendered else "nothing"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("block", nargs="?", help="one block id, or every block")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--ledger", action="store_true", help="rows for docs/part-purpose-audit.md")
    parser.add_argument("--window", type=float, default=SECONDS_BETWEEN_READINGS)
    arguments = parser.parse_args()

    before = read_heartbeats()
    started = time.monotonic()
    time.sleep(arguments.window)
    after = read_heartbeats()
    window = time.monotonic() - started

    parts = blueprint_parts()
    if arguments.block:
        parts = [(block, part) for block, part in parts if block == arguments.block]
        if not parts:
            print(f"no block named {arguments.block!r} in {BLUEPRINT}", file=sys.stderr)
            return 2

    produces, consumes = declared_outputs(), declared_inputs()
    results = {
        part: {
            "block": block,
            **judge(
                before.get(part), after.get(part),
                declares_output=bool(produces.get(part)),
                consumes=consumes.get(part, ()),
            ),
        }
        for block, part in parts
    }

    if arguments.as_json:
        print(json.dumps({"window_seconds": window, "parts": results}, indent=1))
        return 0

    if arguments.ledger:
        for part, result in results.items():
            fed = short(result["input"]) if result["input"] else ""
            out = short(result["output"]) or ""
            work = short(result["work"]) if result["work"] else ""
            came = f"{out}{' — ' + work if work else ''}"
            print(f"| `{part}` | {result['verdict']} | {fed} | {came} |")
        return 0

    counts: dict[str, int] = {}
    for result in results.values():
        counts[result["verdict"]] = counts.get(result["verdict"], 0) + 1

    print("Is each part serving its purpose?\n")
    print(f"  parts in the blueprint  {len(results)}")
    print(f"  window observed         {window:.1f}s")
    for verdict in (PRODUCES, PRODUCES_NOTHING, STARVED, ONLY_REFUSALS, NOT_MEASURED):
        print(f"  {verdict:22s}  {counts.get(verdict, 0)}")

    for verdict in (PRODUCES_NOTHING, ONLY_REFUSALS):
        named = [(p, r) for p, r in results.items() if r["verdict"] == verdict]
        if not named:
            continue
        print(f"\n{'=' * 78}\n{verdict} — {len(named)}\n{'=' * 78}")
        for part, result in named:
            print(f"\n  {part}   ({result['block']})")
            print(f"      {result['why']}")
            print(f"      in    {short(result['input'], 4)}")
            print(f"      out   {short(result['output'], 4)}")
            if result["work"]:
                print(f"      work  {short(result['work'], 4)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
