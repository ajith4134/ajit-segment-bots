"""liquidation-price-tracker: the price at which this bot's own position is force-closed."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trading_types import LONG

PART_ID = "liquidation-price-tracker"

PART_DECLARATION = PartDeclaration(
    part_id="liquidation-price-tracker",
    consumes=("position", "leverage-choice", "market-data"),
    produces=("liquidation-price", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)


@dataclass(frozen=True)
class LiquidationPrice:
    """Where this position dies, and how far away that is right now."""

    venue_id: str
    symbol: str
    direction: str
    entry_price: float
    leverage: float
    maintenance_margin_rate: float
    liquidation_price: float | None
    distance_fraction: float | None
    reason: str
    observed_at_ns: int


@dataclass
class TrackerStanding:
    positions_tracked: int = 0
    without_leverage: int = 0
    without_margin_rate: int = 0
    computed: int = 0
    closest_distance: float | None = None


class LiquidationPriceTracker:
    """Computes the force-close price from entry, leverage and maintenance margin.

    Isolated-margin arithmetic, which is what this project trades: the position
    dies when its own margin is exhausted, so nothing outside the position enters
    the sum. A cross-margin account would need the whole balance and this would
    be the wrong formula rather than an approximate one -- so it says which it is.

    Every input must be present. A liquidation price computed from an assumed
    maintenance rate is a number that looks exactly like a real one and is the
    single most dangerous kind of wrong here.
    """

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._positions: dict[tuple[str, str], object] = {}
        self._leverage: dict[tuple[str, str], float] = {}
        self._margin_rates: dict[tuple[str, str], float] = {}
        self._prices: dict[tuple[str, str], float] = {}
        self.standing = TrackerStanding()

    def observe_position(self, position) -> None:
        key = (position.venue_id, position.symbol)
        if position.is_flat:
            self._positions.pop(key, None)
        else:
            self._positions[key] = position
        self.standing.positions_tracked = len(self._positions)

    def set_leverage(self, venue_id: str, symbol: str, leverage: float) -> None:
        self._leverage[(venue_id, symbol)] = leverage

    def set_maintenance_margin_rate(self, venue_id: str, symbol: str, rate: float) -> None:
        self._margin_rates[(venue_id, symbol)] = rate

    def observe_price(self, venue_id: str, symbol: str, price: float) -> None:
        self._prices[(venue_id, symbol)] = price

    def compute(self, venue_id: str, symbol: str) -> LiquidationPrice | None:
        key = (venue_id, symbol)
        position = self._positions.get(key)
        if position is None:
            return None

        leverage = self._leverage.get(key)
        rate = self._margin_rates.get(key)
        if leverage is None or leverage <= 0:
            self.standing.without_leverage += 1
            return self._unknown(position, leverage or 0.0, rate or 0.0, "no leverage-choice for this position")
        if rate is None:
            self.standing.without_margin_rate += 1
            return self._unknown(position, leverage, 0.0, "the venue's maintenance margin rate is unknown")

        entry = position.average_entry_price
        # Isolated margin: the position is liquidated once its loss consumes the
        # initial margin less what must remain as maintenance.
        move = entry * (1.0 / leverage - rate)
        liquidation = entry - move if position.direction == LONG else entry + move
        self.standing.computed += 1

        current = self._prices.get(key)
        distance = None
        if current:
            distance = abs(current - liquidation) / current
            if self.standing.closest_distance is None or distance < self.standing.closest_distance:
                self.standing.closest_distance = distance

        return LiquidationPrice(
            venue_id=venue_id,
            symbol=symbol,
            direction=position.direction,
            entry_price=entry,
            leverage=leverage,
            maintenance_margin_rate=rate,
            liquidation_price=liquidation,
            distance_fraction=distance,
            reason="isolated margin, from entry, leverage and maintenance rate",
            observed_at_ns=self._now_ns(),
        )

    def _unknown(self, position, leverage, rate, reason) -> LiquidationPrice:
        return LiquidationPrice(
            venue_id=position.venue_id,
            symbol=position.symbol,
            direction=position.direction,
            entry_price=position.average_entry_price,
            leverage=leverage,
            maintenance_margin_rate=rate,
            liquidation_price=None,
            distance_fraction=None,
            reason=reason,
            observed_at_ns=self._now_ns(),
        )

    def compute_all(self) -> tuple[LiquidationPrice, ...]:
        return tuple(
            price
            for venue, symbol in sorted(self._positions)
            if (price := self.compute(venue, symbol)) is not None
        )


def describe_liquidations(tracker: LiquidationPriceTracker) -> dict:
    return {
        "part_id": PART_ID,
        "positions_tracked": tracker.standing.positions_tracked,
        "computed": tracker.standing.computed,
        "without_leverage": tracker.standing.without_leverage,
        "without_margin_rate": tracker.standing.without_margin_rate,
        "closest_distance_fraction": tracker.standing.closest_distance,
    }


def run_liquidation_price_tracker(
    tracker: LiquidationPriceTracker, control_socket, read_inputs, publish_liquidations,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_inputs(tracker)
        publish_liquidations(tracker.compute_all())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
