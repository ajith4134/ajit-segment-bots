"""trade-capital-bounds-gate: no order reaches execution outside the user's bounds (RL-054).

The last thing between sizing and spending. Everything upstream reasons about
risk; this reasons about the operator's stated limits on capital per trade, and
it is the only part that guarantees execution never sees an order outside them.

Two directions, and they are not symmetric:

- **Below the minimum, bump up** (RL-054). A trade too small to be worth its fees
  is worse than no trade, and the operator has said what "worth it" means.
- **Above the maximum, cap down.** Never refuse: a trade at the maximum is the
  trade the operator asked for.

But a bump can breach the risk limit the sizer worked to respect, so a bump is
only made when the larger size still fits inside the risk allowed. Where it does
not, the order is refused and says which of the two bounds it could not satisfy
-- silently trading a size that breaches the risk limit would make every limiter
upstream decorative.

The settings verdict gates everything: an order is refused outright while the
capital settings are inconsistent, because bounds that contradict each other
cannot be enforced and guessing which one the operator meant is not this part's
decision to make.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trade_intent import CLOSE, OPEN, REDUCE
from runtime.trading_types import (
    UNLEVERED,
    capital_committed_by,
    leverage_behind,
    quantity_for_capital,
)

PART_ID = "trade-capital-bounds-gate"

PART_DECLARATION = PartDeclaration(
    part_id="trade-capital-bounds-gate",
    consumes=("sized-order", "trade-capital-bounds", "capital-settings-verdict", "position"),
    produces=("bounded-order", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

WITHIN_BOUNDS = "within-bounds"
BUMPED_TO_MINIMUM = "bumped-to-minimum"
CAPPED_AT_MAXIMUM = "capped-at-maximum"
REFUSED_SETTINGS_INVALID = "refused-capital-settings-invalid"
REFUSED_BUMP_BREACHES_RISK = "refused-bump-would-breach-risk"
REFUSED_NOT_TRADEABLE = "refused-order-not-tradeable"
# The maximum this segment allows per trade will not buy one increment of this
# instrument. Its own outcome and not a cap, because capping to nothing is a
# refusal wearing a success label: on 2026-09-05 this produced a zero-quantity
# order reported as "capped-at-maximum" with the reason "cut from 1,627.75 to the
# 50.00 maximum", which order-destination-router then counted in routed_to_paper
# and paper-fill-simulator dropped before it was ever simulated -- so the gate,
# the router and the book each reported nothing wrong and no order existed.
REFUSED_POSITION_AT_THE_CEILING = "the-position-is-already-at-the-capital-ceiling"
REFUSED_MAXIMUM_BUYS_NOTHING = "refused-the-maximum-cannot-buy-one-increment"
# The order is inside the capital bounds but smaller than one tradeable lot of
# this instrument. Its own outcome for the same reason as the one above: cutting
# it to zero would be a refusal reported as a success.
REFUSED_SMALLER_THAN_ONE_LOT = "refused-smaller-than-one-lot-of-this-instrument"
# The order asks for more units than the exchange accepts in a single order.
# NSE publishes this per contract as `freeze_quantity` -- 1,755 for NIFTY -- and
# an order above it is rejected outright rather than filled small. Until
# 2026-09-12 nothing in this project read that field: `grep -rn freeze_quantity`
# hit the broker adapter that parses it, and tests, and nothing else. On
# 2026-09-08 this gate emitted an order for 2,000,000 units of a NIFTY weekly,
# 1,140 times the largest single order the exchange would have taken, and the
# paper book filled it by walking the price from 0.10 to 0.169539375 -- which is
# how a trade bound to a 200,000 rupee ceiling committed 339,079.
REFUSED_ABOVE_THE_EXCHANGE_FREEZE_QUANTITY = "refused-above-the-exchanges-freeze-quantity"


@dataclass(frozen=True)
class BoundedOrder:
    """One order, inside the operator's capital bounds or refused with the reason."""

    venue_id: str
    symbol: str
    side: str
    quantity: float
    entry_price: float
    stop_price: float
    capital_used: float
    outcome: str
    minimum_capital: float
    maximum_capital: float
    risk_at_stop: float
    risk_allowed: float
    reason: str
    bounded_at_ns: int
    # The decision this order serves. Carried so the stamper can give every
    # order for one decision the same id, which is what makes a republished
    # intent one order rather than one order per tick.
    intent_id: str = ""
    # The leverage the sizer chose, carried on so what this order commits stays
    # computable further along: `capital_used` is the notional over this number,
    # and an order that dropped it would leave the account paying full notional
    # for a levered position.
    leverage: float = UNLEVERED
    # Which segment's money this is. Three segment bots share one spine since
    # 2026-09-05, and every part further along that holds money -- the capital
    # bounds, the money mode, the account that pays for the fill -- publishes one
    # level per segment. An order that did not carry its own would be matched
    # against whichever segment's level arrived last, which is a wrong answer that
    # reports nothing. Empty means the producer named no segment, which is what a
    # spine trading one segment looked like before this.
    segment: str = ""
    # OPEN, ADD_TO, REDUCE or CLOSE, straight through from the sized order.
    # Added 2026-09-08 alongside the reason it matters here: `bound()` reads it
    # to skip the capital-ceiling economics entirely for a close, which is
    # answered by what is held, not by what the operator allows a fresh trade
    # to commit.
    action: str = OPEN

    @property
    def may_be_sent(self) -> bool:
        return self.outcome in (WITHIN_BOUNDS, BUMPED_TO_MINIMUM, CAPPED_AT_MAXIMUM) and self.quantity > 0


@dataclass
class GateStanding:
    passed: int = 0
    bumped: int = 0
    capped: int = 0
    refused_maximum_buys_nothing: int = 0
    refused_settings: int = 0
    refused_bump: int = 0
    refused_not_tradeable: int = 0
    # An order refused because the position already holds the whole ceiling,
    # and one cut to the room left rather than to the ceiling. Both are new on
    # 2026-09-07 and both were previously invisible: the gate capped each
    # order and never saw the position it was adding to.
    refused_position_already_at_the_ceiling: int = 0
    capped_to_the_room_left: int = 0
    largest_capital_used: float = 0.0
    # A close/reduce order, passed straight through -- added 2026-09-08. Real
    # incident that day: a close order's own notional was checked against the
    # position's already-committed capital as though it were adding more,
    # which is backwards for an order that reduces what is held; a position
    # sized exactly at the ceiling refused the one order that would have
    # brought it under.
    closed: int = 0
    # All four new on 2026-09-12, and each counts something that was previously
    # unmeasurable rather than merely unmeasured.
    #
    # An order whose quantity had to be cut to a whole number of the
    # instrument's own lots. Until now only the bump and cap paths snapped; an
    # order that was already inside the bounds went through untouched, which is
    # how 712,985 and 559,703.18 units of a contract NSE trades in blocks of 65
    # reached the book on 2026-09-08.
    snapped_to_whole_lots: int = 0
    refused_smaller_than_one_lot: int = 0
    # An order cut to the exchange's single-order limit for that contract.
    capped_at_the_freeze_quantity: int = 0
    refused_above_the_freeze_quantity: int = 0
    # Orders sized against the global `order_quantity_increment` because the
    # instrument named no lot of its own. Not an error -- a share really does
    # trade in single units -- but it is the state in which a wrong quantity is
    # possible, so it is counted rather than assumed rare. A number that climbs
    # here alongside option orders means the lot never reached this part.
    sized_without_the_instruments_own_lot: int = 0
    # The largest quantity this gate has emitted, and the largest it refused for
    # being above the freeze. Both so an operator can see the shape of what is
    # being asked for without reading the journal.
    largest_quantity_emitted: float = 0.0
    # Capital this gate reserved for an order it let through, which no position
    # ever reported back and which therefore expired. A number that climbs here
    # means orders are being bound and not filled -- worth seeing, because the
    # reserve is bounding the ceiling in the meantime.
    reservations_that_expired: int = 0
    # Orders refused because the position ceiling was already spoken for by
    # another order of this gate's own that had not yet reached a position.
    # These are the four-in-one-millisecond stacks of 2026-09-08.
    refused_for_capital_already_in_flight: int = 0


class TradeCapitalBoundsGate:
    """Bumps, caps or refuses a sized order against the operator's per-trade bounds."""

    def __init__(
        self, quantity_increment: float, now_ns=time.time_ns,
        bound_capital_stands_for_seconds: float = 30.0,
    ) -> None:
        if quantity_increment <= 0:
            raise ValueError("a quantity increment of zero cannot snap anything")
        if bound_capital_stands_for_seconds <= 0:
            raise ValueError(
                "capital bound but not yet visible in a position has to expire, or an order "
                "that never filled would reserve the ceiling for ever"
            )
        self._increment = quantity_increment
        self._now_ns = now_ns
        # What each symbol already has committed to it, so the ceiling bounds
        # the position rather than the slice.
        self._held_capital: dict[tuple[str, str], float] = {}
        # **Capital this gate has itself bound and let through, which no
        # `position` message has reported back yet** (2026-09-12).
        #
        # `_held_capital` answers "what is held" from the position bus, and a
        # position cannot be published until an order fills. Between the two
        # there is a window, and on 2026-09-08 four separate orders for
        # `NIFTY 24550 CE 08 SEP 26` were bound and filled inside the same
        # millisecond -- 09:42:00.309, order ids 55d0de91, a0be0a0c, 594fd4b9
        # and 679e2e0d, eight fills between them -- each one passing the
        # position ceiling against a position record none of them had yet
        # updated. The ceiling bounded every order and the position walked to
        # 6,110,301 units regardless.
        #
        # Keyed by (venue, symbol) with the time it was bound, because it must
        # expire: an order that is refused downstream, cancelled, or simply
        # never filled would otherwise reserve the ceiling permanently. That is
        # the unbounded-`LatestByKey` trap this project has paid for three times
        # -- most recently the 214 switched-off parts that still counted as
        # running on 2026-08-26 -- and the question to ask of any such map is
        # what makes its keys go away.
        self._bound_awaiting_a_position: dict[tuple[str, str], tuple[float, int]] = {}
        self._bound_stands_for_ns = int(bound_capital_stands_for_seconds * 1e9)
        self.standing = GateStanding()

    def _increment_for(self, sized_order) -> float:
        """The step to snap this order to: the one it was sized with.

        `order_quantity_increment` is a single global step, and since 2026-09-07
        `position-sizer` snaps to the venue's own lot size wherever the instrument
        choice names one. Capping with a different step than the sizer used would
        hand the book a quantity neither part chose -- 187.2 lots of a contract
        the exchange trades in blocks of 65.

        The fallback is counted (2026-09-12). It is legitimate -- a share trades
        in single units and names no lot -- but it is also the state in which an
        option order can carry a quantity no exchange would accept, and an
        uncounted fallback is indistinguishable from a lot that arrived.
        """
        step = getattr(sized_order, "quantity_increment", 0.0)
        if step and step > 0:
            return float(step)
        self.standing.sized_without_the_instruments_own_lot += 1
        return self._increment

    def _freeze_quantity_for(self, sized_order) -> float | None:
        """The most units the exchange accepts in one order for this instrument.

        None where the instrument named none, which is honestly different from
        "no limit": a venue that publishes no freeze quantity has not told us
        there is none. Read by shape rather than by import, because this part
        knows the data it consumes and not the part that produced it (T-4).
        """
        freeze = getattr(sized_order, "freeze_quantity", None)
        return float(freeze) if freeze and freeze > 0 else None

    def _tradeable_quantity(self, sized_order, quantity: float):
        """Cut a quantity to what the exchange would actually take, or say why not.

        Two limits, applied in this order and both of them the venue's rather
        than the operator's:

        1. **A whole number of lots.** Every path through this gate ends here
           now. Before 2026-09-12 only the bump and cap paths snapped, so an
           order already inside the capital bounds was emitted at whatever
           fractional size the sizer computed.
        2. **At or below the freeze quantity.** Cutting rather than refusing,
           because a smaller order is still the trade the operator asked for --
           the same asymmetry the capital ceiling already uses. It is refused
           only when one whole lot is already above the freeze, which is a
           contract that cannot be traded at all at this size.

        Returns `(quantity, outcome_or_None, reason)`. A non-None outcome is a
        refusal the caller must return rather than an adjustment it may ignore.
        """
        increment = self._increment_for(sized_order)
        snapped = self._snap_down(quantity, increment)
        if snapped != quantity:
            self.standing.snapped_to_whole_lots += 1
        if snapped <= 0:
            self.standing.refused_smaller_than_one_lot += 1
            return 0.0, REFUSED_SMALLER_THAN_ONE_LOT, (
                f"{quantity:g} is less than one {increment:g} lot of {sized_order.symbol}; "
                f"an exchange rejects a part-lot outright, and rounding it up would spend "
                f"more than the bounds allow"
            )

        freeze = self._freeze_quantity_for(sized_order)
        if freeze is not None and snapped > freeze:
            within_freeze = self._snap_down(freeze, increment)
            if within_freeze <= 0:
                self.standing.refused_above_the_freeze_quantity += 1
                return 0.0, REFUSED_ABOVE_THE_EXCHANGE_FREEZE_QUANTITY, (
                    f"one {increment:g} lot of {sized_order.symbol} is already above the "
                    f"{freeze:g} this exchange accepts in a single order; there is no "
                    f"tradeable size here at all"
                )
            self.standing.capped_at_the_freeze_quantity += 1
            return within_freeze, None, (
                f"cut from {snapped:g} to {within_freeze:g}, the most {sized_order.symbol} "
                f"this exchange accepts in one order"
            )
        return snapped, None, ""

    def observe_position(self, venue_id: str, symbol: str, capital: float) -> None:
        """What is already committed to this symbol, from `position`.

        Replaced rather than accumulated: a position is a state, and the last one
        published is what is held.
        """
        key = (venue_id, symbol)
        if capital <= 0:
            self._held_capital.pop(key, None)
        else:
            self._held_capital[key] = capital
        # The position bus has now spoken for this symbol, so whatever this gate
        # was holding in reserve for it is accounted for in the figure above.
        # Dropped rather than decremented: the position is the state, and this
        # reserve only ever existed because no position had reported yet.
        self._bound_awaiting_a_position.pop(key, None)

    def _reserved_for(self, sized_order) -> float:
        """Capital let through for this symbol that no position has reported yet.

        Expires, for the reason `_bound_awaiting_a_position` states: an order
        that never fills must not hold the ceiling for ever.
        """
        key = (sized_order.venue_id, sized_order.symbol)
        reserved = self._bound_awaiting_a_position.get(key)
        if reserved is None:
            return 0.0
        capital, bound_at_ns = reserved
        if self._now_ns() - bound_at_ns > self._bound_stands_for_ns:
            self._bound_awaiting_a_position.pop(key, None)
            self.standing.reservations_that_expired += 1
            return 0.0
        return capital

    def _reserve(self, sized_order, capital: float) -> None:
        """Remember capital just let through, until a position reports it."""
        if capital <= 0:
            return
        key = (sized_order.venue_id, sized_order.symbol)
        standing, _at = self._bound_awaiting_a_position.get(key, (0.0, 0))
        self._bound_awaiting_a_position[key] = (standing + capital, self._now_ns())

    def held_capital(self, sized_order) -> float:
        """What is committed to this symbol: reported by a position, or in flight.

        The sum of the two, because both are the operator's capital and the
        ceiling is on the trade rather than on the message that happened to
        report it. Reading only the first is what let one contract take four
        simultaneous orders on 2026-09-08.
        """
        held = self._held_capital.get((sized_order.venue_id, sized_order.symbol), 0.0)
        return held + self._reserved_for(sized_order)

    def bound(self, sized_order, bounds, settings_are_valid: bool) -> BoundedOrder:
        if not settings_are_valid:
            self.standing.refused_settings += 1
            return self._refusal(
                sized_order, bounds, REFUSED_SETTINGS_INVALID,
                "the capital settings are inconsistent; which bound the operator meant is "
                "not this part's decision to guess",
            )

        if not sized_order.is_tradeable:
            self.standing.refused_not_tradeable += 1
            return self._refusal(
                sized_order, bounds, REFUSED_NOT_TRADEABLE,
                f"the sizer produced no tradeable order: {sized_order.reason}",
            )

        # A close/reduce is answered by what is held, not by what a fresh
        # trade may commit -- the ceiling below exists to bound a *new*
        # position, and applying it to an order that shrinks one treats
        # "already committed" as a reason to refuse the very order that
        # would reduce it. Passed through at the size the sizer already
        # computed from the position itself.
        if getattr(sized_order, "action", OPEN) in (CLOSE, REDUCE):
            self.standing.closed += 1
            # **A close is capped, never refused** (2026-09-12). The exchange's
            # freeze quantity applies to an exit as much as to an entry, so a
            # position larger than one order's worth has to leave in pieces; but
            # every refusal path above would strand it instead, and a position
            # that cannot be closed is the worst state this system has. So the
            # lot and part-lot rules are deliberately not applied here either: a
            # position holding a fractional quantity -- which is exactly what the
            # unsnapped orders of 2026-09-08 created -- must still be able to
            # exit the residue it was left holding.
            quantity = sized_order.quantity
            freeze = self._freeze_quantity_for(sized_order)
            leaving_in_pieces = ""
            if freeze is not None and quantity > freeze:
                self.standing.capped_at_the_freeze_quantity += 1
                leaving_in_pieces = (
                    f"; cut to the {freeze:g} this exchange takes in one order, so what is "
                    f"held leaves in pieces rather than in an order it would reject"
                )
                quantity = freeze
            capital = capital_committed_by(
                quantity, sized_order.entry_price, leverage_behind(sized_order),
            )
            self.standing.largest_capital_used = max(
                self.standing.largest_capital_used, capital,
            )
            self.standing.largest_quantity_emitted = max(
                self.standing.largest_quantity_emitted, quantity
            )
            return self._bounded(
                sized_order, bounds, quantity, capital,
                WITHIN_BOUNDS, self._risk_at(sized_order, quantity),
                f"closing {quantity:g} of what is held; the capital ceiling "
                f"bounds new commitment, not an order that reduces it{leaving_in_pieces}",
            )

        leverage = leverage_behind(sized_order)
        capital = capital_committed_by(sized_order.quantity, sized_order.entry_price, leverage)

        # **The ceiling is on the position, not on this order** (2026-09-07).
        # `maximum_capital_per_trade` is the operator's "the most one trade may
        # commit", and a trade is a position; this gate applied it to one order
        # while the bots re-decided the same contract every few seconds, so the
        # adds stacked. Measured on the live spine that day: 66 open positions
        # above the ceiling, the largest Rs 1,277,667 against 200,000, and one
        # contract walked 4,149 -> 10,492 units in a single round trip with every
        # add passing this gate.
        #
        # A symbol nothing is held in has `already` zero and behaves exactly as
        # before, so this is the same rule applied to the quantity the operator
        # was talking about, not a new rule for a first entry.
        already = self.held_capital(sized_order)
        room = bounds.maximum_capital - already
        if already > 0 and room <= 0:
            self.standing.refused_position_already_at_the_ceiling += 1
            return self._refusal(
                sized_order, bounds, REFUSED_POSITION_AT_THE_CEILING,
                f"{already:,.2f} is already committed to {sized_order.symbol}, at or past the "
                f"{bounds.maximum_capital:,.2f} a trade may commit; adding to it would put the "
                f"position beyond a bound the operator set, however small this order is",
            )

        if capital < bounds.minimum_capital:
            return self._bump(sized_order, bounds, capital)
        if already > 0 and capital > room:
            # Cap to the room left rather than to the whole ceiling.
            self.standing.capped_to_the_room_left += 1
            return self._cap(sized_order, bounds, capital, room)
        if capital > bounds.maximum_capital:
            return self._cap(sized_order, bounds, capital)

        # Inside the capital bounds still has to be a size the exchange accepts
        # (2026-09-12). This path emitted `sized_order.quantity` untouched until
        # then -- no lot, no freeze -- because the snapping lived only in the
        # bump and cap branches, where a quantity was being recomputed anyway.
        # An order that needed no capital adjustment therefore needed no
        # adjustment at all, which is exactly backwards: the venue's limits do
        # not depend on whether the desk's limits bound.
        quantity, refusal, adjustment = self._tradeable_quantity(
            sized_order, sized_order.quantity
        )
        if refusal is not None:
            return self._refusal(sized_order, bounds, refusal, adjustment)
        capital = capital_committed_by(quantity, sized_order.entry_price, leverage)

        self.standing.passed += 1
        self._reserve(sized_order, capital)
        self.standing.largest_capital_used = max(self.standing.largest_capital_used, capital)
        self.standing.largest_quantity_emitted = max(
            self.standing.largest_quantity_emitted, quantity
        )
        inside = (
            f"{capital:,.2f} is inside "
            f"[{bounds.minimum_capital:,.2f}, {bounds.maximum_capital:,.2f}]"
        )
        return self._bounded(
            sized_order, bounds, quantity, capital, WITHIN_BOUNDS,
            self._risk_at(sized_order, quantity),
            f"{inside}; {adjustment}" if adjustment else inside,
        )

    def _bump(self, sized_order, bounds, capital: float) -> BoundedOrder:
        """Raise to the minimum, unless that would risk more than allowed (RL-054)."""
        leverage = leverage_behind(sized_order)
        quantity = self._snap_up(
            quantity_for_capital(bounds.minimum_capital, sized_order.entry_price, leverage),
            self._increment_for(sized_order),
        )
        scaled_risk = self._risk_at(sized_order, quantity)

        if scaled_risk > sized_order.risk_allowed:
            self.standing.refused_bump += 1
            return self._refusal(
                sized_order, bounds, REFUSED_BUMP_BREACHES_RISK,
                f"bumping {capital:,.2f} to the {bounds.minimum_capital:,.2f} minimum would risk "
                f"{scaled_risk:,.2f} against {sized_order.risk_allowed:,.2f} allowed; the two "
                f"bounds cannot both be satisfied for this trade",
            )

        # A bump snaps *up* to reach the minimum, so it can land above the
        # exchange's single-order limit even though the capital is small -- which
        # is precisely what a cheap contract does: the 100,000 rupee minimum buys
        # a million units of a 0.10 option. Cut it to what the venue takes, and
        # refuse when that no longer reaches the minimum, rather than emitting an
        # order the exchange would reject.
        quantity, refusal, adjustment = self._tradeable_quantity(sized_order, quantity)
        if refusal is not None:
            return self._refusal(sized_order, bounds, refusal, adjustment)
        bumped_capital = capital_committed_by(quantity, sized_order.entry_price, leverage)
        if bumped_capital < bounds.minimum_capital:
            self.standing.refused_above_the_freeze_quantity += 1
            return self._refusal(
                sized_order, bounds, REFUSED_ABOVE_THE_EXCHANGE_FREEZE_QUANTITY,
                f"reaching the {bounds.minimum_capital:,.2f} minimum needs more units of "
                f"{sized_order.symbol} than the {self._freeze_quantity_for(sized_order):g} "
                f"this exchange accepts in one order; at {sized_order.entry_price:g} the two "
                f"bounds cannot both be satisfied",
            )
        scaled_risk = self._risk_at(sized_order, quantity)

        self.standing.bumped += 1
        self._reserve(sized_order, bumped_capital)
        self.standing.largest_capital_used = max(self.standing.largest_capital_used, bumped_capital)
        self.standing.largest_quantity_emitted = max(
            self.standing.largest_quantity_emitted, quantity
        )
        raised = f"raised from {capital:,.2f} to the {bounds.minimum_capital:,.2f} minimum"
        return self._bounded(
            sized_order, bounds, quantity, bumped_capital, BUMPED_TO_MINIMUM, scaled_risk,
            f"{raised}; {adjustment}" if adjustment else raised,
        )

    def _cap(self, sized_order, bounds, capital: float, room: float | None = None) -> BoundedOrder:
        """Cut to what may still be committed, unless that is under one increment.

        `room` is the ceiling less what the position already holds, and is the
        real limit whenever anything is held; the ceiling itself is the limit for
        a first entry. Passing the ceiling where the room was smaller is what let
        a position walk to six times it, one compliant order at a time.
        """
        ceiling = bounds.maximum_capital if room is None else room
        leverage = leverage_behind(sized_order)
        quantity = self._snap_down(
            quantity_for_capital(ceiling, sized_order.entry_price, leverage),
            self._increment_for(sized_order),
        )
        if quantity <= 0:
            # The maximum is smaller than one increment of this instrument at this
            # price, so there is no size to cut to. Refused by name rather than
            # emitted as a cap to zero: an order of nothing is not a smaller order.
            self.standing.refused_maximum_buys_nothing += 1
            return self._bounded(
                sized_order, bounds, 0.0, 0.0, REFUSED_MAXIMUM_BUYS_NOTHING, 0.0,
                f"the {ceiling:,.2f} that may still be committed does not buy one "
                f"{self._increment_for(sized_order):g} increment at "
                f"{sized_order.entry_price:,.2f}",
            )
        quantity, refusal, adjustment = self._tradeable_quantity(sized_order, quantity)
        if refusal is not None:
            return self._refusal(sized_order, bounds, refusal, adjustment)
        capped_capital = capital_committed_by(quantity, sized_order.entry_price, leverage)
        self.standing.capped += 1
        self._reserve(sized_order, capped_capital)
        self.standing.largest_capital_used = max(self.standing.largest_capital_used, capped_capital)
        self.standing.largest_quantity_emitted = max(
            self.standing.largest_quantity_emitted, quantity
        )
        cut = f"cut from {capital:,.2f} to the {ceiling:,.2f} that may still be committed"
        return self._bounded(
            sized_order, bounds, quantity, capped_capital, CAPPED_AT_MAXIMUM,
            self._risk_at(sized_order, quantity),
            f"{cut}; {adjustment}" if adjustment else cut,
        )

    def _risk_at(self, sized_order, quantity: float) -> float:
        """The risk this order would carry at a different size, scaled from its own."""
        if sized_order.quantity <= 0:
            return 0.0
        return sized_order.risk_at_stop * (quantity / sized_order.quantity)

    def _snap_up(self, quantity: float, increment: float | None = None) -> float:
        step = self._increment if increment is None else increment
        return round(math.ceil(quantity / step) * step, 12)

    def _snap_down(self, quantity: float, increment: float | None = None) -> float:
        step = self._increment if increment is None else increment
        return round(math.floor(quantity / step) * step, 12)

    def _bounded(self, sized_order, bounds, quantity, capital, outcome, risk, reason) -> BoundedOrder:
        # `bounds` is None whenever this segment's `trade-capital-bounds` has
        # never been read -- real, live, 2026-09-08: a cold-started gate's
        # very first tick reaches REFUSED_SETTINGS_INVALID (no verdict read
        # yet either) with no bounds behind it, and unconditional attribute
        # reads here crashed the whole part on every restart. The refusal
        # itself is correct -- nothing may be bounded before its bounds are
        # known -- only reading through a None to report it was the bug.
        return BoundedOrder(
            venue_id=sized_order.venue_id,
            symbol=sized_order.symbol,
            side=sized_order.side,
            quantity=quantity,
            entry_price=sized_order.entry_price,
            stop_price=sized_order.stop_price,
            capital_used=capital,
            outcome=outcome,
            minimum_capital=getattr(bounds, "minimum_capital", 0.0),
            maximum_capital=getattr(bounds, "maximum_capital", 0.0),
            risk_at_stop=risk,
            risk_allowed=sized_order.risk_allowed,
            reason=reason,
            bounded_at_ns=self._now_ns(),
            leverage=leverage_behind(sized_order),
            # Straight through: bounding an order does not make it a different
            # decision, and the id has to survive every step between the intent
            # and the venue or it stops being an identity.
            intent_id=getattr(sized_order, "intent_id", ""),
            # Straight through for the same reason, and load-bearing beyond
            # identity: the money mode and the account further along are one level
            # per segment now, and an order that arrived there unnamed would be
            # matched against whichever segment published last (2026-09-05).
            segment=getattr(sized_order, "segment", ""),
            action=getattr(sized_order, "action", OPEN),
        )

    def _refusal(self, sized_order, bounds, outcome, reason) -> BoundedOrder:
        return self._bounded(sized_order, bounds, 0.0, 0.0, outcome, 0.0, reason)


def describe_bounding(gate: TradeCapitalBoundsGate) -> dict:
    return {
        "part_id": PART_ID,
        "passed": gate.standing.passed,
        "bumped_to_minimum": gate.standing.bumped,
        "capped_at_maximum": gate.standing.capped,
        "refused_settings_invalid": gate.standing.refused_settings,
        "refused_bump_breaches_risk": gate.standing.refused_bump,
        "refused_not_tradeable": gate.standing.refused_not_tradeable,
        "refused_position_already_at_the_ceiling": (
            gate.standing.refused_position_already_at_the_ceiling
        ),
        "capped_to_the_room_left": gate.standing.capped_to_the_room_left,
        "refused_maximum_buys_nothing": gate.standing.refused_maximum_buys_nothing,
        "largest_capital_used": gate.standing.largest_capital_used,
        "closed": gate.standing.closed,
        "snapped_to_whole_lots": gate.standing.snapped_to_whole_lots,
        "refused_smaller_than_one_lot": gate.standing.refused_smaller_than_one_lot,
        "capped_at_the_freeze_quantity": gate.standing.capped_at_the_freeze_quantity,
        "refused_above_the_freeze_quantity": gate.standing.refused_above_the_freeze_quantity,
        "sized_without_the_instruments_own_lot": (
            gate.standing.sized_without_the_instruments_own_lot
        ),
        "largest_quantity_emitted": gate.standing.largest_quantity_emitted,
        "reservations_that_expired": gate.standing.reservations_that_expired,
        "capital_in_flight_awaiting_a_position": sum(
            capital for capital, _at in gate._bound_awaiting_a_position.values()
        ),
    }


def does_verdict_permit_trading(verdict) -> bool | None:
    """Whether a capital-settings verdict permits an order, or None if none arrived.

    A named function rather than an attribute read at the call site, because the
    call site got it wrong and nothing noticed: it asked for `is_valid`, which
    `CapitalSettingsVerdict` has never had, so every order was refused as
    "settings inconsistent" while the validator published `consistent` beside it
    -- and that refusal is indistinguishable from the deliberate one for settings
    that really do contradict each other.

    None means no verdict has been read, which the gate treats as unverified
    rather than as permission. A bound checked against settings nobody verified is
    a bound with no authority behind it.
    """
    if verdict is None:
        return None
    return verdict.permits_trading


def run_trade_capital_bounds_gate(
    gate: TradeCapitalBoundsGate, control_socket, read_sized_orders, publish_bounded_orders,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_bounded_orders(
            tuple(
                gate.bound(sized_order, bounds, valid)
                for sized_order, bounds, valid in read_sized_orders()
            )
        )

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_bounding(gate),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The last gate before an order carries an identity. It bounds a sized order
    against what the operator said one trade may commit, and it needs the capital
    settings to have been validated: a bound checked against settings nobody
    verified is a bound with no authority behind it.

    A verdict that has not arrived is unverified, not valid: `settings_are_valid`
    stays None until `capital-settings-validator` publishes one, and the gate
    refuses meanwhile. That is the correct direction to fail -- a bound checked
    against settings nobody verified is a bound with no authority behind it.
    """
    from runtime.input_assembly import Batch, LatestByKey, LatestValue, level_for_segment

    sized = Batch(read=context.bus.reader("sized-order"))
    # One bound per segment (2026-09-05). `capital-allotment-reader` publishes one
    # `trade-capital-bounds` for every segment this spine trades, and a LatestValue
    # here would hand every order whichever segment's bounds arrived last -- the
    # index segment's Rs 100,000 ceiling applied to a cash-equity order, or the
    # reverse, with nothing reporting it. Age-bounded, because bounds that stopped
    # being restated must stop binding rather than stand forever (2026-08-26).
    bounds = LatestByKey(
        read=context.bus.reader("trade-capital-bounds"),
        key_of=lambda bound: bound.segment,
        maximum_age_seconds=context.number("capital_bounds_maximum_age_seconds"),
    )
    verdicts = LatestValue(read=context.bus.reader("capital-settings-verdict"))
    # What is already committed per symbol, so the ceiling bounds the position
    # rather than the slice. Age-bounded for the same reason the bounds above
    # are: a position that stopped being restated must stop counting against the
    # ceiling rather than blocking the symbol forever.
    positions = LatestByKey(
        read=context.bus.reader("position"),
        key_of=lambda position: (position.venue_id, position.symbol),
        maximum_age_seconds=context.number("capital_bounds_maximum_age_seconds"),
    )
    publish_bounded_orders = context.bus.publisher_for("bounded-order")
    gate = TradeCapitalBoundsGate(
        quantity_increment=context.number("order_quantity_increment"),
        bound_capital_stands_for_seconds=context.number(
            "bound_capital_awaits_a_position_for_seconds"
        ),
    )

    def read_sized_orders():
        for (venue_id, symbol), position in positions.mapping().items():
            leverage = getattr(position, "leverage", None) or 1.0
            gate.observe_position(
                venue_id, symbol,
                abs(position.quantity) * position.average_entry_price / leverage,
            )
        bounds_by_segment = bounds.mapping()
        permitted = does_verdict_permit_trading(verdicts.value())
        return tuple(
            # An order whose segment has published no bounds gets None, which the
            # gate already refuses by name: no bounds, no order. An order naming
            # no segment at all takes the only bounds published, and only when
            # there is exactly one -- see `level_for_segment`.
            (
                order,
                level_for_segment(bounds_by_segment, getattr(order, "segment", "")),
                permitted,
            )
            for order in sized.payloads()
        )

    return run_trade_capital_bounds_gate(
        gate=gate,
        control_socket=context.control_socket,
        read_sized_orders=read_sized_orders,
        publish_bounded_orders=publish_bounded_orders,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
