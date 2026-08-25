"""Each new volatility feature separates something the others cannot.

A feature earns its place by distinguishing two states that are different trades.
These build the pairs deliberately: same symbol, same close-to-close volatility,
different in exactly the one way the feature is supposed to notice. If a feature
cannot tell them apart it is not carrying information, however sound its formula.

From the volatility reel read 2026-08-20 (`docs/research/instagram-2026-08-20.md`
section 1), whose ten-feature specification this system had implemented as six.
Three of the four missing ones need nothing this project lacks; the other three it
named -- ATM implied vol, term slope and put skew -- need an options venue futures
does not have, which was recorded honestly at the time and still stands.
"""

from __future__ import annotations

import math

import pytest

from parts.prediction.volatility_feature_builder import VolatilityFeatureBuilder
from runtime.forecast_types import Candle, KlineWindow

MINUTE_NS = 60 * 1_000_000_000


def builder(short=5, medium=10, long=40, minimum=5) -> VolatilityFeatureBuilder:
    return VolatilityFeatureBuilder(
        short_window=short, medium_window=medium, long_window=long,
        minimum_observations=minimum,
    )


def window_of(closes, venue="binance-usdm", symbol="BTCUSDT") -> KlineWindow:
    candles = []
    for index, close in enumerate(closes):
        previous = closes[index - 1] if index else close
        candles.append(Candle(
            open_time_ns=index * MINUTE_NS, open=previous,
            high=max(previous, close), low=min(previous, close),
            close=close, volume=1.0, quote_volume=close, trades=1, is_closed=True,
        ))
    return KlineWindow(
        venue_id=venue, symbol=symbol, interval="1m", candles=tuple(candles),
        length_requested=len(candles), gaps=(), built_at_ns=1,
    )


def features_of(closes, **kwargs) -> dict:
    return builder(**kwargs).build(window_of(closes), horizon_seconds=60.0).features


def walk(start: float, steps: list[float]) -> list[float]:
    """Closes produced by applying a list of returns in order."""
    closes = [start]
    for step in steps:
        closes.append(closes[-1] * (1.0 + step))
    return closes


# ---- downside volatility ----------------------------------------------------

def test_downside_volatility_separates_falling_from_rising():
    """Close-to-close is symmetric; a symbol that only falls is a different trade."""
    size = 0.01
    falling = features_of(walk(100.0, [-size, size / 2] * 20))
    rising = features_of(walk(100.0, [size, -size / 2] * 20))

    # The two have the same magnitude of movement...
    assert falling["close_to_close_long"] == pytest.approx(
        rising["close_to_close_long"], rel=0.15
    )
    # ...and the falling one carries far more of it below zero.
    assert falling["downside_volatility"] > rising["downside_volatility"] * 1.3


def test_downside_volatility_is_zero_when_nothing_falls():
    features = features_of(walk(100.0, [0.005] * 40))
    assert features["downside_volatility"] == pytest.approx(0.0)


def test_one_violent_fall_does_not_read_as_permanent_downside_volatility():
    """Divided by every return, not only the negative ones.

    Dividing by the falls alone would report a symbol that fell once as violently
    downside-volatile for the whole window.
    """
    one_fall = features_of(walk(100.0, [0.001] * 39 + [-0.05]))
    many_falls = features_of(walk(100.0, [-0.05, 0.001] * 20))
    assert one_fall["downside_volatility"] < many_falls["downside_volatility"]


# ---- jump volatility --------------------------------------------------------

def test_jump_volatility_separates_a_jump_from_the_same_move_diffused():
    """The feature Parkinson and Garman-Klass cannot provide.

    One symbol arrives at its volatility through a single violent move; the other
    diffuses there in small steps. Bipower multiplies adjacent returns, so an
    isolated jump has a small neighbour and contributes little to it.
    """
    jumped = features_of(walk(100.0, [0.0005] * 39 + [0.08]))
    diffused = features_of(walk(100.0, [0.012, -0.012] * 20))

    assert jumped["jump_volatility"] > diffused["jump_volatility"]


def test_jump_volatility_is_never_negative():
    """The estimator is noisy, and a negative jump variance is an artefact."""
    features = features_of(walk(100.0, [0.004, -0.004] * 20))
    assert features["jump_volatility"] >= 0.0


# ---- volatility of volatility ----------------------------------------------

def test_volatility_of_volatility_separates_steady_from_travelling():
    """Same mean volatility, different journey -- and not the same risk.

    A symbol sitting at one level and a symbol travelling from calm to violent
    have the same average and nothing else in this feature set tells them apart.
    """
    steady = features_of(walk(100.0, [0.01, -0.01] * 20))
    travelling = features_of(walk(100.0, [0.001, -0.001] * 10 + [0.02, -0.02] * 10))

    assert travelling["volatility_of_volatility"] > steady["volatility_of_volatility"]


def test_a_perfectly_steady_series_has_almost_no_volatility_of_volatility():
    steady = features_of(walk(100.0, [0.01, -0.01] * 20))
    assert steady["volatility_of_volatility"] < steady["close_to_close_long"]


# ---- the middle leg ---------------------------------------------------------

def test_the_medium_window_sits_between_the_short_and_the_long():
    """HAR's third leg. Two windows cannot tell a spike from a steady change."""
    closes = walk(100.0, [0.001] * 30 + [0.02, -0.02] * 10)
    features = features_of(closes, short=5, medium=10, long=40)

    for name in ("close_to_close_short", "close_to_close_medium", "close_to_close_long"):
        assert name in features, f"{name} was not measured"
    # The recent burst shows most in the short window and least in the long one,
    # which is the ordering that makes three horizons worth having.
    assert features["close_to_close_short"] > features["close_to_close_long"]
    assert features["close_to_close_medium"] > features["close_to_close_long"]


def test_a_window_too_short_names_the_feature_missing_rather_than_zeroing_it():
    """The rule every feature builder here follows, and these must not break it."""
    built = builder(minimum=30).build(window_of(walk(100.0, [0.01] * 5)), horizon_seconds=60.0)
    for name in ("downside_volatility", "jump_volatility", "volatility_of_volatility"):
        assert name in built.missing
        assert name not in built.features


def test_no_feature_is_annualised():
    """252 is a count of trading days and these are one-minute candles.

    Applying it here would be a constant borrowed from a different market -- the
    reel's formulas annualise because they are daily, and the ratios transfer
    while the day count does not.
    """
    size = 0.01
    features = features_of(walk(100.0, [size, -size] * 20))
    # A per-candle deviation on ~1% moves stays near 1%, not 1% x sqrt(252).
    assert features["close_to_close_long"] < 10 * size
    assert features["downside_volatility"] < 10 * size
    assert features["jump_volatility"] < 10 * size
