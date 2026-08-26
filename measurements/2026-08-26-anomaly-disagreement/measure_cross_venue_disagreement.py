"""What two venues actually say a symbol is worth, at the moment one of them prints.

`market-anomaly-detector` calls a venue anomalous when its print sits further than
`anomaly_disagreement_threshold` (0.5%) from what the other venues say, and
`trading-halt-decider` halts the symbol on that. On the live spine of 2026-08-26
that fired 25,011 times in one run and the halt kept `position-sizer` at zero.

This replays the same comparison against the tape, which is the real market both
venues actually printed, and answers three questions the live counters cannot:

    how far apart are the two venues, really, at the moment of a print
    how much of the distance is the reference being old rather than wrong
    how many symbols are the flags concentrated in

The rule is the detector's own: the reference is every *other* venue's last trade,
believed for `consolidated_price_maximum_quote_age` (5s) and dropped after that.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from runtime.tape import read_payload, read_tape_index
from runtime.venues.adapter_registry import load_venue_adapter

TAPE_ROOT = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape"
VENUES = ("binance-usdm", "bybit-linear")
DISAGREEMENT_THRESHOLD = 0.005
REFERENCE_MAXIMUM_AGE_NS = 5_000_000_000
TRADES_PER_SYMBOL = 60_000
AGE_BUCKETS_NS = (100_000_000, 500_000_000, 1_000_000_000, 2_000_000_000, 5_000_000_000)


def symbols_on_both_venues(day: str) -> list[str]:
    per_venue = []
    for venue_id in VENUES:
        root = TAPE_ROOT / venue_id
        per_venue.append({
            directory.name
            for directory in root.iterdir()
            if (directory / f"{day}.index").exists()
            and (directory / f"{day}.index").stat().st_size > 0
        })
    return sorted(per_venue[0] & per_venue[1])


def trades_of(venue_id: str, symbol: str, day: str, limit: int) -> list:
    adapter = load_venue_adapter(venue_id)
    index_path = TAPE_ROOT / venue_id / symbol / f"{day}.index"
    blob_path = TAPE_ROOT / venue_id / symbol / f"{day}.blob"
    trades = []
    for record in read_tape_index(index_path):
        for trade in adapter.read_trades(read_payload(blob_path, record)):
            trades.append((trade.venue_time_ns, trade.venue_id, trade.price))
        if len(trades) >= limit:
            break
    return trades


def measure_symbol(symbol: str, day: str) -> dict | None:
    merged = []
    for venue_id in VENUES:
        merged.extend(trades_of(venue_id, symbol, day, TRADES_PER_SYMBOL))
    if not merged:
        return None
    merged.sort()

    latest: dict[str, tuple[int, float]] = {}
    disagreements: list[float] = []
    flagged_by_age: dict[str, list[int]] = {}
    compared = 0
    no_fresh_reference = 0

    for at_ns, venue_id, price in merged:
        others = [
            (other_at_ns, other_price)
            for other, (other_at_ns, other_price) in latest.items()
            if other != venue_id and at_ns - other_at_ns <= REFERENCE_MAXIMUM_AGE_NS
        ]
        latest[venue_id] = (at_ns, price)
        if not others:
            no_fresh_reference += 1
            continue
        reference = sum(other_price for _at, other_price in others) / len(others)
        if reference <= 0:
            continue
        compared += 1
        disagreement = abs(price - reference) / reference
        disagreements.append(disagreement)
        if disagreement > DISAGREEMENT_THRESHOLD:
            age_ns = at_ns - min(other_at_ns for other_at_ns, _price in others)
            bucket = next(
                (f"under_{bound // 1_000_000}ms" for bound in AGE_BUCKETS_NS if age_ns <= bound),
                "over_5s",
            )
            flagged_by_age.setdefault(bucket, []).append(age_ns)

    if not disagreements:
        return None
    ordered = sorted(disagreements)
    flagged = sum(len(ages) for ages in flagged_by_age.values())
    return {
        "symbol": symbol,
        "prints": len(merged),
        "compared": compared,
        "no_fresh_reference": no_fresh_reference,
        "flagged": flagged,
        "flagged_fraction": round(flagged / compared, 4) if compared else None,
        "median_disagreement": round(statistics.median(ordered), 6),
        "q90_disagreement": round(ordered[int(0.90 * len(ordered))], 6),
        "q99_disagreement": round(ordered[int(0.99 * len(ordered))], 6),
        "largest_disagreement": round(ordered[-1], 6),
        "flagged_by_reference_age": {
            bucket: len(ages) for bucket, ages in sorted(flagged_by_age.items())
        },
    }


def main() -> None:
    day = (
        sys.argv[1]
        if len(sys.argv) > 1
        else datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    )
    shared = symbols_on_both_venues(day)
    rows = []
    for symbol in shared:
        row = measure_symbol(symbol, day)
        if row is not None:
            rows.append(row)
            print(json.dumps(row), flush=True)

    compared = sum(row["compared"] for row in rows)
    flagged = sum(row["flagged"] for row in rows)
    medians = [row["median_disagreement"] for row in rows]
    summary = {
        "day": day,
        "symbols_on_both_venues": len(shared),
        "symbols_measured": len(rows),
        "prints_compared": compared,
        "prints_flagged_at_the_live_threshold": flagged,
        "flagged_fraction": round(flagged / compared, 4) if compared else None,
        "median_of_symbol_medians": round(statistics.median(medians), 6) if medians else None,
        "worst_symbols": sorted(
            ({"symbol": r["symbol"], "flagged_fraction": r["flagged_fraction"],
              "median": r["median_disagreement"]} for r in rows),
            key=lambda r: r["flagged_fraction"] or 0,
            reverse=True,
        )[:10],
    }
    print(json.dumps({"summary": summary}, indent=2))
    out = pathlib.Path(__file__).parent / f"cross-venue-disagreement-{day}.json"
    out.write_text(json.dumps({"rows": rows, "summary": summary}, indent=2))
    print(f"written {out}")


if __name__ == "__main__":
    main()
