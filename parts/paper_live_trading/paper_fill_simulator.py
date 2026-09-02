"""paper-fill-simulator: fill a paper order at the live price, with real costs applied.

The part that decides whether paper results mean anything. Every shortcut here
produces a strategy that works on paper and loses money live, and the failure is
invisible until real capital is behind it.

Four things it will not do, each corresponding to a way paper trading lies:

- **It does not fill at the touch.** The fill price comes from walking real book
  depth, so an order larger than the top level pays for the levels it eats.
- **It does not fill instantly.** An order fills only after its simulated round
  trip has elapsed, at whatever price exists *then* -- not the one that was on
  screen when the decision was made.
- **It does not fill a limit order the market never reached.** A buy limit below
  the market waits, exactly as it would live, and may never fill at all -- and it
  keeps waiting, on a book this part holds, until the market comes to it or
  something cancels it. A stop is the same instruction pointed the other way, and
  it is how a position closes: it rests until a live price crosses its trigger,
  then fills as a market order at the price the trigger found rather than at the
  stop, because that is what a venue does and the difference is the slippage a
  strategy actually pays.
- **It does not ignore fees.** A crypto venue fill is charged its real taker or
  maker rate; an Upstox options fill is charged Upstox's real flat brokerage
  plus STT, exchange transaction charge, IPFT charge, stamp duty and GST
  (`runtime/indian_options_fee_model.py`) -- two different fee *models*, not
  one rate swapped for another, because that is what the two venues actually
  charge. A strategy profitable before fees is not profitable.

And one thing it refuses: **a fill during a feed jump**. If the price series
jumped, the prices around the gap are not prices anything could have traded at,
and filling there manufactures profit out of a data artefact.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.indian_options_fee_model import upstox_options_order_cost
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trading_types import (
    BUY,
    LIMIT,
    MARKET,
    SELL,
    STOP_MARKET,
    TAKE_PROFIT_MARKET,
    TRIGGERED_ORDER_TYPES,
    UNLEVERED,
    Fill,
    leverage_behind,
)

PART_ID = "paper-fill-simulator"
# Same local-constant pattern as every broker-bridge part built this session
# (T-4: naming a value, not importing another part's constant).
UPSTOX_VENUE_ID = "upstox"

PART_DECLARATION = PartDeclaration(
    part_id="paper-fill-simulator",
    consumes=(
        "consolidated-price", "cost-estimate", "delayed-order-request", "feed-jump",
        "fill-price-estimate", "market-data", "market-session-state", "money-mode",
        "order-request",
    ),
    produces=("fill", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FILLED = "filled"
PARTIALLY_FILLED = "partially-filled"
RESTING = "resting-limit-not-reached"
# A stop waiting for the market to reach its trigger. Distinct from RESTING for a
# limit, because the two are opposite instructions at the same price and an
# operator reading a resting exit needs to know which one is protecting them.
RESTING_STOP = "resting-stop-not-triggered"
# A market order waiting for the first price on its symbol. Distinct from RESTING,
# which is a limit waiting for a price it names: this one names no price and fills
# at whatever arrives, so an operator reading the book must not take it for an
# order that is choosing to wait.
RESTING_UNPRICED = "resting-no-price-has-arrived-yet"
# The market is not in a session that trades, or no session has been measured
# at all. The order goes on the book and waits for the open rather than filling
# at whatever price was last seen -- which for an order placed at 18:00 is the
# 15:29 price, journalled as a trade that could not have happened.
RESTING_MARKET_CLOSED = "resting-the-market-is-not-open"
STOP_TRIGGERED = "stop-triggered"
CANCELLED = "cancelled"
HELD_IN_FLIGHT = "held-until-the-round-trip-elapses"
REFUSED_FEED_JUMP = "refused-price-jumped"
REFUSED_NO_PRICE = "refused-no-price"
REFUSED_LIVE_ORDER = "refused-live-orders-are-not-simulated"
# The decision behind this order was made at a price the market has left. Filling
# it would open a position somewhere the bot never looked, with exits computed
# around a price that no longer exists -- which is what happened on 2026-08-23:
# the decision half was reading prices up to 56 minutes old, every such trade
# opened six per cent away from where it thought it was, and both its exits were
# already through their triggers before they were placed.
#
# A live venue would fill this. That is the point: the refusal is a guard on the
# decision, not a simulation of the venue, and it is named so nobody reads it as
# the venue's behaviour.
REFUSED_DECISION_PRICE_STALE = "refused-the-decision-was-priced-at-a-market-that-has-gone"
# The id has already been filled for everything it asked for. Its own outcome
# rather than a refusal for no price: nothing is wrong with the order or the
# book, and an operator reading "no price" for a duplicate would look at the feed.
ALREADY_FILLED = "already-filled"

@dataclass(frozen=True)
class PaperFillResult:
    """What happened to one paper order this tick, and why."""

    client_order_id: str
    venue_id: str
    symbol: str
    side: str
    outcome: str
    fill: Fill | None
    filled_quantity: float
    remaining_quantity: float
    fill_price: float | None
    fee_charged: float
    slippage_fraction: float | None
    reason: str
    decided_at_ns: int

    @property
    def did_fill(self) -> bool:
        return self.outcome in (FILLED, PARTIALLY_FILLED) and self.fill is not None


@dataclass(frozen=True)
class RestingOrder:
    """An order sitting on the paper book, waiting for the market to come to it.

    Held with everything needed to fill it later, because the order message that
    placed it arrives once and a real order outlives the message that placed it.
    """

    client_order_id: str
    venue_id: str
    symbol: str
    side: str
    quantity: float
    order_type: str
    limit_price: float | None
    stop_price: float | None
    rested_at_ns: int
    # What the order was sized at. Held with the rest of the order because a
    # resting order fills long after the message that placed it is gone, and the
    # fill it produces has to state the leverage the account will pay for it at.
    leverage: float = UNLEVERED

    @property
    def key(self) -> tuple[str, str]:
        return (self.venue_id, self.symbol)


@dataclass
class SimulatorStanding:
    orders_seen: int = 0
    filled: int = 0
    partially_filled: int = 0
    resting: int = 0
    held_in_flight: int = 0
    released_without_a_verdict: int = 0
    refused_feed_jump: int = 0
    refused_no_price: int = 0
    # Market orders put on the book because no price had arrived for their symbol
    # yet. They are not refusals and must not be counted as one: a refusal is an
    # order that is gone, this is an order that has not filled yet. Read beside
    # `orders_on_the_book` -- a count here that keeps climbing while that one does
    # too is a symbol nothing is trading, which is a fact about the symbol.
    market_orders_waiting_for_a_first_price: int = 0
    refused_already_filled: int = 0
    # Orders put on the book because the market is not in a trading session, or
    # because no session has been measured at all. Not a refusal: the order is
    # still there and fills at the open. A count that climbs during Indian
    # market hours is a session reading that is wrong, not a quiet market.
    rested_market_closed: int = 0
    fees_charged: float = 0.0
    worst_slippage_fraction: float = 0.0
    # The paper book itself: how many orders are on it now, how many stops it has
    # triggered, and how many orders were withdrawn by another order.
    orders_on_the_book: int = 0
    stops_triggered: int = 0
    cancelled: int = 0
    cancels_for_an_unknown_order: int = 0
    # Orders refused because the decision behind them was priced at a market that
    # has since moved away. Counted separately from every other refusal: this one
    # is about the bot's own freshness, not about the order or the book.
    refused_decision_stale: int = 0


class PaperFillSimulator:
    """Fills paper orders the way a venue would, and refuses the fills a venue would not give.

    **It keeps a book.** An order that cannot fill now is not discarded with a
    note saying so -- it rests, and every price that arrives afterwards is tested
    against it, until it fills or something cancels it. Before this, a limit the
    market had not reached was reported RESTING once and then forgotten, which
    meant no order could ever be waiting when the market came to it. Nothing could
    close a position: a stop is an order whose entire purpose is to wait.
    """

    def __init__(
        self, taker_fee_rate: float, maker_fee_rate: float,
        options_flat_brokerage: float, options_stt_sell_rate: float,
        options_exchange_transaction_charge_rate: float, options_ipft_charge_rate: float,
        options_stamp_duty_buy_rate: float, options_gst_rate: float,
        now_ns=time.time_ns,
    ) -> None:
        if taker_fee_rate < 0 or maker_fee_rate < 0:
            raise ValueError("a fee rate cannot be negative")
        self._taker_fee = taker_fee_rate
        self._maker_fee = maker_fee_rate
        # Upstox's real options charge stack -- a flat brokerage plus five
        # percentage-of-premium components, not a taker/maker rate (see
        # runtime/indian_options_fee_model.py). Kept as the raw rates rather
        # than a bundled object so each one is a plain settings.number()
        # read, matching taker_fee_rate/maker_fee_rate's own shape.
        self._options_flat_brokerage = options_flat_brokerage
        self._options_stt_sell_rate = options_stt_sell_rate
        self._options_exchange_transaction_charge_rate = options_exchange_transaction_charge_rate
        self._options_ipft_charge_rate = options_ipft_charge_rate
        self._options_stamp_duty_buy_rate = options_stamp_duty_buy_rate
        self._options_gst_rate = options_gst_rate
        self._now_ns = now_ns
        # None until market-session-calendar says otherwise, and None does not
        # fill: see `may_fill`.
        self._session = None
        self._jumped_symbols: set[tuple[str, str]] = set()
        self._filled_so_far: dict[str, float] = {}
        self._fill_sequence = 0
        # The paper book. Keyed by client order id because that is the id a venue
        # holds an order under, and it is what a cancel names.
        self._resting: dict[str, RestingOrder] = {}
        self.standing = SimulatorStanding()

    def observe_session(self, session) -> None:
        """Which session the market is in. Never inferred from a price arriving:
        a stale price arrives at midnight exactly as a live one does."""
        self._session = session

    @property
    def may_fill(self) -> bool:
        """Whether a fill may happen at all right now.

        `None` -- no session measured yet -- is False, not True (Rule 8).
        Absence of evidence is its own state, and filling off an unmeasured
        session is how a paper account trades on a holiday.
        """
        return self._session is not None and self._session.is_tradeable

    def _why_the_market_is_shut(self) -> str:
        if self._session is None:
            return (
                "no trading session has been measured yet; an unmeasured session is "
                "not an open one, and filling here is how a paper account trades on "
                "a holiday"
            )
        return (
            f"the {self._session.segment} market is {self._session.kind} "
            f"({self._session.reason}); this order waits for the open"
        )

    def observe_feed_jump(self, venue_id: str, symbol: str) -> None:
        """A discontinuity in this symbol's prices; nothing may fill across it."""
        self._jumped_symbols.add((venue_id, symbol))

    def clear_feed_jump(self, venue_id: str, symbol: str) -> None:
        self._jumped_symbols.discard((venue_id, symbol))

    # -- the book ------------------------------------------------------------

    @property
    def resting_orders(self) -> tuple:
        """What is on the book right now, oldest first."""
        return tuple(sorted(self._resting.values(), key=lambda order: order.rested_at_ns))

    def note_release_without_a_verdict(self, client_order_id: str) -> None:
        """An order freed by the wait running out rather than by a latency verdict."""
        self.standing.released_without_a_verdict += 1

    def cancel(self, client_order_id: str, reason: str) -> PaperFillResult | None:
        """Withdraw one resting order. Returns what was withdrawn, or None.

        The case that matters is a position closing on one of its two exits: the
        target fills and the stop must go, or the stop fills and the target must
        go. A stop left resting on a position that no longer exists does not sit
        harmlessly -- when it triggers it opens the opposite position.
        """
        order = self._resting.pop(client_order_id, None)
        self.standing.orders_on_the_book = len(self._resting)
        if order is None:
            self.standing.cancels_for_an_unknown_order += 1
            return None
        self.standing.cancelled += 1
        return self._result(
            client_order_id, order.venue_id, order.symbol, order.side, CANCELLED, None,
            0.0, order.quantity, None, 0.0, None, reason,
        )

    def _rest(self, order: RestingOrder, outcome: str, reason: str) -> PaperFillResult:
        """Put an order on the book, or leave it where it already is.

        Idempotent on the client order id: a venue holds one order per id, and a
        re-sent order must not become a second one -- which for a stop would mean
        two stops closing twice the position that exists.
        """
        if order.client_order_id not in self._resting:
            self._resting[order.client_order_id] = order
            self.standing.resting += 1
        self.standing.orders_on_the_book = len(self._resting)
        return self._result(
            order.client_order_id, order.venue_id, order.symbol, order.side, outcome, None,
            0.0, order.quantity, None, 0.0, None, reason,
        )

    def evaluate_resting(self, price_by_symbol: dict) -> tuple:
        """Test every order on the book against the price that just arrived.

        This is what makes a paper stop a stop. Called on every tick with the
        latest live price per symbol, so an exit placed minutes ago fills at the
        moment the market reaches it -- not at the moment some later message
        happens to mention that order again.

        Orders whose symbol has no new price are left alone rather than refused: a
        quiet symbol is not a reason to withdraw protection.
        """
        if not self.may_fill:
            # Every resting order stays exactly where it is. A stop is not
            # withdrawn because the market closed -- it waits for the open, the
            # same as it would at a venue.
            self.standing.orders_on_the_book = len(self._resting)
            return ()
        results = []
        for order in list(self._resting.values()):
            price = price_by_symbol.get(order.key)
            if price is None:
                continue
            if order.key in self._jumped_symbols:
                # A gap is not a trade. Triggering a stop on a price nothing
                # traded at is how paper accounts invent losses and profits.
                continue
            result = self._test_and_fill(order, price)
            if result is not None:
                results.append(result)
        self.standing.orders_on_the_book = len(self._resting)
        return tuple(results)

    def _test_and_fill(self, order: RestingOrder, price: float) -> PaperFillResult | None:
        """One resting order against one price. None when it must keep waiting."""
        if order.order_type in TRIGGERED_ORDER_TYPES:
            if not self.is_triggered(order.order_type, order.side, order.stop_price, price):
                return None
            self.standing.stops_triggered += 1
            self._resting.pop(order.client_order_id, None)
            # A triggered order becomes a market order and pays the taker fee at
            # the price that exists now. It does not fill at the trigger: a venue
            # fills where the book is when the trigger fires, and a paper book
            # that filled at the trigger would report a loss smaller and a profit
            # larger than the strategy actually takes.
            what = "stop" if order.order_type == STOP_MARKET else "take-profit"
            return self._fill(
                order, price, fillable=order.quantity, is_taker=True, slippage=None,
                note=(
                    f"{what} at {order.stop_price:g} triggered by {price:g}; filled as a market "
                    f"order at the price the trigger found, not at the trigger"
                ),
            )

        if order.order_type == LIMIT:
            reached = price <= order.limit_price if order.side == BUY else price >= order.limit_price
            if not reached:
                return None
            self._resting.pop(order.client_order_id, None)
            return self._fill(
                order, order.limit_price, fillable=order.quantity, is_taker=False, slippage=None,
                note=f"the market reached {price:g} and this limit rested at {order.limit_price:g}",
            )

        if order.order_type == MARKET:
            # A market order names no price, so the first one that arrives is the
            # one it fills at. It rested only because this book could not price
            # its symbol when it was placed; it was never waiting *for* a price
            # in the sense a limit is, and holding it any longer once a price
            # exists would be inventing a condition nobody asked for.
            self._resting.pop(order.client_order_id, None)
            return self._fill(
                order, price, fillable=order.quantity, is_taker=True, slippage=None,
                note=(
                    f"filled at {price:g}, the first price to arrive for this symbol after "
                    f"the order was placed"
                ),
            )

        return None

    @staticmethod
    def is_triggered(order_type: str, side: str, trigger_price: float, price: float) -> bool:
        """Whether the market has reached this order's trigger.

        A sell stop triggers *below* the market and a sell take-profit triggers
        *above* it -- the same side, the same mechanism, opposite directions.
        Getting it backwards turns protection into an order that fires
        immediately at the worst possible moment, so the type decides it and
        nothing here infers the direction from the price.
        """
        if order_type == STOP_MARKET:
            return price <= trigger_price if side == SELL else price >= trigger_price
        if order_type == TAKE_PROFIT_MARKET:
            return price >= trigger_price if side == SELL else price <= trigger_price
        return False

    # -- one order ------------------------------------------------------------

    def simulate(
        self,
        client_order_id: str,
        venue_id: str,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str,
        limit_price: float | None,
        money_mode: str,
        is_in_flight: bool,
        fill_price_estimate=None,
        market_price: float | None = None,
        stop_price: float | None = None,
        cancels_client_order_id: str | None = None,
        decided_at_price: float | None = None,
        maximum_decision_drift: float | None = None,
        leverage: float = UNLEVERED,
    ) -> PaperFillResult:
        self.standing.orders_seen += 1

        # A cancel-replace withdraws the old order before the new one is
        # considered, because the two are one instruction: leaving both on the
        # book for a tick is two stops on one position.
        if cancels_client_order_id:
            self.cancel(
                cancels_client_order_id,
                f"withdrawn by {client_order_id}, which replaces it",
            )
            if quantity <= 0:
                # A pure cancel (as_order_request's is_cancel_only) carries no
                # quantity -- it names only the id above to withdraw, nothing to
                # place. Falling through priced this placeholder as a real order:
                # harmless while an unpriced market order was refused, but once
                # that became RESTING_UNPRICED (2026-08-30) it left a phantom
                # zero-quantity order sitting on the book forever, which is what
                # made `orders_on_the_book` never return to 0 after a position
                # closed on one exit and withdrew the other.
                return self._result(
                    client_order_id, venue_id, symbol, side, CANCELLED, None, 0.0, 0.0,
                    None, 0.0, None, "no quantity named; this call only withdrew an id",
                )

        if money_mode != "paper":
            return self._result(
                client_order_id, venue_id, symbol, side, REFUSED_LIVE_ORDER, None, 0.0, quantity,
                None, 0.0, None, "a live order is filled by the venue, not simulated here",
            )

        if is_in_flight:
            self.standing.held_in_flight += 1
            return self._result(
                client_order_id, venue_id, symbol, side, HELD_IN_FLIGHT, None, 0.0, quantity,
                None, 0.0, None,
                "the simulated round trip has not elapsed; filling now would trade on the price "
                "that was on screen when the decision was made",
            )

        if (venue_id, symbol) in self._jumped_symbols:
            self.standing.refused_feed_jump += 1
            return self._result(
                client_order_id, venue_id, symbol, side, REFUSED_FEED_JUMP, None, 0.0, quantity,
                None, 0.0, None,
                "this symbol's price series jumped; prices around a gap are not prices anything "
                "could have traded at, and filling there manufactures profit from an artefact",
            )

        # A triggered order is an instruction to wait, so it goes on the book
        # whether or not a price is available yet -- exactly as it would at a
        # venue, which accepts a stop without needing the market to be quoting.
        if order_type in TRIGGERED_ORDER_TYPES:
            if not stop_price:
                self.standing.refused_no_price += 1
                return self._result(
                    client_order_id, venue_id, symbol, side, REFUSED_NO_PRICE, None, 0.0,
                    quantity, None, 0.0, None,
                    f"a {order_type} names no trigger price; an order that waits for nothing "
                    f"either fires at once or never, and neither is what was asked for",
                )
            what = "stop" if order_type == STOP_MARKET else "take-profit"
            order = RestingOrder(
                client_order_id=client_order_id, venue_id=venue_id, symbol=symbol, side=side,
                quantity=quantity, order_type=order_type, limit_price=limit_price or None,
                stop_price=stop_price, rested_at_ns=self._now_ns(), leverage=leverage,
            )
            if self.may_fill and market_price is not None and self.is_triggered(
                order_type, side, stop_price, market_price
            ):
                # Already through the trigger when it arrived. A venue fills this
                # immediately rather than resting it, and so must this.
                self.standing.stops_triggered += 1
                return self._fill(
                    order, market_price, fillable=quantity, is_taker=True, slippage=None,
                    note=(
                        f"the market was already at {market_price:g}, through a {what} at "
                        f"{stop_price:g}; a venue would fill this on arrival rather than rest it"
                    ),
                )
            return self._rest(
                order, RESTING_STOP,
                f"a {side} {what} at {stop_price:g} is on the book"
                + (f"; the market is at {market_price:g}" if market_price is not None else ""),
            )

        # Nothing fills outside a trading session. Placed after the triggered
        # block so a stop still reaches the book by its own path with its own
        # validation, and so a stop already through its trigger rests rather
        # than filling at a price from before the close.
        if not self.may_fill:
            self.standing.rested_market_closed += 1
            return self._rest(
                RestingOrder(
                    client_order_id=client_order_id, venue_id=venue_id, symbol=symbol,
                    side=side, quantity=quantity, order_type=order_type,
                    limit_price=limit_price or None, stop_price=stop_price,
                    rested_at_ns=self._now_ns(), leverage=leverage,
                ),
                RESTING_MARKET_CLOSED,
                self._why_the_market_is_shut(),
            )

        # The decision's own price against the price this would fill at. Checked
        # before the fill and not after, because after it there is a position.
        if (
            decided_at_price
            and market_price
            and maximum_decision_drift is not None
            and abs(market_price - decided_at_price) / decided_at_price > maximum_decision_drift
        ):
            drift = abs(market_price - decided_at_price) / decided_at_price
            self.standing.refused_decision_stale += 1
            return self._result(
                client_order_id, venue_id, symbol, side, REFUSED_DECISION_PRICE_STALE, None,
                0.0, quantity, None, 0.0, None,
                f"the decision was priced at {decided_at_price:g} and the market is at "
                f"{market_price:g}, {drift:.2%} away, past the {maximum_decision_drift:.2%} this "
                f"book will fill across; a position opened here would sit somewhere the bot "
                f"never looked and its exits would already be through their triggers",
            )

        if fill_price_estimate is None or fill_price_estimate.average_price is None:
            if market_price is None:
                if order_type == LIMIT and limit_price:
                    return self._rest(
                        RestingOrder(
                            client_order_id=client_order_id, venue_id=venue_id, symbol=symbol,
                            side=side, quantity=quantity, order_type=LIMIT,
                            limit_price=limit_price, stop_price=None,
                            rested_at_ns=self._now_ns(), leverage=leverage,
                        ),
                        RESTING,
                        f"a {side} limit at {limit_price:g} is on the book; no price has arrived "
                        f"for this symbol yet, which is a reason to wait and not to refuse",
                    )
                if order_type == MARKET:
                    # The same reasoning as the limit above, and it belongs here
                    # just as much: a symbol this process has not yet seen a
                    # trade for is a reason to wait, not a reason to refuse.
                    #
                    # `last_price` is built from `market-data` as it arrives and
                    # starts empty on every restart, so for the first seconds of
                    # a run this book can price nothing. Refusing there discards
                    # the order permanently -- measured 2026-08-30, four exits
                    # from a `close-positions` flatten were refused in the first
                    # seconds after a restart (AIXBTUSDT, MVLLUSDT, STORJUSDT,
                    # TURBOUSDT) and the positions were stranded open, while the
                    # tape showed prints for two of them 1 and 11 seconds later.
                    # A venue does not reject a market order because the last
                    # trade has not printed in some client's memory.
                    #
                    # A symbol that never prices again -- a settled contract, as
                    # STORJUSDT is -- leaves its order on the book rather than
                    # silently gone, which is the honest rendering of a position
                    # that cannot be closed because nothing will trade it.
                    self.standing.market_orders_waiting_for_a_first_price += 1
                    return self._rest(
                        RestingOrder(
                            client_order_id=client_order_id, venue_id=venue_id, symbol=symbol,
                            side=side, quantity=quantity, order_type=MARKET,
                            limit_price=None, stop_price=None,
                            rested_at_ns=self._now_ns(), leverage=leverage,
                        ),
                        RESTING_UNPRICED,
                        f"a {side} market order is on the book; no price has arrived for this "
                        f"symbol yet, which is a reason to wait and not to refuse. It fills at "
                        f"the first price that does arrive",
                    )
                self.standing.refused_no_price += 1
                return self._result(
                    client_order_id, venue_id, symbol, side, REFUSED_NO_PRICE, None, 0.0, quantity,
                    None, 0.0, None, "no book estimate and no market price; nothing to fill against",
                )
            price, fillable, slippage, is_taker = market_price, quantity, None, True
        else:
            price = fill_price_estimate.average_price
            fillable = min(quantity, fill_price_estimate.fillable_quantity)
            slippage = fill_price_estimate.slippage_fraction
            is_taker = True

        if order_type == LIMIT and limit_price is not None:
            reached = price <= limit_price if side == BUY else price >= limit_price
            if not reached:
                return self._rest(
                    RestingOrder(
                        client_order_id=client_order_id, venue_id=venue_id, symbol=symbol,
                        side=side, quantity=quantity, order_type=LIMIT,
                        limit_price=limit_price, stop_price=None, rested_at_ns=self._now_ns(),
                        leverage=leverage,
                    ),
                    RESTING,
                    f"the market is at {price:g} and the limit is {limit_price:g}; a real order "
                    f"rests here and may never fill",
                )
            # A limit order that the market came to is a maker fill, and the
            # difference in fee is the whole reason to place one.
            price = limit_price
            is_taker = False

        return self._fill(
            RestingOrder(
                client_order_id=client_order_id, venue_id=venue_id, symbol=symbol, side=side,
                quantity=quantity, order_type=order_type, limit_price=limit_price,
                stop_price=None, rested_at_ns=self._now_ns(), leverage=leverage,
            ),
            price, fillable=fillable, is_taker=is_taker, slippage=slippage, note=None,
        )

    def _fill(
        self, order: RestingOrder, price: float, fillable: float, is_taker: bool,
        slippage: float | None, note: str | None,
    ) -> PaperFillResult:
        """Charge the fee, record the fill, and account for what this id had left.

        Shared by a new order and by one the book was holding, because a stop that
        triggers must be charged and deduplicated exactly as a market order is --
        two paths would be two places for the accounting to drift.
        """
        client_order_id = order.client_order_id
        # What this id still has outstanding. A venue holds one order per client
        # id -- that is the whole reason `order-idempotency-stamper` derives a
        # stable one -- so an id already filled in full is not filled again, and a
        # partially filled one fills only what is left. Without this the simulator
        # counted what it had filled and then filled it again anyway, so a
        # resubmitted order opened a second position on paper while the same
        # order against a real venue would have been rejected as a duplicate.
        already_filled = self._filled_so_far.get(client_order_id, 0.0)
        outstanding = order.quantity - already_filled
        if outstanding <= 0:
            self.standing.refused_already_filled += 1
            return self._result(
                client_order_id, order.venue_id, order.symbol, order.side, ALREADY_FILLED,
                None, 0.0, 0.0, None, 0.0, None,
                f"{client_order_id} has already been filled for {already_filled:g}, which is "
                f"the whole order; a venue holding this id would reject the duplicate rather "
                f"than open a second position",
            )
        fillable = min(fillable, outstanding)

        if fillable <= 0:
            self.standing.refused_no_price += 1
            return self._result(
                client_order_id, order.venue_id, order.symbol, order.side, REFUSED_NO_PRICE,
                None, 0.0, order.quantity, None, 0.0, None,
                "the book showed no fillable quantity at any price",
            )

        if order.venue_id == UPSTOX_VENUE_ID:
            # Upstox charges a flat brokerage plus five percentage-of-premium
            # components (STT, exchange transaction charge, IPFT charge,
            # stamp duty, GST) -- a different fee model from a crypto
            # venue's blended taker/maker rate, not just a different number.
            # is_taker plays no part: none of Upstox's six components read
            # whether the fill crossed the spread, only the order's side and
            # the premium it traded.
            fee = upstox_options_order_cost(
                fillable * price, order.side,
                flat_brokerage=self._options_flat_brokerage,
                stt_sell_rate=self._options_stt_sell_rate,
                exchange_transaction_charge_rate=self._options_exchange_transaction_charge_rate,
                ipft_charge_rate=self._options_ipft_charge_rate,
                stamp_duty_buy_rate=self._options_stamp_duty_buy_rate,
                gst_rate=self._options_gst_rate,
            ).total
        else:
            fee_rate = self._taker_fee if is_taker else self._maker_fee
            fee = fillable * price * fee_rate
        self.standing.fees_charged += fee
        if slippage is not None:
            self.standing.worst_slippage_fraction = max(
                self.standing.worst_slippage_fraction, slippage
            )

        self._filled_so_far[client_order_id] = already_filled + fillable
        remaining = outstanding - fillable
        outcome = FILLED if remaining <= 0 else PARTIALLY_FILLED
        if outcome == FILLED:
            self.standing.filled += 1
        else:
            self.standing.partially_filled += 1
            # A partial fill leaves the rest on the book, exactly as a venue does.
            if order.client_order_id in self._resting or order.order_type in (
                LIMIT, *TRIGGERED_ORDER_TYPES
            ):
                self._resting[order.client_order_id] = RestingOrder(
                    client_order_id=client_order_id, venue_id=order.venue_id,
                    symbol=order.symbol, side=order.side, quantity=order.quantity,
                    order_type=order.order_type, limit_price=order.limit_price,
                    stop_price=order.stop_price, rested_at_ns=order.rested_at_ns,
                    leverage=order.leverage,
                )
        self.standing.orders_on_the_book = len(self._resting)

        self._fill_sequence += 1
        fill = Fill(
            fill_id=f"paper-{client_order_id}-{self._fill_sequence}",
            venue_id=order.venue_id,
            symbol=order.symbol,
            side=order.side,
            price=price,
            quantity=fillable,
            fee=fee,
            filled_at_ns=self._now_ns(),
            order_id=client_order_id,
            is_paper=True,
            # A fill states a price and a quantity, and those are the same number
            # at 1x and at 10x. What the position ties up is the notional over
            # this, and paper-account-keeper has no other source for it.
            leverage=order.leverage,
        )
        return self._result(
            client_order_id, order.venue_id, order.symbol, order.side, outcome, fill,
            fillable, remaining, price, fee, slippage,
            (note + "; " if note else "")
            + f"{fillable:g} at {price:g} as a {'taker' if is_taker else 'maker'}, "
            f"fee {fee:,.4f}"
            + (f", {slippage:.3%} from the touch" if slippage else ""),
        )

    def _result(
        self, client_order_id, venue_id, symbol, side, outcome, fill,
        filled, remaining, price, fee, slippage, reason
    ) -> PaperFillResult:
        return PaperFillResult(
            client_order_id=client_order_id, venue_id=venue_id, symbol=symbol, side=side,
            outcome=outcome, fill=fill, filled_quantity=filled, remaining_quantity=remaining,
            fill_price=price, fee_charged=fee, slippage_fraction=slippage,
            reason=reason, decided_at_ns=self._now_ns(),
        )


def describe_paper_fills(simulator: PaperFillSimulator) -> dict:
    return {
        "part_id": PART_ID,
        "orders_seen": simulator.standing.orders_seen,
        "filled": simulator.standing.filled,
        "partially_filled": simulator.standing.partially_filled,
        "resting": simulator.standing.resting,
        "orders_on_the_book": simulator.standing.orders_on_the_book,
        "stops_triggered": simulator.standing.stops_triggered,
        "cancelled": simulator.standing.cancelled,
        "cancels_for_an_unknown_order": simulator.standing.cancels_for_an_unknown_order,
        "refused_because_the_decision_was_stale": simulator.standing.refused_decision_stale,
        "held_in_flight": simulator.standing.held_in_flight,
        "released_without_a_verdict": simulator.standing.released_without_a_verdict,
        "refused_feed_jump": simulator.standing.refused_feed_jump,
        "refused_no_price": simulator.standing.refused_no_price,
        "refused_already_filled": simulator.standing.refused_already_filled,
        "fees_charged": simulator.standing.fees_charged,
        "worst_slippage_fraction": simulator.standing.worst_slippage_fraction,
    }


def run_paper_fill_simulator(
    simulator: PaperFillSimulator, control_socket, read_orders, publish_fills,
    health_interval_seconds: float, emit_health, read_prices=None,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    """`read_prices` gives the latest live price per symbol, for the book.

    Every tick tests what is resting against what just arrived. Without it an
    order rests forever: the message that placed it came once, and a stop whose
    trigger is only checked when another message mentions it is not a stop.
    """
    def tick() -> None:
        orders = read_orders(simulator)
        results = [simulator.simulate(**order) for order in orders]
        if read_prices is not None:
            results.extend(simulator.evaluate_resting(read_prices()))
        publish_fills(tuple(result.fill for result in results if result.did_fill))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_paper_fills(simulator),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Where the first paper fill happens. It refuses anything whose money mode is not
    exactly paper -- a live order is filled by the venue, not simulated here -- so
    the paper-only guarantee is enforced twice, once by the router addressing the
    order and once here by refusing to pretend.

    The market price it fills against is the last trade this part saw for that
    symbol, live off the bus. A cost estimate or a book-walk price would be better
    and neither is running, so an order with neither is filled at the last price and
    the fill says which it used.
    """
    # `market-data` carries trades AND candles: venue-trade-stream-reader
    # publishes the first, ccxt-venue-reader the second, and both have always
    # declared it. This part wants trades and now says so, rather than assuming
    # the wire holds only what it happens to want -- a part that dies on an
    # unexpected shape is a part the wiring can kill.
    from runtime.market_data_stream import trades_in
    from runtime.input_assembly import Batch, LatestByKey, LatestValue

    requests = Batch(read=context.bus.reader("order-request"))
    trades = Batch(read=context.bus.reader("market-data"))
    modes = LatestValue(read=context.bus.reader("money-mode"))
    # A level: market-session-calendar publishes a one-item tuple, true until
    # it changes. Unbounded here on purpose -- the calendar recomputes it from
    # the clock every tick, so an age bound would only expire a fact that is
    # still true while nothing else could restate it.
    sessions = LatestValue(read=context.bus.reader("market-session-state"))
    costs = Batch(read=context.bus.reader("cost-estimate"))
    delayed = Batch(read=context.bus.reader("delayed-order-request"))
    jumps = Batch(read=context.bus.reader("feed-jump"))
    consolidated = Batch(read=context.bus.reader("consolidated-price"))
    prices = LatestByKey(
        read=context.bus.reader("fill-price-estimate"),
        key_of=lambda estimate: (estimate.venue_id, estimate.symbol),
    )
    publish_fills = context.bus.publisher_for("fill")

    last_price: dict[tuple[str, str], float] = {}
    maximum_decision_drift = context.number("maximum_decision_price_drift")
    # Orders waiting for order-latency-simulator to say the simulated round trip
    # has elapsed, by client order id, with the moment each began waiting.
    waiting: dict[str, tuple[dict, float]] = {}
    # The same number the latency simulator holds an order by, read from the same
    # setting: beyond it that part stops holding, so an order still waiting here
    # is waiting on a release that is not coming rather than on latency.
    longest_hold_seconds = context.number("order_latency_maximum")

    monotonic = time.monotonic

    def read_orders(simulator):
        standing_session = sessions.value()
        if standing_session is not None:
            for session in standing_session:
                simulator.observe_session(session)
        for trade in trades_in(trades.payloads()):
            last_price[(trade.venue_id, trade.symbol)] = trade.price
        for jump in jumps.payloads():
            simulator.observe_feed_jump(jump.venue_id, jump.symbol)
        costs.payloads()
        consolidated.payloads()
        estimate_by_symbol = prices.mapping()
        mode = modes.value()
        mode_name = getattr(mode, "mode", None)

        # What the latency simulator has decided about each order so far. It is a
        # different shape on a different wire: a DelayedOrderRequest names an
        # order and says whether the simulated round trip has elapsed, and it
        # carries no side, no quantity and no price. Reading the two streams as
        # one list is what crashed this part 17 times in 50 minutes on
        # 2026-08-26 -- `'DelayedOrderRequest' object has no attribute
        # 'may_be_sent'` -- and, before the crash, would have filled every paper
        # order at the price that was on screen when the decision was made.
        released_now = {
            record.client_order_id: record.may_fill_now for record in delayed.payloads()
        }

        orders = []
        for request in requests.payloads():
            # A cancel is its own request and carries no quantity, so it is
            # applied before the sendable test rather than dropped by it. A cancel
            # that is silently discarded leaves a stop resting on a position that
            # has already closed, and that stop opens the opposite position when
            # the market reaches it.
            withdraws = getattr(request, "cancels_client_order_id", None)
            if withdraws and not request.may_be_sent:
                waiting.pop(withdraws, None)
                simulator.cancel(
                    withdraws,
                    f"withdrawn by {request.client_order_id}: {request.reason}",
                )
                continue
            if not request.may_be_sent:
                continue
            if withdraws:
                waiting.pop(withdraws, None)
            key = (request.venue_id, request.symbol)
            order = build_order(request, key, mode_name, estimate_by_symbol)
            if released_now.get(request.client_order_id) is not True:
                # Held, and remembered: the release names the order and cannot
                # re-send it, so whoever saw the order first has to keep it. A
                # verdict that has not arrived yet holds the order too -- the two
                # parts read the same order in the same tick, so "no verdict" is
                # nearly always "not decided yet", and filling on it would fill
                # every paper order at the price that was on screen when the
                # decision was made, which is what simulating latency is for.
                order["is_in_flight"] = True
                waiting[request.client_order_id] = (order, monotonic())
            orders.append(order)

        for client_order_id, may_fill_now in released_now.items():
            if not may_fill_now or client_order_id not in waiting:
                continue
            order, _ = waiting.pop(client_order_id)
            orders.append(price_now(order))

        # A verdict that never came does not strand the order. The latency
        # simulator stops holding at order_latency_maximum whatever it measured,
        # so past that the order is waiting on a part that is off or on a message
        # that was lost -- and a venue answering late fills at the market it
        # answers into, which is what this does. Counted, because an order filled
        # without a latency verdict is a different fact from one released by it.
        expired = [
            client_order_id
            for client_order_id, (_, held_since) in waiting.items()
            if monotonic() - held_since > longest_hold_seconds
        ]
        for client_order_id in expired:
            order, _ = waiting.pop(client_order_id)
            simulator.note_release_without_a_verdict(client_order_id)
            orders.append(price_now(order))

        return tuple(orders)

    def price_now(order: dict) -> dict:
        """The same order, freed to fill, priced where the market is now.

        Filling a released order at the price it arrived with is the thing
        simulating latency was meant to prevent.
        """
        released = dict(order)
        released["is_in_flight"] = False
        key = (released["venue_id"], released["symbol"])
        released["market_price"] = last_price.get(key)
        released["fill_price_estimate"] = prices.mapping().get(key)
        return released

    def build_order(request, key, mode_name, estimate_by_symbol) -> dict:
        return {
            "client_order_id": request.client_order_id,
            "venue_id": request.venue_id,
            "symbol": request.symbol,
            "side": request.side,
            "quantity": request.quantity,
            # Stated by the part that sent it, never inferred from which price
            # fields are set: an entry carries the stop price that will protect
            # it, and inferring made every entry a stop.
            "order_type": getattr(request, "order_type", MARKET),
            "limit_price": request.limit_price or None,
            # None when the mode could not be read, which this part refuses
            # rather than treating as paper.
            "money_mode": mode_name,
            "is_in_flight": False,
            "fill_price_estimate": estimate_by_symbol.get(key),
            "market_price": last_price.get(key),
            # A stop rests until a live price crosses it. This is the field that
            # lets a position close: without it every exit order sent by
            # stop-order-manager would fill immediately at the market, which is
            # not a stop, it is a market exit taken the instant the stop was
            # decided.
            # Only a triggered order's stop price is a trigger. On an entry the
            # same field is the protective stop to attach once it fills, and
            # passing that as a trigger would make the entry wait for the market
            # to fall to its own stop.
            "stop_price": request.trigger_price,
            "cancels_client_order_id": request.cancels_client_order_id,
            # What the decision thought the market was, and how far the market
            # may have left it before this book refuses to fill.
            "decided_at_price": getattr(request, "decided_at_price", 0.0) or None,
            # Carried from the request rather than defaulted here: an exit says
            # nothing about leverage and is unlevered, and an entry says what the
            # desk sized it at.
            "leverage": leverage_behind(request),
            "maximum_decision_drift": maximum_decision_drift,
        }

    def read_prices() -> dict:
        """The latest live price per symbol, for the orders already on the book.

        The same dictionary the new orders are filled against, so a stop and a
        market order arriving in the same tick see the same market.
        """
        return dict(last_price)

    return run_paper_fill_simulator(
        simulator=PaperFillSimulator(
            taker_fee_rate=context.number("taker_fee_rate"),
            maker_fee_rate=context.number("maker_fee_rate"),
            options_flat_brokerage=context.number("options_flat_brokerage"),
            options_stt_sell_rate=context.number("options_stt_sell_rate"),
            options_exchange_transaction_charge_rate=context.number(
                "options_exchange_transaction_charge_rate"
            ),
            options_ipft_charge_rate=context.number("options_ipft_charge_rate"),
            options_stamp_duty_buy_rate=context.number("options_stamp_duty_buy_rate"),
            options_gst_rate=context.number("options_gst_rate"),
        ),
        control_socket=context.control_socket,
        read_orders=read_orders,
        publish_fills=publish_fills,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
        read_prices=read_prices,
    )
