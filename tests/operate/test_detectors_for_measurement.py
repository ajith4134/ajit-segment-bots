"""The measured detector chain is the one the spine builds, on the session's clock."""

from __future__ import annotations

import inspect

from operate.detectors_for_measurement import build_detector_chain

A_SESSION_MOMENT_NS = 1_788_000_000_000_000_000


def test_every_detector_the_plan_measures_is_built():
    chain = build_detector_chain(clock=lambda: A_SESSION_MOMENT_NS)
    assert set(chain.detectors) == {
        "mean-reversion-detector",
        "momentum-burst-detector",
        "volatility-gap-detector",
        "expiry-day-zero-to-hero-detector",
    }


def test_the_objects_tell_time_by_the_clock_they_were_given():
    chain = build_detector_chain(clock=lambda: A_SESSION_MOMENT_NS)
    for component in (*chain.detectors.values(), chain.regimes, chain.kline_windows,
                      chain.volatility_features, chain.volatility_regressor):
        assert component._now_ns() == A_SESSION_MOMENT_NS, type(component).__name__


def test_each_live_part_builds_through_the_same_function():
    """A start_part that constructed its object inline again would let the two drift."""
    from parts.opportunity_scanner import (
        expiry_day_zero_to_hero_detector, mean_reversion_detector, momentum_burst_detector,
        regime_classifier, volatility_gap_detector,
    )
    from parts.prediction import kline_window_builder, realised_vol_regressor, volatility_feature_builder

    for module in (expiry_day_zero_to_hero_detector, mean_reversion_detector,
                   momentum_burst_detector, regime_classifier, volatility_gap_detector,
                   kline_window_builder, realised_vol_regressor, volatility_feature_builder):
        source = inspect.getsource(module.start_part)
        assert "_from_settings(context)" in source, module.__name__
