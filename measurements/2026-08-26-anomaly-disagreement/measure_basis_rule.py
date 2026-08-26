"""Would a learned basis flag what a level comparison cannot tell apart?

The level rule -- "this venue's print is more than 0.5% from the others" -- cannot
distinguish a feed that broke from two venues that simply price a contract
differently. Measured on the tape of 2026-08-26: the median disagreement across 36
symbols is 0.0245%, and BTRUSDT sits at 1.38% on 98.9% of its prints. Nothing is
wrong with BTRUSDT's feed. The pair has a basis.

This measures the rule the part's own name describes: learn what the basis between
this venue and the others normally is, and flag the print that departs from it.
The question is what threshold that leaves, and what it still catches.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from measure_cross_venue_disagreement import (  # noqa: E402
    REFERENCE_MAXIMUM_AGE_NS, VENUES, symbols_on_both_venues, trades_of, TRADES_PER_SYMBOL,
)

BASIS_WINDOW = 200
MINIMUM_OBSERVATIONS = 50
DEPARTURES_TESTED = (0.002, 0.003, 0.005, 0.0075, 0.01)


def measure_symbol(symbol: str, day: str) -> dict | None:
    merged = []
    for venue_id in VENUES:
        merged.extend(trades_of(venue_id, symbol, day, TRADES_PER_SYMBOL))
    if not merged:
        return None
    merged.sort()

    latest: dict[str, tuple[int, float]] = {}
    basis_windows: dict[str, list[float]] = {}
    departures: list[float] = []
    compared = 0

    for at_ns, venue_id, price in merged:
        others = [
            other_price
            for other, (other_at_ns, other_price) in latest.items()
            if other != venue_id and at_ns - other_at_ns <= REFERENCE_MAXIMUM_AGE_NS
        ]
        latest[venue_id] = (at_ns, price)
        if not others:
            continue
        reference = sum(others) / len(others)
        if reference <= 0:
            continue
        basis = (price - reference) / reference
        window = basis_windows.setdefault(venue_id, [])
        if len(window) >= MINIMUM_OBSERVATIONS:
            compared += 1
            departures.append(abs(basis - statistics.median(window)))
        window.append(basis)
        if len(window) > BASIS_WINDOW:
            window.pop(0)

    if not departures:
        return None
    ordered = sorted(departures)
    return {
        "symbol": symbol,
        "compared": compared,
        "median_departure": round(statistics.median(ordered), 6),
        "q99_departure": round(ordered[int(0.99 * len(ordered))], 6),
        "largest_departure": round(ordered[-1], 6),
        "flagged_fraction_at": {
            f"{bound:.4f}": round(
                sum(1 for value in ordered if value > bound) / len(ordered), 5
            )
            for bound in DEPARTURES_TESTED
        },
    }


def main() -> None:
    day = (
        sys.argv[1]
        if len(sys.argv) > 1
        else datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    )
    rows = []
    for symbol in symbols_on_both_venues(day):
        row = measure_symbol(symbol, day)
        if row is not None:
            rows.append(row)
            print(json.dumps(row), flush=True)

    compared = sum(row["compared"] for row in rows)
    pooled = {
        f"{bound:.4f}": round(
            sum(row["flagged_fraction_at"][f"{bound:.4f}"] * row["compared"] for row in rows)
            / compared,
            5,
        )
        for bound in DEPARTURES_TESTED
    }
    summary = {
        "day": day,
        "symbols_measured": len(rows),
        "prints_compared": compared,
        "basis_window": BASIS_WINDOW,
        "minimum_observations": MINIMUM_OBSERVATIONS,
        "median_of_symbol_median_departures": round(
            statistics.median([row["median_departure"] for row in rows]), 6
        ),
        "flagged_fraction_at": pooled,
        "worst_symbols_at_0.005": sorted(
            (
                {"symbol": row["symbol"], "flagged": row["flagged_fraction_at"]["0.0050"],
                 "median_departure": row["median_departure"]}
                for row in rows
            ),
            key=lambda row: row["flagged"],
            reverse=True,
        )[:8],
    }
    print(json.dumps({"summary": summary}, indent=2))
    out = pathlib.Path(__file__).parent / f"basis-rule-{day}.json"
    out.write_text(json.dumps({"rows": rows, "summary": summary}, indent=2))
    print(f"written {out}")


if __name__ == "__main__":
    main()
