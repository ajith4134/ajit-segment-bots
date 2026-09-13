"""outage_silence_seconds against every symbol venue-outage-rider actually judges.

621a387 set it to 900s from silence measured on 16-18 underlyings. The rider reads
`symbol-price-frame`, which carries contracts too (price-level-sampler, 1,995 symbols
live), and judges the connection lost when outage_venue_wide_fraction (0.8) of the
symbols it has seen are silent for at least outage_silence_seconds.

Here: every NSE_FO contract and F&O underlying on the tape for 2026-09-07 and -08,
distinct trades, cut at recording holes found from the union of all of them. Sampled
every 60s from 15 minutes into each recorded piece: over symbols that traded earlier
in the piece, the share whose last trade is at least S old.

Run:  .venv/bin/python measurements/2026-09-13-indian-frame-as-parts-see-it/measure_outage_silence_on_the_whole_frame.py
"""

from __future__ import annotations

import bisect
import gzip
import importlib.util
import json
import pathlib
import sys

import numpy

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "cadence", ROOT / "measurements/2026-09-13-indian-observation-cadence/measure_indian_observation_cadence.py"
)
cadence = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cadence)

DAYS = ("2026-09-07", "2026-09-08")
THRESHOLDS = (60, 150, 300, 600, 900, 1800, 3600, 7200)
SAMPLE_SECONDS = 60
VENUE_WIDE_FRACTION = 0.8


def main() -> int:
    master = json.load(gzip.open(cadence.MASTER))
    underlyings = {r["underlying_key"] for r in master
                   if r.get("segment") == "NSE_FO" and r.get("instrument_type") in ("CE", "PE")}
    keys = sorted(p.name for p in cadence.TAPE.glob("NSE_FO*")) + sorted(underlyings)
    shares = {threshold: [] for threshold in THRESHOLDS}
    for day in DAYS:
        times_by_key = {}
        for key in keys:
            trades = cadence.distinct_trades(cadence.TAPE / key, day)
            if trades:
                times_by_key[key] = [at for at, _, _ in trades]
        pieces = cadence.recording_pieces({k: [(t, 0, 0) for t in v] for k, v in times_by_key.items()})
        print(f"{day}: {len(times_by_key):,} symbols traded; {len(pieces)} recorded piece(s), "
              f"{sum(b - a for a, b in pieces) / 60e9:.0f} min", flush=True)
        for start, end in pieces:
            at = start + 15 * 60 * 1_000_000_000
            while at <= end:
                ages = []
                for times in times_by_key.values():
                    position = bisect.bisect_right(times, at) - 1
                    if position >= 0 and times[position] >= start:
                        ages.append((at - times[position]) / 1e9)
                if ages:
                    for threshold in THRESHOLDS:
                        shares[threshold].append(sum(a >= threshold for a in ages) / len(ages))
                at += SAMPLE_SECONDS * 1_000_000_000
    print(f"\nshare of seen symbols silent >= S (connection judged lost at {VENUE_WIDE_FRACTION}):")
    for threshold in THRESHOLDS:
        values = shares[threshold]
        over = sum(v >= VENUE_WIDE_FRACTION for v in values) / len(values)
        print(f"  S={threshold:>5}s  p5 {numpy.quantile(values, .05):.3f}  p50 {numpy.quantile(values, .5):.3f}  "
              f"p95 {numpy.quantile(values, .95):.3f}  max {max(values):.3f}  "
              f"samples at or over {VENUE_WIDE_FRACTION}: {over:.1%} of {len(values)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
