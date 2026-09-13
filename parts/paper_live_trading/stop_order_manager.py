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

import pathlib
import time
from dataclasses import dataclass, field

from runtime.durable_state import (
    CheckpointSchedule,
    DurableStateStore,
    restore_and_arm_checkpoint,
)
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

# One checkpoint per part per component; this part keeps exactly one thing.
CHECKPOINT_COMPONENT = "resting-exits"

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
# The stop stays where it is; only the quantity it closes moves, to match a
# position that has grown or been scaled out of since it was placed.
RESIZE = "resize-stop-to-the-position"
# The other half of an exit. A position that can only close on its stop is a
# position that can only lose: the plan that placed the stop named a target in
# the same breath, and a target nobody places is a plan half carried out.
PLACE_TARGET = "place-target"
# The target's own counterpart to RESIZE. A target was placed once for whatever
# the position held at that moment and never touched again -- so a position
# that grew after its target was resting closed only the target's original
# quantity when it filled, leaving the growth as unprotected, ungated dust.
# Measured live 2026-08-30: ADAUSDT grew from 244.379 to 244.618, its target
# still resting for 244.379, and it closed to exactly 0.239 -- the difference,
# to the thousandth -- when the target filled.
RESIZE_TARGET = "resize-target-to-the-position"
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
    # Whose money this exit spends, carried for exactly the reason
    # `Position.segment` is: the exits are placed long after the decision that
    # opened the position, and every part that reads money reads one level per
    # segment since 2026-09-05. An exit that named no segment was handed no
    # money mode by `level_for_segment` -- correctly, because with three
    # segments built there is nothing to fall back to -- and
    # paper-fill-simulator then refused it as a live order. Measured 2026-09-06:
    # the entry filled, both exits were refused, and the position was left with
    # no stop and no target on it.
    segment: str = ""

    @property
    def is_actionable(self) -> bool:
        # RESIZE was missing until 2026-09-13 -- added as an action on 2026-08-28,
        # never added here, while RESIZE_TARGET was (2026-09-01). The manager
        # recorded the re-cut stop's new id as resting and the action was filtered
        # out before publishing: the new stop was never placed and the old one
        # never withdrawn. When the position closed, the manager cancelled the id
        # that did not exist and the real old stop kept resting until it fired --
        # NIFTY 23700 CE 15 SEP 26 on 2026-09-08: cancel sent for stop -1340, stop
        # -1338 sold 1,430.081 two minutes after the position was flat.
        return self.action in (
            PLACE_NEW, REPLACE, RESIZE, PLACE_TARGET, RESIZE_TARGET, CANCEL_EXIT,
        )

    @property
    def is_cancel_only(self) -> bool:
        """Withdraws an order without placing one. Quantity is not what it is for."""
        return self.action == CANCEL_EXIT


def stop_key_text(key: tuple[str, str]) -> str:
    """One position's key as one string, for a JSON object that has only strings.

    The same separator the lot books use, so the two checkpoints written for one
    position read alike and a person comparing them by eye is comparing the same
    shape.
    """
    return f"{key[0]}|{key[1]}"


def stop_key_of(text: str) -> tuple[str, str]:
    venue_id, _, symbol = text.partition("|")
    return venue_id, symbol


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
    # The quantity the resting target order was actually placed for -- separate
    # from `quantity` (the stop's), because resizing one has never resized the
    # other. None until a target exists; a target's own quantity is a fact
    # about the target, not something to infer from the stop's.
    target_quantity: float | None = None


@dataclass
class ManagerStanding:
    placed: int = 0
    replaced: int = 0
    targets_placed: int = 0
    exits_withdrawn: int = 0
    refused_no_target: int = 0
    refused_widening: int = 0
    # Resting stops re-cut to the position they protect. A stop is placed for
    # whatever was held when it was proposed, and nothing resized it afterwards:
    # on 2026-08-28 `binance-usdm|AKEUSDT` held 231,812 units with a stop resting
    # for 198.634 -- 0.09% of it -- and the board painted that position protected.
    resized_to_the_position: int = 0
    # The target's own counterpart. Absent until 2026-08-30, a position that
    # grew after its target was resting closed only the target's original
    # quantity when it filled -- see RESIZE_TARGET.
    resized_target_to_the_position: int = 0
    refused_no_position: int = 0
    # Exits that arrived after the position they were for had already closed.
    refused_exits_for_a_closed_position: int = 0
    refused_no_mode: int = 0
    unprotected_windows: int = 0
    stops_resting: int = 0
    # Not counters: what happened to the checkpoint at start. A part that came
    # back holding nothing and one whose checkpoint could not be read are
    # different facts, and only the second is a fault (Rule 8).
    restored_symbols: int = 0
    checkpoint_verdict: str = ""


class StopOrderManager:
    """Keeps a real resting stop matching each position, and never widens one."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._resting: dict[tuple[str, str], _RestingStop] = {}
        # Whose money is in each held position, so its exits can name it.
        self._segment_of: dict[tuple[str, str], str] = {}
        self._sequence = 0
        self.standing = ManagerStanding()

    def read_checkpoint_state(self) -> dict:
        """The exits this manager believes are resting, to carry into the next process.

        Held in memory alone until 2026-08-26, which meant every restart forgot
        every resting stop. The consequence is not a board gap: a position whose
        stop this part has forgotten has no protective order and nothing reports
        it, because `apply_adjustment` only ever hears about a stop when something
        upstream proposes a new one. The lot books were fixed for the same reason
        on 2026-08-25, when 86% of everything ever opened turned out to be
        unaccounted for after 46 restarts.

        `_sequence` rides along so order ids stay unique across a restart. Without
        it the next process starts at 1 and mints `stop-binance-usdm-BTCUSDT-1`
        again -- an id the venue may still have resting against the first one.

        The standing counters are deliberately absent: they count what *this*
        process did. `stops_resting` is recomputed from what came back, because
        that is a fact about the stops rather than about the process.
        """
        return {
            "resting": {
                stop_key_text(key): {
                    "order_id": held.order_id,
                    "stop_price": held.stop_price,
                    "quantity": held.quantity,
                    "target_order_id": held.target_order_id,
                    "target_price": held.target_price,
                    "target_quantity": held.target_quantity,
                }
                for key, held in self._resting.items()
            },
            "sequence": self._sequence,
        }

    def restore_from_checkpoint(self, state: dict) -> int:
        """Rebuild what was resting. Returns how many positions came back protected."""
        self._resting = {
            stop_key_of(text): _RestingStop(
                order_id=str(held["order_id"]),
                stop_price=float(held["stop_price"]),
                quantity=float(held["quantity"]),
                target_order_id=held.get("target_order_id"),
                target_price=(
                    None if held.get("target_price") is None else float(held["target_price"])
                ),
                target_quantity=(
                    None if held.get("target_quantity") is None else float(held["target_quantity"])
                ),
            )
            for text, held in (state.get("resting") or {}).items()
        }
        self._sequence = int(state.get("sequence") or 0)
        self.standing.stops_resting = len(self._resting)
        return self.standing.stops_resting

    def is_stop_resting(self, venue_id: str, symbol: str) -> bool:
        """Whether this part has a stop order out on that position right now.

        Asked before an unchanged stop is skipped: "the lock left it where it
        was" is a reason to send nothing only when something is already there.
        """
        return (venue_id, symbol) in self._resting

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
                target_quantity=quantity,
            )
        else:
            held.target_order_id = target_order_id
            held.target_price = target_price
            held.target_quantity = quantity
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
                target_quantity=held.target_quantity if held else None,
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
            target_quantity=held.target_quantity,
        )
        self.standing.replaced += 1
        self.standing.unprotected_windows += 0
        return self._action(
            venue_id, symbol, REPLACE, destination, new_order_id, held.order_id,
            side, quantity, stop_price, previous,
            f"tightening from {previous:g} to {stop_price:g}; the new stop is placed before the "
            f"old one is cancelled, so the position is never briefly unprotected",
        )

    def resize_stop_to_the_position(
        self,
        venue_id: str,
        symbol: str,
        direction: str,
        quantity: float,
        money_mode,
        quantity_increment: float,
    ) -> StopOrderAction | None:
        """Re-cut a resting stop to what the position now holds. None when it fits.

        A stop is proposed for the quantity held at the moment something upstream
        asked for one, and every later fill changes that quantity without
        proposing anything. `apply_adjustment` cannot do this job: a position that
        grew is protected by the same stop *price*, so the proposal that would
        resize it is refused as a widening -- which is the right refusal about the
        price and the wrong outcome for the quantity.

        The stop price is carried across untouched. Nothing here decides where a
        stop belongs; it decides only that whatever was decided applies to the
        whole position. Below one quantity step the difference cannot be traded
        anyway, so it is left alone rather than churning an order per fill.
        """
        key = (venue_id, symbol)
        held = self._resting.get(key)
        if held is None or not held.order_id:
            return None
        if quantity <= 0:
            return None
        if abs(quantity - held.quantity) < quantity_increment:
            return None
        if money_mode is None:
            self.standing.refused_no_mode += 1
            return self._action(
                venue_id, symbol, REFUSED_NO_MODE, "", None, None, "", quantity,
                held.stop_price, held.stop_price,
                "the money mode could not be read; a stop must not be guessed into a destination",
            )
        destination = LIVE_VENUE if money_mode.mode == "live" else PAPER_BOOK
        side = SELL if direction == LONG else BUY
        was = held.quantity
        self._sequence += 1
        new_order_id = f"stop-{venue_id}-{symbol}-{self._sequence}"
        # Place first, cancel second, exactly as a replacement does: two stops
        # briefly is recoverable, no stop briefly is not.
        self._resting[key] = _RestingStop(
            new_order_id, held.stop_price, quantity,
            target_order_id=held.target_order_id, target_price=held.target_price,
            target_quantity=held.target_quantity,
        )
        self.standing.resized_to_the_position += 1
        return self._action(
            venue_id, symbol, RESIZE, destination, new_order_id, held.order_id,
            side, quantity, held.stop_price, held.stop_price,
            f"the position holds {quantity:,.6g} and the stop resting at "
            f"{held.stop_price:g} closed {was:,.6g} of it; re-cut to the whole position",
        )

    def resize_target_to_the_position(
        self,
        venue_id: str,
        symbol: str,
        direction: str,
        quantity: float,
        money_mode,
        quantity_increment: float,
    ) -> StopOrderAction | None:
        """Re-cut a resting target to what the position now holds. None when it fits.

        The target's counterpart to `resize_stop_to_the_position`, and for the
        same reason: a target is placed for whatever was held at the moment it
        was proposed, and every later fill changes that quantity without
        proposing a new one. Unlike the stop, a target has no price-widening
        question -- the price is carried across untouched here too, only the
        quantity moves -- so this is a pure size correction with no refusal
        case beyond "there is nothing to resize."
        """
        key = (venue_id, symbol)
        held = self._resting.get(key)
        if held is None or not held.target_order_id:
            return None
        if quantity <= 0:
            return None
        if held.target_quantity is not None and abs(quantity - held.target_quantity) < quantity_increment:
            return None
        if money_mode is None:
            self.standing.refused_no_mode += 1
            return self._action(
                venue_id, symbol, REFUSED_NO_MODE, "", None, None, "", quantity,
                held.target_price or 0.0, None,
                "the money mode could not be read; a target must not be guessed into a destination",
            )
        destination = LIVE_VENUE if money_mode.mode == "live" else PAPER_BOOK
        side = SELL if direction == LONG else BUY
        was = held.target_quantity
        self._sequence += 1
        new_target_order_id = f"target-{venue_id}-{symbol}-{self._sequence}"
        old_target_order_id = held.target_order_id
        # Place first, cancel second, exactly as the stop's own resize does:
        # two targets briefly resting is recoverable, no target briefly resting
        # leaves the position able to close only on its stop.
        self._resting[key] = _RestingStop(
            held.order_id, held.stop_price, held.quantity,
            target_order_id=new_target_order_id, target_price=held.target_price,
            target_quantity=quantity,
        )
        self.standing.resized_target_to_the_position += 1
        target_price = held.target_price or 0.0
        was_text = "an unknown quantity" if was is None else f"{was:,.6g}"
        return self._action(
            venue_id, symbol, RESIZE_TARGET, destination, new_target_order_id, old_target_order_id,
            side, quantity, target_price, held.target_price,
            f"the position holds {quantity:,.6g} and the target resting at "
            f"{target_price:g} closed {was_text} of it; re-cut to the whole position",
        )

    def resting_quantity(self, venue_id: str, symbol: str) -> float | None:
        """How much the resting stop would close, or None when none is resting."""
        held = self._resting.get((venue_id, symbol))
        return held.quantity if held else None

    def resting_stop(self, venue_id: str, symbol: str) -> float | None:
        held = self._resting.get((venue_id, symbol))
        return held.stop_price if held else None

    def resting_target_quantity(self, venue_id: str, symbol: str) -> float | None:
        """How much the resting target would close, or None when none is resting."""
        held = self._resting.get((venue_id, symbol))
        return held.target_quantity if held else None

    def observe_segment(self, venue_id: str, symbol: str, segment: str) -> None:
        """Whose money is in this position, so its exits can name it.

        Held here rather than beside the manager because every action it
        produces needs it and there are fifteen places that produce one: a map
        kept alongside would be a second source of the same fact, free to
        disagree with this one.
        """
        if segment:
            self._segment_of[(venue_id, symbol)] = segment

    def forget_segment(self, venue_id: str, symbol: str) -> None:
        """Dropped when the position is gone, so the map cannot grow forever."""
        self._segment_of.pop((venue_id, symbol), None)

    def _action(
        self, venue_id, symbol, action, destination, place_id, cancel_id,
        side, quantity, stop_price, previous, reason
    ) -> StopOrderAction:
        return StopOrderAction(
            venue_id=venue_id, symbol=symbol, action=action, destination=destination,
            place_order_id=place_id, cancel_order_id=cancel_id, side=side,
            quantity=quantity, stop_price=stop_price, previous_stop_price=previous,
            reason=reason, decided_at_ns=self._now_ns(),
            segment=self._segment_of.get((venue_id, symbol), ""),
        )


def describe_stop_orders(manager: StopOrderManager, dropped=None) -> dict:
    """The manager's standing, including what never reached it.

    `unreadable_adjustments` is on health because it was not, and that is how a
    shape this part could not read stayed invisible: it is not a refusal, so no
    refusal counter moved, and the only trace was a local dict in start_part. On
    2026-08-26 that hid every one of profit-lock's 34 trailed stops.
    """
    dropped = dropped or {}
    return {
        "part_id": PART_ID,
        "placed": manager.standing.placed,
        "replaced": manager.standing.replaced,
        "refused_widening": manager.standing.refused_widening,
        "resized_to_the_position": manager.standing.resized_to_the_position,
        "resized_target_to_the_position": manager.standing.resized_target_to_the_position,
        "refused_no_position": manager.standing.refused_no_position,
        "refused_exits_for_a_closed_position": (
            manager.standing.refused_exits_for_a_closed_position
        ),
        "refused_no_mode": manager.standing.refused_no_mode,
        "stops_resting": manager.standing.stops_resting,
        "targets_placed": manager.standing.targets_placed,
        "exits_withdrawn": manager.standing.exits_withdrawn,
        "refused_no_target": manager.standing.refused_no_target,
        "restored_symbols": manager.standing.restored_symbols,
        "checkpoint_verdict": manager.standing.checkpoint_verdict,
        "unreadable_adjustments": dropped.get("unreadable", 0),
        "last_unreadable_adjustment": dropped.get("last_unreadable"),
        "adjustments_that_said_hold": dropped.get("held_back", 0),
    }


def run_stop_order_manager(
    manager: StopOrderManager, control_socket, read_adjustments, publish_orders,
    health_interval_seconds: float, emit_health, read_flat_positions=None,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
    write_checkpoint=None,
    dropped=None,
    read_positions_to_cut_stops_to=None,
    quantity_increment: float = 0.0,
) -> int:
    """`read_flat_positions` names the positions that have gone flat this tick.

    Their resting exits are withdrawn, and the withdrawals are published like any
    other order. A cancel that is not sent is a stop still sitting at the venue.

    `write_checkpoint` is called after the orders go out, never before: a
    checkpoint written first would record a stop as resting that no order request
    ever carried, and the next process would believe a position was protected by
    an order nobody sent. That is the same ordering `cost-basis-tracker` uses and
    for the same reason.

    It is called only on a tick that changed something. The manager is woken by
    every `position` message, so checkpointing unconditionally would rewrite the
    file at the position stream's rate to record a set of stops that had not
    moved -- the level-on-every-tick defect, one layer down in the filesystem.
    """
    def tick() -> None:
        actions = []
        # Before anything proposed this tick: a stop resting for less than the
        # position is a stop protecting part of it, and nothing upstream will ever
        # say so -- a fill changes the quantity without proposing a stop price,
        # and a proposal at the unchanged price is refused as a widening.
        if read_positions_to_cut_stops_to is not None:
            for venue_id, symbol, direction, quantity, mode in (
                read_positions_to_cut_stops_to()
            ):
                resize = manager.resize_stop_to_the_position(
                    venue_id=venue_id, symbol=symbol, direction=direction,
                    quantity=quantity, money_mode=mode,
                    quantity_increment=quantity_increment,
                )
                if resize is not None:
                    actions.append(resize)
                resize_target = manager.resize_target_to_the_position(
                    venue_id=venue_id, symbol=symbol, direction=direction,
                    quantity=quantity, money_mode=mode,
                    quantity_increment=quantity_increment,
                )
                if resize_target is not None:
                    actions.append(resize_target)
        for adjustment in read_adjustments():
            target_price = adjustment.pop("target_price", None)
            actions.append(manager.apply_adjustment(**adjustment))
            if target_price is None:
                # A trail moves the stop and says nothing about the target, which
                # stays where the chainer put it. Asking for one anyway counted a
                # refusal for something nobody requested: measured on the live
                # spine at 12:07 on 2026-08-26, 14 trailed stops placed correctly
                # and 14 `refused_no_target` beside them, which reads on health
                # like a protective layer failing while it was working.
                continue
            actions.append(manager.place_target(
                venue_id=adjustment["venue_id"], symbol=adjustment["symbol"],
                direction=adjustment["direction"], quantity=adjustment["quantity"],
                target_price=target_price, money_mode=adjustment["money_mode"],
            ))
        if read_flat_positions is not None:
            for venue_id, symbol in read_flat_positions():
                actions.extend(manager.observe_position_closed(venue_id, symbol))
        actionable = tuple(action for action in actions if action.is_actionable)
        publish_orders(actionable)
        if actionable and write_checkpoint is not None:
            # Resizes count too. They change which order id is resting and for how
            # much, and a checkpoint that ignored them would restore a stop the
            # venue no longer holds and a quantity the position no longer is.
            write_checkpoint(
                manager.standing.placed
                + manager.standing.exits_withdrawn
                + manager.standing.resized_to_the_position
                + manager.standing.resized_target_to_the_position
            )

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_stop_orders(manager, dropped),
    )


# Returned when an adjustment is readable and deliberately not to be sent.
SKIP = object()
# Returned for exits whose position had already closed before they arrived.
ALREADY_CLOSED = object()


def read_adjustment(
    adjustment, held_quantity: dict, is_already_resting=None, closed_at_ns=None,
) -> dict | None:
    """One `stop-adjustment`, whichever of its two shapes it is.

    `stop-adjustment` is one wire carrying two payloads, which is the shape that
    defeats both blueprint checkers -- the same trap `market-data` was, carrying
    trades and candles until `candle` was split out. The two:

    **`exit-order-chainer`** emits the pair of exits the instant an entry fills.
    It names `exit_side`, `quantity`, `stop_price` and `target_price`, because a
    fill names all of them.

    **`profit-lock`** emits a raised stop as a trade goes into profit. It names
    `new_stop` and the position's own `direction`, and it names no quantity at
    all -- trailing a stop does not change how much is held, so it has none to
    state. Its `did_move` says whether the stop actually moved or the lock decided
    to hold it where it was.

    Read wrong, this cost the whole protective layer. Measured on the live spine
    at 12:01 on 2026-08-26, the hour profit-lock first had positions to work on:
    it trailed 34 stops and published all 34, and this part dropped every one as
    unreadable -- `placed` 0, and not one refusal counter moved, because a shape
    it cannot read was never a refusal. 20 open positions, 0 stops resting.

    The quantity for a trail comes from the position itself, which this part is
    already tracking from the `position` stream. Taking it from there rather than
    inventing one is what makes the trailed stop cover what is actually held: a
    stop for a quantity nobody holds is either a naked short when it fills or a
    position still exposed after it does.

    Returns None when the shape is unreadable, SKIP when it is readable and says
    not to send, and the order otherwise.
    """
    venue_id = getattr(adjustment, "venue_id", None)
    symbol = getattr(adjustment, "symbol", None)
    if venue_id is None or symbol is None:
        return None

    exit_side = getattr(adjustment, "exit_side", None)
    stop_price = getattr(adjustment, "stop_price", None)
    if exit_side is not None and stop_price is not None:
        if not getattr(adjustment, "should_be_sent", True):
            return SKIP
        # **Exits for a position that has already closed are not placed**
        # (2026-09-13). An entry and its target can fill in the same second;
        # `fill-reconciler` then reports the position flat, and that can reach
        # this part before `exit-order-chainer`'s exits for the entry do. Seeing
        # flat with nothing resting, the part withdrew nothing -- then placed the
        # stop for a position that no longer existed, and the stop fired on
        # nothing. Measured on 2026-09-08: NIFTY 23700 CE 15 SEP 26 closed by its
        # target at 09:31:36, and its stop sold 1,430.081 at 09:33:20, which is
        # the phantom long `position-close-detector` then held. Both times are fill
        # times, so the comparison does not depend on which part ran first.
        entry_filled_at_ns = int(getattr(adjustment, "entry_filled_at_ns", 0) or 0)
        key = (venue_id, symbol)
        if (
            entry_filled_at_ns
            and closed_at_ns is not None
            and key not in held_quantity
            and closed_at_ns.get(key, -1) >= entry_filled_at_ns
        ):
            return ALREADY_CLOSED
        return {
            "venue_id": venue_id,
            "symbol": symbol,
            # The exit is the opposite side of the position, so the position's
            # own direction is the opposite of the exit's side.
            "direction": LONG if exit_side == SELL else SHORT,
            "quantity": getattr(adjustment, "quantity", None),
            "stop_price": stop_price,
            "target_price": getattr(adjustment, "target_price", None),
        }

    # The lock's shape: a new stop for a position whose direction it states.
    new_stop = getattr(adjustment, "new_stop", None)
    direction = getattr(adjustment, "direction", None)
    if new_stop is None or direction not in (LONG, SHORT):
        return None
    stop_is_resting = (
        is_already_resting is not None and is_already_resting(venue_id, symbol)
    )
    if not getattr(adjustment, "did_move", False) and stop_is_resting:
        # The lock looked and left the stop where it was, and this part already
        # has one resting on that position. Sending it again would replace a
        # resting order with an identical one, and every replace is a window in
        # which the position is unprotected.
        #
        # Only when one is actually resting. Since 2026-08-26 profit-lock states
        # every open position's stop on a cadence rather than only when it moves
        # -- a stop is a level -- so "did not move" now arrives for positions
        # that have no stop at all, and skipping those is how a position stays
        # naked forever. Measured that day: 12 open positions, 0 stops resting,
        # 4,982 adjustments received here and not one order placed, with every
        # refusal counter at zero because nothing was ever refused.
        return SKIP
    quantity = held_quantity.get((venue_id, symbol))
    if not quantity:
        # A trail for a position this part has not been told about. Refused
        # rather than sized at zero: a stop for no quantity protects nothing and
        # would read as a stop that is resting.
        return None
    return {
        "venue_id": venue_id,
        "symbol": symbol,
        "direction": direction,
        "quantity": abs(quantity),
        "stop_price": new_stop,
        # A trail moves the stop and says nothing about the target, which stays
        # where the chainer put it.
        "target_price": None,
    }


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
    from runtime.input_assembly import Batch, LatestByKey, level_for_segment

    adjustments = Batch(read=context.bus.reader("stop-adjustment"))
    positions = Batch(read=context.bus.reader("position"))
    # One money mode per segment (2026-09-05), read for the segment the position
    # belongs to. A stop is placed long after the decision that opened the
    # position, so the segment travels on the position itself -- and this part
    # sends no order at all without a mode, which for an exit means a real
    # position left unprotected. That is why it is read per position rather than
    # per spine.
    modes = LatestByKey(
        read=context.bus.reader("money-mode"),
        key_of=lambda mode: mode.segment,
        maximum_age_seconds=context.number("money_mode_maximum_age_seconds"),
    )
    publish_orders = context.bus.publisher_for("order-request")

    # Positions seen flat since the last tick. Held here rather than asked of the
    # manager, because "this position just closed" is a fact about the position
    # stream and the manager's job starts once it is known.
    gone_flat: list[tuple[str, str]] = []
    held_quantity: dict[tuple[str, str], float] = {}
    # Which way each held position is held, beside how much of it. The resize
    # pass needs both to know which side an exit order sits on.
    held_direction: dict[tuple[str, str], str] = {}
    # Which segment's money each held position is, so the exit reads that
    # segment's mode. Kept beside the quantity for the same reason it is: the
    # position stream is where this part learns what it protects.
    held_segment: dict[tuple[str, str], str] = {}
    unreadable = {"count": 0, "last": None}
    # Adjustments read fine and deliberately not sent: a lock that decided to hold
    # the stop where it was. A different fact from one this part could not read,
    # and both belong on health rather than in a local counter nobody can see.
    held_back = {"count": 0}

    class _Dropped(dict):
        """A live view of the two counters, read fresh each time health is built."""

        def get(self, name, default=None):
            if name == "unreadable":
                return unreadable["count"]
            if name == "last_unreadable":
                return unreadable["last"]
            if name == "held_back":
                return held_back["count"]
            return default

    dropped = _Dropped()
    # When each position was last reported flat, on the fill's own clock
    # (`Position.updated_at_ns` is the closing fill's `filled_at_ns`). See
    # ALREADY_CLOSED in `read_adjustment`.
    closed_at_ns: dict[tuple[str, str], int] = {}

    def read_adjustments():
        mode_by_segment = modes.mapping()
        for position in positions.payloads():
            key = (position.venue_id, position.symbol)
            was_held = held_quantity.get(key, 0.0)
            if position.is_flat:
                closed_at_ns[key] = max(
                    closed_at_ns.get(key, 0), int(getattr(position, "updated_at_ns", 0) or 0)
                )
                if was_held:
                    gone_flat.append(key)
                held_quantity.pop(key, None)
                held_direction.pop(key, None)
                held_segment.pop(key, None)
                manager.forget_segment(*key)
            else:
                held_quantity[key] = position.quantity
                held_direction[key] = position.direction
                held_segment[key] = getattr(position, "segment", "")
                # The manager stamps every action it produces with this, so the
                # exit order names whose money closes the position.
                manager.observe_segment(*key, getattr(position, "segment", ""))

        readable = []
        for adjustment in adjustments.payloads():
            read = read_adjustment(
                adjustment, held_quantity, manager.is_stop_resting, closed_at_ns
            )
            if read is ALREADY_CLOSED:
                manager.standing.refused_exits_for_a_closed_position += 1
                continue
            if read is None:
                unreadable["count"] += 1
                unreadable["last"] = type(adjustment).__name__
                continue
            if read is SKIP:
                held_back["count"] += 1
                continue
            read["money_mode"] = level_for_segment(
                mode_by_segment,
                held_segment.get((adjustment.venue_id, adjustment.symbol), ""),
            )
            readable.append(read)
        return readable

    def read_flat_positions():
        closed = tuple(gone_flat)
        gone_flat.clear()
        return closed

    def read_positions_to_cut_stops_to():
        """Every held position, every tick -- not the ones seen to change.

        The invariant is that a resting stop closes the whole position it
        protects, and an invariant is checked, not triggered. A change-triggered
        version resized on one sample and never looked again: on 2026-08-28 it
        cut `binance-usdm|AKEUSDT`'s stop to 57,735.536 while the book held
        177,120.635 and, having seen no further change, left it there. Checked
        every tick, a wrong sample is corrected by the next right one.

        This costs nothing when nothing is wrong: `resize_stop_to_the_position`
        returns None for a position with no stop resting and for one already
        within a quantity step of its stop, which is every position almost always.
        """
        mode_by_segment = modes.mapping()
        # `Position.quantity` is signed -- negative is short -- and an order's
        # quantity is not. `read_adjustment` already takes the absolute value for
        # the same reason; a resize that forgot to would refuse every short as
        # having no position to protect.
        return tuple(
            (
                venue_id,
                symbol,
                held_direction.get((venue_id, symbol), ""),
                abs(quantity),
                level_for_segment(
                    mode_by_segment, held_segment.get((venue_id, symbol), "")
                ),
            )
            for (venue_id, symbol), quantity in held_quantity.items()
        )

    def publish_as_order_requests(actions) -> None:
        publish_orders(tuple(as_order_request(action) for action in actions))

    # What is resting, carried across a restart. Until 2026-08-26 this was memory
    # alone: every restart forgot every stop, and because this part only ever
    # hears about a stop when something upstream proposes a new one, a position
    # already open was then unprotected with nothing reporting it. The spine had
    # restarted 46 times by the day the lot books were fixed for the same reason.
    #
    # Beside the lot books, under position_state_root, because it is the same
    # fact about the same position and a board reading one should not have to
    # look somewhere else for the other.
    manager = StopOrderManager()
    store = DurableStateStore(
        pathlib.Path(str(context.setting("position_state_root").value)).expanduser()
    )
    store.root.mkdir(parents=True, exist_ok=True)
    write_checkpoint = restore_and_arm_checkpoint(
        store,
        # Every change, not every N: a resting stop changes when a position opens
        # or closes, which is tens of times an hour, and losing one costs a
        # position its protection. That is the same reasoning the lot books use
        # and the opposite of the price series, which changes hundreds of times a
        # second and costs nothing to lose a second of.
        CheckpointSchedule(1),
        PART_ID,
        CHECKPOINT_COMPONENT,
        manager,
        {},
    )

    return run_stop_order_manager(
        manager=manager,
        control_socket=context.control_socket,
        read_adjustments=read_adjustments,
        publish_orders=publish_as_order_requests,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
        read_flat_positions=read_flat_positions,
        read_positions_to_cut_stops_to=read_positions_to_cut_stops_to,
        # The same step the sizer and the bounds gate snap every order to. Below
        # one of these a stop and its position differ by an amount no order could
        # correct, so re-cutting would churn an order per fill and change nothing.
        quantity_increment=context.number("order_quantity_increment"),
        write_checkpoint=write_checkpoint,
        # Live views of the two local counters, so health reports what never
        # reached the manager rather than only what did.
        dropped=dropped,
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
        # Whose money closes this position. Without it the book resolves no
        # money mode on a spine with more than one segment and refuses the exit
        # as a live order, which leaves a real position with no stop.
        segment=action.segment,
    )
