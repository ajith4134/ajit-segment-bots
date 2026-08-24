"""One snapshot of what a step of the universe walk costs, from what is running.

RL-009 walks towards the full universe rather than jumping to it, and the step
from 30 to 100 on 2026-08-23 is why: the decision half could not keep up, symbols
froze, and trades were decided on prices the market had left an hour earlier. The
walk is only meaningful if each step is measured, so this is the instrument.

Nothing here asks a part a question. Every number is read from something the
system already writes:

    the heartbeat table   what each part lost since its previous report, and the
                          refusals it counted for a price too old to act on
    the trade journal     every opening fill against the price its own decision
                          was made at, matched by trade id and direction
    the tape              how many symbols are being written, and how recently

Run before a step and after it, and compare. A step is kept only if decision
staleness holds and input loss does not become continuous.

    .venv/bin/python measurements/2026-08-24-universe-walk/measure_walk_step.py --label at-30
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

SHARE = pathlib.Path.home() / ".local/share/ajit-segment-bots"
HEARTBEAT_TABLE = SHARE / "heartbeat-table.json"
TAPE_ROOT = SHARE / "tape"
LIFECYCLE_JOURNAL = SHARE / "journal.trade-lifecycle-recorder.sqlite"
JOURNAL_TAIL_BYTES = 400_000_000


def read_heartbeats() -> dict:
    """What every running part says about itself right now."""
    if not HEARTBEAT_TABLE.is_file():
        return {"state": "NOT MEASURED", "why": f"{HEARTBEAT_TABLE} does not exist"}
    document = json.loads(HEARTBEAT_TABLE.read_text())
    age_seconds = (time.time_ns() - int(document.get("collected_at_ns", 0))) / 1e9
    losing_now, losing_ever, refusals = [], [], collections.Counter()
    for beat in document.get("heartbeats", ()):
        since_previous = dict(beat.get("input_loss_since_previous") or ())
        if since_previous:
            losing_now.append({"part_id": beat["part_id"], "lost": since_previous})
        if beat.get("input_loss"):
            losing_ever.append({"part_id": beat["part_id"], "lost": dict(beat["input_loss"])})
        standing = beat.get("standing") or {}
        for name in ("refused_for_a_stale_price", "refused_for_no_price_ever", "series_breaks"):
            if standing.get(name):
                refusals[f"{beat['part_id']}.{name}"] = standing[name]
    return {
        "table_age_seconds": age_seconds,
        "reporting": document.get("reporting"),
        "silent": document.get("silent"),
        "never_reported": document.get("never_reported"),
        "parts_losing_input_since_their_previous_report": losing_now,
        "parts_that_have_ever_lost_input": losing_ever,
        "refusals_and_breaks": dict(refusals),
    }


def read_tape() -> dict:
    """How many symbols are actually being written, and how recently."""
    if not TAPE_ROOT.is_dir():
        return {"state": "NOT MEASURED", "why": f"{TAPE_ROOT} does not exist"}
    now = time.time()
    per_venue = {}
    for venue in sorted(path for path in TAPE_ROOT.iterdir() if path.is_dir()):
        written_recently = 0
        symbols = 0
        for symbol in venue.iterdir():
            if not symbol.is_dir():
                continue
            symbols += 1
            newest = max(
                (blob.stat().st_mtime for blob in symbol.glob("*.blob")), default=None
            )
            if newest is not None and now - newest < 60.0:
                written_recently += 1
        per_venue[venue.name] = {
            "symbols_on_the_tape": symbols,
            "symbols_written_in_the_last_minute": written_recently,
        }
    return per_venue


def read_decision_freshness(since_ns: int | None) -> dict:
    """Every opening fill against the price its own decision named.

    Matched by trade id and direction: a closing fill lands wherever the trade
    went, and comparing it against the entry measures the trade's result rather
    than what the bot believed the market was.
    """
    if not LIFECYCLE_JOURNAL.is_file():
        return {"state": "NOT MEASURED", "why": f"{LIFECYCLE_JOURNAL} does not exist"}
    size = LIFECYCLE_JOURNAL.stat().st_size
    orders, measured, drifts = {}, set(), []
    with open(LIFECYCLE_JOURNAL, "rb") as handle:
        if size > JOURNAL_TAIL_BYTES:
            handle.seek(size - JOURNAL_TAIL_BYTES)
            handle.readline()  # discard the partial line the seek landed inside
        for raw in handle:
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if since_ns is not None and record.get("recorded_at_ns", 0) < since_ns:
                continue
            payload = record.get("payload") or {}
            trade_id = payload.get("trade_id")
            if not trade_id:
                continue
            if record.get("kind") == "bounded-order" and payload.get("entry_price"):
                orders[trade_id] = (
                    float(payload["entry_price"]), payload.get("side"), payload.get("symbol")
                )
            elif record.get("kind") == "fill" and payload.get("price"):
                order = orders.get(trade_id)
                if order is None or trade_id in measured:
                    continue
                decided, side, symbol = order
                if (side is not None and payload.get("side") != side) or decided <= 0:
                    continue
                measured.add(trade_id)
                drifts.append((abs(float(payload["price"]) - decided) / decided, symbol))
    if not drifts:
        return {"state": "NOTHING YET", "why": "no opening fill has both numbers in this window"}
    values = sorted(drift for drift, _ in drifts)
    worst, worst_symbol = max(drifts)
    return {
        "opening_fills": len(values),
        "median_drift": statistics.median(values),
        "p95_drift": values[max(0, int(0.95 * len(values)) - 1)],
        "worst_drift": worst,
        "worst_symbol": worst_symbol,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True, help="which step this snapshot is of")
    parser.add_argument("--since-ns", type=int, default=None,
                        help="ignore journal entries older than this, e.g. the last restart")
    arguments = parser.parse_args()

    snapshot = {
        "label": arguments.label,
        "taken_at_ns": time.time_ns(),
        "heartbeats": read_heartbeats(),
        "tape": read_tape(),
        "decision_freshness": read_decision_freshness(arguments.since_ns),
    }
    print(json.dumps(snapshot, indent=2, default=float))
    output = pathlib.Path(__file__).with_name(f"snapshot-{arguments.label}.json")
    output.write_text(json.dumps(snapshot, indent=2, default=float))
    print(f"\nwritten {output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
