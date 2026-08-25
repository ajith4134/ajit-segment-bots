"""position-sizer: one trade's size, from intent, stop, leverage and the tightest limit.

The part every risk limiter exists to constrain. It answers one question: given
that this trade may lose down to its stop, how large may it be so that the loss
costs no more than the smallest limit allows.

Sized from the **stop distance**, not from a fixed fraction of the balance. A
fixed fraction risks a different amount on every trade, because the same position
in a symbol with a wide stop loses many times what it loses in one with a narrow
stop. Sizing from the distance to the stop is what makes "risk 1% per trade"
actually mean 1%.

Three refusals and one adjustment, in that order:

- **Refuse** when the limit is zero, when the stop is on the wrong side of entry,
  or when the smallest tradeable size already risks more than allowed.
- **Shrink** rather than refuse when the size merely exceeds a bound: the
  blueprint asks for the largest affordable size, because a trade at 80% of the
  intended size is a trade, and a refusal is not.

Fees are charged against the risk budget and the size is re-solved until they
fit, because a size computed before fees is a size that breaches its own limit by
exactly the fees.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.risk_types import NO_RISK_ALLOWED
from runtime.trading_types import BUY, LONG, SELL, SHORT, order_side_for

PART_ID = "position-sizer"

PART_DECLARATION = PartDeclaration(
    part_id="position-sizer",
    consumes=(
        "trade-intent", "instrument-choice", "leverage-choice", "stop-target-plan",
        "account-balance", "risk-limit", "slippage-profile", "locked-allocation",
        "price-increment", "size-hint", "timed-intent",
    ),
    produces=("sized-order", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

SIZED = "sized"
REFUSED_NO_LIMIT = "refused-no-risk-allowed"
REFUSED_STOP_INVALID = "refused-stop-on-the-wrong-side"
REFUSED_TOO_SMALL = "refused-smallest-size-risks-too-much"
REFUSED_NO_INCREMENT = "refused-no-price-increment"
REFUSED_NO_FREE_CAPITAL = "refused-free-capital-funds-nothing-tradeable"
SHRUNK_TO_FIT = "shrunk-to-fit"

# How many times the size is re-solved against fees before giving up. Each pass
# converges quickly because fees are a small fraction of risk; the bound exists
# so a pathological input cannot loop.
FEE_SOLVE_PASSES = 8

# The whole of the risk the binding limit allows. A size hint scales what may be
# risked and cannot scale past it: the quantity solved above is already the
# largest whose loss at the stop stays inside the limit.
FULL_RISK_BUDGET = 1.0


@dataclass(frozen=True)
class SizedOrder:
    """One order's size, and every figure that produced it."""

    venue_id: str
    symbol: str
    side: str
    quantity: float
    entry_price: float
    stop_price: float
    outcome: str
    risk_allowed: float
    risk_at_stop: float
    fees_charged: float
    notional: float
    leverage: float
    reason: str
    sized_at_ns: int
    # The decision this order serves. Carried so the stamper can give every
    # order for one decision the same id, which is what makes a republished
    # intent one order rather than one order per tick.
    intent_id: str = ""

    @property
    def is_tradeable(self) -> bool:
        return self.outcome in (SIZED, SHRUNK_TO_FIT) and self.quantity > 0


@dataclass
class SizerStanding:
    sized: int = 0
    shrunk: int = 0
    refused_no_limit: int = 0
    refused_stop_invalid: int = 0
    refused_too_small: int = 0
    refused_no_increment: int = 0
    refused_no_free_capital: int = 0
    # Hints asking for more than the risk limit allows, which are clipped to it.
    # A hint the desk could not honour is a fact about the brain that wrote it.
    hints_above_the_risk_ceiling: int = 0
    # Orders the risk budget would have allowed and the free balance would not.
    # Counted apart from the shrinks risk caused: a bot cut short by its own
    # committed capital is a bot that needs fewer orders in flight, and a bot cut
    # short by its stop distance is a different fact with a different answer.
    shrunk_by_free_capital: int = 0
    largest_risk_taken: float = 0.0
    # Intents that said stand aside. Not a refusal by this part -- the decision
    # was made upstream -- but counted, because a bot whose every opinion is a
    # stand-aside and a bot with no opinions look the same from here.
    stood_aside: int = 0


class PositionSizer:
    """Solves the largest size whose loss at the stop stays inside the allowed risk."""

    def __init__(self, taker_fee_rate: float, slippage_fraction: float, now_ns=time.time_ns) -> None:
        if taker_fee_rate < 0 or slippage_fraction < 0:
            raise ValueError("a fee or slippage allowance cannot be negative")
        self._fee_rate = taker_fee_rate
        self._slippage = slippage_fraction
        self._now_ns = now_ns
        self.standing = SizerStanding()

    def size(
        self,
        venue_id: str,
        symbol: str,
        side: str,
        entry_price: float,
        stop_price: float,
        allotment: float,
        risk_limit_fraction: float,
        leverage: float,
        price_increment: float | None,
        quantity_increment: float,
        minimum_quantity: float,
        maximum_quantity: float | None = None,
        size_multiple: float | None = None,
        free_capital: float | None = None,
        intent_id: str = "",
    ) -> SizedOrder:
        if risk_limit_fraction <= NO_RISK_ALLOWED:
            self.standing.refused_no_limit += 1
            return self._refusal(
                venue_id, symbol, side, entry_price, stop_price, REFUSED_NO_LIMIT, leverage,
                "the binding risk limit allows nothing to be risked", intent_id,
            )

        if price_increment is None or price_increment <= 0:
            # Without the increment the entry cannot be snapped to a price the
            # venue accepts, and an order at an invalid price is rejected after
            # the decision has already been made.
            self.standing.refused_no_increment += 1
            return self._refusal(
                venue_id, symbol, side, entry_price, stop_price, REFUSED_NO_INCREMENT, leverage,
                "no price increment is known for this symbol", intent_id,
            )

        entry = self._snap_price(entry_price, price_increment, side)
        stop = self._snap_price(stop_price, price_increment, side)
        if (side == BUY and stop >= entry) or (side != BUY and stop <= entry):
            self.standing.refused_stop_invalid += 1
            return self._refusal(
                venue_id, symbol, side, entry, stop, REFUSED_STOP_INVALID, leverage,
                f"a {side} stop at {stop} is on the wrong side of an entry at {entry}", intent_id,
            )

        # The loss per unit if the stop is hit, including the slippage past it
        # that a stop actually suffers -- a stop is a trigger, not a guarantee.
        loss_per_unit = abs(entry - stop) * (1.0 + self._slippage)
        risk_allowed = allotment * risk_limit_fraction

        quantity = risk_allowed / loss_per_unit
        fees = 0.0
        for _ in range(FEE_SOLVE_PASSES):
            fees = self._fees_for(quantity, entry, stop)
            budget = risk_allowed - fees
            if budget <= 0:
                break
            next_quantity = budget / loss_per_unit
            if abs(next_quantity - quantity) <= quantity_increment / 2:
                quantity = next_quantity
                break
            quantity = next_quantity

        if size_multiple is not None and size_multiple > 0:
            # A hint is a multiple of a normal size, not a quantity. It was read as
            # a quantity from a field SizeHint has never carried until 2026-08-25,
            # so `getattr(hint, "quantity", None)` returned None on every intent
            # and no conviction has ever changed a size -- the silent half of the
            # same defect that crashed this part on locked-allocation.
            #
            # Clipped at one, and the clipping is counted rather than hidden: the
            # quantity above is already the largest the risk limit allows, so a
            # hint of 2x is a hint to breach it. Conviction may size a trade down
            # inside the limit and may never size it past one.
            if size_multiple > FULL_RISK_BUDGET:
                self.standing.hints_above_the_risk_ceiling += 1
            quantity *= min(size_multiple, FULL_RISK_BUDGET)
        if maximum_quantity is not None:
            quantity = min(quantity, maximum_quantity)

        # Capital already locked against orders in flight is capital this order
        # cannot spend. Risk and capital are different limits and both bind: the
        # risk budget says how much this trade may lose, and the free balance says
        # whether the account can pay for it at all. Sized against equity alone,
        # two decisions in one tick are both sized against the same money, and the
        # second is only discovered when fund-lock-ledger refuses its lock -- after
        # the order exists. Shrinking here turns that refusal into a smaller order,
        # which is the choice this part makes everywhere else.
        affordable = quantity
        if free_capital is not None:
            affordable = min(quantity, self._quantity_free_capital_funds(free_capital, leverage, entry))

        snapped = self._snap_quantity(affordable, quantity_increment)
        outcome = SIZED if snapped >= quantity - quantity_increment else SHRUNK_TO_FIT
        if outcome == SHRUNK_TO_FIT and affordable < quantity:
            self.standing.shrunk_by_free_capital += 1

        if snapped < minimum_quantity:
            if affordable < quantity and self._snap_quantity(quantity, quantity_increment) >= minimum_quantity:
                # The risk budget would have funded a tradeable size and the free
                # balance would not. Said as its own refusal, because "too small"
                # points at the stop and this points at capital already committed.
                self.standing.refused_no_free_capital += 1
                return self._refusal(
                    venue_id, symbol, side, entry, stop, REFUSED_NO_FREE_CAPITAL, leverage,
                    f"{free_capital:,.2f} free at {leverage:g}x funds {affordable:g}, "
                    f"below the smallest tradeable size of {minimum_quantity:g}",
                    intent_id,
                )
            smallest_risk = minimum_quantity * loss_per_unit + self._fees_for(
                minimum_quantity, entry, stop
            )
            self.standing.refused_too_small += 1
            return self._refusal(
                venue_id, symbol, side, entry, stop, REFUSED_TOO_SMALL, leverage,
                f"the smallest tradeable size of {minimum_quantity:g} would risk "
                f"{smallest_risk:,.2f} against {risk_allowed:,.2f} allowed",
                intent_id,
            )

        risk_at_stop = snapped * loss_per_unit + self._fees_for(snapped, entry, stop)
        self.standing.sized += 1
        if outcome == SHRUNK_TO_FIT:
            self.standing.shrunk += 1
        self.standing.largest_risk_taken = max(self.standing.largest_risk_taken, risk_at_stop)

        return SizedOrder(
            venue_id=venue_id,
            symbol=symbol,
            side=side,
            quantity=snapped,
            entry_price=entry,
            stop_price=stop,
            outcome=outcome,
            risk_allowed=risk_allowed,
            risk_at_stop=risk_at_stop,
            fees_charged=self._fees_for(snapped, entry, stop),
            notional=snapped * entry,
            leverage=leverage,
            # The decision this order serves. Only refusals carried it until
            # 2026-08-23: every sized order reached the stamper with an empty
            # intent id, the stamper fell back to hashing quantity and price, both
            # drift with the market, and one standing AAVEUSDT decision became
            # seven orders and seven fills on the run of 12:08.
            intent_id=intent_id,
            reason=(
                f"{snapped:g} risks {risk_at_stop:,.2f} of {risk_allowed:,.2f} allowed, "
                f"stopping {abs(entry - stop):g} away"
            ),
            sized_at_ns=self._now_ns(),
        )

    def _quantity_free_capital_funds(self, free_capital: float, leverage: float, entry: float) -> float:
        """The largest position the unlocked balance pays the margin for.

        Leverage is what makes this a different number from the notional: a 10x
        position of 1,000 costs 100 of the balance, and refusing to see that would
        cap every levered trade at its unlevered size.
        """
        if entry <= 0 or free_capital <= 0:
            return 0.0
        return free_capital * leverage / entry

    def _fees_for(self, quantity: float, entry: float, stop: float) -> float:
        """Both sides charged: an entry that stops out pays to get in and to get out."""
        return quantity * (entry + stop) * self._fee_rate

    def _snap_price(self, price: float, increment: float, side: str) -> float:
        """To a price the venue accepts, and never in the direction that adds risk.

        A buy entry rounds down and a buy stop rounds down too: the first cannot
        pay more than intended, and the second cannot sit closer than intended.
        """
        steps = price / increment
        rounded = math.floor(steps) if side == BUY else math.ceil(steps)
        return round(rounded * increment, 12)

    def _snap_quantity(self, quantity: float, increment: float) -> float:
        if increment <= 0:
            return quantity
        return round(math.floor(quantity / increment) * increment, 12)

    def _refusal(
        self, venue_id, symbol, side, entry, stop, outcome, leverage, reason, intent_id=""
    ) -> SizedOrder:
        return SizedOrder(
            venue_id=venue_id, symbol=symbol, side=side, quantity=0.0,
            entry_price=entry, stop_price=stop, outcome=outcome,
            risk_allowed=0.0, risk_at_stop=0.0, fees_charged=0.0, notional=0.0,
            leverage=leverage, reason=reason, sized_at_ns=self._now_ns(),
            intent_id=intent_id,
        )


def free_capital_from_locks(locks) -> float | None:
    """What fund-lock-ledger last said is unlocked, or None if it has never said.

    The newest decision wins rather than the smallest, because a free balance is a
    level and not a total: the ledger owns the arithmetic of what is held against
    what, and re-deriving it here from the locks this part happens to have seen
    would be a second answer free to disagree with the one the ledger refuses
    against. A released lock is a decision like any other, so the balance rises
    again the moment the ledger says it has.

    **None is not zero.** Nothing locked and nothing said are different facts, and
    reading silence as a free balance of zero would refuse every order the first
    time this part started before the ledger did.
    """
    newest = None
    for lock in locks:
        if newest is None or lock.decided_at_ns > newest.decided_at_ns:
            newest = lock
    return None if newest is None else newest.free_balance_after


def entry_price_for(plan, instrument) -> float | None:
    """What to size against, or None when nothing here is a price.

    A stop-target-plan refines the stop against clusters and measured excursions,
    and stop-target-placer refuses to make one until it has a volatility forecast
    or excursion history -- neither of which exists before any trade has closed.
    The intent already carries the stop the bot's own exit plan proposed, and the
    instrument choice carries what the symbol last traded at, so both numbers are
    available from inputs this part declares. The plan is preferred when it
    exists, because it is the refined one.

    **A choice that chose nothing lends nothing.** Its reference price is evidence
    about the refusal -- what the symbol last printed, when the part that knows
    about instruments declined to name one -- and not an entry. Until 2026-08-24
    it was read as an entry anyway, so a symbol whose instrument was unlisted, or
    in an unbuilt segment, or whose last price was too old to believe, still
    produced a sized order. Read by shape rather than by import, because this part
    knows the data it consumes and not the part that produces it (T-4).
    """
    refined = getattr(plan, "entry_price", None)
    if refined:
        return refined
    if instrument is None or not getattr(instrument, "is_actionable", False):
        return None
    return getattr(instrument, "reference_price", None)


def describe_sizing(sizer: PositionSizer) -> dict:
    return {
        "part_id": PART_ID,
        "sized": sizer.standing.sized,
        "shrunk_to_fit": sizer.standing.shrunk,
        "refused_no_risk_allowed": sizer.standing.refused_no_limit,
        "refused_stop_invalid": sizer.standing.refused_stop_invalid,
        "refused_too_small": sizer.standing.refused_too_small,
        "refused_no_price_increment": sizer.standing.refused_no_increment,
        "refused_no_free_capital": sizer.standing.refused_no_free_capital,
        "hints_above_the_risk_ceiling": sizer.standing.hints_above_the_risk_ceiling,
        "shrunk_by_free_capital": sizer.standing.shrunk_by_free_capital,
        "largest_risk_taken": sizer.standing.largest_risk_taken,
        "intents_that_stood_aside": sizer.standing.stood_aside,
    }


def run_position_sizer(
    sizer: PositionSizer, control_socket, read_intents, publish_sized_orders,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_sized_orders(tuple(sizer.size(**intent) for intent in read_intents()))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_sizing(sizer),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Eleven declared inputs, and a size cannot be computed without four of them: the
    intent, the plan that says where the stop goes, the allotment the risk is a
    fraction of, and the binding risk limit. An intent missing any of those is not
    sized -- it is left, and the reason is in the standing. Sizing a position
    against a missing risk limit would be sizing it against no limit at all.

    The quantity increment is a single global setting and the coarsest number in
    the system: every venue publishes a per-symbol lot size and nothing consumes it
    yet. It is named as temporary where it is set.
    """
    from runtime.input_assembly import Batch, LatestByKey

    intents = Batch(read=context.bus.reader("trade-intent"))
    publish_sized_orders = context.bus.publisher_for("sized-order")

    def by_symbol(data_type: str) -> LatestByKey:
        return LatestByKey(
            read=context.bus.reader(data_type),
            key_of=lambda payload: (payload.venue_id, payload.symbol),
        )

    instruments = by_symbol("instrument-choice")
    leverages = by_symbol("leverage-choice")
    stop_plans = by_symbol("stop-target-plan")
    increments = by_symbol("price-increment")
    hints = by_symbol("size-hint")
    slippage = by_symbol("slippage-profile")
    allotments = LatestByKey(
        read=context.bus.reader("account-balance"),
        key_of=lambda balance: balance.segment,
    )
    # Keyed by the order the capital is held against, which is what identifies a
    # lock. It was keyed on a segment until 2026-08-25 -- a field LockedAllocation
    # has never carried -- and nothing failed while fund-lock-ledger was unbuilt,
    # because an assembly with no messages never calls its key function. The hour
    # that part first ran, this one crashed on every tick that saw a lock.
    locked = LatestByKey(
        read=context.bus.reader("locked-allocation"),
        key_of=lambda lock: lock.order_id,
    )
    # The binding limit is the smallest fraction any limiter allows, so they are
    # kept per limiter and the minimum is taken: a limiter that says nothing must
    # not be able to raise a limit another one lowered.
    limits = LatestByKey(
        read=context.bus.reader("risk-limit"),
        key_of=lambda limit: limit.limiter,
    )
    timed = Batch(read=context.bus.reader("timed-intent"))

    quantity_increment = context.number("order_quantity_increment")
    segment = str(context.setting("segment_id").value)

    sizer = PositionSizer(
        taker_fee_rate=context.number("taker_fee_rate"),
        slippage_fraction=context.number("entry_slippage_fraction"),
    )

    def read_intents():
        timed.payloads()
        instrument_by_symbol = instruments.mapping()
        leverage_by_symbol = leverages.mapping()
        plan_by_symbol = stop_plans.mapping()
        increment_by_symbol = increments.mapping()
        hint_by_symbol = hints.mapping()
        slippage.mapping()
        free_capital = free_capital_from_locks(locked.mapping().values())
        balance_by_segment = allotments.mapping()
        every_limit = limits.mapping()

        balance = balance_by_segment.get(segment)
        binding = min(
            (limit.fraction_of_allotment for limit in every_limit.values()),
            default=None,
        )

        sizable = []
        for intent in intents.payloads():
            # An intent that says stand aside is a decision, not a request. It was
            # sized anyway until 2026-08-23, and because a stand-aside intent
            # carries a degenerate stop the size it produced was whatever the
            # bounds gate happened to cut it to -- which opened ZECUSDT and
            # ETHUSDT on the live run at 07:29 and 07:31 from decisions the bot
            # had made not to trade. Counted rather than dropped: a refusal
            # nobody can see is indistinguishable from an input that never came.
            if not intent.is_actionable:
                sizer.standing.stood_aside += 1
                continue
            key = (intent.venue_id, intent.symbol)
            plan = plan_by_symbol.get(key)
            leverage = leverage_by_symbol.get(key)
            hint = hint_by_symbol.get(key)
            increment = increment_by_symbol.get(key)
            instrument = instrument_by_symbol.get(key)

            entry_price = entry_price_for(plan, instrument)
            stop_price = getattr(plan, "stop_price", None) or getattr(intent, "stop_price", None)
            if entry_price is None or stop_price is None or balance is None or binding is None:
                continue
            sizable.append(
                {
                    "venue_id": intent.venue_id,
                    "symbol": intent.symbol,
                    # The decision this order serves, so every order for one
                    # standing intent carries one id all the way to the venue.
                    "intent_id": intent.decision_id,
                    # Translated once, here, where the brain's vocabulary meets the
                    # venue's: the sizer reasons about an order, and an untranslated
                    # "long" would read as not-a-buy and put the stop on the wrong
                    # side of the entry.
                    "side": order_side_for(intent.side),
                    "entry_price": entry_price,
                    "stop_price": stop_price,
                    # Equity rather than cash: the fraction risked is a fraction of
                    # what the account is worth, and cash alone would shrink the
                    # risk budget every time a position was opened -- sizing each
                    # new trade smaller because earlier ones are still open, which
                    # is a rule nobody stated.
                    "allotment": balance.equity,
                    "risk_limit_fraction": binding,
                    "leverage": leverage.leverage if leverage is not None else 1.0,
                    "price_increment": getattr(increment, "increment", None),
                    "quantity_increment": quantity_increment,
                    "minimum_quantity": quantity_increment,
                    # Read off the field SizeHint carries, not through a getattr
                    # default: a default is what let this read return None on every
                    # intent for three days without anything reporting it.
                    "size_multiple": hint.multiple_of_normal if hint is not None else None,
                    "free_capital": free_capital,
                }
            )
        return tuple(sizable)

    return run_position_sizer(
        sizer=sizer,
        control_socket=context.control_socket,
        read_intents=read_intents,
        publish_sized_orders=publish_sized_orders,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
