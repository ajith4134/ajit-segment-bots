#!/usr/bin/env python3
"""Sample the trading chain across a market open, and say what it did.

Armed for the first session after 2026-09-04, when five defects that were each
independently fatal to trading were fixed with the market shut and none of them
could be tested against a live decision. A paper trade cannot open while the
market is closed and that is RL-071 working rather than a fault: history replay
reaches `market-data` and `candle`, never `symbol-price-frame`, so it feeds the
fill path and the candle path and never the decision path.

So this waits for the session the calendar actually reports rather than for a
clock. A holiday produces "the market never opened", which is a finding; assuming
the session from the date is how a watch reports a trading day that never
happened.

Writes to `~/.local/share/ajit-segment-bots/`, never to /tmp -- /tmp on this
server is tmpfs, so a log written there is in RAM and is gone at the next reboot.
"""

from __future__ import annotations

import json
import pathlib
import sys
import time

STATE = pathlib.Path.home() / ".local/share/ajit-segment-bots"
HEARTBEAT = STATE / "heartbeat-table.json"
LOG = STATE / "market-open-watch.log"

# What each part has to say about whether a trade happened, in chain order.
WATCHED = {
    "market-session-calendar": ("is_warm",),
    "broker-market-feed-reader": ("subscribed_instruments", "instruments_tracked"),
    "mean-reversion-detector": (
        "observations", "deepest_window", "symbols_with_a_full_window",
        "windows_broken_by_a_gap", "candidates",
    ),
    "momentum-burst-detector": ("deepest_window", "candidates", "series_breaks"),
    "spread-reversion-detector": ("candidates", "stale_leg"),
    "bull-setup-filter": ("accepted",),
    "bull-conviction-model": ("convictions_formed",),
    "opinion-arbiter": ("intents_formed",),
    "instrument-selector": ("intents_seen", "chosen", "views_converted_to_a_buy"),
    "position-sizer": (
        "actionable_intents_seen", "sized", "refused_stop_invalid",
        "opens_without_an_instrument_choice",
    ),
    "paper-fill-simulator": (
        "orders_seen", "filled", "refused_feed_jump", "feed_jumps_cleared", "held_in_flight",
    ),
    "position-close-detector": ("fills_observed", "positions_open", "closed_trades"),
    "feed-jump-detector": ("jumps_found", "candles_seen", "continuity_restored"),
}

SAMPLE_SECONDS = 180.0
# How long to wait for the calendar to report a tradeable session before giving
# up and saying so. A little over three hours past the scheduled open: enough for
# a late start, short enough that a holiday is reported the same morning.
WAIT_FOR_OPEN_SECONDS = 3 * 3600.0
WATCH_SECONDS = 4 * 3600.0


def note(line: str) -> None:
    stamped = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}"
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(stamped + "\n")
        handle.flush()
    print(stamped, flush=True)


def standing_by_part() -> dict:
    """Every part's standing, or an empty mapping if the table cannot be read.

    A table that cannot be read is not an absence of parts, so the caller is told
    nothing rather than told zero.
    """
    try:
        document = json.loads(HEARTBEAT.read_text())
    except (OSError, ValueError):
        return {}
    rows = document.get("heartbeats", {})
    rows = list(rows.values()) if isinstance(rows, dict) else rows
    return {(row.get("part_id") or row.get("part")): (row.get("standing") or {}) for row in rows}


# broker-history-reader stands down while the market is open and counts every
# tick it skipped for that reason, so a *rising* count is a part saying the
# session is live -- measured, not inferred from the clock or from the date.
OPEN_SIGNAL_PART = "broker-history-reader"
OPEN_SIGNAL_FIELD = "skipped_because_the_market_is_open"


def open_signal(standing: dict) -> float | None:
    """The counter that only climbs while the market is open, or None if unread.

    None is "nothing said", which is not "shut" -- the distinction the whole
    spine keeps. A caller that treated it as shut would report a holiday on a
    trading day whenever this part happened to be off.
    """
    values = standing.get(OPEN_SIGNAL_PART)
    if not values or OPEN_SIGNAL_FIELD not in values:
        return None
    return float(values[OPEN_SIGNAL_FIELD])


def sample(standing: dict) -> str:
    parts = []
    for part_id, fields in WATCHED.items():
        values = standing.get(part_id)
        if not values:
            continue
        shown = [f"{name}={int(values[name])}" for name in fields if values.get(name)]
        if shown:
            parts.append(f"{part_id.split('-')[0]}.{' '.join(shown)}")
    return " | ".join(parts) if parts else "nothing reporting"


def main() -> int:
    note("=== armed: waiting for a part to report the session is live ===")
    deadline = time.monotonic() + WAIT_FOR_OPEN_SECONDS
    opened = False
    # Two readings, because the signal is a rate and not a level: the counter is
    # non-zero for the rest of the day once the market has been open, so its
    # value alone cannot say whether it is open *now*.
    previous = open_signal(standing_by_part())
    while time.monotonic() < deadline:
        time.sleep(60)
        current = open_signal(standing_by_part())
        if previous is not None and current is not None and current > previous:
            opened = True
            break
        previous = current

    if not opened:
        note(
            "the market never opened within the wait -- a holiday, a feed that "
            "never connected, or a spine that is not running. Nothing was sampled, "
            "and that is the finding rather than a zero."
        )
        return 0

    note("=== the session is live; sampling the chain ===")
    end = time.monotonic() + WATCH_SECONDS
    first_fill_said = False
    while time.monotonic() < end:
        standing = standing_by_part()
        note(sample(standing))
        filled = (standing.get("paper-fill-simulator") or {}).get("filled")
        if filled and not first_fill_said:
            note(f"*** FIRST PAPER FILL: {int(filled)} ***")
            first_fill_said = True
        time.sleep(SAMPLE_SECONDS)

    note("=== watch ended ===")
    if not first_fill_said:
        note("no paper order filled during this watch.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
