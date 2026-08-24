"""momentum-burst-detector: a sudden jump the playbook says tends to pull back.

The mirror of the reversion detector and its opposite in temperament: this one
watches for a move so fast that it is unlikely to be information, and expects the
pullback rather than the continuation.

What distinguishes a burst from a trend is **speed relative to this symbol's own
normal speed**. A 2% move in a minute is nothing in a thin altcoin and enormous
in BTCUSDT, so the threshold is in standard deviations of that symbol's own
returns, never in percent.

The playbook decides what follows. A burst is the observation; whether this
symbol's bursts tend to pull back or run is a fact about the symbol, and it is
learned per regime rather than assumed -- a detector that always expected the
pullback would be short every breakout.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.market_signal import (
    CONTINUATION, LONG, REVERSION, SHORT, SignalCalibrator, make_candidate,
)
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow

PART_ID = "momentum-burst-detector"

PART_DECLARATION = PartDeclaration(
    part_id="momentum-burst-detector",
    consumes=("market-data", "symbol-profile", "playbook-rule"),
    produces=("entry-candidate", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FIRED = "fired"
NOT_A_BURST = "move-is-ordinary-for-this-symbol"
TOO_FEW_OBSERVATIONS = "too-few-observations"
NO_PLAYBOOK = "no-playbook-rule-for-this-symbol"


@dataclass
class BurstStanding:
    observations: int = 0
    candidates: int = 0
    not_a_burst: int = 0
    too_few: int = 0
    no_playbook: int = 0
    symbols_tracked: int = 0
    outcomes_learned: int = 0
    largest_burst_z: float = 0.0
    expecting_pullback: int = 0
    expecting_continuation: int = 0
    # How many times a hole in the feed stopped a return being computed. A
    # detector that quietly stops firing looks exactly like a quiet market.
    series_breaks: int = 0


class MomentumBurstDetector:
    """Fires on a return far outside this symbol's own distribution of returns."""

    def __init__(
        self,
        window_length: int,
        minimum_observations: int,
        burst_z_threshold: float,
        horizon_seconds: float,
        calibrator: SignalCalibrator,
        maximum_gap_seconds: float | None = None,
        now_ns=time.time_ns,
    ) -> None:
        if burst_z_threshold <= 0:
            raise ValueError("a threshold of zero calls every tick a burst")
        self._window_length = window_length
        self._minimum = minimum_observations
        self._threshold = burst_z_threshold
        self._horizon = horizon_seconds
        self._calibrator = calibrator
        self._now_ns = now_ns
        # How long a symbol may be silent before the series is judged to have a
        # hole in it rather than a gap between prints. None means the caller stated
        # no bound, and this part does not invent one (RL-061).
        self._maximum_gap_seconds = maximum_gap_seconds
        self._returns: dict[tuple[str, str], RollingWindow] = {}
        self._last_price_at_ns: dict[tuple[str, str], int] = {}
        self._last_price: dict[tuple[str, str], float] = {}
        self._playbook: dict[str, str] = {}
        self.standing = BurstStanding()

    def set_playbook_expectation(self, symbol: str, expectation: str) -> None:
        """What this symbol's bursts have historically done: pull back, or run.

        A fact about the symbol rather than about bursts in general, which is why
        it comes from the playbook rather than being assumed here.
        """
        if expectation not in (REVERSION, CONTINUATION):
            raise ValueError(f"{expectation!r} is not something a burst can be expected to do")
        self._playbook[symbol] = expectation

    def observe_price(self, venue_id: str, symbol: str, price: float, at_ns: int) -> None:
        """One print, turned into a return against the previous one.

        **A return is only computed across an unbroken pair of prints.** The bus
        drops rather than blocks, so a symbol's prints stop and resume; the first
        print after the hole differs from the last one before it by everything the
        market did while nobody was listening, and computed as a return that is a
        move of any size in an instant -- which is precisely the shape this
        detector fires on. Past the bound the previous price is dropped instead,
        and this print becomes the new starting point.
        """
        self.standing.observations += 1
        key = (venue_id, symbol)
        previous = self._last_price.get(key)
        previous_at_ns = self._last_price_at_ns.get(key)
        self._last_price[key] = price
        self._last_price_at_ns[key] = at_ns
        if previous is None or previous == 0:
            return
        if (
            self._maximum_gap_seconds is not None
            and previous_at_ns is not None
            and (at_ns - previous_at_ns) / 1e9 > self._maximum_gap_seconds
        ):
            self.standing.series_breaks += 1
            return
        window = self._returns.get(key)
        if window is None:
            window = RollingWindow(
                length=self._window_length, maximum_gap_seconds=self._maximum_gap_seconds
            )
            self._returns[key] = window
        window.observe((price - previous) / previous, at_ns)
        self.standing.symbols_tracked = len(self._returns)

    def observe_outcome(self, regime: str, was_right: bool) -> None:
        self._calibrator.observe_outcome(PART_ID, regime, was_right)
        self.standing.outcomes_learned += 1

    def detect(self, venue_id: str, symbol: str, regime) -> tuple[object | None, str]:
        window = self._returns.get((venue_id, symbol))
        if window is None or window.count < self._minimum:
            self.standing.too_few += 1
            return None, TOO_FEW_OBSERVATIONS

        latest = window.latest
        z = window.z_score(latest, self._minimum)
        if z is None or abs(z) < self._threshold:
            self.standing.not_a_burst += 1
            return None, NOT_A_BURST

        expectation = self._playbook.get(symbol)
        if expectation is None:
            # Without the playbook this detector knows a burst happened and
            # nothing about what follows, which is not a tradeable claim.
            self.standing.no_playbook += 1
            return None, NO_PLAYBOOK

        self.standing.largest_burst_z = max(self.standing.largest_burst_z, abs(z))
        self.standing.candidates += 1
        moved_up = latest > 0
        if expectation == REVERSION:
            direction = SHORT if moved_up else LONG
            self.standing.expecting_pullback += 1
        else:
            direction = LONG if moved_up else SHORT
            self.standing.expecting_continuation += 1

        confidence = self._calibrator.confidence(PART_ID, regime.regime)
        return (
            make_candidate(
                detector=PART_ID,
                venue_id=venue_id,
                symbol=symbol,
                direction=direction,
                expectation=expectation,
                signal_strength=abs(z),
                confidence=confidence,
                horizon_seconds=self._horizon,
                evidence={
                    "return": latest,
                    "z_score": z,
                    "observations": window.count,
                    "regime": regime.regime,
                    "playbook_expectation": expectation,
                },
                reason=(
                    f"a {latest:+.2%} move is {abs(z):.2f} standard deviations of this symbol's "
                    f"own returns; the playbook expects {expectation} and that has held "
                    f"{confidence.value:.0%} of the time "
                    f"({'measured' if confidence.is_fitted else 'the prior'})"
                ),
                now_ns=self._now_ns,
            ),
            FIRED,
        )


def describe_bursts(detector: MomentumBurstDetector) -> dict:
    return {
        "part_id": PART_ID,
        "observations": detector.standing.observations,
        "symbols_tracked": detector.standing.symbols_tracked,
        "candidates": detector.standing.candidates,
        "not_a_burst": detector.standing.not_a_burst,
        "too_few_observations": detector.standing.too_few,
        "no_playbook_rule": detector.standing.no_playbook,
        "expecting_pullback": detector.standing.expecting_pullback,
        "expecting_continuation": detector.standing.expecting_continuation,
        "outcomes_learned": detector.standing.outcomes_learned,
        "largest_burst_z": detector.standing.largest_burst_z,
    }


def run_momentum_burst_detector(
    detector: MomentumBurstDetector, control_socket, read_prices_and_regimes, publish_candidates,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        regimes = read_prices_and_regimes(detector)
        candidates = []
        for regime in regimes:
            candidate, _ = detector.detect(regime.venue_id, regime.symbol, regime)
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
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch, LatestByKey

    trades = Batch(read=context.bus.reader("market-data"))
    # This detector is not given market-regime; the regime it judges in is
    # what the symbol's profile records, and "unclassified" until one does.
    profiles = LatestByKey(read=context.bus.reader("symbol-profile"), key_of=lambda p: (p.venue_id, p.symbol))
    rules = Batch(read=context.bus.reader("playbook-rule"))
    publish_candidates = context.bus.publisher_for("entry-candidate")
    detector = MomentumBurstDetector(
        window_length=int(context.number("detector_window_length")),
        minimum_observations=int(context.number("detector_minimum_observations")),
        burst_z_threshold=context.number("detector_z_threshold"),
        horizon_seconds=context.number("momentum_burst_horizon"),
        calibrator=SignalCalibrator(
            prior_hit_rate=context.number("signal_prior_hit_rate"),
            prior_weight=context.number("signal_prior_weight"),
            half_life_observations=context.number("signal_half_life_observations"),
            minimum_observations=int(context.number("signal_minimum_observations")),
        ),
            maximum_gap_seconds=context.number("price_series_maximum_gap_seconds"),
    )

    class _Regime:
        __slots__ = ("venue_id", "symbol", "regime", "is_classified")

        def __init__(self, venue_id, symbol, regime):
            self.venue_id, self.symbol = venue_id, symbol
            self.regime = regime or "unclassified"
            self.is_classified = bool(regime)

    def read_prices_and_regimes(_detector):
        for rule in rules.payloads():
            # A playbook rule about a symbol's bursts says what to expect of them.
            expectation = getattr(rule, "then", None)
            symbol = getattr(rule, "when", "")
            if expectation and symbol:
                detector.set_playbook_expectation(str(symbol), str(expectation))
        touched = set()
        for trade in trades.payloads():
            detector.observe_price(
                trade.venue_id, trade.symbol, trade.price, trade.venue_time_ns
            )
            touched.add((trade.venue_id, trade.symbol))
        by_symbol = profiles.mapping()
        return tuple(
            _Regime(key[0], key[1], (by_symbol[key].fields or {}).get("regime") if key in by_symbol else None)
            for key in sorted(touched)
        )

    def publish(candidates) -> None:
        if candidates:
            publish_candidates(candidates)

    return run_momentum_burst_detector(
        detector=detector,
        control_socket=context.control_socket,
        read_prices_and_regimes=read_prices_and_regimes,
        publish_candidates=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
