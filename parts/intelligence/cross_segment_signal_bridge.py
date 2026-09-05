"""cross-segment-signal-bridge: what one segment sees that another needs.

Six segments trade different instruments on the same underlying markets, and
each sees things the others cannot. Index/stock futures and options see
open interest build; cash equity sees the actual price the underlying trades
at. A signal visible in one is often about all of them, and without something
carrying it across, each segment rediscovers it late or not at all.

This is that carrier, and its restraint is the design:

- **It carries observations, never opinions.** "Open interest is four
  deviations above normal" travels; "therefore short" does not. A bridge that
  carried conclusions would be a fourth bot with none of a bot's checks, and its
  conclusions would arrive already weighted by a segment that does not trade
  what the receiver trades.
- **Every signal names where it was observed and what it is about.** An
  open-interest surge observed on the index-options segment is about the
  underlying, and a receiver needs both facts to know whether it applies to
  the instrument it trades.
- **A signal expires.** Open-interest surges and positioning are all about a
  moment; a bridge without expiry lets a segment act on a fact that stopped
  being true hours ago, and the segment cannot tell because the fact arrived
  without a clock.

**A signal the receiving segment cannot act on is not sent.** An
open-interest observation means nothing to a cash-equity segment with no
derivative to hold open interest at all; forwarding it anyway trains every
receiver to ignore the bridge.

**2026-09-01, options-segment-bots conversion:** `WHALE_FLOW` (on-chain
transfers) and `FUNDING_SKEW` (perpetual funding rate) are both retired --
crypto-only concepts with no Indian equivalent. `FUNDING_SKEW`'s *role* --
an unusual, forwardable observation about crowd positioning -- has an honest
Indian analogue in `OPEN_INTEREST_SURGE`, sourced from `broker-open-interest`
the same way the bull/bear feature builders already read it
(`runtime/underlying_open_interest.py`). `PRICE_DISLOCATION` was already
segment-agnostic and, once index-futures exists, becomes the honest carrier
of what funding skew used to proxy for anyway: the same underlying priced
differently in a derivative than in the market it settles against -- that is
a basis, not a guess.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.price_frames import levels_in
from runtime.online_learner import RunningMoments
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "cross-segment-signal-bridge"

PART_DECLARATION = PartDeclaration(
    part_id="cross-segment-signal-bridge",
    consumes=(
        "broker-subscribed-instrument-listing", "broker-open-interest", "symbol-price-frame", "position",
    ),
    produces=("cross-segment-signal", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

OPEN_INTEREST_SURGE = "underlying-open-interest-far-from-its-own-normal"
POSITIONING = "this-system-already-holds-this-underlying-in-another-segment"
PRICE_DISLOCATION = "the-same-underlying-is-priced-differently-across-segments"

INDEX_OPTIONS = "index-options"
STOCK_OPTIONS = "stock-options"
INDEX_FUTURES = "index-futures"
STOCK_FUTURES = "stock-futures"
COMMODITIES = "commodities"
CASH_EQUITY = "cash-equity"
ALL_SEGMENTS = (INDEX_OPTIONS, STOCK_OPTIONS, INDEX_FUTURES, STOCK_FUTURES, COMMODITIES, CASH_EQUITY)

# Which segments can act on each kind of observation. Open interest is a
# derivatives concept -- cash equity and commodities (as built so far, MCX
# futures aside) hold none, and forwarding it anyway trains every receiver to
# ignore the bridge.
RELEVANT_TO = {
    OPEN_INTEREST_SURGE: (INDEX_OPTIONS, STOCK_OPTIONS, INDEX_FUTURES, STOCK_FUTURES),
    POSITIONING: ALL_SEGMENTS,
    PRICE_DISLOCATION: ALL_SEGMENTS,
}


@dataclass(frozen=True)
class CrossSegmentSignal:
    """One observation, from where it was seen, to a segment that can use it."""

    signal: str
    underlying: str
    observed_in: str
    relevant_to: tuple
    magnitude: float
    deviations_from_normal: float | None
    expires_at_ns: int
    observed_at_ns: int
    evidence: dict
    reason: str

    def has_expired(self, now_ns: int) -> bool:
        return now_ns > self.expires_at_ns

    @property
    def carries_an_opinion(self) -> bool:
        """Always false. A bridge that carried conclusions would be a fourth bot."""
        return False


@dataclass
class BridgeStanding:
    observations: int = 0
    signals_sent: int = 0
    dropped_below_threshold: int = 0
    dropped_no_relevant_segment: int = 0
    expired: int = 0
    by_signal: dict = field(default_factory=dict)
    by_receiving_segment: dict = field(default_factory=dict)


class CrossSegmentSignalBridge:
    """Carries observations between segments, with an expiry and no opinion."""

    def __init__(
        self,
        deviation_threshold: float,
        minimum_observations: int,
        half_life_observations: float,
        validity_seconds: float,
        now_ns=time.time_ns,
    ) -> None:
        if deviation_threshold <= 0:
            raise ValueError("a threshold of zero forwards every ordinary observation")
        if validity_seconds <= 0:
            raise ValueError(
                "a signal without an expiry lets a segment act on a fact that stopped being "
                "true hours ago, and it cannot tell because the fact arrived without a clock"
            )
        self._threshold = deviation_threshold
        self._minimum = minimum_observations
        self._half_life = half_life_observations
        self._validity_ns = int(validity_seconds * 1e9)
        self._now_ns = now_ns
        self._moments: dict[tuple[str, str], RunningMoments] = {}
        self._held: dict[str, str] = {}
        self._prices: dict[tuple[str, str], float] = {}
        self.standing = BridgeStanding()

    def observe_position(self, segment: str, underlying: str, is_held: bool) -> None:
        """Which segment already holds this underlying, so the others know."""
        if is_held:
            self._held[underlying] = segment
        elif self._held.get(underlying) == segment:
            del self._held[underlying]

    def observe_segment_price(self, segment: str, underlying: str, price: float) -> None:
        self._prices[(segment, underlying)] = price

    def observe_open_interest_signal(
        self, underlying: str, observed_in: str, open_interest: float
    ) -> tuple:
        """One underlying's summed open interest, forwarded only if it is
        unusual *for this underlying's own history* -- the same reasoning
        the retired funding-skew signal used, real Indian data instead."""
        return self._forward(
            OPEN_INTEREST_SURGE, underlying, observed_in, open_interest,
            {"open_interest": open_interest},
        )

    def signals_for(self, segment: str, underlying: str) -> tuple:
        """Everything this segment should know about this underlying right now."""
        signals = []

        holder = self._held.get(underlying)
        if holder is not None and holder != segment:
            signals.append(
                self._signal(
                    POSITIONING, underlying, holder, 1.0, None,
                    {"held_by": holder},
                    f"the {holder} segment already holds {underlying}; that is exposure this "
                    f"segment would be adding to, not diversifying from",
                )
            )

        dislocation = self._dislocation(underlying)
        if dislocation is not None:
            gap, cheap, expensive = dislocation
            if abs(gap) > self._threshold / 100:
                signals.append(
                    self._signal(
                        PRICE_DISLOCATION, underlying, cheap, gap, None,
                        {"cheaper_segment": cheap, "richer_segment": expensive, "gap": gap},
                        f"{underlying} is {abs(gap):.2%} cheaper in {cheap} than in "
                        f"{expensive}; this is an observation about where the same underlying "
                        f"is priced, not a suggestion to trade the difference",
                    )
                )

        return tuple(
            signal for signal in signals if segment in signal.relevant_to
        )

    def _forward(self, signal: str, underlying: str, observed_in: str, value: float, evidence: dict) -> tuple:
        """One observation, forwarded only if it is unusual for this underlying."""
        self.standing.observations += 1
        moments = self._moment_for(signal, underlying)
        standardised = moments.standardise(value, self._minimum)
        moments.observe(value)

        if standardised is None or abs(standardised) < self._threshold:
            self.standing.dropped_below_threshold += 1
            return ()

        receivers = tuple(
            segment for segment in RELEVANT_TO.get(signal, ()) if segment != observed_in
        )
        if not receivers:
            self.standing.dropped_no_relevant_segment += 1
            return ()

        return (
            self._signal(
                signal, underlying, observed_in, value, standardised, evidence,
                f"{signal} in {underlying}, observed in {observed_in}: {value:.6g} is "
                f"{standardised:+.1f} deviations from its own normal. This is what was seen, "
                f"not what to do about it -- a bridge that carried conclusions would be a "
                f"fourth bot with none of a bot's checks",
            ),
        )

    def _dislocation(self, underlying: str) -> tuple | None:
        """The same underlying priced differently across segments."""
        prices = {
            segment: price
            for (segment, held), price in self._prices.items()
            if held == underlying and price > 0
        }
        if len(prices) < 2:
            return None
        cheap = min(prices, key=prices.get)
        expensive = max(prices, key=prices.get)
        if prices[expensive] <= 0:
            return None
        return (prices[expensive] - prices[cheap]) / prices[expensive], cheap, expensive

    def _signal(
        self, signal, underlying, observed_in, magnitude, deviations, evidence, reason
    ) -> CrossSegmentSignal:
        receivers = tuple(
            segment for segment in RELEVANT_TO.get(signal, ()) if segment != observed_in
        )
        self.standing.signals_sent += 1
        self.standing.by_signal[signal] = self.standing.by_signal.get(signal, 0) + 1
        for segment in receivers:
            self.standing.by_receiving_segment[segment] = (
                self.standing.by_receiving_segment.get(segment, 0) + 1
            )
        now = self._now_ns()
        return CrossSegmentSignal(
            signal=signal,
            underlying=underlying,
            observed_in=observed_in,
            relevant_to=receivers,
            magnitude=magnitude,
            deviations_from_normal=deviations,
            expires_at_ns=now + self._validity_ns,
            observed_at_ns=now,
            evidence=dict(evidence),
            reason=reason,
        )

    def _moment_for(self, signal: str, underlying: str) -> RunningMoments:
        key = (signal, underlying)
        moments = self._moments.get(key)
        if moments is None:
            moments = RunningMoments(half_life_observations=self._half_life)
            self._moments[key] = moments
        return moments

    def drop_expired(self, signals) -> tuple:
        now = self._now_ns()
        live = tuple(signal for signal in signals if not signal.has_expired(now))
        self.standing.expired += len(signals) - len(live)
        return live


def describe_bridging(bridge: CrossSegmentSignalBridge) -> dict:
    return {
        "part_id": PART_ID,
        "observations": bridge.standing.observations,
        "signals_sent": bridge.standing.signals_sent,
        "dropped_as_ordinary": bridge.standing.dropped_below_threshold,
        "dropped_with_no_segment_that_could_act": bridge.standing.dropped_no_relevant_segment,
        "expired": bridge.standing.expired,
        "by_signal": dict(sorted(bridge.standing.by_signal.items())),
        "by_receiving_segment": dict(sorted(bridge.standing.by_receiving_segment.items())),
        "underlyings_with_a_normal": len(bridge._moments),
        "carries_opinions": False,
    }


def run_cross_segment_signal_bridge(
    bridge: CrossSegmentSignalBridge, control_socket, read_observations, publish_signals,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        requests, direct_signals = read_observations(bridge)
        # `direct_signals` is what an _forward()-based observe_* call returned
        # this tick -- an open-interest surge, say -- and it must be published
        # here rather than only relied on for its side effect. Discarding it
        # was a real defect: the observation still updated the moving normal
        # it is judged against, but the signal it crossed the threshold to
        # produce never reached the bus (found while retiring
        # observe_whale_transfer/observe_funding, which had the same shape).
        signals = list(direct_signals)
        for segment, underlying in requests:
            signals.extend(bridge.signals_for(segment, underlying))
        publish_signals(bridge.drop_expired(tuple(signals)))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_bridging(bridge),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    This segment's prices, positions and open interest are observed under
    its own name. Open interest arrives per option contract
    (broker-open-interest); the aggregator resolves each contract to its
    underlying via broker-instrument-listing and sums the chain
    (runtime.underlying_open_interest), the same as the bull/bear feature
    builders. Each tick asks, for every underlying touched, what the other
    five segments should know about it. An underlying is the symbol with the
    settlement currency taken off its end -- a crypto-era normalisation that
    is a harmless no-op on an Indian trading_symbol, which never carries one.
    """
    from runtime.input_assembly import Batch
    from runtime.underlying_open_interest import UnderlyingOpenInterestAggregator

    # A frame already carries the latest price per symbol, so a keyed level shape
    # on top of it would be keeping the latest of the latest. Read as a batch and
    # flattened to its levels.
    trades = Batch(read=context.bus.reader("symbol-price-frame"))
    positions = Batch(read=context.bus.reader("position"))
    listings = Batch(read=context.bus.reader("broker-subscribed-instrument-listing"))
    open_interest = Batch(read=context.bus.reader("broker-open-interest"))
    publish_signals = context.bus.publisher_for("cross-segment-signal")
    segment = str(context.setting("segment_id").value)
    settlement = str(context.setting("settlement_currency").value)
    others = tuple(s for s in ALL_SEGMENTS if s != segment)
    bridge = CrossSegmentSignalBridge(
        deviation_threshold=context.number("signal_bridge_deviation_threshold"),
        minimum_observations=int(context.number("signal_bridge_minimum_observations")),
        half_life_observations=context.number("learning_half_life_observations"),
        validity_seconds=context.number("signal_bridge_validity_seconds"),
    )
    oi_aggregator = UnderlyingOpenInterestAggregator()

    def underlying_of(symbol: str) -> str:
        return symbol[: -len(settlement)] if settlement and symbol.endswith(settlement) and len(symbol) > len(settlement) else symbol

    def read_observations(_bridge):
        for listing in listings.payloads():
            oi_aggregator.observe_listing(listing)
        for reading in open_interest.payloads():
            oi_aggregator.observe_open_interest(reading)
        touched: set[str] = set()
        for level in levels_in(trades.payloads()):
            underlying = underlying_of(level.symbol)
            bridge.observe_segment_price(segment, underlying, level.price)
            touched.add(underlying)
        for position in positions.payloads():
            underlying = underlying_of(position.symbol)
            bridge.observe_position(segment, underlying, position.quantity != 0)
            touched.add(underlying)
        direct_signals: list = []
        for underlying in sorted(touched):
            totals = oi_aggregator.totals_for(underlying)
            if totals is not None:
                direct_signals.extend(
                    bridge.observe_open_interest_signal(underlying, segment, totals.open_interest)
                )
        requests = tuple((other, underlying) for underlying in sorted(touched) for other in others)
        return requests, tuple(direct_signals)

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_signals(kept)

    return run_cross_segment_signal_bridge(
        bridge=bridge,
        control_socket=context.control_socket,
        read_observations=read_observations,
        publish_signals=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
