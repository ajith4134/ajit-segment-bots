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
