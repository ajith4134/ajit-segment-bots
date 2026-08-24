"""A window must not span a hole in the series it claims to describe.

Twenty-eight parts judge a price series through `RollingWindow`, and until
2026-08-24 it held values with no idea when any of them arrived. Under input loss
-- which the bus reports and counts, because it drops rather than blocks -- a
symbol's prints stop and then resume. To a window that cannot see time, the first
print after the gap sits directly beside the last one before it, and the jump
between them reads as a move that happened in an instant. That is the shape that
fires a momentum-burst or a liquidation-cascade detector on a reconnect.

The other half is the mirror: statistics computed across a window that no longer
describes a continuous stretch of market are not conservative, they are wrong.
"""

from __future__ import annotations

import pytest

from runtime.rolling_statistics import RollingWindow

SECOND_NS = 1_000_000_000


def test_a_window_without_a_gap_bound_behaves_as_it_always_did():
    """Not every series is a price. A window told nothing about time keeps none."""
    window = RollingWindow(length=4)
    for value in (1.0, 2.0, 3.0):
        window.observe(value)

    assert window.count == 3
    assert window.latest == 3.0
    assert window.series_breaks == 0


def test_a_gap_longer_than_the_bound_clears_the_window():
    window = RollingWindow(length=8, maximum_gap_seconds=5.0)
    for index in range(5):
        window.observe(100.0 + index, at_ns=index * SECOND_NS)
    assert window.count == 5

    window.observe(140.0, at_ns=(4 + 60) * SECOND_NS)

    assert window.count == 1, "the series before the hole does not describe the series after it"
    assert window.latest == 140.0
    assert window.series_breaks == 1


def test_a_cleared_window_cannot_answer_until_it_refills():
    """The point of clearing: every statistic goes back to None, so a detector
    with a minimum-observations guard simply does not fire."""
    window = RollingWindow(length=8, maximum_gap_seconds=5.0)
    for index in range(8):
        window.observe(100.0 + index, at_ns=index * SECOND_NS)
    assert window.standard_deviation(minimum_observations=5) is not None

    window.observe(140.0, at_ns=(7 + 60) * SECOND_NS)

    assert window.standard_deviation(minimum_observations=5) is None
    assert window.mean(minimum_observations=5) is None
    assert window.z_score(140.0, minimum_observations=5) is None


def test_the_jump_across_a_gap_is_never_a_change():
    """The failure this closes: a reconnect must not read as one enormous move."""
    window = RollingWindow(length=16, maximum_gap_seconds=5.0)
    for index in range(6):
        window.observe(100.0, at_ns=index * SECOND_NS)
    window.observe(130.0, at_ns=(5 + 3_360) * SECOND_NS)

    assert window.returns() == [], "there is no pair of adjacent observations to change between"


def test_a_gap_inside_the_bound_is_an_ordinary_observation():
    window = RollingWindow(length=8, maximum_gap_seconds=5.0)
    window.observe(100.0, at_ns=0)
    window.observe(101.0, at_ns=4 * SECOND_NS)

    assert window.count == 2
    assert window.series_breaks == 0


def test_a_bounded_window_refuses_an_observation_with_no_time():
    """A caller that passed no time would be asserting continuity it never checked."""
    window = RollingWindow(length=4, maximum_gap_seconds=5.0)
    with pytest.raises(ValueError):
        window.observe(100.0)


def test_how_long_the_series_has_been_silent_is_answerable():
    window = RollingWindow(length=4, maximum_gap_seconds=5.0)
    window.observe(100.0, at_ns=1_000 * SECOND_NS)

    assert window.seconds_since_last_observation(1_030 * SECOND_NS) == pytest.approx(30.0)


def test_a_window_that_has_seen_nothing_has_no_silence_to_report():
    """Never observed and just observed are opposite facts."""
    assert RollingWindow(length=4).seconds_since_last_observation(SECOND_NS) is None


def test_a_gap_bound_that_is_not_positive_is_refused():
    with pytest.raises(ValueError):
        RollingWindow(length=4, maximum_gap_seconds=0.0)
    with pytest.raises(ValueError):
        RollingWindow(length=4, maximum_gap_seconds=-1.0)


def test_the_same_moment_redelivered_is_not_a_new_observation():
    """Since 2026-08-24 a price travels in a frame published four times a second,
    so a symbol quiet between frames arrives again carrying the same print time.
    Appended, those repeats fill the window with a flat run the market never
    traded, and every statistic over it describes the sampler's cadence rather
    than the market."""
    window = RollingWindow(length=8, maximum_gap_seconds=120.0)
    window.observe(100.0, at_ns=1 * SECOND_NS)
    for _ in range(6):
        window.observe(100.0, at_ns=1 * SECOND_NS)
    window.observe(101.0, at_ns=2 * SECOND_NS)

    assert window.count == 2
    assert list(window.values) == [100.0, 101.0]
    assert window.redelivered_skipped == 6


def test_an_ordinarily_slow_series_earns_patience_past_the_floor():
    """The 120s floor was measured against symbols whose worst ordinary p99 gap
    was 42.3s. A symbol whose ordinary rhythm is three minutes of quiet would
    clear its window on every pause under that floor and stay blind forever;
    once its own gaps have been observed, the bound becomes a multiple of its
    own p99 rather than the liquid symbols' floor."""
    window = RollingWindow(length=8, maximum_gap_seconds=120.0, gap_patience_multiple=2.8)
    at = 0
    # An ordinary rhythm of 180s gaps, long enough to measure (length//2 gaps).
    for index in range(6):
        at += 180 * SECOND_NS
        window.observe(100.0 + index, at_ns=at)
    # The early pauses cleared the window -- the patience was not yet earned.
    assert window.series_breaks > 0
    breaks_before = window.series_breaks

    # The same 180s pause again: inside 2.8x its own p99, so the series holds.
    at += 180 * SECOND_NS
    window.observe(110.0, at_ns=at)
    assert window.series_breaks == breaks_before

    # A real outage is far outside any ordinary rhythm, and still clears.
    at += 1_031 * SECOND_NS
    window.observe(111.0, at_ns=at)
    assert window.series_breaks == breaks_before + 1


def test_patience_never_tightens_the_floor_for_a_liquid_series():
    """For a series whose own p99 gap sits inside the floor, the floor stands:
    the patience may only extend the bound, never shrink it."""
    window = RollingWindow(length=8, maximum_gap_seconds=120.0, gap_patience_multiple=2.8)
    at = 0
    for index in range(6):
        at += 2 * SECOND_NS  # a liquid rhythm: p99 gap of 2s
        window.observe(100.0 + index, at_ns=at)
    assert window.series_breaks == 0

    # 100s is far past 2.8 x 2s, but inside the floor: still one series.
    at += 100 * SECOND_NS
    window.observe(110.0, at_ns=at)
    assert window.series_breaks == 0


def test_patience_without_a_gap_bound_is_refused():
    with pytest.raises(ValueError):
        RollingWindow(length=4, gap_patience_multiple=2.8)
    with pytest.raises(ValueError):
        RollingWindow(length=4, maximum_gap_seconds=120.0, gap_patience_multiple=0.0)
