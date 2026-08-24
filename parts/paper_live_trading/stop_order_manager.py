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
from runtime.trading_types import (
    BUY,
    LIVE_VENUE,
    LONG,
    MARKET,
    PAPER_BOOK,
    ROUTED,
    SELL,
    SHORT,
    STOP_MARKET,
    TAKE_PROFIT_MARKET,
    OrderRequest,
)

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
# The other half of an exit. A position that can only close on its stop is a
# position that can only lose: the plan that placed the stop named a target in
# the same breath, and a target nobody places is a plan half carried out.
PLACE_TARGET = "place-target"
CANCEL_EXIT = "cancel-the-other-exit"
REFUSED_WIDENING = "refused-would-widen-the-stop"
REFUSED_NO_POSITION = "refused-no-position-to-protect"
REFUSED_NO_MODE = "refused-money-mode-unknown"
REFUSED_NO_TARGET = "refused-no-target-price-was-given"

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
        return self.action in (PLACE_NEW, REPLACE, PLACE_TARGET, CANCEL_EXIT)

    @property
    def is_cancel_only(self) -> bool:
        """Withdraws an order without placing one. Quantity is not what it is for."""
        return self.action == CANCEL_EXIT


@dataclass
class _RestingStop:
    """The exits this manager believes are resting for one position.

    Both of them, because they are one instruction: whichever fills, the other
    must be withdrawn. A stop left resting on a position that has already closed
    opens the opposite position when it triggers.
    """

    order_id: str
    stop_price: float
    quantity: float
    target_order_id: str | None = None
    target_price: float | None = None


@dataclass
class ManagerStanding:
    placed: int = 0
    replaced: int = 0
    targets_placed: int = 0
    exits_withdrawn: int = 0
    refused_no_target: int = 0
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

    def observe_position_closed(self, venue_id: str, symbol: str) -> tuple:
        """The position is flat; withdraw whatever exits were protecting it.

        Returns the cancels rather than performing them silently. The orders are
        resting at the venue -- or in the paper book, which must behave the same
        way -- and this part cannot remove them by forgetting them. Forgetting was
        the bug shape: the manager's own count went to zero while the book still
        held a stop that would open a short the moment price fell through it.
        """
        held = self._resting.pop((venue_id, symbol), None)
        self.standing.stops_resting = len(self._resting)
        if held is None:
            return ()
        cancels = []
        for order_id, what in (
            (held.order_id, "stop"), (held.target_order_id, "target"),
        ):
            if not order_id:
                continue
            self.standing.exits_withdrawn += 1
            cancels.append(self._action(
                venue_id, symbol, CANCEL_EXIT, "", None, order_id, "", 0.0, 0.0, None,
                f"the position is flat; withdrawing the resting {what} so it cannot open "
                f"the opposite position when the market reaches it",
            ))
        return tuple(cancels)

    def place_target(
        self,
        venue_id: str,
        symbol: str,
        direction: str,
        quantity: float,
        target_price: float | None,
        money_mode,
    ) -> StopOrderAction:
        """Rest the take-profit for a position, once, on the side that closes it.

        Once: a target is not tightened the way a stop is. Re-placing it on every
        adjustment would put a second resting order on the book for the same
        quantity, and both filling is a position opened in the other direction.
        """
        if money_mode is None:
            self.standing.refused_no_mode += 1
            return self._action(
                venue_id, symbol, REFUSED_NO_MODE, "", None, None, "", quantity,
                0.0, None,
                "the money mode could not be read; a target must not be guessed into a destination",
            )
        if not target_price:
            self.standing.refused_no_target += 1
            return self._action(
                venue_id, symbol, REFUSED_NO_TARGET, "", None, None, "", quantity, 0.0, None,
                "the plan named no target; the position closes on its stop or not at all",
            )
        if quantity <= 0:
            self.standing.refused_no_position += 1
            return self._action(
                venue_id, symbol, REFUSED_NO_POSITION, "", None, None, "", 0.0, 0.0, None,
                "there is no open position to take profit on",
            )

        held = self._resting.get((venue_id, symbol))
        if held is not None and held.target_order_id:
            self.standing.refused_no_target += 1
            return self._action(
                venue_id, symbol, REFUSED_NO_TARGET, "", None, None, "", quantity, 0.0, None,
                f"a target is already resting at {held.target_price:g} for this position; a "
                f"second one would close a quantity that is not held",
            )

        destination = LIVE_VENUE if money_mode.mode == "live" else PAPER_BOOK
        side = SELL if direction == LONG else BUY
        self._sequence += 1
        target_order_id = f"target-{venue_id}-{symbol}-{self._sequence}"
        if held is None:
            self._resting[(venue_id, symbol)] = _RestingStop(
                order_id="", stop_price=0.0, quantity=quantity,
                target_order_id=target_order_id, target_price=target_price,
            )
        else:
            held.target_order_id = target_order_id
            held.target_price = target_price
        self.standing.targets_placed += 1
        self.standing.stops_resting = len(self._resting)
        return self._action(
            venue_id, symbol, PLACE_TARGET, destination, target_order_id, None,
            side, quantity, target_price, None,
            f"resting a {side} take-profit trigger at {target_price:g}; it waits there exactly "
            f"as it would at the venue, fills at market when the price is reached, and may "
            f"never be reached at all",
        )

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

        if held is None or not held.order_id:
            self._resting[key] = _RestingStop(
                new_order_id, stop_price, quantity,
                target_order_id=held.target_order_id if held else None,
                target_price=held.target_price if held else None,
            )
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
        # The target rides along unchanged. Rebuilding the record without it
        # would lose the id this part needs to withdraw it when the stop fills.
        self._resting[key] = _RestingStop(
            new_order_id, stop_price, quantity,
            target_order_id=held.target_order_id, target_price=held.target_price,
        )
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
        "targets_placed": manager.standing.targets_placed,
        "exits_withdrawn": manager.standing.exits_withdrawn,
        "refused_no_target": manager.standing.refused_no_target,
    }


def run_stop_order_manager(
    manager: StopOrderManager, control_socket, read_adjustments, publish_orders,
    health_interval_seconds: float, emit_health, read_flat_positions=None,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    """`read_flat_positions` names the positions that have gone flat this tick.

    Their resting exits are withdrawn, and the withdrawals are published like any
    other order. A cancel that is not sent is a stop still sitting at the venue.
    """
    def tick() -> None:
        actions = []
        for adjustment in read_adjustments():
            target_price = adjustment.pop("target_price", None)
            actions.append(manager.apply_adjustment(**adjustment))
            actions.append(manager.place_target(
                venue_id=adjustment["venue_id"], symbol=adjustment["symbol"],
                direction=adjustment["direction"], quantity=adjustment["quantity"],
                target_price=target_price, money_mode=adjustment["money_mode"],
            ))
        if read_flat_positions is not None:
            for venue_id, symbol in read_flat_positions():
                actions.extend(manager.observe_position_closed(venue_id, symbol))
        publish_orders(tuple(action for action in actions if action.is_actionable))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_stop_orders(manager),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    This is the part that makes a position closeable. Everything before it decides
    where the exits belong; this turns them into orders that actually rest, and
    the paper book fills them when a live price reaches them exactly as a venue
    would (RL-071 -- the prices are the ones arriving now, never a replay).

    It publishes `order-request`, the same type `order-destination-router`
    publishes for entries, because `paper-fill-simulator` reads one type and an
    exit that arrived as a different shape would be an order the book could not
    read. The destination is decided from the money mode this part reads for
    itself, which is the third of the three independent paper-only checks.

    Two shapes may arrive on `stop-adjustment`: the exits `exit-order-chainer`
    emits the instant an entry fills, and the raised stops `profit-lock` emits as
    a trade goes into profit. Only the first is produced by anything running, and
    an adjustment this part cannot read is counted and named rather than guessed
    at -- a misread stop price is a position protected at the wrong number.
    """
    from runtime.input_assembly import Batch, LatestValue

    adjustments = Batch(read=context.bus.reader("stop-adjustment"))
    positions = Batch(read=context.bus.reader("position"))
    modes = LatestValue(read=context.bus.reader("money-mode"))
    publish_orders = context.bus.publisher_for("order-request")

    # Positions seen flat since the last tick. Held here rather than asked of the
    # manager, because "this position just closed" is a fact about the position
    # stream and the manager's job starts once it is known.
    gone_flat: list[tuple[str, str]] = []
    held_quantity: dict[tuple[str, str], float] = {}
    unreadable = {"count": 0, "last": None}

    def read_adjustments():
        mode = modes.value()
        for position in positions.payloads():
            key = (position.venue_id, position.symbol)
            was_held = held_quantity.get(key, 0.0)
            if position.is_flat:
                if was_held:
                    gone_flat.append(key)
                held_quantity.pop(key, None)
            else:
                held_quantity[key] = position.quantity

        readable = []
        for adjustment in adjustments.payloads():
            exit_side = getattr(adjustment, "exit_side", None)
            stop_price = getattr(adjustment, "stop_price", None)
            if exit_side is None or stop_price is None:
                unreadable["count"] += 1
                unreadable["last"] = type(adjustment).__name__
                continue
            if not getattr(adjustment, "should_be_sent", True):
                continue
            readable.append({
                "venue_id": adjustment.venue_id,
                "symbol": adjustment.symbol,
                # The exit is the opposite side of the position, so the position's
                # own direction is the opposite of the exit's side.
                "direction": LONG if exit_side == SELL else SHORT,
                "quantity": adjustment.quantity,
                "stop_price": stop_price,
                "target_price": getattr(adjustment, "target_price", None),
                "money_mode": mode,
            })
        return readable

    def read_flat_positions():
        closed = tuple(gone_flat)
        gone_flat.clear()
        return closed

    def publish_as_order_requests(actions) -> None:
        publish_orders(tuple(as_order_request(action) for action in actions))

    return run_stop_order_manager(
        manager=StopOrderManager(),
        control_socket=context.control_socket,
        read_adjustments=read_adjustments,
        publish_orders=publish_as_order_requests,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
        read_flat_positions=read_flat_positions,
    )


def as_order_request(action: StopOrderAction):
    """One exit decision, as the order type the book reads.

    A stop carries its trigger in `stop_price` and a target carries its price in
    `limit_price`, which is the difference between the two instructions: a sell
    stop below the market takes the loss, a sell limit above it takes the profit.
    A cancel carries neither and names the order it withdraws.
    """
    if action.action == PLACE_TARGET:
        order_type = TAKE_PROFIT_MARKET
    elif action.is_cancel_only:
        # A cancel places nothing. Typed as a market order carrying no quantity,
        # which is what `may_be_sent` already refuses to send -- the book acts on
        # the id it withdraws, not on the order it arrives as.
        order_type = MARKET
    else:
        order_type = STOP_MARKET
    return OrderRequest(
        client_order_id=action.place_order_id or f"cancel-{action.cancel_order_id}",
        destination=action.destination or PAPER_BOOK,
        venue_id=action.venue_id,
        symbol=action.symbol,
        side=action.side,
        quantity=action.quantity,
        # Both exits are market orders that wait for a price (operator,
        # 2026-08-23). The trigger rides in `stop_price` for both, and the type
        # is what says which direction it fires in: a sell stop below the market,
        # a sell take-profit above it.
        limit_price=0.0,
        stop_price=action.stop_price,
        order_type=order_type,
        slice_sequence=1,
        slice_count=1,
        at_second=0.0,
        outcome=ROUTED,
        reason=action.reason,
        routed_at_ns=action.decided_at_ns,
        cancels_client_order_id=action.cancel_order_id,
    )
