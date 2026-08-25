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
- **It does not ignore fees.** Every fill is charged the venue's real taker or
  maker rate, because a strategy profitable before fees is not profitable.

And one thing it refuses: **a fill during a feed jump**. If the price series
jumped, the prices around the gap are not prices anything could have traded at,
and filling there manufactures profit out of a data artefact.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

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
    Fill,
)

PART_ID = "paper-fill-simulator"

PART_DECLARATION = PartDeclaration(
    part_id="paper-fill-simulator",
    consumes=(
        "order-request", "market-data", "cost-estimate", "money-mode",
        "delayed-order-request", "fill-price-estimate", "feed-jump", "consolidated-price",
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
    refused_feed_jump: int = 0
    refused_no_price: int = 0
    refused_already_filled: int = 0
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

    def __init__(self, taker_fee_rate: float, maker_fee_rate: float, now_ns=time.time_ns) -> None:
        if taker_fee_rate < 0 or maker_fee_rate < 0:
            raise ValueError("a fee rate cannot be negative")
        self._taker_fee = taker_fee_rate
        self._maker_fee = maker_fee_rate
        self._now_ns = now_ns
        self._jumped_symbols: set[tuple[str, str]] = set()
        self._filled_so_far: dict[str, float] = {}
        self._fill_sequence = 0
        # The paper book. Keyed by client order id because that is the id a venue
        # holds an order under, and it is what a cancel names.
        self._resting: dict[str, RestingOrder] = {}
        self.standing = SimulatorStanding()

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
                stop_price=stop_price, rested_at_ns=self._now_ns(),
            )
            if market_price is not None and self.is_triggered(
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
                            rested_at_ns=self._now_ns(),
                        ),
                        RESTING,
                        f"a {side} limit at {limit_price:g} is on the book; no price has arrived "
                        f"for this symbol yet, which is a reason to wait and not to refuse",
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
                stop_price=None, rested_at_ns=self._now_ns(),
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

    def read_orders(simulator):
        for trade in trades_in(trades.payloads()):
            last_price[(trade.venue_id, trade.symbol)] = trade.price
        for jump in jumps.payloads():
            simulator.observe_feed_jump(jump.venue_id, jump.symbol)
        costs.payloads()
        consolidated.payloads()
        estimate_by_symbol = prices.mapping()
        mode = modes.value()
        mode_name = getattr(mode, "mode", None)

        orders = []
        for request in list(requests.payloads()) + list(delayed.payloads()):
            # A cancel is its own request and carries no quantity, so it is
            # applied before the sendable test rather than dropped by it. A cancel
            # that is silently discarded leaves a stop resting on a position that
            # has already closed, and that stop opens the opposite position when
            # the market reaches it.
            withdraws = getattr(request, "cancels_client_order_id", None)
            if withdraws and not request.may_be_sent:
                simulator.cancel(
                    withdraws,
                    f"withdrawn by {request.client_order_id}: {request.reason}",
                )
                continue
            if not request.may_be_sent:
                continue
            key = (request.venue_id, request.symbol)
            orders.append(
                {
                    "client_order_id": request.client_order_id,
                    "venue_id": request.venue_id,
                    "symbol": request.symbol,
                    "side": request.side,
                    "quantity": request.quantity,
                    # Stated by the part that sent it, never inferred from which
                    # price fields are set: an entry carries the stop price that
                    # will protect it, and inferring made every entry a stop.
                    "order_type": getattr(request, "order_type", MARKET),
                    "limit_price": request.limit_price or None,
                    # None when the mode could not be read, which this part refuses
                    # rather than treating as paper.
                    "money_mode": mode_name,
                    "is_in_flight": False,
                    "fill_price_estimate": estimate_by_symbol.get(key),
                    "market_price": last_price.get(key),
                    # A stop rests until a live price crosses it. This is the
                    # field that lets a position close: without it every exit
                    # order sent by stop-order-manager would fill immediately at
                    # the market, which is not a stop, it is a market exit taken
                    # the instant the stop was decided.
                    # Only a triggered order's stop price is a trigger. On an
                    # entry the same field is the protective stop to attach once
                    # it fills, and passing that as a trigger would make the
                    # entry wait for the market to fall to its own stop.
                    "stop_price": request.trigger_price,
                    "cancels_client_order_id": request.cancels_client_order_id,
                    # What the decision thought the market was, and how far the
                    # market may have left it before this book refuses to fill.
                    "decided_at_price": getattr(request, "decided_at_price", 0.0) or None,
                    "maximum_decision_drift": maximum_decision_drift,
                }
            )
        return tuple(orders)

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
