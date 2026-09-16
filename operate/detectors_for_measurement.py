"""The detector chain the spine runs, built for an offline measurement.

The detector-edge measurement (docs/superpowers/plans/2026-09-16-detector-edge-across-sessions.md)
drives the detectors' own classes over past sessions. Each object here is built by the
same `build_..._from_settings` function its part's `start_part` calls, from the same
operator settings, so a measured detector is the one that trades -- not a copy whose
arguments were typed again and drifted.

**The clock is the session's, not this machine's.** Every object that stamps a time
is handed `clock`, so a candidate raised on a 2025 bar is stamped 2025. The replay
learned this the hard way: a forty-minute round trip recorded as a few milliseconds
made `luck-skill-separator` report outcomes of -5,689 standard deviations.

This is a measurement (RL-071): nothing here publishes on the bus or writes under a
live state root.
"""

from __future__ import annotations

from dataclasses import dataclass

from runtime.settings_reader import load_settings_document, settings_directory

RUNTIME_SCOPE_FILE = "runtime.toml"


class MeasurementSettings:
    """The `.number()` / `.setting()` shape a part context offers, over the operator's
    own runtime settings. Refuses a missing name exactly as a part context does:
    there is no default (RL-061)."""

    def __init__(self) -> None:
        self._document = load_settings_document(
            settings_directory() / RUNTIME_SCOPE_FILE, "runtime",
        )

    def setting(self, name: str):
        return self._document.read_entry(name)

    def number(self, name: str) -> float:
        value = self.setting(name).value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"setting '{name}' is {value!r}, which is not a number")
        return value


@dataclass
class DetectorChain:
    """Every object the measurement drives, in the order data flows through them."""

    settings: MeasurementSettings
    kline_windows: object
    volatility_features: object
    volatility_regressor: object
    volatility_training: object
    regimes: object
    detectors: dict[str, object]


def build_detector_chain(clock) -> DetectorChain:
    from parts.opportunity_scanner.expiry_day_zero_to_hero_detector import (
        PART_ID as ZERO_TO_HERO, build_zero_to_hero_detector_from_settings,
    )
    from parts.opportunity_scanner.mean_reversion_detector import (
        PART_ID as MEAN_REVERSION, build_mean_reversion_detector_from_settings,
    )
    from parts.opportunity_scanner.momentum_burst_detector import (
        PART_ID as MOMENTUM_BURST, build_momentum_burst_detector_from_settings,
    )
    from parts.opportunity_scanner.regime_classifier import build_regime_classifier_from_settings
    from parts.opportunity_scanner.volatility_gap_detector import (
        PART_ID as VOLATILITY_GAP, build_volatility_gap_detector_from_settings,
    )
    from parts.prediction.kline_window_builder import build_kline_window_builder_from_settings
    from parts.prediction.realised_vol_regressor import (
        RealisedVolTrainingPairer, build_realised_vol_regressor_from_settings,
    )
    from parts.prediction.volatility_feature_builder import (
        build_volatility_feature_builder_from_settings,
    )

    settings = MeasurementSettings()
    regressor = build_realised_vol_regressor_from_settings(settings, now_ns=clock)
    return DetectorChain(
        settings=settings,
        kline_windows=build_kline_window_builder_from_settings(settings, now_ns=clock),
        volatility_features=build_volatility_feature_builder_from_settings(settings, now_ns=clock),
        volatility_regressor=regressor,
        volatility_training=RealisedVolTrainingPairer(regressor, settings.number("forecast_horizon")),
        regimes=build_regime_classifier_from_settings(settings, now_ns=clock),
        detectors={
            MEAN_REVERSION: build_mean_reversion_detector_from_settings(settings, now_ns=clock),
            MOMENTUM_BURST: build_momentum_burst_detector_from_settings(settings, now_ns=clock),
            VOLATILITY_GAP: build_volatility_gap_detector_from_settings(settings, now_ns=clock),
            ZERO_TO_HERO: build_zero_to_hero_detector_from_settings(settings, now_ns=clock),
        },
    )


__all__ = ["DetectorChain", "MeasurementSettings", "build_detector_chain"]
