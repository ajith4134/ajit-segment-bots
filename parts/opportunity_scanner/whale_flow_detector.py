"""whale-flow-detector: an exchange inflow or outflow large enough to precede a move.

Chain flow is one of the few signals that is genuinely early rather than
coincident. Coins moving *onto* an exchange are being positioned to sell; coins
moving *off* are leaving the tradeable float. Both happen before the price
reflects them, which is what makes this worth watching and also what makes it
easy to over-read.

Two things keep it honest:

- **Size is relative to that exchange's own normal flow**, not absolute. A
  thousand BTC is a rounding error on one venue and an event on another.
- **A transfer is not a trade.** Coins arriving may be collateral, a custody
  rotation, or an internal move, and the detector says so in its confidence
  rather than pretending every inflow is a seller.

The direction is deliberately the opposite of the intuitive one for outflows:
coins leaving an exchange reduce the supply available to sell, which supports
price. It reads backwards until you think about who is doing it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.price_frames import levels_in
from runtime.market_signal import CONTINUATION, LONG, SHORT, SignalCalibrator, make_candidate
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow

PART_ID = "whale-flow-detector"

PART_DECLARATION = PartDeclaration(
    part_id="whale-flow-detector",
    consumes=("whale-transfer", "symbol-price-frame", "onchain-flow"),
    produces=("entry-candidate", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FIRED = "fired"
NOT_LARGE_ENOUGH = "flow-ordinary-for-this-venue"
TOO_FEW_OBSERVATIONS = "too-few-flow-observations"

INFLOW = "inflow"
OUTFLOW = "outflow"


@dataclass
class FlowStanding:
    transfers_seen: int = 0
    candidates: int = 0
    not_large_enough: int = 0
    too_few: int = 0
    inflows: int = 0
    outflows: int = 0
    outcomes_learned: int = 0
    largest_flow_z: float = 0.0


class WhaleFlowDetector:
    """Fires on a transfer far outside this venue's own distribution of flows."""

    def __init__(
        self,
        window_length: int,
        minimum_observations: int,
        flow_z_threshold: float,
        horizon_seconds: float,
        calibrator: SignalCalibrator,
        now_ns=time.time_ns,
    ) -> None:
        if flow_z_threshold <= 0:
            raise ValueError("a threshold of zero calls every transfer a whale")
        self._window_length = window_length
        self._minimum = minimum_observations
        self._threshold = flow_z_threshold
        self._horizon = horizon_seconds
        self._calibrator = calibrator
        self._now_ns = now_ns
        self._flows: dict[tuple[str, str], RollingWindow] = {}
        self.standing = FlowStanding()

    def observe_transfer(
        self, venue_id: str, symbol: str, quantity: float, direction: str
    ) -> tuple[object | None, str]:
        """One transfer to or from an exchange wallet. Returns any candidate it produced."""
        self.standing.transfers_seen += 1
        if direction not in (INFLOW, OUTFLOW):
            raise ValueError(f"{direction!r} is neither an inflow nor an outflow")

        key = (venue_id, symbol)
        window = self._flows.get(key)
        if window is None:
            window = RollingWindow(length=self._window_length)
            self._flows[key] = window

        signed = quantity if direction == INFLOW else -quantity
        window.observe(signed)

        if window.count < self._minimum:
            self.standing.too_few += 1
            return None, TOO_FEW_OBSERVATIONS

        z = window.z_score(signed, self._minimum)
        if z is None or abs(z) < self._threshold:
            # A thousand coins is a rounding error on one venue and an event on
            # another, so the comparison is always against this venue's own flow.
            self.standing.not_large_enough += 1
            return None, NOT_LARGE_ENOUGH

        self.standing.largest_flow_z = max(self.standing.largest_flow_z, abs(z))
        self.standing.candidates += 1
        if direction == INFLOW:
            self.standing.inflows += 1
            # Coins arriving are positioned to sell.
            trade_direction = SHORT
        else:
            self.standing.outflows += 1
            # Coins leaving reduce the float available to sell.
            trade_direction = LONG

        confidence = self._calibrator.confidence(PART_ID, direction)
        return (
            make_candidate(
                detector=PART_ID,
                venue_id=venue_id,
                symbol=symbol,
                direction=trade_direction,
                expectation=CONTINUATION,
                signal_strength=abs(z),
                confidence=confidence,
                horizon_seconds=self._horizon,
                evidence={
                    "quantity": quantity,
                    "flow_direction": direction,
                    "flow_z": z,
                    "observations": window.count,
                    "may_not_be_a_trade": True,
                },
                reason=(
                    f"an {direction} of {quantity:g} is {abs(z):.2f} standard deviations of this "
                    f"venue's own flow. Coins "
                    f"{'arriving are positioned to sell' if direction == INFLOW else 'leaving reduce the float available to sell'}, "
                    f"though a transfer is not necessarily a trade -- it moved price the expected "
                    f"way {confidence.value:.0%} of the time "
                    f"({'measured' if confidence.is_fitted else 'the prior'})"
                ),
                now_ns=self._now_ns,
            ),
            FIRED,
        )

    def observe_outcome(self, flow_direction: str, moved_as_expected: bool) -> None:
        self._calibrator.observe_outcome(PART_ID, flow_direction, moved_as_expected)
        self.standing.outcomes_learned += 1


def describe_whale_flow(detector: WhaleFlowDetector) -> dict:
    return {
        "part_id": PART_ID,
        "transfers_seen": detector.standing.transfers_seen,
        "candidates": detector.standing.candidates,
        "not_large_enough": detector.standing.not_large_enough,
        "too_few_observations": detector.standing.too_few,
        "inflows": detector.standing.inflows,
        "outflows": detector.standing.outflows,
        "outcomes_learned": detector.standing.outcomes_learned,
        "largest_flow_z": detector.standing.largest_flow_z,
    }


def run_whale_flow_detector(
    detector: WhaleFlowDetector, control_socket, read_transfers, publish_candidates,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        candidates = []
        for transfer in read_transfers():
            candidate, _ = detector.observe_transfer(**transfer)
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
        read_standing=lambda: describe_whale_flow(detector),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A transfer names an asset and the venue it went to; it is read as a
    transfer of that asset's perpetual on that venue where one has printed.
    """
    from runtime.input_assembly import Batch

    transfers = Batch(read=context.bus.reader("whale-transfer"))
    trades = Batch(read=context.bus.reader("symbol-price-frame"))
    flows = Batch(read=context.bus.reader("onchain-flow"))
    publish_candidates = context.bus.publisher_for("entry-candidate")
    detector = WhaleFlowDetector(
        window_length=int(context.number("detector_window_length")),
        minimum_observations=int(context.number("detector_minimum_observations")),
        flow_z_threshold=context.number("detector_z_threshold"),
        horizon_seconds=context.number("whale_flow_horizon"),
        calibrator=SignalCalibrator(
            prior_hit_rate=context.number("signal_prior_hit_rate"),
            prior_weight=context.number("signal_prior_weight"),
            half_life_observations=context.number("signal_half_life_observations"),
            minimum_observations=int(context.number("signal_minimum_observations")),
        ),
    )
    symbol_of: dict[tuple[str, str], str] = {}

    def read_transfers():
        flows.payloads()
        for trade in levels_in(trades.payloads()):
            symbol_of[(trade.venue_id, trade.symbol.removesuffix("USDT"))] = trade.symbol
            if hasattr(detector, "observe_price"):
                detector.observe_price(trade.venue_id, trade.symbol, trade.price)
        requests = []
        for transfer in transfers.payloads():
            venue_id = transfer.to_venue_id
            if venue_id is None:
                continue
            symbol = symbol_of.get((venue_id, transfer.asset))
            if symbol is None:
                continue
            if transfer.could_become_supply:
                direction = INFLOW
            elif transfer.leaves_the_market:
                direction = OUTFLOW
            else:
                continue  # wallet to wallet; neither side is a venue
            requests.append({
                "venue_id": venue_id, "symbol": symbol,
                "quantity": transfer.quantity, "direction": direction,
            })
        return tuple(requests)

    def publish(candidates) -> None:
        if candidates:
            publish_candidates(candidates)

    return run_whale_flow_detector(
        detector=detector,
        control_socket=context.control_socket,
        read_transfers=read_transfers,
        publish_candidates=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
