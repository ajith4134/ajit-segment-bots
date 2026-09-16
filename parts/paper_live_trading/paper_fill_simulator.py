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
from dataclasses import asdict, dataclass, field, fields, replace

from runtime.indian_equity_fee_model import upstox_equity_intraday_order_cost
from runtime.indian_options_fee_model import upstox_options_order_cost
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.tape import TradeFidelity
from runtime.trading_types import (
    BUY,
    LIMIT,
    MARKET,
    SELL,
    OPTION,
    SPOT,
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
    # Whose money placed it, held for the same reason the leverage is: a resting
    # order fills long after the message that placed it is gone, and the fill it
    # produces has to say which segment's account pays for it (2026-09-05).
    segment: str = ""
    # The other half of this position's bracket. A real bracket is
    # one-cancels-other; without the link both halves rest at the size of the
    # whole position and both fill.
    linked_exit_order_id: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.venue_id, self.symbol)


# What this part's checkpoint is called under `position_state_root`.
CHECKPOINT_COMPONENT = "resting-orders"


@dataclass
class SimulatorStanding:
    orders_seen: int = 0
    filled: int = 0
    partially_filled: int = 0
    # A bracket's other half, given up when this one filled. Counted because the
    # alternative -- both halves filling at the size of the whole position -- is
    # indistinguishable from a strategy that decided to sell twice.
    bracket_siblings_withdrawn: int = 0
    bracket_siblings_reduced: int = 0
    resting: int = 0
    # The paper book, carried across a restart since 2026-09-13. Named
    # `restored_symbols` because that is the field `restore_and_arm_checkpoint`
    # sets (T-4). Until then the book lived in memory alone while
    # `stop-order-manager` checkpointed what it believed was resting, so every
    # restart left positions the manager counted as protected with no stop on
    # the paper book at all -- the venue a real order rests at does not forget
    # it when the process that placed it restarts, and neither may this one.
    restored_symbols: int = 0
    checkpoint_verdict: str = "no checkpoint has been read yet"
    book_changes: int = 0
    held_in_flight: int = 0
    released_without_a_verdict: int = 0
    refused_feed_jump: int = 0
    # How many times a symbol's bar was lifted because its prices went continuous
    # again. Zero here beside a climbing refused_feed_jump is the 2026-09-04
    # defect returning: the break is seen and the recovery is not.
    feed_jumps_cleared: int = 0
    refused_no_price: int = 0
    # Orders refused because the mode this order's segment published was not
    # "paper" -- including the case where it published none at all, which
    # arrives here as None and is correctly not treated as paper. Every other
    # refusal on this part was counted and this one was not, so a book that
    # refused every order it ever saw reported `orders_seen` climbing beside a
    # row of zeroes and no way to tell why (measured 2026-09-06, diagnosing the
    # closing-chain test: two orders seen, every other counter at zero, and the
    # reason invisible).
    refused_live_order: int = 0
    # Market orders put on the book because no price had arrived for their symbol
    # yet. They are not refusals and must not be counted as one: a refusal is an
    # order that is gone, this is an order that has not filled yet. Read beside
    # `orders_on_the_book` -- a count here that keeps climbing while that one does
    # too is a symbol nothing is trading, which is a fact about the symbol.
    market_orders_waiting_for_a_first_price: int = 0
    refused_already_filled: int = 0
    # Which of Upstox's two charge stacks each fill was priced by. Counted
    # rather than assumed, because the wrong one is not visibly wrong: an
    # equity fill charged the options stack still produces a plausible
    # number. A cash-equity segment trading while `fills_priced_as_equity`
    # stays at zero is the defect this counter exists to make visible.
    fills_priced_as_options: int = 0
    fills_priced_as_equity: int = 0
    fills_priced_by_the_fallback_stack: int = 0
    # Orders put on the book because the market is not in a trading session, or
    # because no session has been measured at all. Not a refusal: the order is
    # still there and fills at the open. A count that climbs during Indian
    # market hours is a session reading that is wrong, not a quiet market.
    rested_market_closed: int = 0
    # More than one exchange segment's session was standing at once, so this part
    # could not tell which one an order belonged to and refused to guess. Counted
    # rather than logged because it is a standing condition, not an event: while
    # it is above zero nothing fills, and the number says how many ticks that has
    # been true for.
    sessions_too_many_to_choose: int = 0
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
        equity_intraday_rates: dict | None = None,
        instrument_kind_of=None,
        kinds_by_segment: dict | None = None,
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
        # Upstox's Equity Intraday stack, which is a different stack and not a
        # different number: STT is 0.025% of turnover where the options rate is
        # 0.1% of premium, the exchange transaction charge is 0.00297% against
        # 0.03503%, and brokerage is min(Rs20, 0.1%) rather than a flat Rs20.
        # See runtime/indian_equity_fee_model.py. None means the caller stated
        # no equity rates, and this part does not invent them (RL-061) -- it
        # counts the fills it could not price that way instead.
        self._equity_intraday_rates = dict(equity_intraday_rates or {})
        # How a symbol says which stack it belongs to. Injected for the same
        # reason instrument-selector injects `segment_of`: the answer lives in
        # the instrument master and the operator's settings, and a table in this
        # module would be wrong the moment a segment traded something new.
        # An OrderRequest does not carry the instrument kind, so without this
        # there is nothing to read it from.
        self._instrument_kind_of = instrument_kind_of
        # segment id -> the one instrument kind that segment trades, read from
        # the operator's segment files. Empty means nothing stated it, and the
        # fallback below answers for that rather than this module guessing.
        self._kinds_by_segment = dict(kinds_by_segment or {})
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

    def read_checkpoint_state(self) -> dict:
        """The paper book, and how much of each resting order has already filled."""
        return {
            "resting": [asdict(order) for order in self.resting_orders],
            "filled_so_far": {
                client_order_id: filled
                for client_order_id, filled in self._filled_so_far.items()
                if client_order_id in self._resting
            },
        }

    def restore_from_checkpoint(self, state: dict) -> int:
        """Put the book back. Returns how many orders came back resting."""
        known = {field_.name for field_ in fields(RestingOrder)}
        self._resting = {}
        for entry in state.get("resting") or ():
            order = RestingOrder(**{name: value for name, value in entry.items() if name in known})
            self._resting[order.client_order_id] = order
        self._filled_so_far = {
            client_order_id: float(filled)
            for client_order_id, filled in (state.get("filled_so_far") or {}).items()
            if client_order_id in self._resting
        }
        self.standing.orders_on_the_book = len(self._resting)
        return len(self._resting)

    def book_signature(self) -> tuple:
        """What is resting, as a value that changes exactly when the book does."""
        return tuple(sorted(
            (order.client_order_id, order.quantity, order.stop_price, order.limit_price)
            for order in self._resting.values()
        ))

    def _upstox_fee_for(self, order, turnover: float) -> float:
        """Which of Upstox's two charge stacks this fill pays, and what it costs.

        The instrument decides, not the venue. A bought option pays a flat
        brokerage and five percentage-of-premium components; a share bought
        intraday pays min(Rs20, 0.1%) and six percentage-of-turnover ones, at
        rates that are not the same rates. Charging one for the other is wrong
        in the base as well as in the number.

        Falls back to the options stack when nothing can say what the instrument
        is, because that is what every Upstox fill was charged before this
        existed and a silent change of cost for the two options segments -- the
        only ones that have ever traded -- would be a worse surprise than the
        fallback. The fallback is counted rather than hidden: a run whose
        `fills_priced_by_the_fallback_stack` is climbing while a cash-equity
        segment is trading is a run reporting fees it did not really compute.
        """
        # The order already says whose money is buying it, and a segment's own
        # settings say what that segment trades -- so the kind is derivable from
        # what is in hand, with no lookup against an instrument master in the
        # fill path. `segment` is carried on OrderRequest for the neighbouring
        # reason that three segments share one spine and each keeps its own
        # balance; this reads the same field rather than adding another.
        kind = self._kinds_by_segment.get(order.segment)

        if kind is None and self._instrument_kind_of is not None:
            try:
                kind = self._instrument_kind_of(order.venue_id, order.symbol)
            except Exception:
                # A resolver that raises must not stop a fill. It means the kind
                # is unknown, which the fallback below already answers for.
                kind = None

        if kind == SPOT and self._equity_intraday_rates:
            self.standing.fills_priced_as_equity += 1
            return upstox_equity_intraday_order_cost(
                turnover, order.side, **self._equity_intraday_rates
            ).total

        if kind == OPTION:
            self.standing.fills_priced_as_options += 1
        else:
            # Either nothing resolved the kind, or it resolved to one this has
            # no rates for. Both are "priced by the fallback", and the counter
            # says so rather than letting an options-priced share pass as
            # measured (Rule 8).
            self.standing.fills_priced_by_the_fallback_stack += 1

        return upstox_options_order_cost(
            turnover, order.side,
            flat_brokerage=self._options_flat_brokerage,
            stt_sell_rate=self._options_stt_sell_rate,
            exchange_transaction_charge_rate=self._options_exchange_transaction_charge_rate,
            ipft_charge_rate=self._options_ipft_charge_rate,
            stamp_duty_buy_rate=self._options_stamp_duty_buy_rate,
            gst_rate=self._options_gst_rate,
        ).total

    def observe_session(self, session) -> None:
        """Which session the market is in. Never inferred from a price arriving:
        a stale price arrives at midnight exactly as a live one does."""
        self._session = session

    @property
    def may_fill(self) -> bool:
        """Whether a fill may happen at all right now, against a live price.

        `None` -- no session measured yet -- is False, not True (Rule 8).
        Absence of evidence is its own state, and filling off an unmeasured
        session is how a paper account trades on a holiday.
        """
        return self._session is not None and self._session.is_tradeable

    def may_fill_against(self, price_fidelity) -> bool:
        """Whether a fill may happen against a price of this kind.

        A historical bar close is its own evidence. Upstox serves a one-minute
        bar for 09:15 IST only because the market traded that minute, so the
        bar's existence proves the session it printed in -- no calendar lookup
        is needed, and none is done. Phase A replays history for exactly the
        hours the wall clock says the market is shut, which is why this cannot
        be answered by `may_fill` alone.

        Every other fidelity is a live price and stays gated. That includes
        LAST_TRADED_PRICE_ONLY, which is Upstox's live ticker rather than
        history: a coarse live price is still a live price, and filling one at
        18:00 against the 15:29 print is the defect this guard exists for.
        """
        if price_fidelity == TradeFidelity.HISTORICAL_BAR_CLOSE:
            return True
        return self.may_fill

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
        """This symbol's prices are continuous again, so it may fill again.

        Until 2026-09-04 this had no caller anywhere in the repository and
        `_jumped_symbols` only ever grew: one discontinuity barred a symbol from
        filling for the life of the process. On the 2026-09-04 tape 993 of 1,474
        streams cross the jump threshold at least once, so within minutes of a
        start two thirds of everything tradeable was unfillable -- 54 of the 111
        orders this part had ever seen were refused for a jump and none had ever
        filled. Nothing reported it, because every one of those refusals was a
        decision this part was entitled to make. Only the return was missing.

        `feed-jump` states continuity in both directions now, so the release is
        driven by the part that measures continuity rather than by a timer here:
        the bar lifts on evidence the feed recovered, not on a clock.
        """
        if (venue_id, symbol) in self._jumped_symbols:
            self.standing.feed_jumps_cleared += 1
        self._jumped_symbols.discard((venue_id, symbol))

    # -- the book ------------------------------------------------------------

    @property
    def resting_orders(self) -> tuple:
        """What is on the book right now, oldest first."""
        return tuple(sorted(self._resting.values(), key=lambda order: order.rested_at_ns))

    def note_release_without_a_verdict(self, client_order_id: str) -> None:
        """An order freed by the wait running out rather than by a latency verdict."""
        self.standing.released_without_a_verdict += 1

    def _withdraw_the_other_half_of_the_bracket(self, order, filled: float) -> None:
        """One exit filled, so the other must give up the same quantity.

        A bracket's stop and target are both sized to the whole position, so a
        venue that fills one and leaves the other resting at full size sells the
        position twice. A real bracket is one-cancels-other; this is that, done
        where the fill happens rather than on the next tick of the part that
        placed them.

        Measured live 2026-09-16 on `HINDUNILVR 1960 PE 29 SEP 26`: at 06:09:04
        the stop sold 3,900 and the target sold 3,900 in the same second against
        a holding of 10,500, and five such pairs fired inside ninety seconds.
        `stop-order-manager` resizes both exits to the position on every tick and
        that is exactly what was not fast enough -- both filled between two
        ticks. Selling twice what is held is how a segment that only buys options
        came to hold them short.

        Reduced rather than always cancelled: a partial fill on one half leaves a
        real position behind, and the other half still protects what is left.
        """
        sibling_id = getattr(order, "linked_exit_order_id", None)
        if not sibling_id or filled <= 0:
            return
        sibling = self._resting.get(sibling_id)
        if sibling is None:
            return
        left = sibling.quantity - filled
        if left <= 0:
            self._resting.pop(sibling_id, None)
            self.standing.bracket_siblings_withdrawn += 1
        else:
            self._resting[sibling_id] = replace(sibling, quantity=left)
            self.standing.bracket_siblings_reduced += 1
        self.standing.orders_on_the_book = len(self._resting)

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

    def evaluate_resting(self, price_by_symbol: dict, price_fidelity=None) -> tuple:
        """Test every order on the book against the price that just arrived.

        This is what makes a paper stop a stop. Called on every tick with the
        latest live price per symbol, so an exit placed minutes ago fills at the
        moment the market reaches it -- not at the moment some later message
        happens to mention that order again.

        Orders whose symbol has no new price are left alone rather than refused: a
        quiet symbol is not a reason to withdraw protection.
        """
        if not self.may_fill_against(price_fidelity):
            # Every resting order stays exactly where it is. A stop is not
            # withdrawn because the market closed -- it waits for the open, the
            # same as it would at a venue.
            #
            # A historical bar passes this, and must: an exit is a resting stop,
            # and a stop that could only trigger against a live price would open
            # a position on a replay and never close it -- a paper account that
            # only ever loses its exits.
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
        price_fidelity=None,
        segment: str = "",
        linked_exit_order_id: str | None = None,
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
            self.standing.refused_live_order += 1
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
                segment=segment, linked_exit_order_id=linked_exit_order_id,
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
        if not self.may_fill_against(price_fidelity):
            self.standing.rested_market_closed += 1
            return self._rest(
                RestingOrder(
                    client_order_id=client_order_id, venue_id=venue_id, symbol=symbol,
                    side=side, quantity=quantity, order_type=order_type,
                    limit_price=limit_price or None, stop_price=stop_price,
                    rested_at_ns=self._now_ns(), leverage=leverage, segment=segment,
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
                            segment=segment,
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
                            segment=segment,
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
                        leverage=leverage, segment=segment,
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
                segment=segment,
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
            #
            # Which of Upstox's two stacks applies is decided by the instrument,
            # not by the venue. Until 2026-09-05 every Upstox fill was charged
            # the options stack, so a cash-equity intraday trade paid 0.1% STT
            # on premium where it really owes 0.025% on turnover, and a
            # transaction charge an order of magnitude too large.
            fee = self._upstox_fee_for(order, fillable * price)
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
        self._withdraw_the_other_half_of_the_bracket(order, fillable)
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
                    leverage=order.leverage, segment=order.segment,
                    linked_exit_order_id=order.linked_exit_order_id,
                )
        self.standing.orders_on_the_book = len(self._resting)

        self._fill_sequence += 1
        filled_at_ns = self._now_ns()
        fill = Fill(
            # The moment is part of the id since 2026-09-13. `_fill_sequence`
            # starts again at zero every time this part starts, so a resting order
            # filled again after a restart reproduced an id already spent: three
            # real fills on 2026-09-07 carried an earlier fill's id, and every
            # book -- each refusing an id it has seen -- dropped them as duplicates.
            fill_id=f"paper-{client_order_id}-{filled_at_ns}-{self._fill_sequence}",
            venue_id=order.venue_id,
            symbol=order.symbol,
            side=order.side,
            price=price,
            quantity=fillable,
            fee=fee,
            filled_at_ns=filled_at_ns,
            order_id=client_order_id,
            is_paper=True,
            # A fill states a price and a quantity, and those are the same number
            # at 1x and at 10x. What the position ties up is the notional over
            # this, and paper-account-keeper has no other source for it.
            leverage=order.leverage,
            # And whose account that is. One paper account per segment since
            # 2026-09-05, and a fill is the only thing that reaches the keeper.
            segment=getattr(order, "segment", ""),
            # The instrument's own lot, which trade-capital-bounds-gate already
            # snapped this order to. Carried so the account keeping the position
            # can tell a real holding from a rounding residue no order could sell.
            quantity_increment=float(getattr(order, "quantity_increment", 0.0) or 0.0),
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
        # One-cancels-other, done at the venue. Both are on health because a
        # bracket that never withdraws its other half sells the position twice
        # and nothing else reports it (2026-09-16).
        "bracket_siblings_withdrawn": simulator.standing.bracket_siblings_withdrawn,
        "bracket_siblings_reduced": simulator.standing.bracket_siblings_reduced,
        "resting": simulator.standing.resting,
        "orders_on_the_book": simulator.standing.orders_on_the_book,
        "stops_triggered": simulator.standing.stops_triggered,
        "cancelled": simulator.standing.cancelled,
        "cancels_for_an_unknown_order": simulator.standing.cancels_for_an_unknown_order,
        "restored_symbols": simulator.standing.restored_symbols,
        "checkpoint_verdict": simulator.standing.checkpoint_verdict,
        "book_changes": simulator.standing.book_changes,
        "refused_because_the_decision_was_stale": simulator.standing.refused_decision_stale,
        "held_in_flight": simulator.standing.held_in_flight,
        "released_without_a_verdict": simulator.standing.released_without_a_verdict,
        "refused_feed_jump": simulator.standing.refused_feed_jump,
        "feed_jumps_cleared": simulator.standing.feed_jumps_cleared,
        "refused_no_price": simulator.standing.refused_no_price,
        "refused_live_order": simulator.standing.refused_live_order,
        # The three below were counted and never published until 2026-09-05, which
        # is why 325 orders were refused on 2026-09-04 with nothing on any board
        # saying so. A refusal nobody can read is the same as a silent one: the
        # part knew exactly why it was not filling and had no way to say it.
        "market_orders_waiting_for_a_first_price": (
            simulator.standing.market_orders_waiting_for_a_first_price
        ),
        "rested_market_closed": simulator.standing.rested_market_closed,
        "sessions_too_many_to_choose": simulator.standing.sessions_too_many_to_choose,
        "refused_already_filled": simulator.standing.refused_already_filled,
        "fees_charged": simulator.standing.fees_charged,
        # Which of Upstox's two charge stacks priced the fills, published because
        # the wrong one is not visibly wrong -- an equity fill charged the
        # options stack still produces a plausible number, twice the real one.
        # `fills_priced_by_the_fallback_stack` climbing is the only outward sign
        # that a fill was priced by a stack nothing confirmed applied to it.
        "fills_priced_as_options": simulator.standing.fills_priced_as_options,
        "fills_priced_as_equity": simulator.standing.fills_priced_as_equity,
        "fills_priced_by_the_fallback_stack": (
            simulator.standing.fills_priced_by_the_fallback_stack
        ),
        "worst_slippage_fraction": simulator.standing.worst_slippage_fraction,
    }


def one_kind_per_segment(context) -> dict:
    """Each built segment, and the single instrument kind it trades.

    This is what lets a fill be charged the right stack without a lookup against
    the instrument master in the fill path: the order already names the segment
    whose money is buying it, and the segment's own settings name what it
    trades. Two settings already in hand, no third source.

    A segment stating more than one kind is left out rather than reduced to its
    first -- which stack such an order paid would then depend on the order of a
    list in a settings file, and "priced by the fallback" is the honest answer
    to a question the settings did not decide. None of the three segments built
    for 2026-09-07 states more than one.
    """
    from runtime.segment_settings import (
        built_segments,
        instrument_types_this_segment_trades,
    )

    kinds: dict[str, str] = {}
    for segment in built_segments(context):
        try:
            types = instrument_types_this_segment_trades(segment)
        except Exception:
            continue
        if len(types) == 1:
            kinds[segment] = types[0]
    return kinds


def run_paper_fill_simulator(
    simulator: PaperFillSimulator, control_socket, read_orders, publish_fills,
    health_interval_seconds: float, emit_health, read_prices=None,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
    write_checkpoint=None,
) -> int:
    """`read_prices` gives the latest live price per symbol, for the book.

    Every tick tests what is resting against what just arrived. Without it an
    order rests forever: the message that placed it came once, and a stop whose
    trigger is only checked when another message mentions it is not a stop.
    """
    def tick() -> None:
        book_before = simulator.book_signature()
        orders = read_orders(simulator)
        results = [simulator.simulate(**order) for order in orders]
        if read_prices is not None:
            prices, fidelity = read_prices()
            results.extend(simulator.evaluate_resting(prices, price_fidelity=fidelity))
        publish_fills(tuple(result.fill for result in results if result.did_fill))
        if write_checkpoint is not None and simulator.book_signature() != book_before:
            # After the fills go out: a checkpoint written first would drop an
            # order from the book whose fill no part ever received. Only when the
            # book changed -- this part ticks on every trade, and rewriting an
            # unchanged book at that rate is the level-on-every-tick defect.
            simulator.standing.book_changes += 1
            write_checkpoint(simulator.standing.book_changes)

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
    from runtime.input_assembly import Batch, LatestByKey, LatestValue, level_for_segment

    requests = Batch(read=context.bus.reader("order-request"))
    trades = Batch(read=context.bus.reader("market-data"))
    # One money mode per segment (2026-09-05), read for the segment the order
    # names rather than for the spine. This part refuses anything that is not
    # paper, so on a spine where one segment goes live it must be able to tell
    # which order that is.
    modes = LatestByKey(
        read=context.bus.reader("money-mode"),
        key_of=lambda mode: mode.segment,
        maximum_age_seconds=context.number("money_mode_maximum_age_seconds"),
    )
    # A level, per exchange segment, aged here as well as at its producer. The
    # bus sends each item of a level as its own message (runtime/bus.py
    # `publish`), so this arrives as one MarketSessionState rather than the
    # tuple the calendar published -- and a calendar that dies would otherwise
    # leave its last "open" standing here forever. Expired reads as no session
    # at all, which does not fill.
    sessions = LatestByKey(
        read=context.bus.reader("market-session-state"),
        key_of=lambda session: session.segment,
        maximum_age_seconds=context.number("market_condition_level_maximum_age_seconds"),
    )
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
    # The kind of price each of those is. A historical bar close is evidence of
    # the session it printed in; every other kind is a live price and stays
    # gated by the calendar. Kept beside the price rather than inferred later:
    # by the time a stop is being tested, the message that carried it is gone.
    last_price_fidelity: dict[tuple[str, str], object] = {}
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
        # Passing None when nothing is standing is the point of the age bound:
        # holding the last "open" after the calendar stopped saying so is how a
        # dead sensor keeps a door open. One segment is read because this part
        # fills for one segment; a second would need its own simulator.
        # One session is all this part can answer for. The calendar publishes the
        # single segment `market_session_segment` names ("FO"), while an order
        # names its bot segment ("index-options"), so the two cannot be matched
        # here and no mapping between them exists to read. While exactly one
        # session stands, it is the one every order is judged against and that is
        # right. If a second ever stands -- a second calendar, or three segment
        # bots each with their own -- this part can no longer tell which order
        # belongs to which, and taking whichever the mapping happened to yield
        # first would apply one exchange's holiday to another exchange's orders,
        # silently and differently on each restart. So it refuses to choose, which
        # reads as no session measured and does not fill (Rule 8).
        standing_sessions = sessions.values()
        if len(standing_sessions) == 1:
            simulator.observe_session(standing_sessions[0])
        else:
            if len(standing_sessions) > 1:
                simulator.standing.sessions_too_many_to_choose += 1
            simulator.observe_session(None)
        for trade in trades_in(trades.payloads()):
            last_price[(trade.venue_id, trade.symbol)] = trade.price
            last_price_fidelity[(trade.venue_id, trade.symbol)] = getattr(
                trade, "fidelity", None
            )
        for jump in jumps.payloads():
            # A level in both directions since 2026-09-04: false is the break,
            # true is the break being over. Read as a level and not an event --
            # the last thing said about a symbol is what is true of it.
            if jump.is_continuous:
                simulator.clear_feed_jump(jump.venue_id, jump.symbol)
            else:
                simulator.observe_feed_jump(jump.venue_id, jump.symbol)
        costs.payloads()
        consolidated.payloads()
        estimate_by_symbol = prices.mapping()
        mode_by_segment = modes.mapping()

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
            # None when this order's segment has published no mode, which this
            # part refuses rather than treating as paper -- the same answer it
            # gave for an absent mode before three segments ran.
            mode = level_for_segment(
                mode_by_segment, getattr(request, "segment", "") or ""
            )
            order = build_order(
                request, key, getattr(mode, "mode", None), estimate_by_symbol
            )
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
        released["price_fidelity"] = last_price_fidelity.get(key)
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
            # Carried from the order request through to the fill, so the account
            # that pays knows which of the three it is (2026-09-05).
            "segment": getattr(request, "segment", ""),
            "linked_exit_order_id": getattr(request, "linked_exit_order_id", None),
            "is_in_flight": False,
            "fill_price_estimate": estimate_by_symbol.get(key),
            "market_price": last_price.get(key),
            "price_fidelity": last_price_fidelity.get(key),
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

    def read_prices() -> tuple[dict, object]:
        """The latest price per symbol, and what kind of price those are.

        The same dictionary the new orders are filled against, so a stop and a
        market order arriving in the same tick see the same market.

        One fidelity for the sweep: it is the kind every symbol's newest price
        shares, or None when they do not agree. A mixed sweep is gated as live,
        which is the safe direction -- a replayed price failing to trigger a
        stop delays an exit, while a live price triggering one outside a session
        invents a trade.
        """
        kinds = set(last_price_fidelity.values())
        return dict(last_price), (kinds.pop() if len(kinds) == 1 else None)

    simulator = PaperFillSimulator(
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
        equity_intraday_rates={
            "flat_brokerage": context.number("equity_intraday_flat_brokerage"),
            "brokerage_rate": context.number("equity_intraday_brokerage_rate"),
            "stt_sell_rate": context.number("equity_intraday_stt_sell_rate"),
            "exchange_transaction_charge_rate": context.number(
                "equity_intraday_exchange_transaction_charge_rate"
            ),
            "ipft_charge_rate": context.number("equity_intraday_ipft_charge_rate"),
            "stamp_duty_buy_rate": context.number("equity_intraday_stamp_duty_buy_rate"),
            "sebi_charge_rate": context.number("equity_intraday_sebi_charge_rate"),
            "gst_rate": context.number("equity_intraday_gst_rate"),
        },
        kinds_by_segment=one_kind_per_segment(context),
    )
    import pathlib

    from runtime.durable_state import (
        CheckpointSchedule,
        DurableStateStore,
        restore_and_arm_checkpoint,
    )

    store = DurableStateStore(
        pathlib.Path(str(context.setting("position_state_root").value)).expanduser()
    )
    store.root.mkdir(parents=True, exist_ok=True)
    # Every change to the book: a resting stop lost to a restart is a position
    # with no stop, which is what this checkpoint exists to prevent.
    write_checkpoint = restore_and_arm_checkpoint(
        store, CheckpointSchedule(1), PART_ID, CHECKPOINT_COMPONENT, simulator, {},
    )

    return run_paper_fill_simulator(
        simulator=simulator,
        control_socket=context.control_socket,
        read_orders=read_orders,
        publish_fills=publish_fills,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
        read_prices=read_prices,
        write_checkpoint=write_checkpoint,
    )
