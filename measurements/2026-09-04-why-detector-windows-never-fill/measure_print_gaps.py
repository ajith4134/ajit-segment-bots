"""Why the scanner's detectors report ~100% `too_few_observations`.

`momentum-burst-detector` and `mean-reversion-detector` refuse to detect until a
symbol's rolling window holds `detector_minimum_observations` (256) returns. On
the live spine on 2026-09-04 both sat at effectively 100% `too_few_observations`
and produced zero `entry-candidate`, which is one of the two reasons no paper
trade has ever opened on the index-options segment.

Two explanations were possible and they call for opposite fixes:

  1. the windows fill but are being emptied -- `observe_price` drops a print and
     counts a series break when a symbol's own trade time jumps further than
     `price_series_maximum_gap_seconds` (120.0s). The time compared is the
     venue's, not the reader's: `price-level-sampler` builds each frame with
     `observed_at_ns=int(trade.venue_time_ns)` (price_level_sampler.py:143). That
     bound's own note records it was measured on binance-usdm perpetuals, where
     the median symbol's p99 gap was 3.0s, and predicts this failure -- "an
     illiquid perpetual admitted to the universe can be quiet for minutes".

  2. the windows simply have not filled yet, because most contracts on an option
     chain do not trade 256 times in a session.

`series_breaks` would separate them, but `describe_bursts` does not export it,
so it cannot be read off the heartbeat table or any board. This measures the
same thing from the tape instead: `venue_time_ns` is a column of
TAPE_RECORD_DTYPE, so the gaps are readable straight from the index with no
payload decoding.

**Read the per-segment breakdown, not the total.** Sampling the tape
alphabetically answers the wrong question: the first 739 instrument directories
are `NCD_FO` (NCDEX commodity derivatives), which barely trade, and reading only
those says 100% of gaps break the bound -- a true number about instruments this
segment is not trading. NSE_INDEX and NSE_FO are Phase A.

Run:  .venv/bin/python measurements/2026-09-04-why-detector-windows-never-fill/measure_print_gaps.py
"""

from __future__ import annotations

import pathlib
import statistics
import sys

import numpy

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from runtime.tape import read_tape_index  # noqa: E402

TAPE_ROOT = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/upstox"
GAP_BOUND_SECONDS = 120.0
MINIMUM_OBSERVATIONS = 256
TRADING_DAY = "2026-09-04"

# Every prefix the tape holds, most relevant to this segment first. NSE_INDEX is
# the three underlyings the bot forms an opinion about; NSE_FO is the option
# chain it would actually buy. The commodity segments are Phase B at the
# earliest and are here only so their contribution is visible rather than
# averaged into the number that matters.
SEGMENTS = ("NSE_INDEX", "NSE_FO", "NSE_EQ", "NSE_COM", "NCD_FO")


def distinct_trade_times(index) -> numpy.ndarray:
    """The venue times this instrument actually printed at, in order, deduplicated.

    Deduplicated because a restated level carries the trade time it was last
    traded at: a repeat computes a zero return against an unchanged price and
    does not move the symbol's clock. The gaps that can break a series are the
    gaps between genuinely new trade times.
    """
    times = numpy.asarray(index["venue_time_ns"], dtype=numpy.uint64)
    times = times[times > 0]
    if times.size == 0:
        return times
    return numpy.unique(times)


def measure_segment(directories) -> dict | None:
    with_trades = 0
    reached_the_floor = 0
    per_instrument_p99: list[float] = []
    total_gaps = 0
    breaking_gaps = 0

    for directory in directories:
        index_path = directory / f"{TRADING_DAY}.index"
        if not index_path.exists():
            continue
        index = read_tape_index(index_path)
        if index.size == 0:
            continue
        times = distinct_trade_times(index)
        if times.size < 2:
            continue
        with_trades += 1
        if times.size >= MINIMUM_OBSERVATIONS:
            reached_the_floor += 1
        gaps = numpy.diff(times).astype(numpy.float64) / 1e9
        total_gaps += gaps.size
        breaking_gaps += int((gaps > GAP_BOUND_SECONDS).sum())
        per_instrument_p99.append(float(numpy.percentile(gaps, 99)))

    if with_trades == 0:
        return None
    return {
        "with_trades": with_trades,
        "reached_the_floor": reached_the_floor,
        "total_gaps": total_gaps,
        "breaking_gaps": breaking_gaps,
        "median_p99_gap": statistics.median(per_instrument_p99),
    }


def main() -> int:
    if not TAPE_ROOT.is_dir():
        print(f"no tape at {TAPE_ROOT}")
        return 1

    print(f"tape root            {TAPE_ROOT}")
    print(f"trading day          {TRADING_DAY}")
    print(f"gap bound            {GAP_BOUND_SECONDS}s  (price_series_maximum_gap_seconds)")
    print(f"window floor         {MINIMUM_OBSERVATIONS}  (detector_minimum_observations)")
    print()
    header = (
        f"{'segment':10s} {'dirs':>6s} {'traded':>7s} {'filled a window':>16s} "
        f"{'gaps>bound':>11s} {'median p99 gap':>15s}"
    )
    print(header)
    print("-" * len(header))

    for prefix in SEGMENTS:
        directories = sorted(
            p for p in TAPE_ROOT.iterdir()
            if p.is_dir() and p.name.startswith(prefix + "|")
        )
        measured = measure_segment(directories)
        if measured is None:
            print(f"{prefix:10s} {len(directories):6,d} "
                  f"{'0':>7s} {'no instrument has two distinct trade times':>16s}")
            continue
        filled_share = measured["reached_the_floor"] / measured["with_trades"]
        breaking_share = measured["breaking_gaps"] / measured["total_gaps"]
        print(
            f"{prefix:10s} {len(directories):6,d} {measured['with_trades']:7,d} "
            f"{measured['reached_the_floor']:7,d} ({filled_share:5.1%}) "
            f"{breaking_share:10.1%} {measured['median_p99_gap']:14.1f}s"
        )

    print()
    print("`traded` counts instruments with two or more distinct trade times today.")
    print("`filled a window` counts those with at least the window floor -- the only")
    print("ones that could produce a detection at all, before any gap bound applies.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
