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

from runtime.price_frames import levels_in
from runtime.market_signal import (
    CONTINUATION,
    LONG,
    REVERSION,
    SHORT,
    SignalCalibrator,
    make_candidate,
    settle_claims_from,
)
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow

PART_ID = "momentum-burst-detector"

PART_DECLARATION = PartDeclaration(
    part_id="momentum-burst-detector",
    consumes=("symbol-price-frame", "symbol-profile", "playbook-rule", "training-label"),
    produces=("entry-candidate", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FIRED = "fired"
NOT_A_BURST = "move-is-ordinary-for-this-symbol"
TOO_FEW_OBSERVATIONS = "too-few-observations"
NO_PLAYBOOK = "no-playbook-rule-for-this-regime"


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


def expectation_word_in(phrase: str) -> str | None:
    """The recognised continuation/reversion word inside a playbook rule's phrase.

    `runtime.knowledge_types.PlaybookRule.then` is free text compiled by
    procedural-playbook ("long expecting continuation"), not the bare
    CONTINUATION/REVERSION vocabulary `set_playbook_expectation` validates
    against -- passing it through unextracted raised on every real rule and
    crash-looped this part continuously (found live 2026-08-30). None when
    neither word appears, which this part reads the same as no rule at all.
    """
    lowered = phrase.lower()
    if CONTINUATION in lowered:
        return CONTINUATION
    if REVERSION in lowered:
        return REVERSION
    return None


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
        gap_patience_multiple: float | None = None,
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
        self._gap_patience_multiple = gap_patience_multiple
        self._returns: dict[tuple[str, str], RollingWindow] = {}
        self._last_price_at_ns: dict[tuple[str, str], int] = {}
        self._last_price: dict[tuple[str, str], float] = {}
        self._playbook: dict[str, str] = {}
        self.standing = BurstStanding()

    def set_playbook_expectation(self, regime_tag: str, expectation: str) -> None:
        """What bursts in this regime have historically done: pull back, or run.

        Keyed by regime, not by symbol -- `PlaybookRule` carries no symbol field
        at all (`when`/`then` are free-text condition/action phrases), only
        `regime_tag`. A rule about "trending" bursts applies to every symbol
        this detector currently judges as trending, which is what "learned per
        regime rather than assumed" in this module's own docstring already said.
        """
        if expectation not in (REVERSION, CONTINUATION):
            raise ValueError(f"{expectation!r} is not something a burst can be expected to do")
        self._playbook[regime_tag] = expectation

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
                length=self._window_length,
                maximum_gap_seconds=self._maximum_gap_seconds,
                gap_patience_multiple=self._gap_patience_multiple,
            )
            self._returns[key] = window
        window.observe((price - previous) / previous, at_ns)
        self.standing.symbols_tracked = len(self._returns)

    def observe_outcome(self, calibration_key: str, was_right: bool) -> None:
        self._calibrator.observe_outcome(PART_ID, calibration_key, was_right)
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

        expectation = self._playbook.get(regime.regime)
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
                calibration_key=regime.regime,
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
        read_standing=lambda: describe_bursts(detector),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch, LatestByKey

    trades = Batch(read=context.bus.reader("symbol-price-frame"))
    # This detector is not given market-regime; the regime it judges in is
    # what the symbol's profile records, and "unclassified" until one does.
    profiles = LatestByKey(read=context.bus.reader("symbol-profile"), key_of=lambda p: (p.venue_id, p.symbol))
    rules = Batch(read=context.bus.reader("playbook-rule"))
    # The record of whether this detector was right, back from
    # `signal-outcome-labeller`. Until 2026-08-28 nothing carried it here and
    # `observe_outcome` had never been called by anything that runs.
    labels = Batch(read=context.bus.reader("training-label"))
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
            gap_patience_multiple=context.number("price_gap_patience_multiple"),
    )

    class _Regime:
        __slots__ = ("venue_id", "symbol", "regime", "is_classified")

        def __init__(self, venue_id, symbol, regime):
            self.venue_id, self.symbol = venue_id, symbol
            self.regime = regime or "unclassified"
            self.is_classified = bool(regime)

    def read_prices_and_regimes(_detector):
        # Whichever of this detector's own claims the market has settled since
        # the last tick. Drained first, so a candidate raised below is priced by
        # the record including everything already known -- a claim settled this
        # tick and used next tick would make the confidence one tick stale for
        # no reason.
        settle_claims_from(labels.payloads(), detector, PART_ID)
        for rule in rules.payloads():
            # A playbook rule about a regime's bursts says what to expect of them.
            # `rule.when` is a condition phrase, not a symbol -- PlaybookRule
            # carries no symbol field at all, only regime_tag.
            expectation = expectation_word_in(str(getattr(rule, "then", "") or ""))
            regime_tag = getattr(rule, "regime_tag", None)
            if expectation and regime_tag:
                detector.set_playbook_expectation(str(regime_tag), expectation)
        touched = set()
        for trade in levels_in(trades.payloads()):
            detector.observe_price(
                trade.venue_id, trade.symbol, trade.price, trade.observed_at_ns
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
