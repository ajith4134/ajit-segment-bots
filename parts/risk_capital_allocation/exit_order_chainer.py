"""exit-order-chainer: emit the stop and target the instant the entry fills.

The window this closes is small and expensive. Between an entry filling and its
stop being placed, the position is naked -- and that is exactly the moment a fast
move is most likely, because the fill happened for a reason.

Nothing may wait on a position appearing in a state store, on a reconciler
running, or on a poll. The fill itself is the trigger: it names the quantity and
the price, and the plan was made before the entry was ever sent.

Two properties it must have:

- **Idempotent on the fill id**, because a venue re-sending a fill would
  otherwise place a second stop and a second target, and a doubled stop closes
  twice the position that exists.
- **Correct on partial fills.** A partial fill gets exits for the quantity that
  actually filled, and the next partial adds to them. Exits sized to the order
  rather than the fill would leave a stop for a position never taken -- which
  opens the opposite position when it triggers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trading_types import BUY, SELL

PART_ID = "exit-order-chainer"

PART_DECLARATION = PartDeclaration(
    part_id="exit-order-chainer",
    consumes=("stop-target-plan", "fill"),
    produces=("stop-adjustment", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

CHAINED = "chained"
EXTENDED = "extended-for-a-further-partial-fill"
NO_PLAN = "no-plan-for-this-order"
DUPLICATE_FILL = "duplicate-fill-ignored"


@dataclass(frozen=True)
class ExitOrders:
    """The stop and target that must exist the moment an entry fills."""

    venue_id: str
    symbol: str
    entry_order_id: str
    exit_side: str
    quantity: float
    stop_price: float
    target_price: float | None
    outcome: str
    filled_quantity_so_far: float
    reason: str
    chained_at_ns: int

    @property
    def should_be_sent(self) -> bool:
        return self.outcome in (CHAINED, EXTENDED) and self.quantity > 0


@dataclass
class ChainerStanding:
    plans_held: int = 0
    fills_seen: int = 0
    chained: int = 0
    extended: int = 0
    duplicates_ignored: int = 0
    fills_without_a_plan: int = 0
    naked_positions_prevented: int = 0
    # Which price the exits were computed against. A plan carrying no reference
    # price can only use the absolute numbers from decision time, and that is a
    # different claim about the trade -- so the two are counted apart.
    exits_repriced_onto_the_fill: int = 0
    exits_from_the_decision_price: int = 0


class ExitOrderChainer:
    """Turns an entry fill straight into the exits its plan already specified."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        # Keyed by what a stop-target plan actually identifies: a venue, a symbol
        # and a side. Not by an order id -- the plan is made before an order is
        # stamped, so a plan filed under an order id could only ever be filed
        # under an id nobody had assigned yet.
        # Held as *distances* from the price the plan was made at, not as absolute
        # prices. A plan says "stop 1.5% below entry"; which price that is depends
        # on the price actually obtained, and until 2026-08-23 the absolute price
        # from decision time was sent instead. Measured on the live run at
        # 10:25:15: an exit computed around 0.1707 was placed against a market at
        # 0.18043, already through its trigger, so it fired on arrival and closed
        # the position one second after opening it for zero profit and two lots of
        # fees.
        self._plans: dict[tuple[str, str, str], tuple[float, float | None]] = {}
        self._filled: dict[str, float] = {}
        self._seen_fills: set[str] = set()
        self.standing = ChainerStanding()

    def register_plan(
        self,
        venue_id: str,
        symbol: str,
        entry_side: str,
        stop_price: float,
        target_price: float | None,
        entry_price: float | None = None,
    ) -> None:
        """Hold the exits for a position before its entry is sent, as distances.

        Before, not after: a plan registered on the fill would be a plan made
        while the position was already naked.

        `entry_price` is the price the plan was computed against. With it, the
        stop and target are kept as fractions of that price and re-priced against
        whatever the entry actually fills at -- which is the only version that
        survives the market moving between the decision and the fill. Without it
        the absolute prices are kept, which is what a caller with no reference
        price is asking for and is recorded as such.
        """
        stop_fraction = target_fraction = None
        if entry_price and entry_price > 0:
            stop_fraction = (entry_price - stop_price) / entry_price
            if target_price:
                target_fraction = (target_price - entry_price) / entry_price
        self._plans[(venue_id, symbol, entry_side)] = (
            stop_price, target_price, stop_fraction, target_fraction
        )
        self.standing.plans_held = len(self._plans)

    def _exit_prices(self, plan, fill_price: float | None) -> tuple:
        """The stop and target for this fill, re-priced onto what it cost.

        A stop 1.5% below entry means 1.5% below the price actually obtained. The
        absolute prices from decision time are used only when the plan carried no
        reference price to take a fraction of, and that case is counted.
        """
        stop_price, target_price, stop_fraction, target_fraction = plan
        if fill_price and fill_price > 0 and stop_fraction is not None:
            repriced_stop = fill_price * (1.0 - stop_fraction)
            repriced_target = (
                fill_price * (1.0 + target_fraction) if target_fraction is not None else None
            )
            self.standing.exits_repriced_onto_the_fill += 1
            return repriced_stop, repriced_target
        self.standing.exits_from_the_decision_price += 1
        return stop_price, target_price

    def observe_entry_fill(
        self,
        fill_id: str,
        entry_order_id: str,
        venue_id: str,
        symbol: str,
        entry_side: str,
        filled_quantity: float,
        fill_price: float | None = None,
    ) -> ExitOrders | None:
        """One entry fill; returns the exits that must now exist for it.

        `fill_price` is what the entry actually cost. The exits are priced against
        it rather than against the price the plan was made at, because those are
        not the same number the moment the market moves -- and on 2026-08-23 they
        differed by six per cent, which put both exits through their triggers
        before they were ever placed.
        """
        self.standing.fills_seen += 1

        if fill_id in self._seen_fills:
            self.standing.duplicates_ignored += 1
            return None
        self._seen_fills.add(fill_id)

        plan = self._plans.get((venue_id, symbol, entry_side))
        if plan is None:
            self.standing.fills_without_a_plan += 1
            return ExitOrders(
                venue_id=venue_id, symbol=symbol, entry_order_id=entry_order_id,
                exit_side="", quantity=0.0, stop_price=0.0, target_price=None,
                outcome=NO_PLAN, filled_quantity_so_far=filled_quantity,
                reason=(
                    f"a fill arrived for {entry_order_id} on {symbol} with no stop-target plan "
                    f"held for a {entry_side}; the position is naked and nothing here can size "
                    f"its exits"
                ),
                chained_at_ns=self._now_ns(),
            )

        stop_price, target_price = self._exit_prices(plan, fill_price)
        already = self._filled.get(entry_order_id, 0.0)
        self._filled[entry_order_id] = already + filled_quantity
        outcome = EXTENDED if already > 0 else CHAINED
        if outcome == CHAINED:
            self.standing.chained += 1
            self.standing.naked_positions_prevented += 1
        else:
            self.standing.extended += 1

        return ExitOrders(
            venue_id=venue_id,
            symbol=symbol,
            entry_order_id=entry_order_id,
            # The exit is the other side of the entry, always.
            exit_side=SELL if entry_side == BUY else BUY,
            quantity=filled_quantity,
            stop_price=stop_price,
            target_price=target_price,
            outcome=outcome,
            filled_quantity_so_far=self._filled[entry_order_id],
            reason=(
                f"{filled_quantity:g} filled"
                + (f" (adding to {already:g} already filled)" if already > 0 else "")
                + f"; exits sized to what actually filled, not to the order"
            ),
            chained_at_ns=self._now_ns(),
        )

    def forget_position(self, venue_id: str, symbol: str, entry_side: str) -> None:
        """Drop a closed position's plan so it cannot chain exits again."""
        self._plans.pop((venue_id, symbol, entry_side), None)
        self.standing.plans_held = len(self._plans)

    def filled_quantity(self, entry_order_id: str) -> float:
        return self._filled.get(entry_order_id, 0.0)


def describe_chaining(chainer: ExitOrderChainer) -> dict:
    return {
        "part_id": PART_ID,
        "plans_held": chainer.standing.plans_held,
        "fills_seen": chainer.standing.fills_seen,
        "chained": chainer.standing.chained,
        "extended_for_partials": chainer.standing.extended,
        "duplicate_fills_ignored": chainer.standing.duplicates_ignored,
        "fills_without_a_plan": chainer.standing.fills_without_a_plan,
        "naked_positions_prevented": chainer.standing.naked_positions_prevented,
    }


def run_exit_order_chainer(
    chainer: ExitOrderChainer, control_socket, read_plans_and_fills, publish_exits,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        plans, fills = read_plans_and_fills()
        for plan in plans:
            chainer.register_plan(**plan)
        exits = [chainer.observe_entry_fill(**fill) for fill in fills]
        publish_exits(tuple(exit_orders for exit_orders in exits if exit_orders is not None))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_chaining(chainer),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The part that closes the window between an entry filling and its stop
    existing. It holds each plan by the position it is for, and the instant a fill
    for that position arrives it emits the exits sized to what actually filled.

    A fill with no plan held is emitted as NO_PLAN rather than dropped. That is a
    naked position, and it must be visible: an exit chainer that silently produced
    nothing would look identical to one with nothing to do.
    """
    from runtime.input_assembly import Batch

    plans = Batch(read=context.bus.reader("stop-target-plan"))
    fills = Batch(read=context.bus.reader("fill"))
    publish_exits = context.bus.publisher_for("stop-adjustment")

    # Which side each symbol's plan was made for, so a fill can find its plan.
    # A fill names its own side, and for an entry that side is the plan's side --
    # an exit fill arrives on the opposite side and finds no plan, which is
    # exactly right: exits do not chain exits.
    def read_plans_and_fills():
        registered = tuple(
            {
                "venue_id": plan.venue_id,
                "symbol": plan.symbol,
                "entry_side": plan.side,
                "stop_price": plan.stop_price,
                "target_price": plan.target_price,
                # What the plan was computed against, so its stop and target can
                # be kept as distances and re-priced onto the fill.
                "entry_price": plan.entry_price,
            }
            for plan in plans.payloads()
            if plan.is_placeable
        )
        seen_fills = tuple(
            {
                "fill_id": fill.fill_id,
                "entry_order_id": fill.order_id or fill.fill_id,
                "venue_id": fill.venue_id,
                "symbol": fill.symbol,
                "entry_side": fill.side,
                "filled_quantity": fill.quantity,
                "fill_price": fill.price,
            }
            for fill in fills.payloads()
        )
        return registered, seen_fills

    return run_exit_order_chainer(
        chainer=ExitOrderChainer(),
        control_socket=context.control_socket,
        read_plans_and_fills=read_plans_and_fills,
        publish_exits=publish_exits,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
