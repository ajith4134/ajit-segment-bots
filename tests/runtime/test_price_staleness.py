"""The bound on a price's age, learned per symbol from that symbol's own moves.

Checked against the tape rather than against invented prices (RL-063): the whole
claim is that BTCUSDT and ETHUSDT need different bounds, and only real prices can
show that. The reference numbers are the ones measured directly in
`measurements/2026-08-24-reference-price-staleness/` on 2026-08-23.
"""

from __future__ import annotations

import pytest

from runtime.price_staleness import PriceStalenessEstimator

ROUND_TRIP_COST = 2 * 0.00055
MEASURED_MEDIAN_ONE_SECOND_MOVE = 0.000898


def an_estimator(**overrides) -> PriceStalenessEstimator:
    settings = dict(
        materiality_fraction=ROUND_TRIP_COST,
        anchor_seconds=1.0,
        quantile=0.95,
        window=3_600,
        observations_needed=300,
        prior_one_second_move=MEASURED_MEDIAN_ONE_SECOND_MOVE,
        minimum_age_seconds=1.0,
        maximum_age_seconds=60.0,
    )
    settings.update(overrides)
    return PriceStalenessEstimator(**settings)


def test_a_symbol_never_seen_falls_back_to_the_measured_prior():
    """Unmeasured is not unbounded, and the prior is itself a measured number."""
    estimate = an_estimator().believable_age_seconds("binance-usdm", "BTCUSDT")

    assert estimate.is_fitted is False
    assert estimate.observations == 0
    assert estimate.value == pytest.approx(
        (ROUND_TRIP_COST / MEASURED_MEDIAN_ONE_SECOND_MOVE) ** 2, rel=1e-9
    )


def test_consecutive_prints_are_not_moves():
    """Two trades a millisecond apart differ by the tick, not by volatility."""
    subject = an_estimator()
    for index in range(500):
        subject.observe_price("binance-usdm", "BTCUSDT", 64_000.0 + index, index * 1_000_000)

    assert subject.one_second_move("binance-usdm", "BTCUSDT").is_fitted is False, (
        "half a second of prints must not fit a one-second move estimate"
    )


def test_a_quiet_symbol_and_a_busy_one_are_measured_in_the_same_unit():
    """The scaling is what makes a symbol printing once a minute comparable.

    Two symbols moving at the same rate must estimate the same bound whether the
    prints arrive every second or every thirty.
    """
    second_ns = 1_000_000_000
    busy, quiet = an_estimator(), an_estimator()
    price = 100.0
    for index in range(1, 601):
        # The same random-walk rate, sampled at two spacings: a move of s per
        # second compounds to s * sqrt(30) over thirty seconds.
        busy.observe_price("v", "BUSY", price * (1 + 0.001 * (1 if index % 2 else -1)),
                           index * second_ns)
        quiet.observe_price("v", "QUIET", price * (1 + 0.001 * 30 ** 0.5 * (1 if index % 2 else -1)),
                            index * 30 * second_ns)

    assert busy.one_second_move("v", "BUSY").value == pytest.approx(
        quiet.one_second_move("v", "QUIET").value, rel=0.15
    )


def test_the_bound_learned_from_the_tape_matches_what_the_tape_measures(read_captured_trades):
    """BTCUSDT on 2026-08-22: the estimator lands near the measured tolerance.

    The captured run is about half a minute long, which yields 28 one-second
    moves, so the fit threshold here is that and not the live one. The number the
    test is about is the move itself, not how many it took to believe it.
    """
    subject = an_estimator(observations_needed=20)
    for trade in read_captured_trades():
        subject.observe_price(trade.venue_id, trade.symbol, trade.price, trade.venue_time_ns)

    move = subject.one_second_move("binance-usdm", "BTCUSDT")
    assert move.is_fitted, "the captured run must be long enough to fit a one-second move"
    # Measured directly off the full day's tape: BTCUSDT's p95 one-second move was
    # 0.037%, which the estimator should be in the neighbourhood of rather than an
    # order of magnitude away from.
    assert 0.0001 < move.value < 0.002, move.reason

    bound = subject.believable_age_seconds("binance-usdm", "BTCUSDT")
    assert 1.0 <= bound.value <= 60.0
    assert bound.is_fitted


def test_a_symbol_that_moves_more_gets_a_shorter_bound():
    """The whole reason the bound is per symbol and not a setting."""
    second_ns = 1_000_000_000
    subject = an_estimator(observations_needed=30)
    for index in range(1, 401):
        direction = 1 if index % 2 else -1
        subject.observe_price("v", "CALM", 100.0 * (1 + 0.0002 * direction), index * second_ns)
        subject.observe_price("v", "WILD", 100.0 * (1 + 0.01 * direction), index * second_ns)

    calm = subject.believable_age_seconds("v", "CALM")
    wild = subject.believable_age_seconds("v", "WILD")
    assert calm.value > wild.value
    assert wild.value == pytest.approx(1.0), "a symbol moving 1% a second is believable for the floor only"


def test_the_bound_is_clamped_at_both_ends():
    second_ns = 1_000_000_000
    subject = an_estimator(observations_needed=30, minimum_age_seconds=2.0, maximum_age_seconds=10.0)
    for index in range(1, 401):
        direction = 1 if index % 2 else -1
        subject.observe_price("v", "GLACIAL", 100.0 * (1 + 0.0000001 * direction), index * second_ns)
        subject.observe_price("v", "WILD", 100.0 * (1 + 0.05 * direction), index * second_ns)

    glacial = subject.believable_age_seconds("v", "GLACIAL")
    wild = subject.believable_age_seconds("v", "WILD")
    assert glacial.value == 10.0 and glacial.was_clamped
    assert wild.value == 2.0 and wild.was_clamped


def test_settings_that_would_make_the_bound_meaningless_are_refused():
    with pytest.raises(ValueError):
        an_estimator(materiality_fraction=0.0)
    with pytest.raises(ValueError):
        an_estimator(prior_one_second_move=0.0)
    with pytest.raises(ValueError):
        an_estimator(anchor_seconds=0.0)
    with pytest.raises(ValueError):
        an_estimator(minimum_age_seconds=60.0, maximum_age_seconds=1.0)


def test_a_price_that_is_not_a_price_is_ignored():
    subject = an_estimator()
    subject.observe_price("v", "S", 0.0, 0)
    subject.observe_price("v", "S", -1.0, 1_000_000_000)

    assert subject.symbols_measured == 0
