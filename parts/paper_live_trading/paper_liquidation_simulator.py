"""paper-liquidation-simulator: close a paper position when the live range crosses its liquidation.

Without this, paper trading at leverage is fiction. A paper position that would
have been force-closed simply carries on, recovers, and books a profit that could
never have existed -- and the strategy that produced it looks like it survives
drawdowns it would not have survived.

Two details make the difference between a simulation and a comfort blanket:

- **The high and low are checked, not the close.** A liquidation is triggered by
  the worst price in the interval, not by where it ended. A candle that wicked
  through the liquidation price and closed above it liquidated the position.
- **It closes at the bankruptcy price, not the liquidation price.** A real
  liquidation is a forced market order into whatever depth exists, and the
  position holder does not get the trigger price. Filling at the trigger would
  understate the loss by exactly the amount that matters.

Once liquidated the position is gone, and the fee the venue charges for doing it
is charged too -- it is often larger than a normal trading fee, and it is real.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trading_types import BUY, LONG, SELL, SHORT, Fill

PART_ID = "paper-liquidation-simulator"

PART_DECLARATION = PartDeclaration(
    part_id="paper-liquidation-simulator",
    consumes=("position", "market-data", "liquidation-price", "money-mode"),
    produces=("fill", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

SURVIVED = "survived"
LIQUIDATED = "liquidated"
NOT_WATCHED = "no-liquidation-price-known"
LIVE_NOT_SIMULATED = "live-position-not-simulated"
PAPER = "paper"


@dataclass(frozen=True)
class LiquidationResult:
    """Whether a paper position survived this interval, and what it cost if not."""

    venue_id: str
    symbol: str
    direction: str
    outcome: str
    liquidation_price: float | None
    worst_price: float | None
    bankruptcy_price: float | None
    fill: Fill | None
    loss: float
    liquidation_fee: float
    reason: str
    decided_at_ns: int

    @property
    def was_liquidated(self) -> bool:
        return self.outcome == LIQUIDATED


@dataclass
class _WatchedPosition:
    direction: str
    quantity: float
    entry_price: float
    liquidation_price: float | None


@dataclass
class SimulatorStanding:
    positions_watched: int = 0
    intervals_checked: int = 0
    liquidations: int = 0
    survived_wicks: int = 0
    unwatched_positions: int = 0
    total_liquidation_loss: float = 0.0
    fees_charged: float = 0.0


class PaperLiquidationSimulator:
    """Force-closes a paper position whose liquidation price was touched."""

    def __init__(
        self,
        liquidation_fee_rate: float,
        bankruptcy_slippage_fraction: float,
        now_ns=time.time_ns,
    ) -> None:
        if liquidation_fee_rate < 0 or bankruptcy_slippage_fraction < 0:
            raise ValueError("a liquidation cost cannot be negative")
        self._fee_rate = liquidation_fee_rate
        self._slippage = bankruptcy_slippage_fraction
        self._now_ns = now_ns
        self._watched: dict[tuple[str, str], _WatchedPosition] = {}
        self._sequence = 0
        self.standing = SimulatorStanding()

    def watch_position(
        self,
        venue_id: str,
        symbol: str,
        direction: str,
        quantity: float,
        entry_price: float,
        liquidation_price: float | None,
    ) -> None:
        key = (venue_id, symbol)
        if quantity <= 0:
            self._watched.pop(key, None)
        else:
            self._watched[key] = _WatchedPosition(direction, quantity, entry_price, liquidation_price)
        self.standing.positions_watched = len(self._watched)

    def check_interval(
        self,
        venue_id: str,
        symbol: str,
        high_price: float,
        low_price: float,
        money_mode: str,
    ) -> LiquidationResult:
        """One candle's range against the position's liquidation price."""
        self.standing.intervals_checked += 1
        key = (venue_id, symbol)
        position = self._watched.get(key)

        if position is None:
            return self._result(venue_id, symbol, "", NOT_WATCHED, None, None, None, None, 0.0, 0.0,
                                "no paper position is watched for this symbol")

        if money_mode != "paper":
            return self._result(
                venue_id, symbol, position.direction, LIVE_NOT_SIMULATED,
                position.liquidation_price, None, None, None, 0.0, 0.0,
                "a live position is liquidated by the venue, not simulated here",
            )

        if position.liquidation_price is None:
            self.standing.unwatched_positions += 1
            return self._result(
                venue_id, symbol, position.direction, NOT_WATCHED, None, None, None, None, 0.0, 0.0,
                "no liquidation price is known for this position; it cannot be simulated and "
                "must not be assumed safe",
            )

        # The worst price of the interval, not the close: a wick through the
        # liquidation price liquidated the position even if it closed above.
        worst = low_price if position.direction == LONG else high_price
        touched = (
            worst <= position.liquidation_price
            if position.direction == LONG
            else worst >= position.liquidation_price
        )

        if not touched:
            self.standing.survived_wicks += 1
            return self._result(
                venue_id, symbol, position.direction, SURVIVED, position.liquidation_price,
                worst, None, None, 0.0, 0.0,
                f"the worst price of the interval was {worst:g}, short of the liquidation "
                f"price at {position.liquidation_price:g}",
            )

        # The bankruptcy price: a forced market order into whatever depth exists,
        # which is worse than the trigger by the slippage a liquidation suffers.
        bankruptcy = (
            position.liquidation_price * (1 - self._slippage)
            if position.direction == LONG
            else position.liquidation_price * (1 + self._slippage)
        )
        gain = (bankruptcy - position.entry_price) * position.quantity
        loss = gain if position.direction == LONG else -gain
        fee = abs(position.quantity) * bankruptcy * self._fee_rate

        del self._watched[key]
        self.standing.positions_watched = len(self._watched)
        self.standing.liquidations += 1
        self.standing.total_liquidation_loss += -loss
        self.standing.fees_charged += fee

        self._sequence += 1
        fill = Fill(
            fill_id=f"liquidation-{venue_id}-{symbol}-{self._sequence}",
            venue_id=venue_id,
            symbol=symbol,
            side=SELL if position.direction == LONG else BUY,
            price=bankruptcy,
            quantity=position.quantity,
            fee=fee,
            filled_at_ns=self._now_ns(),
            order_id=None,
            is_paper=True,
        )
        return self._result(
            venue_id, symbol, position.direction, LIQUIDATED, position.liquidation_price,
            worst, bankruptcy, fill, loss, fee,
            f"the interval reached {worst:g}, through the liquidation price at "
            f"{position.liquidation_price:g}; closed at the bankruptcy price of {bankruptcy:g}, "
            f"not the trigger, plus a {fee:,.4f} liquidation fee",
        )

    def _result(
        self, venue_id, symbol, direction, outcome, liquidation_price, worst,
        bankruptcy, fill, loss, fee, reason
    ) -> LiquidationResult:
        return LiquidationResult(
            venue_id=venue_id, symbol=symbol, direction=direction, outcome=outcome,
            liquidation_price=liquidation_price, worst_price=worst,
            bankruptcy_price=bankruptcy, fill=fill, loss=loss, liquidation_fee=fee,
            reason=reason, decided_at_ns=self._now_ns(),
        )


def describe_liquidations(simulator: PaperLiquidationSimulator) -> dict:
    return {
        "part_id": PART_ID,
        "positions_watched": simulator.standing.positions_watched,
        "intervals_checked": simulator.standing.intervals_checked,
        "liquidations": simulator.standing.liquidations,
        "survived_wicks": simulator.standing.survived_wicks,
        "positions_without_a_liquidation_price": simulator.standing.unwatched_positions,
        "total_liquidation_loss": simulator.standing.total_liquidation_loss,
        "fees_charged": simulator.standing.fees_charged,
    }


def run_paper_liquidation_simulator(
    simulator: PaperLiquidationSimulator, control_socket, read_positions_and_candles, publish_fills,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        intervals = read_positions_and_candles(simulator)
        results = [simulator.check_interval(**interval) for interval in intervals]
        publish_fills(tuple(result.fill for result in results if result.was_liquidated))

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

    Positions are watched from the reconciler's position stream, with the
    liquidation price the tracker computes for each; every trade since the
    last wake gives each symbol's high and low for the interval, which is the
    path a liquidation would have triggered on. A liquidated position
    publishes the fill that closes it, the way the venue would.
    """
    from runtime.input_assembly import Batch, LatestByKey
    from runtime.trading_types import LONG, SHORT
    from runtime.venues.venue_adapter import NormalisedTrade

    positions = Batch(read=context.bus.reader("position"))
    trades = Batch(read=context.bus.reader("market-data"))
    liquidations = LatestByKey(read=context.bus.reader("liquidation-price"), key_of=lambda l: (l.venue_id, l.symbol))
    modes = LatestByKey(read=context.bus.reader("money-mode"), key_of=lambda m: m.segment)
    publish_fills = context.bus.publisher_for("fill")
    segment = str(context.setting("segment_id").value)
    simulator = PaperLiquidationSimulator(
        liquidation_fee_rate=context.number("liquidation_fee_rate"),
        bankruptcy_slippage_fraction=context.number("bankruptcy_slippage_fraction"),
    )
    open_positions: dict[tuple[str, str], object] = {}

    def read_positions_and_candles(_simulator):
        for position in positions.payloads():
            key = (position.venue_id, position.symbol)
            if position.quantity == 0:
                open_positions.pop(key, None)
            else:
                open_positions[key] = position
        liquidation_by_symbol = liquidations.mapping()
        for key, position in open_positions.items():
            liquidation = liquidation_by_symbol.get(key)
            simulator.watch_position(
                venue_id=key[0], symbol=key[1],
                direction=LONG if position.quantity > 0 else SHORT,
                quantity=abs(position.quantity),
                entry_price=position.average_entry_price,
                liquidation_price=None if liquidation is None else liquidation.liquidation_price,
            )
        highs: dict[tuple[str, str], float] = {}
        lows: dict[tuple[str, str], float] = {}
        for trade in trades.payloads():
            if not isinstance(trade, NormalisedTrade):
                continue
            key = (trade.venue_id, trade.symbol)
            highs[key] = max(highs.get(key, trade.price), trade.price)
            lows[key] = min(lows.get(key, trade.price), trade.price)
        mode = modes.mapping().get(segment)
        money_mode = mode.mode if mode is not None else PAPER
        return tuple(
            {
                "venue_id": key[0], "symbol": key[1],
                "high_price": highs[key], "low_price": lows[key], "money_mode": money_mode,
            }
            for key in highs if key in open_positions
        )

    def publish(fills) -> None:
        if fills:
            publish_fills(fills)

    return run_paper_liquidation_simulator(
        simulator=simulator,
        control_socket=context.control_socket,
        read_positions_and_candles=read_positions_and_candles,
        publish_fills=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
