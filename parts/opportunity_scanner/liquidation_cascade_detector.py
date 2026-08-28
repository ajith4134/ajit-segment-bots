"""liquidation-cascade-detector: price approaching a cluster dense enough to cascade.

A liquidation cluster is not a level, it is a pool of forced orders that will
execute whether anyone wants them to or not. Price reaching one produces selling
that produces more liquidations, and the move is mechanical rather than
informational -- which is exactly why it is tradeable and exactly why being on the
wrong side of it is so expensive.

Two things must both be true, and either alone is a losing trade:

- **The cluster is dense enough to feed itself.** A thin cluster absorbs into the
  book and nothing happens.
- **Price is close enough to reach it**, measured against what this symbol
  actually moves in the horizon, not a fixed percentage.

`stop-target-placer` uses the same map to place stops *away* from these pools.
This part trades toward them. Both follow from the same fact: price is pulled
toward forced orders, and pretending otherwise puts stops exactly where they will
be taken.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.price_frames import levels_in
from runtime.market_signal import (
    CONTINUATION,
    LONG,
    SHORT,
    SignalCalibrator,
    make_candidate,
    settle_claims_from,
)
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow

PART_ID = "liquidation-cascade-detector"

PART_DECLARATION = PartDeclaration(
    part_id="liquidation-cascade-detector",
    consumes=("liquidation-map", "symbol-price-frame", "training-label"),
    produces=("entry-candidate", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FIRED = "fired"
NO_CLUSTER_IN_REACH = "no-cluster-within-reach"
CLUSTER_TOO_THIN = "cluster-too-thin-to-cascade"
NO_VOLATILITY = "no-volatility-measurement-for-reach"


@dataclass(frozen=True)
class LiquidationCluster:
    """A pool of forced exits at a price, and how much sits there."""

    price: float
    notional: float
    side: str


@dataclass
class CascadeStanding:
    tests: int = 0
    candidates: int = 0
    none_in_reach: int = 0
    too_thin: int = 0
    no_volatility: int = 0
    outcomes_learned: int = 0
    densest_cluster_seen: float = 0.0
    # How many times a hole in the feed stopped a return being computed. A
    # detector that quietly stops firing looks exactly like a quiet market.
    series_breaks: int = 0


class LiquidationCascadeDetector:
    """Fires when price is within reach of a cluster large enough to feed itself."""

    def __init__(
        self,
        window_length: int,
        minimum_observations: int,
        reach_in_volatilities: float,
        minimum_cluster_notional: float,
        cascade_depth_multiple: float,
        horizon_seconds: float,
        calibrator: SignalCalibrator,
        maximum_gap_seconds: float | None = None,
        gap_patience_multiple: float | None = None,
        now_ns=time.time_ns,
    ) -> None:
        if reach_in_volatilities <= 0 or cascade_depth_multiple <= 0:
            raise ValueError("reach and depth multiples must be positive to mean anything")
        self._window_length = window_length
        self._minimum = minimum_observations
        self._reach = reach_in_volatilities
        self._minimum_notional = minimum_cluster_notional
        self._depth_multiple = cascade_depth_multiple
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
        self._clusters: dict[tuple[str, str], tuple[LiquidationCluster, ...]] = {}
        self._book_depth: dict[tuple[str, str], float] = {}
        self.standing = CascadeStanding()

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

    def set_clusters(self, venue_id: str, symbol: str, clusters: tuple[LiquidationCluster, ...]) -> None:
        self._clusters[(venue_id, symbol)] = tuple(clusters)

    def set_book_depth(self, venue_id: str, symbol: str, notional: float) -> None:
        """How much the book can absorb, which decides whether a cluster cascades."""
        self._book_depth[(venue_id, symbol)] = notional

    def observe_outcome(self, calibration_key: str, cascaded: bool) -> None:
        self._calibrator.observe_outcome(PART_ID, calibration_key, cascaded)
        self.standing.outcomes_learned += 1

    def detect(self, venue_id: str, symbol: str, regime_name: str = "any") -> tuple[object | None, str]:
        self.standing.tests += 1
        key = (venue_id, symbol)
        price = self._last_price.get(key)
        clusters = self._clusters.get(key, ())
        returns = self._returns.get(key)

        if price is None or not clusters:
            self.standing.none_in_reach += 1
            return None, NO_CLUSTER_IN_REACH

        volatility = returns.standard_deviation(self._minimum) if returns else None
        if volatility is None or volatility <= 0:
            # Reach must be measured in what this symbol actually moves; a fixed
            # percentage is wrong for every symbol but one.
            self.standing.no_volatility += 1
            return None, NO_VOLATILITY

        reach = price * volatility * self._reach
        in_reach = [
            cluster for cluster in clusters if abs(cluster.price - price) <= reach
        ]
        if not in_reach:
            self.standing.none_in_reach += 1
            return None, NO_CLUSTER_IN_REACH

        nearest = min(in_reach, key=lambda cluster: abs(cluster.price - price))
        self.standing.densest_cluster_seen = max(
            self.standing.densest_cluster_seen, nearest.notional
        )

        depth = self._book_depth.get(key)
        cascades = nearest.notional >= self._minimum_notional and (
            depth is None or nearest.notional >= depth * self._depth_multiple
        )
        if not cascades:
            # A thin cluster absorbs into the book and nothing happens.
            self.standing.too_thin += 1
            return None, CLUSTER_TOO_THIN

        self.standing.candidates += 1
        # Price is pulled toward the pool, and the forced orders push it further
        # the same way: below price, the cascade is downward.
        direction = SHORT if nearest.price < price else LONG
        confidence = self._calibrator.confidence(PART_ID, regime_name)

        return (
            make_candidate(
                detector=PART_ID,
                venue_id=venue_id,
                symbol=symbol,
                direction=direction,
                expectation=CONTINUATION,
                signal_strength=nearest.notional / max(self._minimum_notional, 1.0),
                confidence=confidence,
                calibration_key=regime_name,
                horizon_seconds=self._horizon,
                evidence={
                    "price": price,
                    "cluster_price": nearest.price,
                    "cluster_notional": nearest.notional,
                    "cluster_side": nearest.side,
                    "distance_fraction": abs(nearest.price - price) / price,
                    "reach_fraction": reach / price,
                    "book_depth_notional": depth,
                    "clusters_in_reach": len(in_reach),
                },
                reason=(
                    f"{nearest.notional:,.0f} of forced {nearest.side} exits sit at "
                    f"{nearest.price:g}, {abs(nearest.price - price) / price:.2%} away and inside "
                    f"the {reach / price:.2%} this symbol moves in the horizon"
                    + (f", against book depth of {depth:,.0f}" if depth else "")
                    + f". Cascaded {confidence.value:.0%} of the time "
                    f"({'measured' if confidence.is_fitted else 'the prior'})"
                ),
                now_ns=self._now_ns,
            ),
            FIRED,
        )


def describe_cascades(detector: LiquidationCascadeDetector) -> dict:
    return {
        "part_id": PART_ID,
        "tests": detector.standing.tests,
        "candidates": detector.standing.candidates,
        "no_cluster_in_reach": detector.standing.none_in_reach,
        "cluster_too_thin": detector.standing.too_thin,
        "no_volatility": detector.standing.no_volatility,
        "outcomes_learned": detector.standing.outcomes_learned,
        "densest_cluster_seen": detector.standing.densest_cluster_seen,
    }


def run_liquidation_cascade_detector(
    detector: LiquidationCascadeDetector, control_socket, read_map, publish_candidates,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        symbols = read_map(detector)
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
        read_standing=lambda: describe_cascades(detector),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Clusters come from the liquidation map; book depth, which the detector
    compares a cluster against, is not among this part's inputs, so the
    map's own open interest stands in for it until a blueprint edit gives
    the detector the book.
    """
    from runtime.input_assembly import Batch

    trades = Batch(read=context.bus.reader("symbol-price-frame"))
    maps = Batch(read=context.bus.reader("liquidation-map"))
    # The record of whether this detector was right, back from
    # `signal-outcome-labeller`. Until 2026-08-28 nothing carried it here and
    # `observe_outcome` had never been called by anything that runs.
    labels = Batch(read=context.bus.reader("training-label"))
    publish_candidates = context.bus.publisher_for("entry-candidate")
    detector = LiquidationCascadeDetector(
        window_length=int(context.number("detector_window_length")),
        minimum_observations=int(context.number("detector_minimum_observations")),
        reach_in_volatilities=context.number("liquidation_cascade_reach_in_volatilities"),
        minimum_cluster_notional=context.number("liquidation_cascade_minimum_cluster_notional"),
        cascade_depth_multiple=context.number("liquidation_cascade_depth_multiple"),
        horizon_seconds=context.number("liquidation_cascade_horizon"),
        calibrator=SignalCalibrator(
            prior_hit_rate=context.number("signal_prior_hit_rate"),
            prior_weight=context.number("signal_prior_weight"),
            half_life_observations=context.number("signal_half_life_observations"),
            minimum_observations=int(context.number("signal_minimum_observations")),
        ),
            maximum_gap_seconds=context.number("price_series_maximum_gap_seconds"),
            gap_patience_multiple=context.number("price_gap_patience_multiple"),
    )

    def read_map(_detector):
        # Whichever of this detector's own claims the market has settled since
        # the last tick. Drained first, so a candidate raised below is priced by
        # the record including everything already known -- a claim settled this
        # tick and used next tick would make the confidence one tick stale for
        # no reason.
        settle_claims_from(labels.payloads(), detector, PART_ID)
        touched = set()
        for liquidation_map in maps.payloads():
            detector.set_clusters(liquidation_map.venue_id, liquidation_map.symbol, tuple(liquidation_map.clusters))
            detector.set_book_depth(liquidation_map.venue_id, liquidation_map.symbol, liquidation_map.open_interest_notional)
            touched.add((liquidation_map.venue_id, liquidation_map.symbol))
        for trade in levels_in(trades.payloads()):
            detector.observe_price(
                trade.venue_id, trade.symbol, trade.price, trade.observed_at_ns
            )
            touched.add((trade.venue_id, trade.symbol))
        return tuple(sorted(touched))

    def publish(candidates) -> None:
        if candidates:
            publish_candidates(candidates)

    return run_liquidation_cascade_detector(
        detector=detector,
        control_socket=context.control_socket,
        read_map=read_map,
        publish_candidates=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
