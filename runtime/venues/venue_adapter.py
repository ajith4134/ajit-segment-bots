"""The shape every venue adapter is: the questions a venue must answer, and the
only place in the project where a venue's oddities are allowed to live.

The user's condition on phase 1 was *"mark it so if the 2 are not enough we can
add more later"*. That makes venue-independence structural rather than
aspirational, and this module is where the structure sits (spec section 3.1):

- exactly one module per venue, in this package
- a fixed question set every one of them answers
- no part imports a venue module; a part is handed an adapter, and the set of
  adapters comes from settings (T-4 at the venue boundary -- a part names data,
  never a venue)

The question set is deliberately phrased so the caller never computes anything
venue-shaped itself. "Does one more subscription fit" is the sharpest example:
Binance's limit is 1024 *streams* per connection and Bybit's is 21,000
*characters* of subscribe payload, so a caller that counted subscriptions would
be right for one venue and silently wrong for the other. The adapter answers the
question; nothing outside counts.

RL-061 and venue facts. A venue's stated limits are numbers a part acts on, so
they cannot be anonymous literals. They are not settings either -- the operator
does not get to decide that Binance allows 1024 streams. So they are declared as
`VenueFact`s carrying value, unit and the source they were read from, which is
the same three-part shape `settings_reader.SettingEntry` uses for the numbers the
operator *does* own. A fact with no source is refused at construction.
"""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass
from typing import Mapping, Sequence

from runtime.tape import NOT_SENT, StreamKind, TradeFidelity
from runtime.trading_types import BUY

__all__ = [
    "BanSignal",
    "ConnectionDiscipline",
    "ContractFunding",
    "HeartbeatDiscipline",
    "MessageFacts",
    "NormalisedTrade",
    "SequenceContinuity",
    "QUESTIONS_ANSWERED_FROM_A_VENUE_MESSAGE",
    "QUESTIONS_ANSWERED_WITHOUT_VENUE_DATA",
    "StreamRequest",
    "SymbolListing",
    "VenueAdapter",
    "VenueFact",
    "VenueFactWithoutSource",
    "VenueMessageNotRecognised",
]


class VenueFactWithoutSource(ValueError):
    """A venue limit was declared without saying where it was read from."""


class VenueMessageNotRecognised(ValueError):
    """A venue sent a data message this adapter has no reading for.

    Deliberately not the same as `read_message_facts` returning None. None means
    "this is a control frame and belongs on no tape"; this means "the venue sent
    something real and we do not know what it is", which is a fact about the
    adapter being out of date and must be visible rather than silently dropped.
    Whether that ends the capture is the reading part's call, not the adapter's --
    a venue adding an event type should not take a tape down.
    """


@dataclass(frozen=True)
class VenueFact:
    """One number the venue itself fixes, and where that number was read.

    The operator owns settings; the venue owns these. Both carry provenance,
    because RL-061 is about a number being answerable for, not about which side
    of the boundary it came from.
    """

    name: str
    # A tuple is admitted because some venue facts are an enumeration rather than
    # a magnitude -- the depth levels a venue offers, the update speeds it pushes
    # at. Written as a string it would be a fact the code had to re-parse, which
    # is a literal with extra steps.
    value: int | float | str | tuple[int | str, ...]
    unit: str
    source: str

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise VenueFactWithoutSource(
                f"venue fact '{self.name}' carries no source. A capacity figure a part "
                f"acts on must say which document or measurement it came from -- RL-061 "
                f"does not stop at the venue boundary."
            )


class SequenceContinuity(enum.StrEnum):
    """What a venue's sequence numbers promise on one stream, so §6 can check it.

    The gap detector needs one comparison rather than one per venue, and the only
    thing that varies is what "continuous" means here. Getting this wrong in
    either direction is expensive: a stream declared stricter than it is cries
    gap on every message, and one declared looser than it is silently accepts a
    missed message -- which on a delta book means every later price is wrong
    while the feed still looks healthy.
    """

    # The venue numbers nothing on this stream, so silence is the only detector.
    NOT_NUMBERED = "not-numbered"
    # Each message's sequence is the previous one plus one.
    INCREMENTS_BY_ONE = "increments-by-one"
    # Sequences never go backwards but may repeat or jump; a jump is not a gap.
    NON_DECREASING = "non-decreasing"
    # Each message names its predecessor's sequence, so continuity is checked
    # against what the message itself claims rather than against arithmetic.
    CHAINED_TO_PREVIOUS = "chained-to-previous"


# A request for every symbol at once rather than for one. Only meaningful on a
# stream kind whose venue offers an all-market topic -- `every_symbol_quote_topic`
# is how a caller finds out, and asking for this where the venue has no such topic
# is refused rather than silently turned into one symbol named "*".
#
# It exists so a plan says what will really be opened. Binance quotes the whole
# market on one topic; planning 872 per-symbol topics and then opening one would
# make the plan's own connection count fiction, and that count is what the open
# file ceiling is checked against.
EVERY_SYMBOL = "*"


@dataclass(frozen=True)
class StreamRequest:
    """One thing we want a venue to stream: a kind, a symbol, and its parameters.

    Depth and interval live here rather than in separate methods because they are
    what makes a subscription concrete, and because a venue couples them to its
    push rate: choosing Bybit's book depth chooses how often it sends.
    """

    stream_kind: StreamKind
    symbol: str
    candle_interval: str | None = None
    book_depth_levels: int | None = None


@dataclass(frozen=True)
class VenueRequest:
    """One REST call an adapter asks the reader to make on its behalf.

    Headers exist because a private endpoint needs them: the adapter knows how
    this venue authenticates and the reader knows how to fetch, and neither
    should have to learn the other's half. A public request carries none.

    `describes` names what the response is expected to contain -- a symbol, or
    the whole venue -- so a reader can attribute a failure to the thing it was
    asking about rather than to a URL.
    """

    url: str
    describes: str
    headers: dict | None = None


@dataclass(frozen=True)
class SymbolListing:
    """One contract a venue lists, as the venue describes it.

    `contract_type` is kept rather than filtered on. Binance USDⓈ-M lists ~170
    tokenised equities as `TRADIFI_PERPETUAL` alongside its crypto perpetuals,
    quoted in USDT and indistinguishable from outside except by this field, and
    the user's ruling on 2026-08-21 was to capture them and decide tradeability
    later: capture is irreversible, a filter at order time is not (spec 1.1).
    """

    symbol: str
    contract_type: str
    status: str
    quote_volume_24h: float | None = None
    price_increment: float | None = None
    # `contract_type` translated into this system's words -- one of
    # trading_types.PERPETUAL_FUTURE, DATED_FUTURE, SPOT, OPTION -- because how a
    # position is charged for being held depends on which it is, and no part may
    # recognise a venue's spelling to find out (T-4). None when this adapter does
    # not know the word the venue used, which is a fact about the adapter being
    # out of date and must not read as any particular kind of contract: pricing an
    # unrecognised listing as a perpetual would charge a quarterly's basis as
    # funding, and be wrong in the same direction every time.
    instrument_kind: str | None = None
    # How often this contract settles funding, per day, when the venue states it
    # in the catalogue response itself -- Bybit does, in `fundingInterval`
    # minutes. None where the catalogue does not say, which is Binance: its
    # exchangeInfo is silent and the figure arrives on a separate endpoint
    # instead, through `read_funding_facts`. None means undeclared, never
    # "the usual eight hours".
    funding_settlements_per_day: float | None = None


@dataclass(frozen=True)
class ContractFunding:
    """What a venue says holding one of its perpetuals costs, and where it said it.

    Two numbers, because a funding rate alone prices nothing: a rate is charged
    per settlement, and 0.01% costs three times as much on a contract that
    settles every four hours as on one that settles every eight. Both are the
    venue's to state.

    `settlements_per_day` is None when the venue declares a rate but not an
    interval -- 132 of Binance's 872 listed symbols on 2026-08-22. That is
    carried rather than filled in with the documented default: an assumed
    eight-hour interval is a carry cost wrong by a factor of two on every
    four-hourly symbol, and it would be wrong invisibly.

    `rate_cap`/`rate_floor`/`interest_rate_per_interval` are the rest of the
    venue's own funding formula -- `funding-rate-forecaster` needs all three
    to compute a settlement the way the venue does rather than fit a curve to
    past rates, and they vary by symbol on both venues (measured: Binance's
    `adjustedFundingRateCap` runs 0.003 to 0.02 across symbols on 2026-08-28;
    Bybit's `fundingCap` differs too). None where the venue did not state one
    for this symbol on this read -- never a platform default, for the same
    reason `settlements_per_day` is never assumed to be eight hours.
    """

    symbol: str
    rate_per_settlement: float
    settlements_per_day: float | None
    source: str
    rate_cap: float | None
    rate_floor: float | None
    interest_rate_per_interval: float | None

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise VenueFactWithoutSource(
                f"the funding rate for {self.symbol} carries no source. It is a number a "
                f"position is priced against, so which endpoint declared it is part of it "
                f"(RL-061)."
            )


@dataclass(frozen=True)
class MessageFacts:
    """What the tape needs out of a venue message, without normalising the message.

    The payload itself goes onto the tape exactly as it arrived (spec 2.2); these
    are only the fields the index is built from, plus the closed-candle flag the
    candle reader needs. Absent fields are `NOT_SENT`, never a guessed value: no
    venue numbers a message zero and none timestamps the epoch, so a reader can
    tell "the venue said nothing" from "the venue said something small".
    """

    stream_kind: StreamKind
    symbol: str
    venue_time_ns: int = NOT_SENT
    sequence: int = NOT_SENT
    is_closed_candle: bool | None = None
    # True when this message restarts the numbering rather than continuing it --
    # Bybit re-sends a full book snapshot when its own service restarts, and the
    # sequence after it has no relationship to the one before. Without this the
    # gap detector would report a gap at every legitimate resnapshot, which is
    # the fastest way to make a real gap invisible among false ones.
    resets_sequence: bool = False


@dataclass(frozen=True)
class NormalisedTrade:
    """One trade, in this project's own terms rather than a venue's.

    Normalisation happens on read (spec section 2.2): the tape keeps the venue's
    bytes, and this is what a reader hands the rest of the system. Doing it here,
    once, is the whole point -- `market-data` has 65 consumers in the blueprint, and
    a design where each of them parsed a venue's JSON would be 65 parts that know
    what a venue looks like, which is the opposite of what an adapter is for.

    `side` is the **aggressor's** side, always. Venues do not agree on how to say
    it: Binance sends whether the buyer was the maker, Bybit sends the taker's
    direction outright. A consumer reasoning about buying pressure must not have to
    know which venue phrased it which way.

    `fidelity` travels with the trade because the two venues do not mean the same
    thing by "a trade": Binance offers only 100 ms aggregates and Bybit sends every
    print. A consumer counting trades per second is counting different things on
    each, and this is what lets it know that.
    """

    venue_id: str
    symbol: str
    price: float
    quantity: float
    side: str
    venue_time_ns: int
    sequence: int
    fidelity: TradeFidelity

    @property
    def signed_quantity(self) -> float:
        """Positive when the aggressor bought, negative when it sold."""
        return self.quantity if self.side == BUY else -self.quantity

    @property
    def quote_volume(self) -> float:
        return self.price * self.quantity


@dataclass(frozen=True)
class NormalisedCandle:
    """One candle update, in this project's own terms, with the venue's closed flag.

    Both venues push updates to the *current* candle continuously and only one
    field says which update is the last one for its minute -- `k.x` on Binance,
    `confirm` on Bybit. That flag is carried rather than used to filter, because
    a consumer that wants the live bar and one that wants only finished bars are
    both legitimate and neither should have to know what a venue calls it.
    """

    venue_id: str
    symbol: str
    interval: str
    open_time_ns: int
    close_time_ns: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    # None when the venue does not send a count: Bybit's kline stream has no
    # trade count, and zero would read as a minute in which nothing traded.
    trades: int | None
    is_closed: bool
    venue_time_ns: int


@dataclass(frozen=True)
class BookUpdate:
    """One order-book message in this project's terms: levels, and whether they
    replace the book or amend it.

    Binance's partial-depth stream sends a fresh top-N every push, so every
    message is a snapshot. Bybit sends one snapshot and then deltas in which a
    quantity of zero removes the level. The flag is what lets one keeper hold
    both without knowing which venue phrased it which way.
    """

    venue_id: str
    symbol: str
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    is_snapshot: bool
    sequence: int
    venue_time_ns: int


@dataclass(frozen=True)
class QuoteChange:
    """What one message said about a symbol's best bid and ask -- possibly one side.

    Not every venue restates a whole quote. Bybit's `tickers` stream sends one
    snapshot and then deltas carrying only what moved, and a real one measured
    2026-08-24 was exactly this:

        {"topic":"tickers.BTCUSDT","type":"delta",
         "data":{"symbol":"BTCUSDT","bid1Price":"79114.50","bid1Size":"1.496"},
         "cs":793092205004,"ts":1787590405983}

    A bid, no ask. An adapter that filled the ask with zero would be inventing a
    market; one that filled it from the last message it saw would be holding
    state, and an adapter is a reader of bytes rather than a keeper of books. So
    the adapter reports the absence as `None` and whoever is assembling quotes
    merges -- which is why this type exists separately from NormalisedQuote.

    `None` therefore means "this message did not say", never "zero" and never
    "unchanged as far as we know". The difference is the whole point.
    """

    venue_id: str
    symbol: str
    bid_price: float | None
    bid_quantity: float | None
    ask_price: float | None
    ask_quantity: float | None
    venue_time_ns: int
    # True when the message restates the symbol's whole state rather than amending
    # it -- Bybit's first `tickers` message per subscription, and every Binance
    # `!bookTicker` frame. A merge may drop what it held on a snapshot; on a delta
    # it must not.
    is_snapshot: bool

    def is_complete(self) -> bool:
        """Whether this message alone names both sides, so nothing has to be remembered."""
        return None not in (self.bid_price, self.bid_quantity, self.ask_price, self.ask_quantity)


@dataclass(frozen=True)
class NormalisedQuote:
    """One symbol's resting best bid and ask on one venue, at the venue's own moment.

    The fact a print is not. A symbol that has not traded for ten minutes has no
    recent trade and still has a quote, which is why this exists: measured on the
    live run of 2026-08-24, instrument-selector refused 525 of 9 945 intents for a
    price too old and none at all for never having seen a price. Those symbols
    were captured; nobody had traded them.

    Sizes travel with the prices because a quote with no size behind it is a
    number rather than a market, and a caller deciding whether to believe the mid
    needs both. `venue_time_ns` is the venue's stamp, never our arrival time --
    the whole value of a quote is its age, and an age measured from when we
    happened to read the socket is our latency, not the market's.
    """

    venue_id: str
    symbol: str
    bid_price: float
    bid_quantity: float
    ask_price: float
    ask_quantity: float
    venue_time_ns: int

    @property
    def mid_price(self) -> float:
        """Halfway between the two, which is the price to size against."""
        return (self.bid_price + self.ask_price) / 2.0

    @property
    def spread(self) -> float:
        return self.ask_price - self.bid_price


@dataclass(frozen=True)
class VenuePremium:
    """What a perpetual is marked at against its index, and what that costs.

    The premium -- mark minus index, as a fraction of the index -- is what a
    venue averages into the next funding rate, so a system that wants to know
    what holding a position will cost before it opens one has to see both
    numbers. Neither is on any other wire: `market-data` is what printed and
    `market-quote` is what is resting, and a perpetual's mark price is neither.

    `declared_funding_rate` and `next_settlement_at_ns` ride along because both
    venues send them in the same message and a reader that dropped them would be
    asking for them again a moment later. They are the venue's own statement of
    the rate it will charge, not a forecast of it.

    Any of the four may be None: Bybit amends rather than restates, so a delta
    can carry a mark price and nothing else. Missing is not zero here, for the
    reason it never is -- a premium of zero says the perpetual is trading exactly
    at its index, which is a claim about the market rather than about the
    message.
    """

    venue_id: str
    symbol: str
    mark_price: float | None
    index_price: float | None
    declared_funding_rate: float | None
    next_settlement_at_ns: int | None
    venue_time_ns: int

    @property
    def premium_fraction(self) -> float | None:
        """How far the perpetual sits from its index, as a fraction of the index."""
        if self.mark_price is None or not self.index_price:
            return None
        return (self.mark_price - self.index_price) / self.index_price


@dataclass(frozen=True)
class BanSignal:
    """The venue telling us to stop, in whatever form that venue tells us.

    Binance escalates 429 to 418 and bans an IP for two minutes to three days;
    Bybit answers 403 with code 20003 and lifts it automatically after at least
    ten minutes. Both ban per IP, so one box means a mistake against one venue
    does not protect the other, and neither ban ends when a part restarts.
    """

    venue_id: str
    observed_code: str
    reason: str
    retry_after_seconds: float | None = None


@dataclass(frozen=True)
class ConnectionDiscipline:
    """What a venue says about connections themselves, rather than about data.

    Every field is optional because for both phase 1 venues at least one of them
    is genuinely unstated, and an unstated limit is not an absent one. Binance
    documents no connections-per-IP figure for futures at all -- the widely
    repeated "300 per 5 minutes" is a spot number -- and Bybit's 10-minute idle
    cutoff is documented only for private connections. `None` here means "the
    venue does not say", which a caller must treat as a thing it cannot check
    rather than as permission (Rule 8 at the venue boundary).
    """

    # The age at which the venue closes a connection on purpose. A close at this
    # age is routine and must not escalate a backoff: Binance disconnects every
    # connection at 24 hours, so a reader treating it as a fault would back off
    # further every day for no reason.
    lifetime_seconds: float | None = None
    new_connections_per_window: int | None = None
    rate_window_seconds: float | None = None
    concurrent_connections: int | None = None


@dataclass(frozen=True)
class HeartbeatDiscipline:
    """How this venue expects a live connection to be kept alive.

    Venues differ on who pings. Binance sends a ping frame and expects a pong
    inside its own window; Bybit expects the client to send an application-level
    ping on an interval. A connection that gets this wrong is closed by the venue
    and reads exactly like a network fault, so it is the adapter's answer.
    """

    expects_client_ping: bool
    interval_seconds: float | None = None
    ping_frame: bytes | None = None


class VenueAdapter(abc.ABC):
    """Every question a venue has to answer, and nothing a part has to know.

    Subclasses are the only code in the project permitted to know a venue's URL,
    message shape, limits or error codes. `adapter_registry` resolves them by id
    from settings, so no import anywhere names one.
    """

    @property
    @abc.abstractmethod
    def venue_id(self) -> str:
        """The id this venue is named by in settings and on the tape."""

    @property
    @abc.abstractmethod
    def trade_fidelity(self) -> TradeFidelity:
        """Whether this venue's trades are every print or its own aggregates.

        Binance USDⓈ-M has no raw trade stream at all -- only `@aggTrade`, which
        is aggregated per 100 ms -- while Bybit's `publicTrade` is every print.
        A tape that stored the two identically would claim a resolution one of
        them does not have, so this travels with the data (spec section 7).
        """

    @abc.abstractmethod
    def declared_limits(self) -> Mapping[str, VenueFact]:
        """Every capacity figure this adapter acts on, each with its source."""

    @abc.abstractmethod
    def stream_endpoint_url(self, stream_kind: StreamKind) -> str:
        """The websocket URL for this stream kind, routed path included.

        Binance has split its endpoint into `/public`, `/market` and `/private`,
        and a connection that omits the route **stays open and delivers nothing**
        -- no error, no close, an empty tape. That hazard lives in this method
        and nowhere else (spec 1.1).
        """

    @abc.abstractmethod
    def subscription_topic(self, request: StreamRequest) -> str:
        """How this venue phrases one subscription."""

    @abc.abstractmethod
    def does_topic_fit_connection(self, existing_topics: Sequence[str], candidate: str) -> bool:
        """Whether one more topic fits on a connection already carrying these.

        The whole point of asking rather than counting: Binance's cap is a stream
        count, Bybit's is a character count of the subscribe payload, and the
        second scales with symbol-name length. A caller that multiplied a symbol
        count by anything would be wrong at exactly the moment the universe grew.
        """

    @abc.abstractmethod
    def subscribe_frame(self, topics: Sequence[str]) -> bytes:
        """The bytes that subscribe to these topics on this venue."""

    @abc.abstractmethod
    def unsubscribe_frame(self, topics: Sequence[str]) -> bytes:
        """The bytes that unsubscribe from these topics on this venue."""

    @abc.abstractmethod
    def heartbeat_discipline(self) -> HeartbeatDiscipline:
        """Who pings whom, how often, and with what."""

    @abc.abstractmethod
    def connection_discipline(self) -> ConnectionDiscipline:
        """What this venue says about opening, holding and losing connections."""

    @abc.abstractmethod
    def book_stream_delivers_full_depth(self) -> bool:
        """Whether every book message stands alone, or only makes sense in sequence.

        True when each message carries the whole requested depth -- Binance's
        partial-depth stream sends a fresh top-N every push -- so a reader may
        record one message every few seconds and lose only resolution.

        False when messages are deltas against a snapshot the venue sent once, as
        Bybit's are. Dropping one of those does not cost resolution, it costs the
        book: every later price is wrong, and nothing in the record says so. A
        reader that thinned a delta stream would produce a tape that cannot be
        replayed and cannot be told from one that can.
        """

    @abc.abstractmethod
    def sequence_continuity(self, stream_kind: StreamKind) -> SequenceContinuity:
        """What this venue's sequence numbers promise on this stream.

        Answered per stream kind because one venue can promise different things
        on different streams -- Binance chains its book by `pu` while numbering
        its trades one by one, and its candles carry no sequence at all.
        """

    @abc.abstractmethod
    def read_message_facts(self, payload: bytes) -> MessageFacts | None:
        """The index fields inside one raw venue message, or None if it carries no data.

        None means a control frame -- a subscribe acknowledgement, a pong, an
        error envelope. Those are not tape records, and telling them apart is
        venue knowledge, so the adapter decides rather than the caller guessing
        from a missing field.
        """

    @abc.abstractmethod
    def read_trades(self, payload: bytes) -> tuple[NormalisedTrade, ...]:
        """Every trade inside one stream message, in this project's terms.

        Returns an empty tuple for a message that carries no trades -- a control
        frame, a candle, a book update -- so a caller never has to ask what kind of
        message it has before asking for its trades.

        A tuple rather than one trade because a venue decides how many it packs
        into a message: Binance sends one aggregate per frame, Bybit sends a list.
        A signature that returned one would have quietly dropped the rest.
        """

    @abc.abstractmethod
    def read_candles(self, payload: bytes) -> tuple[NormalisedCandle, ...]:
        """Every candle update inside one stream message, in this project's terms.

        Empty for a message that carries no candle, for the same reason
        read_trades is: a caller never has to ask what kind of message it holds.
        A tuple because Bybit packs the closing update of one minute and the
        opening update of the next into one message.
        """

    @abc.abstractmethod
    def read_premiums(self, payload: bytes) -> tuple[VenuePremium, ...]:
        """What one stream message says about mark price, index price and funding.

        Empty for a message carrying none, for the same reason read_trades is: a
        caller never has to ask what kind of message it holds.

        A tuple because the venues differ by three orders of magnitude in how
        they pack it -- Binance sends every listed symbol in one frame once a
        second, Bybit one symbol per frame -- and a caller written for many is
        correct for both.
        """

    @abc.abstractmethod
    def read_book_update(self, payload: bytes) -> BookUpdate | None:
        """The book levels inside one stream message, or None for a message that
        carries no book -- a trade, a candle, a control frame."""

    @abc.abstractmethod
    def read_quote_changes(self, payload: bytes) -> tuple[QuoteChange, ...]:
        """What one stream message says about best bids and asks, in this project's terms.

        Empty for a message carrying no quote, for the same reason read_trades is.

        A tuple because a venue decides how many it packs into a frame: Binance's
        `!bookTicker` sends one symbol per frame while a venue that batched would
        send many.

        Changes rather than quotes, because a venue that amends rather than
        restates can name one side and no other -- see QuoteChange. A venue that
        always restates returns changes that are all complete, so a caller written
        for the general case is correct for both, and a caller that assumed
        completeness would drop Bybit's entire stream but its rare snapshots.
        """

    @abc.abstractmethod
    def quote_stream_amends_rather_than_restates(self) -> bool:
        """Whether this venue's quote stream sends changes rather than whole quotes.

        True means a reader must hold the last complete quote per symbol and
        merge, because a message can carry one side. False means every message
        stands alone. Measured rather than assumed: Binance `!bookTicker` restates
        both sides every frame; Bybit `tickers` snapshots once and then amends.
        """

    @abc.abstractmethod
    def every_symbol_quote_topic(self) -> str | None:
        """The one topic that quotes the whole market, or None if there is no such thing.

        Binance has `!bookTicker`, so one subscription covers every symbol listed
        and every symbol listed later, at no cost per symbol. Bybit has no
        wildcard: each symbol is named, and whether they fit one connection is a
        question for `does_topic_fit_connection` -- measured 2026-08-24, all 833
        linear symbols serialise to 17 006 characters against a 21 000 cap, which
        is 81% of it and not a guarantee about tomorrow's symbol names.

        None is therefore not a failure. It is the venue saying "name them", and
        a reader that treated it as one would drop Bybit entirely.
        """

    @abc.abstractmethod
    def read_previous_sequence(self, payload: bytes) -> int | None:
        """The sequence this message says its predecessor had, or None.

        Only a `CHAINED_TO_PREVIOUS` stream answers this with a number. It exists
        on the shape rather than on the one venue that has it so that §6's check
        is one comparison over an adapter's answers, not a branch on which venue
        is being read.
        """

    @abc.abstractmethod
    def read_http_ban_signal(
        self, status_code: int, headers: Mapping[str, str]
    ) -> BanSignal | None:
        """A ban this venue signalled over REST, or None if it did not."""

    @abc.abstractmethod
    def read_stream_ban_signal(self, payload: bytes) -> BanSignal | None:
        """A ban this venue signalled over the websocket, or None if it did not."""

    @abc.abstractmethod
    def catalogue_url(self, cursor: str | None = None) -> str:
        """Where this venue's contract list is fetched from, one page at a time.

        Paged because one of them is: Bybit's instruments-info returns 500
        entries by default against 837 live symbols, and asking without a cursor
        silently returns a prefix. A truncated universe is the worst kind of
        wrong here -- the symbols missing from it are captured by nobody, and
        nothing about the response says any are missing.
        """

    @abc.abstractmethod
    def ticker_url(self) -> str:
        """Where this venue's 24-hour volume figures are fetched from over REST.

        Separate from the catalogue because both venues serve them separately:
        what a contract *is* and how much of it traded are different endpoints
        with different weights, and merging them is the reader's job.
        """

    @abc.abstractmethod
    def funding_request_urls(self) -> tuple[str, ...]:
        """The extra REST calls this venue needs before its funding can be stated.

        Empty for a venue that already puts funding in the catalogue and ticker
        responses the reader fetches anyway -- Bybit does, so asking it again
        would be a request paid for nothing. Binance publishes neither figure on
        either endpoint, so it names two: the per-symbol rate and the per-symbol
        settlement interval live apart from each other and apart from everything
        else.

        The order is the adapter's own and is fed straight back to
        `read_funding_facts`, which is the only thing that has to know it.
        """

    # -- the maintenance margin ladder ---------------------------------------
    #
    # Deliberately NOT abstract, and the reason is a measured one. On 2026-08-25
    # an abstract `read_premiums` was added and implemented for Binance only, so
    # `BybitLinearAdapter` could not be constructed and every part that loads a
    # venue adapter crash-looped -- invisibly, for hours, because the running
    # spine held the pre-change code in memory. A default that answers "this
    # adapter has no schedule to offer" cannot do that to a venue nobody got to
    # yet, and the reader already distinguishes an empty ladder from a zero rate.

    def margin_schedule_requests(self, symbols: Sequence[str]) -> tuple["VenueRequest", ...]:
        """The REST calls this venue needs before its maintenance margin can be stated.

        Empty for an adapter that cannot state one, which is not the same as a
        venue with no maintenance margin -- every perpetual venue has one, so an
        empty tuple here is always a fact about this process rather than about
        the market. `margin_schedule_unavailable_reason` says which.

        `symbols` are the ones actually being captured. A venue that serves its
        whole schedule in one call may ignore them; a venue that serves one
        symbol per call must not ask for the eight hundred nobody is watching.
        """
        return ()

    def margin_schedule_unavailable_reason(self) -> str | None:
        """Why `margin_schedule_requests` is empty, or None when it is not.

        Always safe to print. A venue whose schedule sits behind a signed
        endpoint says so and names what is missing; it never includes any part of
        a credential that was found.
        """
        return None

    def read_margin_tiers(self, responses: Sequence[object]) -> Mapping[str, tuple]:
        """This venue's maintenance margin ladder per symbol, from its own responses.

        Keyed by the venue's own symbol string. A symbol absent from the mapping
        has no ladder that could be read, and a reader must carry that absence
        rather than filling it: a maintenance margin of zero puts a liquidation
        price at the entry, so a defaulted ladder does not make a map slightly
        wrong, it makes every cluster in it wrong in the same direction.
        """
        return {}

    @abc.abstractmethod
    def read_funding_facts(
        self,
        listings: Sequence["SymbolListing"],
        ticker_response: object,
        funding_responses: Sequence[object],
    ) -> Mapping[str, "ContractFunding"]:
        """What each perpetual costs to hold, out of this venue's own responses.

        Given everything the reader has already fetched -- the listings from the
        catalogue, the ticker response, and the decoded responses of
        `funding_request_urls` in that order -- because *which* of them carries
        funding is exactly what differs between venues, and is therefore the
        adapter's business rather than the reader's. Bybit's rate is on the
        ticker and its interval on the catalogue; Binance publishes neither on
        either and names two endpoints of its own. The reader does the same thing
        for both: hand over what it has, receive one mapping.

        A symbol the venue quotes no rate for is absent from the mapping rather
        than present with a zero. Zero funding is a real and common state, and a
        symbol that is merely unquoted must not be indistinguishable from it.
        """

    @abc.abstractmethod
    def read_symbol_listings(self, catalogue_response: object) -> tuple[SymbolListing, ...]:
        """Every contract this venue lists, as listings, from its own catalogue response."""

    @abc.abstractmethod
    def read_catalogue_cursor(self, catalogue_response: object) -> str | None:
        """The cursor for the next page of this catalogue, or None when it is whole."""

    @abc.abstractmethod
    def read_quote_volumes(self, ticker_response: object) -> Mapping[str, float]:
        """Each symbol's 24-hour quote volume, from this venue's own ticker response.

        Quote volume rather than base volume, because it is the only figure
        comparable across symbols and across venues: both quote in USDT, while
        base volumes are counts of different coins.
        """

    @abc.abstractmethod
    def is_symbol_capturable(self, listing: SymbolListing) -> bool:
        """Whether this listing is one the tape should carry at all.

        Not a tradeability judgement: the ruling of 2026-08-21 keeps every
        contract type, including tokenised equities, because a day not captured
        is gone permanently while a filter at order time costs nothing. What this
        excludes is a contract that will stop existing -- Binance's `SETTLING`.
        """


# The questions a conformance test can put to an adapter with nothing but the
# adapter itself. Split from the next tuple because these need no venue data, so
# every adapter can be asked all of them without a network or a captured tape.
QUESTIONS_ANSWERED_WITHOUT_VENUE_DATA = (
    "venue_id",
    "trade_fidelity",
    "declared_limits",
    "stream_endpoint_url",
    "subscription_topic",
    "does_topic_fit_connection",
    "subscribe_frame",
    "unsubscribe_frame",
    "heartbeat_discipline",
    "connection_discipline",
    "catalogue_url",
    "ticker_url",
    "funding_request_urls",
    "book_stream_delivers_full_depth",
    "sequence_continuity",
    "quote_stream_amends_rather_than_restates",
    "every_symbol_quote_topic",
)

# The questions that need a real message or a real catalogue response to ask.
# RL-063 forbids inventing one, so the conformance test checks these are answered
# by the adapter rather than inherited unimplemented, and each venue's own tests
# exercise them against payloads that venue actually sent.
QUESTIONS_ANSWERED_FROM_A_VENUE_MESSAGE = (
    "read_message_facts",
    "read_trades",
    "read_candles",
    "read_book_update",
    "read_quote_changes",
    "read_premiums",
    "read_previous_sequence",
    "read_catalogue_cursor",
    "read_quote_volumes",
    "read_http_ban_signal",
    "read_stream_ban_signal",
    "read_symbol_listings",
    "read_funding_facts",
    "is_symbol_capturable",
)
