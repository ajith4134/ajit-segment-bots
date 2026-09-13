#!/usr/bin/env python3
"""What liquidity-grader decides on real NSE order books, under its old and re-derived bands.

Every book snapshot on this project's own tape for 2026-09-07 and 2026-09-08 (Upstox's
five levels a side), sampled once a minute per instrument, graded by the part's own
`LiquidityGrader` at `liquidity_reference_order_size` -- so the cost is exactly the
figure the live part computes: spread plus the walk on each side.

Reported separately: books too shallow to absorb the order at all (graded untradeable
whatever the bands say) and books the bands decide.

    .venv/bin/python measurements/2026-09-13-indian-liquidity-grades/measure_grades.py
"""
import collections
import json
import pathlib
import statistics
import sys
import tomllib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from parts.opportunity_scanner.liquidity_grader import LiquidityGrader  # noqa: E402
from runtime.tape import read_payload, read_tape_index  # noqa: E402

TAPE = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/upstox"
SETTINGS = pathlib.Path.home() / ".config/ajit-segment-bots/settings/runtime.toml"
DAYS = ("2026-09-07", "2026-09-08")
MINUTE_NS = 60 * 1_000_000_000
OLD = (0.004266, 0.005, 0.02)
NEW = (0.004266, 0.0203, 0.0609)


def kind_of(name: str) -> str:
    parts = name.split()
    if len(parts) >= 4 and parts[2] in ("CE", "PE"):
        return "option"
    if "FUT" in parts:
        return "future"
    return "share-or-index"


def main() -> int:
    settings = tomllib.loads(SETTINGS.read_text())
    size = float(settings["liquidity_reference_order_size"]["value"])
    costs = collections.defaultdict(list)
    shallow = collections.Counter()
    graded = collections.Counter()
    grades = {name: collections.Counter() for name in ("old", "new")}
    for root in sorted(p for p in TAPE.iterdir() if p.is_dir() and "|" not in p.name):
        kind = kind_of(root.name)
        for day in DAYS:
            index = root / f"{day}.book.index"
            if not index.exists():
                continue
            last_minute = None
            for record in read_tape_index(index):
                try:
                    book = json.loads(read_payload(root / f"{day}.book.blob", record))
                except (ValueError, OSError):
                    continue
                at = int(book.get("broker_time_ns") or 0)
                if last_minute is not None and at - last_minute < MINUTE_NS:
                    continue
                last_minute = at
                levels = book.get("levels") or []
                bids = [(l["bid_price"], l["bid_quantity"]) for l in levels if l.get("bid_price")]
                asks = [(l["ask_price"], l["ask_quantity"]) for l in levels if l.get("ask_price")]
                graded[kind] += 1
                for name, bands in (("old", OLD), ("new", NEW)):
                    grader = LiquidityGrader(1.0, *bands, 60)
                    grader.observe_book("upstox", root.name, bids, asks)
                    grade = grader.grade("upstox", root.name, size, force=True)
                    grades[name][(kind, grade.grade)] += 1
                    if name == "old" and grade.round_trip_cost_fraction is not None:
                        costs[kind].append(grade.round_trip_cost_fraction)
                    if name == "old" and grade.grade == "untradeable" and grade.round_trip_cost_fraction is None:
                        shallow[kind] += 1
    print(f"tape {', '.join(DAYS)}; order size {size:,.0f}; one book a minute per instrument")
    for kind in sorted(graded):
        values = sorted(costs[kind])
        print(f"\n{kind}: {graded[kind]:,} books; {shallow[kind]:,} ({shallow[kind] / graded[kind]:.1%}) too shallow "
              f"to absorb the order at any shown price")
        if values:
            q = lambda p: values[min(len(values) - 1, int(p * len(values)))]
            print(f"  round-trip book cost where measurable: p25 {q(.25):.3%}  p50 {q(.5):.3%}  "
                  f"p75 {q(.75):.3%}  p90 {q(.9):.3%}")
        for name in ("old", "new"):
            row = {g: grades[name][(kind, g)] for g in ("deep", "tradeable", "thin", "untradeable", "ungradeable")}
            total = sum(row.values())
            print(f"  {name} bands {OLD if name == 'old' else NEW}: " + ", ".join(
                f"{g} {n / total:.1%}" for g, n in row.items() if n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
