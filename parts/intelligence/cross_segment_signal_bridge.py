"""cross-segment-signal-bridge: what one segment sees that another needs.

The three segments trade different instruments on the same underlying markets,
and each sees things the others cannot. Futures sees funding and open interest;
spot sees the actual coin moving between wallets; options sees what the market
pays for uncertainty. A signal visible in one is often about all three, and
without something carrying it across, each segment rediscovers it late or not at
all.

This is that carrier, and its restraint is the design:

- **It carries observations, never opinions.** "Exchange inflows are four
  deviations above normal" travels; "therefore short" does not. A bridge that
  carried conclusions would be a fourth bot with none of a bot's checks, and its
  conclusions would arrive already weighted by a segment that does not trade
  what the receiver trades.
- **Every signal names where it was observed and what it is about.** A funding
  spike observed on the futures segment is about the underlying, and a receiver
  needs both facts to know whether it applies to the instrument it trades.
- **A signal expires.** Whale transfers, funding skews and positioning are all
  about a moment; a bridge without expiry lets a segment act on a fact that
  stopped being true hours ago, and the segment cannot tell because the fact
  arrived without a clock.

**A signal the receiving segment cannot act on is not sent.** A funding
observation means nothing to a spot segment with no perpetual; forwarding it
anyway trains every receiver to ignore the bridge.
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
    consumes=("whale-transfer", "symbol-price-frame", "funding-forecast", "position"),
    produces=("cross-segment-signal", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

WHALE_FLOW = "large-transfer-to-or-from-an-exchange"
FUNDING_SKEW = "perpetual-funding-is-far-from-its-own-normal"
POSITIONING = "this-system-already-holds-this-underlying-in-another-segment"
PRICE_DISLOCATION = "the-same-underlying-is-priced-differently-across-segments"

FUTURES = "futures"
SPOT = "spot"
OPTIONS = "options"

# Which segments can act on each kind of observation. A funding observation is
# meaningless to a segment with no perpetual, and forwarding it anyway trains
# every receiver to ignore the bridge.
RELEVANT_TO = {
    WHALE_FLOW: (FUTURES, SPOT, OPTIONS),
    FUNDING_SKEW: (FUTURES, OPTIONS),
    POSITIONING: (FUTURES, SPOT, OPTIONS),
    PRICE_DISLOCATION: (FUTURES, SPOT),
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

    def observe_whale_transfer(
        self, underlying: str, observed_in: str, quantity: float, direction: str
    ) -> tuple:
        """A large transfer, forwarded only if it is large *for this underlying*."""
        return self._forward(
            WHALE_FLOW, underlying, observed_in, quantity,
            {"direction": direction, "quantity": quantity},
        )

    def observe_funding(self, underlying: str, observed_in: str, rate: float) -> tuple:
        return self._forward(FUNDING_SKEW, underlying, observed_in, rate, {"rate": rate})

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
        requests = read_observations(bridge)
        signals = []
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
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    This segment's prices, positions and funding forecasts are observed
    under its own name; a whale transfer is observed under the chain it was
    seen on. Each tick asks, for every underlying touched, what the other
    two segments should know about it. An underlying is the symbol with the
    settlement currency taken off its end.
    """
    from runtime.input_assembly import Batch

    transfers = Batch(read=context.bus.reader("whale-transfer"))
    # A frame already carries the latest price per symbol, so a keyed level shape
    # on top of it would be keeping the latest of the latest. Read as a batch and
    # flattened to its levels.
    trades = Batch(read=context.bus.reader("symbol-price-frame"))
    forecasts = Batch(read=context.bus.reader("funding-forecast"))
    positions = Batch(read=context.bus.reader("position"))
    publish_signals = context.bus.publisher_for("cross-segment-signal")
    segment = str(context.setting("segment_id").value)
    settlement = str(context.setting("settlement_currency").value)
    others = tuple(s for s in (FUTURES, SPOT, OPTIONS) if s != segment)
    bridge = CrossSegmentSignalBridge(
        deviation_threshold=context.number("signal_bridge_deviation_threshold"),
        minimum_observations=int(context.number("signal_bridge_minimum_observations")),
        half_life_observations=context.number("learning_half_life_observations"),
        validity_seconds=context.number("signal_bridge_validity_seconds"),
    )

    def underlying_of(symbol: str) -> str:
        return symbol[: -len(settlement)] if settlement and symbol.endswith(settlement) and len(symbol) > len(settlement) else symbol

    def read_observations(_bridge):
        touched: set[str] = set()
        for level in levels_in(trades.payloads()):
            underlying = underlying_of(level.symbol)
            bridge.observe_segment_price(segment, underlying, level.price)
            touched.add(underlying)
        for position in positions.payloads():
            underlying = underlying_of(position.symbol)
            bridge.observe_position(segment, underlying, position.quantity != 0)
            touched.add(underlying)
        for forecast in forecasts.payloads():
            if forecast.predicted_rate is None:
                continue
            underlying = underlying_of(forecast.symbol)
            bridge.observe_funding(underlying, segment, float(forecast.predicted_rate))
            touched.add(underlying)
        for read in transfers.payloads():
            transfer = getattr(read, "transfer", None)
            if transfer is None:
                continue
            bridge.observe_whale_transfer(
                str(transfer.asset), str(transfer.chain), float(transfer.quantity),
                f"{transfer.from_kind}->{transfer.to_kind}",
            )
            touched.add(str(transfer.asset))
        return tuple((other, underlying) for underlying in sorted(touched) for other in others)

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
