"""sentiment-shift-detector: crowd sentiment turning faster than price has.

The signal is the **divergence**, not the level. Sentiment being bullish says
nothing -- it is bullish most of the time in a bull market, and a detector that
faded it would be short for a year. What carries information is sentiment moving
sharply while price has not yet, because one of the two is about to follow the
other.

Which one follows is the honest open question, and it is learned rather than
assumed (RL-060). Sentiment sometimes leads price and sometimes is a contrarian
signal at extremes, and the same detector is right in both cases only if it knows
which regime it is in. So the direction it proposes follows what has actually
happened after past divergences, not a theory about crowds.

**Sentiment with no price to compare against is not a signal.** A reading on its
own has no divergence in it, and the detector says so rather than trading a mood.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.price_frames import levels_in
from runtime.market_signal import CONTINUATION, LONG, REVERSION, SHORT, SignalCalibrator, make_candidate
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow

PART_ID = "sentiment-shift-detector"

PART_DECLARATION = PartDeclaration(
    part_id="sentiment-shift-detector",
    consumes=("sentiment-reading", "symbol-price-frame", "playbook-rule"),
    produces=("entry-candidate", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FIRED = "fired"
NO_DIVERGENCE = "sentiment-and-price-agree"
SENTIMENT_STEADY = "sentiment-has-not-shifted"
TOO_FEW_OBSERVATIONS = "too-few-observations"
NO_PRICE = "no-price-to-compare-against"

LEADS = "sentiment-leads-price"
CONTRARIAN = "sentiment-is-contrarian-at-extremes"


@dataclass
class SentimentStanding:
    readings: int = 0
    candidates: int = 0
    no_divergence: int = 0
    steady: int = 0
    too_few: int = 0
    no_price: int = 0
    outcomes_learned: int = 0
    largest_divergence: float = 0.0
    leading_calls: int = 0
    contrarian_calls: int = 0


class SentimentShiftDetector:
    """Fires when sentiment moves sharply and price has not followed yet."""

    def __init__(
        self,
        window_length: int,
        minimum_observations: int,
        shift_z_threshold: float,
        price_agreement_fraction: float,
        horizon_seconds: float,
        calibrator: SignalCalibrator,
        maximum_gap_seconds: float | None = None,
        now_ns=time.time_ns,
    ) -> None:
        if shift_z_threshold <= 0:
            raise ValueError("a threshold of zero calls every reading a shift")
        self._window_length = window_length
        self._minimum = minimum_observations
        self._threshold = shift_z_threshold
        self._agreement = price_agreement_fraction
        self._horizon = horizon_seconds
        self._calibrator = calibrator
        self._now_ns = now_ns
        # How long a symbol may be silent before its window is judged to have a
        # hole in it rather than a series. None means the caller stated no bound,
        # and this part does not invent one (RL-061).
        self._maximum_gap_seconds = maximum_gap_seconds
        self._sentiment: dict[tuple[str, str], RollingWindow] = {}
        self._prices: dict[tuple[str, str], RollingWindow] = {}
        self.standing = SentimentStanding()

    def observe_sentiment(self, venue_id: str, symbol: str, reading: float) -> None:
        self.standing.readings += 1
        key = (venue_id, symbol)
        window = self._sentiment.get(key)
        if window is None:
            window = RollingWindow(
                length=self._window_length,
                maximum_gap_seconds=self._maximum_gap_seconds,
            )
            self._sentiment[key] = window
        window.observe(reading)

    def observe_price(self, venue_id: str, symbol: str, price: float, at_ns: int) -> None:
        key = (venue_id, symbol)
        window = self._prices.get(key)
        if window is None:
            window = RollingWindow(
                length=self._window_length,
                maximum_gap_seconds=self._maximum_gap_seconds,
            )
            self._prices[key] = window
        window.observe(price, at_ns)

    def observe_outcome(self, relationship: str, was_right: bool) -> None:
        """Whether price followed sentiment, or reversed against it, after a call."""
        self._calibrator.observe_outcome(PART_ID, relationship, was_right)
        self.standing.outcomes_learned += 1

    def detect(self, venue_id: str, symbol: str) -> tuple[object | None, str]:
        key = (venue_id, symbol)
        sentiment = self._sentiment.get(key)
        prices = self._prices.get(key)

        if sentiment is None or sentiment.count < self._minimum:
            self.standing.too_few += 1
            return None, TOO_FEW_OBSERVATIONS
        if prices is None or prices.count < 2:
            self.standing.no_price += 1
            return None, NO_PRICE

        shift = sentiment.z_score(sentiment.latest, self._minimum)
        if shift is None or abs(shift) < self._threshold:
            self.standing.steady += 1
            return None, SENTIMENT_STEADY

        returns = prices.returns()
        recent_move = sum(returns[-min(len(returns), 5) :]) if returns else 0.0

        sentiment_up = shift > 0
        price_up = recent_move > 0
        if abs(recent_move) >= self._agreement and sentiment_up == price_up:
            # Price has already followed. There is no divergence left to trade.
            self.standing.no_divergence += 1
            return None, NO_DIVERGENCE

        # Which way to trade is learned: sentiment leads price sometimes and is
        # contrarian at extremes, and only the record says which is happening.
        leading = self._calibrator.confidence(PART_ID, LEADS)
        contrarian = self._calibrator.confidence(PART_ID, CONTRARIAN)
        if contrarian.value > leading.value:
            relationship, confidence = CONTRARIAN, contrarian
            direction = SHORT if sentiment_up else LONG
            expectation = REVERSION
            self.standing.contrarian_calls += 1
        else:
            relationship, confidence = LEADS, leading
            direction = LONG if sentiment_up else SHORT
            expectation = CONTINUATION
            self.standing.leading_calls += 1

        self.standing.largest_divergence = max(self.standing.largest_divergence, abs(shift))
        self.standing.candidates += 1

        return (
            make_candidate(
                detector=PART_ID,
                venue_id=venue_id,
                symbol=symbol,
                direction=direction,
                expectation=expectation,
                signal_strength=abs(shift),
                confidence=confidence,
                horizon_seconds=self._horizon,
                evidence={
                    "sentiment": sentiment.latest,
                    "sentiment_z": shift,
                    "recent_price_move": recent_move,
                    "relationship": relationship,
                    "observations": sentiment.count,
                },
                reason=(
                    f"sentiment moved {abs(shift):.2f} standard deviations "
                    f"{'up' if sentiment_up else 'down'} while price moved {recent_move:+.2%}. "
                    f"On this symbol sentiment has behaved as "
                    f"{'a contrarian signal' if relationship == CONTRARIAN else 'a leading one'} "
                    f"{confidence.value:.0%} of the time "
                    f"({'measured' if confidence.is_fitted else 'the prior'})"
                ),
                now_ns=self._now_ns,
            ),
            FIRED,
        )


def describe_sentiment(detector: SentimentShiftDetector) -> dict:
    return {
        "part_id": PART_ID,
        "readings": detector.standing.readings,
        "candidates": detector.standing.candidates,
        "sentiment_steady": detector.standing.steady,
        "no_divergence": detector.standing.no_divergence,
        "too_few_observations": detector.standing.too_few,
        "no_price": detector.standing.no_price,
        "leading_calls": detector.standing.leading_calls,
        "contrarian_calls": detector.standing.contrarian_calls,
        "outcomes_learned": detector.standing.outcomes_learned,
        "largest_divergence": detector.standing.largest_divergence,
    }


def run_sentiment_shift_detector(
    detector: SentimentShiftDetector, control_socket, read_sentiment, publish_candidates,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        symbols = read_sentiment(detector)
        candidates = []
        for venue_id, symbol in symbols:
            candidate, _ = detector.detect(venue_id, symbol)
            if candidate is not None:
                candidates.append(candidate)
        publish_candidates(tuple(candidates))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A sentiment reading names a symbol and no venue; it is observed for
    every venue on which that symbol has printed.
    """
    from runtime.input_assembly import Batch

    trades = Batch(read=context.bus.reader("symbol-price-frame"))
    readings = Batch(read=context.bus.reader("sentiment-reading"))
    rules = Batch(read=context.bus.reader("playbook-rule"))
    publish_candidates = context.bus.publisher_for("entry-candidate")
    detector = SentimentShiftDetector(
        window_length=int(context.number("detector_window_length")),
        minimum_observations=int(context.number("detector_minimum_observations")),
        shift_z_threshold=context.number("detector_z_threshold"),
        price_agreement_fraction=context.number("sentiment_price_agreement_fraction"),
        horizon_seconds=context.number("sentiment_shift_horizon"),
        calibrator=SignalCalibrator(
            prior_hit_rate=context.number("signal_prior_hit_rate"),
            prior_weight=context.number("signal_prior_weight"),
            half_life_observations=context.number("signal_half_life_observations"),
            minimum_observations=int(context.number("signal_minimum_observations")),
        ),
            maximum_gap_seconds=context.number("price_series_maximum_gap_seconds"),
    )
    venues_of: dict[str, set[str]] = {}

    def read_sentiment(_detector):
        rules.payloads()
        for trade in levels_in(trades.payloads()):
            detector.observe_price(
                trade.venue_id, trade.symbol, trade.price, trade.observed_at_ns
            )
            venues_of.setdefault(trade.symbol, set()).add(trade.venue_id)
        touched = set()
        for reading in readings.payloads():
            for venue_id in venues_of.get(reading.symbol, ()):
                detector.observe_sentiment(venue_id, reading.symbol, reading.level)
                touched.add((venue_id, reading.symbol))
        return tuple(sorted(touched))

    def publish(candidates) -> None:
        if candidates:
            publish_candidates(candidates)

    return run_sentiment_shift_detector(
        detector=detector,
        control_socket=context.control_socket,
        read_sentiment=read_sentiment,
        publish_candidates=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
