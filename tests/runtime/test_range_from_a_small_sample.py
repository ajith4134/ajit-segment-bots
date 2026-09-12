"""A range over a handful of prints is an underestimate, and by a measured amount.

RL-063: the correction under test is not invented here -- it is the table
measured on this project's own Upstox tape in
measurements/2026-09-08-a-range-from-a-small-sample/ (300 symbols, 1,974 windows
of 300 s, 9,070 random draws per sample size), and the property being pinned is
checked against real captured prints rather than against a constructed series.
"""

from __future__ import annotations

import json
import pathlib
import random
import statistics

import pytest

from runtime.range_from_a_small_sample import RangeFromASmallSample, RecoveryTableRefused
from runtime.tape import StreamKind, read_tape_index, tape_paths_for

PRINT_COUNTS = (3, 4, 5, 6, 8, 10, 15, 20, 30)
RECOVERED_FRACTIONS = (0.414, 0.529, 0.602, 0.657, 0.730, 0.769, 0.851, 0.892, 0.942)


def a_recovery():
    return RangeFromASmallSample(
        print_counts=PRINT_COUNTS, recovered_fractions=RECOVERED_FRACTIONS
    )


def test_a_sample_below_the_measured_table_makes_no_claim():
    """A 2.4x correction from two prints is guesswork wearing a measurement's clothes."""
    subject = a_recovery()
    assert subject.recovered_share_at(2) is None
    assert subject.corrected(0.01, 2) is None
    assert subject.fewest_prints_correctable == 3


def test_a_range_from_five_prints_is_corrected_upward():
    """0.602 of the true range at the median, so the estimate is the range over it."""
    subject = a_recovery()
    assert subject.corrected(0.006, 5) == pytest.approx(0.006 / 0.602)


def test_a_large_sample_is_not_corrected_at_all():
    subject = a_recovery()
    assert subject.recovered_share_at(30) == 1.0
    assert subject.recovered_share_at(500) == 1.0
    assert subject.corrected(0.02, 500) == pytest.approx(0.02)


def test_the_correction_is_continuous_between_measured_counts():
    """Snapping to the nearest measured count would step the stop distance as one
    more print arrives, which is a different plan for the same market."""
    subject = a_recovery()
    at_five, at_six = subject.recovered_share_at(5), subject.recovered_share_at(6)
    between = subject.recovered_share_at(5)  # exact hit
    assert between == pytest.approx(0.602)
    assert at_five < at_six
    # 5 and 6 are both measured; 7 is not, and must fall between 6 and 8.
    assert subject.recovered_share_at(6) < subject.recovered_share_at(7) < subject.recovered_share_at(8)


def test_the_correction_never_shrinks_a_range():
    """Correcting downward would place the stop tighter, which is the direction
    that stops a trade out of a move it was right about."""
    subject = a_recovery()
    for prints in range(3, 60):
        assert subject.corrected(0.01, prints) >= 0.01


def test_a_mismatched_table_is_refused_at_construction():
    with pytest.raises(RecoveryTableRefused):
        RangeFromASmallSample(print_counts=(3, 5), recovered_fractions=(0.414,))


def test_an_empty_table_is_refused_at_construction():
    with pytest.raises(RecoveryTableRefused):
        RangeFromASmallSample(print_counts=(), recovered_fractions=())


def test_a_recovery_above_one_is_refused():
    with pytest.raises(RecoveryTableRefused):
        RangeFromASmallSample(print_counts=(3, 5), recovered_fractions=(0.4, 1.4))


def test_unsorted_counts_are_refused():
    with pytest.raises(RecoveryTableRefused):
        RangeFromASmallSample(print_counts=(5, 3), recovered_fractions=(0.6, 0.4))


TAPE_ROOT = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/upstox"


def _a_busy_captured_contract():
    """One real symbol with enough prints in a 300 s window to draw from."""
    if not TAPE_ROOT.exists():
        return None
    for directory in sorted(TAPE_ROOT.iterdir())[:400]:
        for index_path in sorted(directory.glob("*.index")):
            if index_path.name.count(".") > 1:  # a non-TRADE stream kind
                continue
            records = read_tape_index(index_path)
            if len(records) < 2_000:
                continue
            day = index_path.name.removesuffix(".index")
            blob_path = tape_paths_for(
                TAPE_ROOT.parent, "upstox", directory.name, day, StreamKind.TRADE
            )[1]
            prints = []
            with open(blob_path, "rb") as handle:
                for record in records:
                    handle.seek(int(record["blob_offset"]))
                    try:
                        row = json.loads(handle.read(int(record["blob_length"])))
                    except ValueError:
                        continue
                    price = row.get("last_traded_price")
                    if price:
                        prints.append(
                            (int(row.get("broker_time_ns") or record["venue_time_ns"]), float(price))
                        )
            if len(prints) >= 2_000:
                prints.sort()
                return prints
    return None


def test_the_correction_recovers_the_real_range_on_captured_prints():
    """The claim, checked against the market rather than against the table.

    A window of real prints has a true range. Draw five of them, correct what
    they span, and the corrected figure should land near the true range at the
    median -- which is exactly what "unbiased at the median" means and is the
    only property that makes this correction worth applying to a live stop.
    """
    prints = _a_busy_captured_contract()
    if prints is None:
        pytest.skip("no captured Upstox tape on this machine to measure against")

    subject = a_recovery()
    rng = random.Random(23)
    span_ns = 300 * 1_000_000_000
    ratios = []
    start = prints[0][0]
    window = []
    for at_ns, price in prints:
        if at_ns - start >= span_ns:
            if len(window) >= 60:
                true_range = max(window) - min(window)
                last = window[-1]
                if true_range > 0 and last > 0:
                    for _ in range(20):
                        drawn = rng.sample(window, 5)
                        measured = (max(drawn) - min(drawn)) / last
                        corrected = subject.corrected(measured, 5)
                        ratios.append(corrected / (true_range / last))
            start, window = at_ns, []
        window.append(price)

    assert len(ratios) >= 100, f"only {len(ratios)} draws; not enough real windows to judge"
    median_ratio = statistics.median(ratios)
    # Uncorrected this median sits near 0.60 by construction. Corrected it should
    # sit near 1.0; the band is wide because one contract's own distribution is
    # not the 300-symbol median the table was fitted on.
    assert 0.80 <= median_ratio <= 1.25, median_ratio
