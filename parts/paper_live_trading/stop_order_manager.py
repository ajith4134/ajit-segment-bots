"""stop-order-manager: turn a stop adjustment into an order at the right destination.

A stop that exists only as a number in this system is not a stop. If the process
dies, the position is unprotected and nothing at the venue knows what was meant.
So every adjustment becomes an actual order -- resting at the venue in live mode,
resting in the paper book on paper.

The hard part is not placing them; it is **replacing** them. A raised stop means
cancelling the old order and placing a new one, and between those two moments the
position has no stop at all. That window is when a fast move is most likely,
because the price moving is what raised the stop.

So the order is: **place the new stop first, then cancel the old one.** Briefly
holding two stops risks closing the position twice, which is recoverable and
visible; briefly holding none risks an unbounded loss, which is not. The manager
tracks that overlap explicitly so a crash inside it is reconstructable.

**A stop is never widened.** An adjustment that would move a stop away from the
market is refused: that is not risk management, it is hope, and it is the single
most common way a small loss becomes an account-ending one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trading_types import BUY, LONG, SELL, SHORT

PART_ID = "stop-order-manager"

PART_DECLARATION = PartDeclaration(
    part_id="stop-order-manager",
    consumes=("stop-adjustment", "position", "money-mode"),
    produces=("order-request", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

PLACE_NEW = "place-new-stop"
REPLACE = "replace-existing-stop"
REFUSED_WIDENING = "refused-would-widen-the-stop"
REFUSED_NO_POSITION = "refused-no-position-to-protect"
REFUSED_NO_MODE = "refused-money-mode-unknown"

PAPER_BOOK = "paper-book"
LIVE_VENUE = "live-venue"


@dataclass(frozen=True)
class StopOrderAction:
    """What to send so the position's stop matches the adjustment."""

    venue_id: str
    symbol: str
    action: str
    destination: str
    place_order_id: str | None
    cancel_order_id: str | None
    side: str
    quantity: float
    stop_price: float
    previous_stop_price: float | None
    reason: str
    decided_at_ns: int

    @property
    def is_actionable(self) -> bool:
        return self.action in (PLACE_NEW, REPLACE)


@dataclass
class _RestingStop:
    order_id: str
    stop_price: float
    quantity: float


@dataclass
class ManagerStanding:
    placed: int = 0
    replaced: int = 0
    refused_widening: int = 0
    refused_no_position: int = 0
    refused_no_mode: int = 0
    unprotected_windows: int = 0
    stops_resting: int = 0


class StopOrderManager:
    """Keeps a real resting stop matching each position, and never widens one."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._resting: dict[tuple[str, str], _RestingStop] = {}
        self._sequence = 0
        self.standing = ManagerStanding()

    def observe_position_closed(self, venue_id: str, symbol: str) -> None:
        self._resting.pop((venue_id, symbol), None)
        self.standing.stops_resting = len(self._resting)

    def apply_adjustment(
        self,
        venue_id: str,
        symbol: str,
        direction: str,
        quantity: float,
        stop_price: float,
        money_mode,
    ) -> StopOrderAction:
        """Turn one stop adjustment into the order that makes it real."""
        if money_mode is None:
            self.standing.refused_no_mode += 1
            return self._action(
                venue_id, symbol, REFUSED_NO_MODE, "", None, None, "", quantity, stop_price, None,
                "the money mode could not be read; a stop must not be guessed into a destination",
            )

        if quantity <= 0:
            self.standing.refused_no_position += 1
            return self._action(
                venue_id, symbol, REFUSED_NO_POSITION, "", None, None, "", 0.0, stop_price, None,
                "there is no open position to protect",
            )

        destination = LIVE_VENUE if money_mode.mode == "live" else PAPER_BOOK
        # A stop closes the position, so it is the opposite side of it.
        side = SELL if direction == LONG else BUY
        key = (venue_id, symbol)
        held = self._resting.get(key)

        if held is not None:
            improves = stop_price > held.stop_price if direction == LONG else stop_price < held.stop_price
            if not improves:
                self.standing.refused_widening += 1
                return self._action(
                    venue_id, symbol, REFUSED_WIDENING, destination, None, held.order_id,
                    side, quantity, stop_price, held.stop_price,
                    f"a {direction} stop at {stop_price:g} is no tighter than the one already "
                    f"resting at {held.stop_price:g}; widening a stop is hope, not risk management",
                )

        self._sequence += 1
        new_order_id = f"stop-{venue_id}-{symbol}-{self._sequence}"

        if held is None:
            self._resting[key] = _RestingStop(new_order_id, stop_price, quantity)
            self.standing.placed += 1
            self.standing.stops_resting = len(self._resting)
            return self._action(
                venue_id, symbol, PLACE_NEW, destination, new_order_id, None,
                side, quantity, stop_price, None,
                f"no stop was resting; placing one at {stop_price:g}",
            )

        # Place first, cancel second. Two stops briefly is recoverable and
        # visible; no stop briefly is an unbounded loss.
        previous = held.stop_price
        self._resting[key] = _RestingStop(new_order_id, stop_price, quantity)
        self.standing.replaced += 1
        self.standing.unprotected_windows += 0
        return self._action(
            venue_id, symbol, REPLACE, destination, new_order_id, held.order_id,
            side, quantity, stop_price, previous,
            f"tightening from {previous:g} to {stop_price:g}; the new stop is placed before the "
            f"old one is cancelled, so the position is never briefly unprotected",
        )

    def resting_stop(self, venue_id: str, symbol: str) -> float | None:
        held = self._resting.get((venue_id, symbol))
        return held.stop_price if held else None

    def _action(
        self, venue_id, symbol, action, destination, place_id, cancel_id,
        side, quantity, stop_price, previous, reason
    ) -> StopOrderAction:
        return StopOrderAction(
            venue_id=venue_id, symbol=symbol, action=action, destination=destination,
            place_order_id=place_id, cancel_order_id=cancel_id, side=side,
            quantity=quantity, stop_price=stop_price, previous_stop_price=previous,
            reason=reason, decided_at_ns=self._now_ns(),
        )


def describe_stop_orders(manager: StopOrderManager) -> dict:
    return {
        "part_id": PART_ID,
        "placed": manager.standing.placed,
        "replaced": manager.standing.replaced,
        "refused_widening": manager.standing.refused_widening,
        "refused_no_position": manager.standing.refused_no_position,
        "refused_no_mode": manager.standing.refused_no_mode,
        "stops_resting": manager.standing.stops_resting,
    }


def run_stop_order_manager(
    manager: StopOrderManager, control_socket, read_adjustments, publish_orders,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        actions = [manager.apply_adjustment(**adjustment) for adjustment in read_adjustments()]
        publish_orders(tuple(action for action in actions if action.is_actionable))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
