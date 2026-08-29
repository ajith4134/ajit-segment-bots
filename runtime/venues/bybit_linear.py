"""Bybit v5 linear (USDT) perpetuals, and every oddity that venue has.

Facts quoted from Bybit's own documentation, fetched raw on 2026-08-21 and saved
with their URLs in `~/research/segment-bots-phase1/bybit-v5-stream-limits.md`, or
measured live on 2026-08-22 and saved under `tests/captured/`.

Three of them decide the shape of everything here:

**The cap is on characters, not on topics.** "For one public connection, you
cannot have length of `args` array over 21,000 characters", and for futures there
is no stated topic-count cap at all. So how many symbols fit depends on how long
their names are, and a caller that multiplied a symbol count by anything would be
right until the day a long symbol listed. `does_topic_fit_connection` measures the
serialised array; nothing outside this module counts anything.

**Nothing will resync the book for us.** Bybit's book is a delta stream carrying
an update id, and its own reference SDK, `pybit`, implements no continuity check
whatsoever -- it applies every delta in arrival order and stores `u` without ever
comparing it. So a shallow book capture that trusts the library is untrustworthy
by construction, and §6's check is this project's own job. Measured on
2026-08-22: `u` does step by exactly one on consecutive deltas, which the
documentation never states.

**The client pings here.** Binance pings us; Bybit expects a ping every 20
seconds and closes a connection that stops sending them, which fails in a way
indistinguishable from a network fault.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Mapping, Sequence

from runtime.tape import NOT_SENT, StreamKind, TradeFidelity
from runtime.trading_types import BUY, DATED_FUTURE, PERPETUAL_FUTURE, SELL
from runtime.symbol_universe import MarginTier
from runtime.venues.venue_adapter import (
    BanSignal,
    ConnectionDiscipline,
    ContractFunding,
    HeartbeatDiscipline,
    MessageFacts,
    BookUpdate,
    NormalisedCandle,
    EVERY_SYMBOL,
    NormalisedTrade,
    QuoteChange,
    SequenceContinuity,
    StreamRequest,
    SymbolListing,
    VenueAdapter,
    VenueFact,
    VenueMessageNotRecognised,
    VenueRequest,
    VenuePremium,
)

VENUE_ID = "bybit-linear"

# One public endpoint carries every stream kind here -- unlike Binance, where the
# routed path differs per stream and getting it wrong delivers silence.
LINEAR_PUBLIC_STREAM = "wss://stream.bybit.com/v5/public/linear"
REST_HOST = "https://api.bybit.com"
# The largest page this endpoint serves. Asking without it returns 500 entries
# against 837 live symbols -- measured 2026-08-22 -- and says nothing about the
# ones it left out, so the default is a silently truncated universe.
CATALOGUE_PAGE_LIMIT = 1000
CATALOGUE_URL = f"{REST_HOST}/v5/market/instruments-info?category=linear&limit={CATALOGUE_PAGE_LIMIT}"
# Every linear symbol's 24-hour statistics in one call, against the blanket
# 600-requests-per-5-seconds IP limit -- this venue publishes no per-endpoint
# figure for its public market-data calls at all.
TICKER_URL = f"{REST_HOST}/v5/market/tickers?category=linear"
# Public, unauthenticated, one call per symbol. Measured 2026-08-26: asking
# without a symbol returns 407 rows covering 15 symbols and a cursor, so reading
# the whole venue that way is ~56 paged calls for ~840 symbols -- and this system
# captures 50 of them. Asked per captured symbol instead: BTCUSDT returns its 35
# tiers with an empty cursor, in one call.
RISK_LIMIT_URL = f"{REST_HOST}/v5/market/risk-limit?category=linear&symbol={{symbol}}"
# Public, one call per symbol -- this venue bulk-serves nothing shorter than
# 24h (TICKER_URL), so a recent-window scan has no bulk alternative here either.
KLINE_URL = (
    f"{REST_HOST}/v5/market/kline?category=linear&symbol={{symbol}}&interval={{interval}}"
    f"&limit={{limit}}"
)

_DOCS = "https://bybit-exchange.github.io/docs/v5"
_CONNECT_PAGE = f"{_DOCS}/ws/connect"
_RATE_LIMIT_PAGE = f"{_DOCS}/rate-limit"
_TRADE_PAGE = f"{_DOCS}/websocket/public/trade"
_KLINE_PAGE = f"{_DOCS}/websocket/public/kline"
_ORDERBOOK_PAGE = f"{_DOCS}/websocket/public/orderbook"
_ERROR_PAGE = f"{_DOCS}/error"
_INSTRUMENTS_PAGE = f"{_DOCS}/market/instrument"
_LIVE_CAPTURE = "live capture 2026-08-22, tests/captured/bybit-linear/"

# What the venue calls each thing on the wire.
TRADE_TOPIC_PREFIX = "publicTrade"
CANDLE_TOPIC_PREFIX = "kline"
BOOK_TOPIC_PREFIX = "orderbook"
QUOTE_TOPIC_PREFIX = "tickers"
# The premium arrives on the topic the quote reader already subscribes to. This
# venue puts mark price, index price, the funding rate it will charge next and
# the moment it charges it into the same `tickers` message as the best bid and
# ask, so nothing new is subscribed to -- unlike Binance, which carries the same
# four on a separate `!markPrice@arr@1s` stream. Measured 2026-08-24 against
# tests/captured/bybit-linear/2026-08-24-public-linear-tickers-through-one-sided-delta.jsonl.
PREMIUM_TOPIC_PREFIX = QUOTE_TOPIC_PREFIX
# The four fields a premium is read from. A tickers delta that carries none of
# them is a quote-only amend and yields no premium at all.
PREMIUM_FIELDS = ("markPrice", "indexPrice", "fundingRate", "nextFundingTime")
SUBSCRIBE_OPERATION = "subscribe"
UNSUBSCRIBE_OPERATION = "unsubscribe"
PING_OPERATION = "ping"

# A book message says whether it is a fresh snapshot or a delta against one.
SNAPSHOT_MESSAGE_TYPE = "snapshot"

# The project's candle vocabulary is Binance-shaped ("1m"); Bybit numbers its
# minute intervals bare ("1") and letters the long ones. Translation lives here
# because a part must be able to ask both venues for the same timeframe without
# knowing that either spelling exists.
CANDLE_INTERVAL_BY_CANONICAL_NAME = {
    "1m": "1",
    "3m": "3",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "2h": "120",
    "4h": "240",
    "6h": "360",
    "12h": "720",
    "1d": "D",
    "1w": "W",
    "1M": "M",
}

# Bybit's own codes for "you are asking too often". 10018 is the IP rate limit,
# 20003 is too-frequent requests inside one websocket session, and the HTTP side
# answers "403, access too frequent" as text rather than as a code.
IP_RATE_LIMIT_CODE = 10018
SESSION_TOO_FREQUENT_CODE = 20003
ACCESS_TOO_FREQUENT_TEXT = "access too frequent"

MILLISECONDS_TO_NANOSECONDS = 1_000_000

_LIMITS: dict[str, VenueFact] = {
    "subscribe_args_characters": VenueFact(
        name="subscribe_args_characters",
        value=21_000,
        unit="characters of the serialised args array",
        source=f'{_CONNECT_PAGE} -- "for one public connection, you cannot have length of '
        f'\'args\' array over 21,000 characters... No args limit for Futures and Spread for now." '
        f"The cap is on the string, so it scales with symbol-name length.",
    ),
    "new_connections_per_five_minutes": VenueFact(
        name="new_connections_per_five_minutes",
        value=500,
        unit="connections per rolling 5 minutes per IP",
        source=f'{_RATE_LIMIT_PAGE} -- "Do not establish more than 500 connections within a '
        f'5-minute window." A reconnect storm is self-harm, not persistence.',
    ),
    "concurrent_connections_per_ip": VenueFact(
        name="concurrent_connections_per_ip",
        value=1000,
        unit="connections per IP for market data",
        source=f"{_RATE_LIMIT_PAGE} -- counted separately for Spot, Linear, Inverse and Options.",
    ),
    "client_ping_interval_seconds": VenueFact(
        name="client_ping_interval_seconds",
        value=20,
        unit="seconds",
        source=f'{_CONNECT_PAGE} -- "we recommend that you send the ping heartbeat packet every '
        f'20 seconds to maintain the WebSocket connection."',
    ),
    "book_depth_levels": VenueFact(
        name="book_depth_levels",
        value=(1, 50, 200, 1000),
        unit="levels per side",
        source=f"{_ORDERBOOK_PAGE} -- linear and inverse depths, pushed at 10/20/100/200 ms "
        f"respectively. Depth and cadence are coupled: choosing one chooses the other.",
    ),
    "trades_per_message": VenueFact(
        name="trades_per_message",
        value=1024,
        unit="trades",
        source=f'{_TRADE_PAGE} -- "a single message may have up to 1024 trades. As such, '
        f'multiple messages may be sent for the same seq."',
    ),
    "catalogue_page_limit": VenueFact(
        name="catalogue_page_limit",
        value=CATALOGUE_PAGE_LIMIT,
        unit="instruments per page",
        source=f'{_INSTRUMENTS_PAGE} -- "This endpoint returns 500 entries by default. There are '
        f'now more than 500 linear symbols on the platform. As a result, you will need to use '
        f'cursor for pagination or limit to get all entries." Measured 2026-08-22: the default '
        f"returned 500 of 837.",
    ),
    "rest_requests_per_five_seconds": VenueFact(
        name="rest_requests_per_five_seconds",
        value=600,
        unit="requests per 5 seconds per IP",
        source=f"{_RATE_LIMIT_PAGE} -- the blanket HTTP IP limit. The three public market-data "
        f"endpoints this project uses appear in no per-endpoint table at all.",
    ),
    "http_ban_minimum_seconds": VenueFact(
        name="http_ban_minimum_seconds",
        value=600,
        unit="seconds",
        source=f'{_RATE_LIMIT_PAGE} -- on "403, access too frequent", terminate all HTTP '
        f'sessions and "wait for at least 10 minutes. The ban will be lifted automatically." '
        f"No duration is documented for a websocket-specific ban.",
    ),
    "book_update_id_step": VenueFact(
        name="book_update_id_step",
        value=1,
        unit="update ids between consecutive deltas",
        source=f"{_LIVE_CAPTURE} -- measured, because the docs never state it and pybit, "
        f"Bybit's own reference SDK, stores u without ever comparing it. A fresh snapshot "
        f"restarts the numbering, which is why a snapshot is marked rather than compared.",
    ),
    "capturable_status": VenueFact(
        name="capturable_status",
        value="Trading",
        unit="instruments-info status",
        source=f"{_INSTRUMENTS_PAGE} and the live catalogue of 2026-08-21: 725 USDT "
        f"LinearPerpetual, 40 dated LinearFutures and 68 USDC perpetuals were Trading.",
    ),
}


def build_venue_adapter() -> "BybitLinearAdapter":
    """The factory `adapter_registry` looks for. Every venue module exposes this."""
    return BybitLinearAdapter()


class BybitLinearAdapter(VenueAdapter):
    """Answers the venue question set for Bybit v5 linear perpetuals."""

    @property
    def venue_id(self) -> str:
        return VENUE_ID

    @property
    def trade_fidelity(self) -> TradeFidelity:
        """Every print, unlike Binance's 100 ms aggregates.

        Recorded rather than assumed equivalent: a microstructure feature
        computed across both venues without knowing which is which produces a
        number that is wrong in a way nothing detects (spec §7).
        """
        return TradeFidelity.EVERY_PRINT

    def declared_limits(self) -> Mapping[str, VenueFact]:
        return dict(_LIMITS)

    def stream_endpoint_url(self, stream_kind: StreamKind) -> str:
        """One public endpoint for every stream kind on this venue.

        Asked per stream kind anyway, because the shape is the shape for every
        venue (T-1) and because the venue that does split its endpoints is the
        one where forgetting costs a silent, healthy-looking connection.
        """
        if stream_kind in (
            StreamKind.TRADE,
            StreamKind.CANDLE,
            StreamKind.BOOK,
            StreamKind.QUOTE,
            StreamKind.PREMIUM,
        ):
            return LINEAR_PUBLIC_STREAM
        raise VenueMessageNotRecognised(f"{VENUE_ID} has no endpoint for {stream_kind!r}")

    def subscription_topic(self, request: StreamRequest) -> str:
        symbol = request.symbol.upper()
        if request.stream_kind is StreamKind.TRADE:
            return f"{TRADE_TOPIC_PREFIX}.{symbol}"
        if request.stream_kind is StreamKind.CANDLE:
            return f"{CANDLE_TOPIC_PREFIX}.{self.resolve_candle_interval(request.candle_interval)}.{symbol}"
        if request.stream_kind is StreamKind.BOOK:
            return f"{BOOK_TOPIC_PREFIX}.{self.resolve_book_depth_levels(request.book_depth_levels)}.{symbol}"
        if request.stream_kind is StreamKind.QUOTE:
            if request.symbol == EVERY_SYMBOL:
                raise VenueMessageNotRecognised(
                    f"{VENUE_ID} has no all-market quote topic, so every symbol is named. "
                    f"A caller reaching here asked for one anyway -- ask "
                    f"every_symbol_quote_topic() first, which answers None for this venue. "
                    f"Subscribing to a symbol called '*' would look subscribed and deliver "
                    f"nothing, which is the failure this refusal exists to prevent."
                )
            return f"{QUOTE_TOPIC_PREFIX}.{symbol}"
        if request.stream_kind is StreamKind.PREMIUM:
            if request.symbol == EVERY_SYMBOL:
                raise VenueMessageNotRecognised(
                    f"{VENUE_ID} has no all-market premium topic, so every symbol is named "
                    f"-- the same as its quote, because it is the same topic. A caller "
                    f"reaching here asked for one anyway; subscribing to a symbol called "
                    f"'*' would look subscribed and deliver nothing."
                )
            # Deliberately the quote topic. This venue packs mark price, index
            # price and funding into the same `tickers` message as the best bid
            # and ask, so a reader wanting both subscribes once.
            return f"{PREMIUM_TOPIC_PREFIX}.{symbol}"
        raise VenueMessageNotRecognised(f"{VENUE_ID} has no stream for {request.stream_kind!r}")

    def resolve_candle_interval(self, canonical_interval: str | None) -> str:
        """This venue's spelling of a timeframe the caller named in the project's."""
        if not canonical_interval:
            raise ValueError(
                f"{VENUE_ID} candle subscriptions name an interval; the request named none, "
                f"and a default here would be a timeframe nobody chose being written to the tape"
            )
        try:
            return CANDLE_INTERVAL_BY_CANONICAL_NAME[canonical_interval]
        except KeyError:
            raise ValueError(
                f"{VENUE_ID} does not offer a '{canonical_interval}' candle. It offers "
                f"{sorted(CANDLE_INTERVAL_BY_CANONICAL_NAME)}."
            ) from None

    def resolve_book_depth_levels(self, requested_levels: int | None) -> int:
        """The venue's shallowest offered depth that is at least as deep as asked.

        Rounding up rather than down means a book is never shallower than what
        was asked for. On this venue that costs push rate as well as bandwidth --
        depth 50 is pushed every 20 ms where depth 1 is every 10 ms -- so the
        level actually used is returned rather than hidden in the topic string.
        """
        if requested_levels is None:
            raise ValueError(
                f"{VENUE_ID} book subscriptions name a depth; a default here would be a book "
                f"depth nobody chose, and on this venue depth also chooses the push rate"
            )
        offered = _LIMITS["book_depth_levels"].value
        deep_enough = [levels for levels in offered if levels >= requested_levels]
        if not deep_enough:
            raise ValueError(
                f"{VENUE_ID} offers book depths {offered}; {requested_levels} was asked for."
            )
        return min(deep_enough)

    def does_topic_fit_connection(self, existing_topics: Sequence[str], candidate: str) -> bool:
        """Bybit's cap is a character count, so this measures characters.

        The `args` array is what the venue counts, so this measures the array as
        it will actually be serialised rather than summing topic lengths -- the
        quotes, commas and brackets are characters too, and at 21,000 the
        difference is tens of symbols.

        The documented sentence caps `args` "for one public connection", which
        reads as a per-connection total rather than per subscribe frame. The two
        readings differ, and this takes the stricter one: being wrong toward
        fewer topics costs an extra connection, and being wrong toward more costs
        a ban that outlasts the part.
        """
        if candidate in existing_topics:
            return True
        serialised = json.dumps(list(existing_topics) + [candidate], separators=(",", ":"))
        return len(serialised) <= _LIMITS["subscribe_args_characters"].value

    def subscribe_frame(self, topics: Sequence[str]) -> bytes:
        return self._control_frame(SUBSCRIBE_OPERATION, topics)

    def unsubscribe_frame(self, topics: Sequence[str]) -> bytes:
        return self._control_frame(UNSUBSCRIBE_OPERATION, topics)

    def _control_frame(self, operation: str, topics: Sequence[str]) -> bytes:
        """One subscribe or unsubscribe request, with a request id that says what it was.

        The venue echoes `req_id` back on its acknowledgement. It names the
        operation and the number of topics rather than carrying a counter, so a
        reconnecting connection has no id state to have got wrong.
        """
        return json.dumps(
            {"op": operation, "args": list(topics), "req_id": f"{operation}-{len(topics)}"},
            separators=(",", ":"),
        ).encode("utf-8")

    def heartbeat_discipline(self) -> HeartbeatDiscipline:
        """We ping; the venue answers with `ret_msg: "pong"`.

        A connection that stops pinging is closed, and the close is
        indistinguishable from a network fault -- so this is a real obligation
        rather than a recommendation to follow when convenient.
        """
        return HeartbeatDiscipline(
            expects_client_ping=True,
            interval_seconds=float(_LIMITS["client_ping_interval_seconds"].value),
            ping_frame=json.dumps({"op": PING_OPERATION}, separators=(",", ":")).encode("utf-8"),
        )

    def connection_discipline(self) -> ConnectionDiscipline:
        """500 new connections per 5 minutes is the number a reconnect storm breaks.

        1.67 connections a second across everything this box holds, so the
        backoff floor is not a politeness -- it is what keeps one venue outage
        from becoming a ban that outlasts the outage.

        No lifetime: the 10-minute idle cutoff is documented under private and
        order-entry connections, not the public stream, and it was not re-stated
        on any public topic page. Claiming it here would make an unexpected close
        look scheduled, which is the one reading that would stop it being
        investigated.
        """
        return ConnectionDiscipline(
            new_connections_per_window=_LIMITS["new_connections_per_five_minutes"].value,
            rate_window_seconds=300.0,
            concurrent_connections=_LIMITS["concurrent_connections_per_ip"].value,
        )

    def book_stream_delivers_full_depth(self) -> bool:
        """False: one snapshot, then deltas -- and nothing will resend the snapshot.

        Measured 2026-08-22: the subscription opened with a `type: "snapshot"`
        message and every message after it was a `delta`. A reader that dropped
        one of those would not lose resolution, it would lose the book, and
        every later price it produced would be wrong with nothing in the record
        to say so.
        """
        return False

    def sequence_continuity(self, stream_kind: StreamKind) -> SequenceContinuity:
        """What each stream promises, one measured and one documented as weaker.

        The book's `u` steps by exactly one between consecutive deltas -- measured
        on 2026-08-22, stated nowhere -- and a fresh snapshot restarts it, which
        arrives marked rather than as a discontinuity to puzzle over.

        Trades are only non-decreasing: the venue's own text says "multiple
        messages may be sent for the same seq", so expecting a step of one there
        would report a gap on every batch that split.
        """
        if stream_kind is StreamKind.BOOK:
            return SequenceContinuity.INCREMENTS_BY_ONE
        if stream_kind is StreamKind.TRADE:
            return SequenceContinuity.NON_DECREASING
        if stream_kind is StreamKind.PREMIUM:
            # The same tickers message as the quote, so the same `cs` and the same
            # promise. Stated separately rather than folded in with QUOTE: they
            # share a topic today by this venue's choice, not by a rule, and a
            # reader of one must not inherit the other's promise silently.
            return SequenceContinuity.NON_DECREASING
        if stream_kind is StreamKind.QUOTE:
            # `cs` on a tickers message is a cross sequence, the same counter the
            # trade stream carries, and the venue promises no step size for it.
            # Measured 2026-08-24 across 1 178 consecutive quotes on BTCUSDT and
            # ETHUSDT: it never went backwards, and not one step was 1 -- the
            # median step was 2 518 and the largest 11 876, because it counts
            # matching-engine events rather than quote events. Declaring this
            # increments-by-one would have reported a gap on every message.
            return SequenceContinuity.NON_DECREASING
        return SequenceContinuity.NOT_NUMBERED

    def read_quote_changes(self, payload: bytes) -> tuple[QuoteChange, ...]:
        """What one tickers message says, which on a delta may be one side only.

        Measured 2026-08-24, a real delta from this venue:

            {"topic":"tickers.BTCUSDT","type":"delta",
             "data":{"symbol":"BTCUSDT","bid1Price":"79114.50","bid1Size":"1.496"},
             "cs":793092205004,"ts":1787590405983}

        A bid, no ask. The absent side is reported as None -- not zero, which
        would be a market nobody quoted, and not the last value seen, which this
        adapter does not keep. Whoever assembles quotes merges; that is why
        `quote_stream_amends_rather_than_restates` says so out loud.

        A snapshot names both sides, but that is a property of the message rather
        than of the type, so it is read the same way and reports itself complete.
        """
        message = json.loads(payload)
        if not isinstance(message, dict):
            return ()
        topic = message.get("topic")
        if not topic or not topic.startswith(f"{QUOTE_TOPIC_PREFIX}."):
            return ()
        quote = message.get("data")
        if not isinstance(quote, dict):
            return ()

        def number(field: str) -> float | None:
            """The venue's number, or None when this message did not carry the field.

            An empty string is treated as absent too: Bybit sends "" for a field it
            has no value for, and float("") raises rather than meaning zero.
            """
            raw = quote.get(field)
            if raw is None or raw == "":
                return None
            return float(raw)

        return (
            QuoteChange(
                venue_id=VENUE_ID,
                symbol=quote.get("symbol", topic.split(".")[-1]),
                bid_price=number("bid1Price"),
                bid_quantity=number("bid1Size"),
                ask_price=number("ask1Price"),
                ask_quantity=number("ask1Size"),
                venue_time_ns=int(message["ts"]) * MILLISECONDS_TO_NANOSECONDS,
                is_snapshot=message.get("type") == SNAPSHOT_MESSAGE_TYPE,
            ),
        )

    def read_premiums(self, payload: bytes) -> tuple[VenuePremium, ...]:
        """What one tickers message says about mark, index and funding.

        The same message the quote reader reads. This venue packs the premium
        into `tickers` alongside the best bid and ask, so a premium costs no
        extra subscription here -- where Binance carries the same four fields on
        a separate `!markPrice@arr@1s` stream, one frame for every listed symbol.
        One symbol per message either way for this venue.

        Measured 2026-08-24, a real snapshot from this venue, trimmed:

            {"topic":"tickers.BTCUSDT","type":"snapshot",
             "data":{"symbol":"BTCUSDT","markPrice":"79041.51",
                     "indexPrice":"79065.05","nextFundingTime":"1787616000000",
                     "fundingRate":"0.00001984", ...},
             "cs":793104107391,"ts":1787590743883}

        and a real delta, which is why every field on VenuePremium is optional:

            {"topic":"tickers.BTCUSDT","type":"delta",
             "data":{"symbol":"BTCUSDT","markPrice":"79029.65",
                     "indexPrice":"79063.98", ...},
             "cs":793104118729,"ts":1787590744183}

        Mark and index moved; the funding rate did not, so the venue did not
        resend it. Reporting it as zero there would say this venue is about to
        charge nothing, which is a claim about the market rather than about the
        message -- so it is None and whoever assembles premiums merges, exactly
        as `quote_stream_amends_rather_than_restates` already tells them to for
        the quote.

        A tickers amend carrying none of the four -- a bid moving on its own --
        yields no premium rather than one made entirely of None. An empty tuple
        says "this message was not about the premium"; four Nones would say "the
        premium is unknown", and those are different facts.
        """
        message = json.loads(payload)
        if not isinstance(message, dict):
            return ()
        topic = message.get("topic")
        if not topic or not topic.startswith(f"{PREMIUM_TOPIC_PREFIX}."):
            return ()
        premium = message.get("data")
        if not isinstance(premium, dict):
            return ()
        if not any(field in premium for field in PREMIUM_FIELDS):
            return ()

        def number(field: str) -> float | None:
            """The venue's number, or None when this message did not carry the field.

            An empty string is absent too: this venue sends "" for a field it has
            no value for, and float("") raises rather than meaning zero.
            """
            raw = premium.get(field)
            if raw is None or raw == "":
                return None
            return float(raw)

        settlement = premium.get("nextFundingTime")
        return (
            VenuePremium(
                venue_id=VENUE_ID,
                symbol=premium.get("symbol", topic.split(".")[-1]),
                mark_price=number("markPrice"),
                index_price=number("indexPrice"),
                declared_funding_rate=number("fundingRate"),
                next_settlement_at_ns=(
                    int(settlement) * MILLISECONDS_TO_NANOSECONDS
                    if settlement not in (None, "")
                    else None
                ),
                venue_time_ns=int(message["ts"]) * MILLISECONDS_TO_NANOSECONDS,
            ),
        )

    def quote_stream_amends_rather_than_restates(self) -> bool:
        """True: one snapshot, then deltas carrying only what moved.

        Measured 2026-08-24. A reader that assumed otherwise would see a complete
        quote once per subscription and nothing usable afterwards.
        """
        return True

    def every_symbol_quote_topic(self) -> str | None:
        """None: this venue has no wildcard, so every symbol is named.

        Measured 2026-08-24: all 833 trading linear symbols named at once
        serialise to 17 006 characters against this venue's 21 000-character cap,
        and all 833 quoted within 100 seconds with none silent. That is 81% of the
        cap and a fact about today's symbol names -- `does_topic_fit_connection`
        is what a caller asks, never this measurement.
        """
        return None

    def read_candles(self, payload: bytes) -> tuple[NormalisedCandle, ...]:
        """Every kline entry in the batch; `confirm` is the closed flag.

        Bybit does not send a trade count in its kline stream, so `trades` is
        None rather than zero: zero would read as a minute in which nothing
        traded, and the venue said no such thing. The interval is the venue's
        own unit (minutes as a string), passed through unchanged.
        """
        message = json.loads(payload)
        if not isinstance(message, dict):
            return ()
        topic = message.get("topic")
        if not topic or not topic.startswith(f"{CANDLE_TOPIC_PREFIX}."):
            return ()
        _prefix, interval, symbol = topic.split(".", 2)
        venue_time_ns = int(message["ts"]) * MILLISECONDS_TO_NANOSECONDS
        return tuple(
            NormalisedCandle(
                venue_id=VENUE_ID,
                symbol=symbol,
                interval=str(candle.get("interval", interval)),
                open_time_ns=int(candle["start"]) * MILLISECONDS_TO_NANOSECONDS,
                close_time_ns=int(candle["end"]) * MILLISECONDS_TO_NANOSECONDS,
                open=float(candle["open"]),
                high=float(candle["high"]),
                low=float(candle["low"]),
                close=float(candle["close"]),
                volume=float(candle["volume"]),
                quote_volume=float(candle["turnover"]),
                trades=None,
                is_closed=bool(candle["confirm"]),
                venue_time_ns=venue_time_ns,
            )
            for candle in message.get("data", ())
        )

    def read_book_update(self, payload: bytes) -> BookUpdate | None:
        """A snapshot replaces the book; a delta amends it, and a zero quantity
        removes that level. `u` is the update id the continuity check reads."""
        message = json.loads(payload)
        if not isinstance(message, dict):
            return None
        topic = message.get("topic")
        if not topic or not topic.startswith(f"{BOOK_TOPIC_PREFIX}."):
            return None
        book = message["data"]
        symbol = book.get("s") or topic.split(".", 2)[2]
        return BookUpdate(
            venue_id=VENUE_ID,
            symbol=symbol,
            bids=tuple((float(price), float(quantity)) for price, quantity in book.get("b", ())),
            asks=tuple((float(price), float(quantity)) for price, quantity in book.get("a", ())),
            is_snapshot=message.get("type") == SNAPSHOT_MESSAGE_TYPE,
            sequence=int(book["u"]),
            venue_time_ns=int(message["ts"]) * MILLISECONDS_TO_NANOSECONDS,
        )

    def read_trades(self, payload: bytes) -> tuple[NormalisedTrade, ...]:
        """Every print in the batch, and a batch can hold up to 1024 of them.

        Each trade carries its own timestamp here, unlike the message-level `ts`
        the tape indexes on: the index describes the record, and a consumer
        reasoning about a trade wants when that trade happened. `S` is the taker's
        side already, so no inversion is needed -- which is exactly the venue
        difference this method exists to absorb.
        """
        message = json.loads(payload)
        if not isinstance(message, dict):
            return ()
        topic = message.get("topic")
        if not topic or not topic.startswith(f"{TRADE_TOPIC_PREFIX}."):
            return ()
        symbol = topic.split(".", 1)[1]
        return tuple(
            NormalisedTrade(
                venue_id=VENUE_ID,
                symbol=trade.get("s", symbol),
                price=float(trade["p"]),
                quantity=float(trade["v"]),
                side=BUY if str(trade["S"]).lower() == BUY else SELL,
                venue_time_ns=int(trade["T"]) * MILLISECONDS_TO_NANOSECONDS,
                sequence=int(trade["seq"]),
                fidelity=self.trade_fidelity,
            )
            for trade in message.get("data", ())
        )

    def read_previous_sequence(self, payload: bytes) -> int | None:
        """Nothing here names its predecessor.

        Binance's book states its previous update id in every message; Bybit's
        does not, which is precisely why the continuity check has to be built by
        this project rather than read off the feed.
        """
        return None

    def read_message_facts(self, payload: bytes) -> MessageFacts | None:
        """The index fields inside one message, or None when it carries no data.

        A control frame here is one without a `topic`: the subscribe
        acknowledgement and the pong both answer with `success`/`ret_msg` and
        name no topic at all.
        """
        message = json.loads(payload)
        if not isinstance(message, dict):
            raise VenueMessageNotRecognised(f"{VENUE_ID} sent a non-object message: {payload[:120]!r}")
        topic = message.get("topic")
        if topic is None:
            return None

        parts = topic.split(".")
        kind = parts[0]
        symbol = parts[-1]
        # `ts` is when the venue emitted the message. Bybit timestamps individual
        # trades too, but a message can carry up to 1024 of them, so the message's
        # own time is the only one that describes the record the tape stores.
        venue_time_ns = int(message["ts"]) * MILLISECONDS_TO_NANOSECONDS

        if kind == TRADE_TOPIC_PREFIX:
            trades = message["data"]
            return MessageFacts(
                stream_kind=StreamKind.TRADE,
                symbol=symbol,
                venue_time_ns=venue_time_ns,
                # The highest cross sequence in the batch. Taking the last would
                # assume an ordering the venue does not promise, and the check
                # this feeds is only ever "did it go backwards".
                sequence=max(int(trade["seq"]) for trade in trades) if trades else NOT_SENT,
            )

        if kind == CANDLE_TOPIC_PREFIX:
            candles = message["data"]
            return MessageFacts(
                stream_kind=StreamKind.CANDLE,
                symbol=symbol,
                venue_time_ns=venue_time_ns,
                sequence=NOT_SENT,
                # A message carrying any closed candle is one the candle reader
                # must not miss, so `any` rather than the last entry: a batch that
                # ended with an open candle would otherwise hide the close before it.
                is_closed_candle=any(bool(candle["confirm"]) for candle in candles),
            )

        if kind == BOOK_TOPIC_PREFIX:
            book = message["data"]
            return MessageFacts(
                stream_kind=StreamKind.BOOK,
                symbol=book.get("s", symbol),
                venue_time_ns=venue_time_ns,
                sequence=int(book["u"]),
                resets_sequence=message.get("type") == SNAPSHOT_MESSAGE_TYPE,
            )

        if kind == QUOTE_TOPIC_PREFIX:
            quote = message["data"]
            return MessageFacts(
                stream_kind=StreamKind.QUOTE,
                symbol=quote.get("symbol", symbol),
                venue_time_ns=venue_time_ns,
                sequence=int(message["cs"]),
                # The first message of a subscription restates everything; the rest
                # amend. Marked rather than inferred, for the same reason the book
                # marks it: a resnapshot is not a discontinuity to puzzle over.
                resets_sequence=message.get("type") == SNAPSHOT_MESSAGE_TYPE,
            )

        raise VenueMessageNotRecognised(
            f"{VENUE_ID} sent topic {topic!r}, which this adapter has no reading for."
        )

    def read_http_ban_signal(
        self, status_code: int, headers: Mapping[str, str]
    ) -> BanSignal | None:
        """403 is the whole HTTP ban story on this venue: at least ten minutes, auto-lifted.

        There is no escalating ladder documented as Binance has, and no
        Retry-After -- the ten minutes is the documented floor, so it is what a
        caller waits rather than a number it invents.
        """
        if status_code != 403:
            return None
        return BanSignal(
            venue_id=VENUE_ID,
            observed_code="403",
            reason="access too frequent; terminate all HTTP sessions and wait it out",
            retry_after_seconds=float(_LIMITS["http_ban_minimum_seconds"].value),
        )

    def read_stream_ban_signal(self, payload: bytes) -> BanSignal | None:
        """A rate-limit refusal over the websocket, and only that.

        An ordinary failed subscribe -- a symbol that does not exist, a malformed
        topic -- is a mistake to fix, not a venue withholding service. Reading
        every `success: false` as a ban would have the rotator stand a venue down
        for a typo.
        """
        try:
            message = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(message, dict) or message.get("success") is not False:
            return None

        code = message.get("retCode", message.get("ret_code"))
        text = str(message.get("ret_msg", message.get("retMsg", "")))
        if code in (IP_RATE_LIMIT_CODE, SESSION_TOO_FREQUENT_CODE):
            observed = str(code)
        elif ACCESS_TOO_FREQUENT_TEXT in text.lower():
            observed = "403"
        else:
            return None
        return BanSignal(
            venue_id=VENUE_ID,
            observed_code=observed,
            reason=text or "rate limited on the websocket session",
            # No duration is documented for a websocket ban here. None means
            # exactly that, and the backoff is what decides the wait -- a number
            # borrowed from the HTTP case would be a guess wearing a fact's shape.
            retry_after_seconds=None,
        )

    def catalogue_url(self, cursor: str | None = None) -> str:
        """One page of the contract list, at the largest size this venue serves."""
        if cursor is None:
            return CATALOGUE_URL
        return f"{CATALOGUE_URL}&cursor={urllib.parse.quote(cursor)}"

    def read_catalogue_cursor(self, catalogue_response: object) -> str | None:
        """The venue's own nextPageCursor, empty string meaning there is no next page."""
        cursor = catalogue_response.get("result", {}).get("nextPageCursor")
        return cursor or None

    def ticker_url(self) -> str:
        return TICKER_URL

    def read_quote_volumes(self, ticker_response: object) -> Mapping[str, float]:
        """Each symbol's 24-hour turnover, which this venue calls turnover24h.

        `volume24h` on the same record is the base-asset count. This venue names
        the quote-denominated figure differently from the other one -- turnover
        rather than quoteVolume -- which is exactly the kind of difference that
        would otherwise be a field name repeated in a part.
        """
        return {
            entry["symbol"]: float(entry["turnover24h"])
            for entry in ticker_response["result"]["list"]
            if entry.get("turnover24h") is not None
        }

    def read_volatility_facts(self, ticker_response: object) -> Mapping[str, float]:
        """24-hour high-low range over last price, from the same response as turnover.

        `highPrice24h`, `lowPrice24h` and `lastPrice` sit on the same `tickers`
        entry `read_quote_volumes` already reads. This venue writes "" rather
        than omitting a field on a dated or newly-listed contract, same as it
        does for `fundingRate` -- treated as absent, not as a zero range.
        """
        facts = {}
        for entry in ticker_response["result"]["list"]:
            last = entry.get("lastPrice")
            high = entry.get("highPrice24h")
            low = entry.get("lowPrice24h")
            if last in (None, "") or high in (None, "") or low in (None, ""):
                continue
            last = float(last)
            if last <= 0:
                continue
            facts[entry["symbol"]] = (float(high) - float(low)) / last
        return facts

    def read_momentum_facts(self, ticker_response: object) -> Mapping[str, float]:
        """Signed change over the last hour, from `prevPrice1h` on the same response.

        This venue states a price level from an hour ago, not a computed percent
        change -- `read_quote_volumes` and `read_volatility_facts` read the same
        response, so this costs no extra request. Binance's bulk ticker states
        nothing shorter than 24h at all, so this figure exists on one venue and
        not the other; `select_capturable_symbols` treats its absence as
        undeclared, never as zero momentum.
        """
        facts = {}
        for entry in ticker_response["result"]["list"]:
            last = entry.get("lastPrice")
            previous = entry.get("prevPrice1h")
            if last in (None, "") or previous in (None, ""):
                continue
            previous = float(previous)
            if previous <= 0:
                continue
            facts[entry["symbol"]] = (float(last) - previous) / previous
        return facts

    def short_window_kline_requests(
        self, symbols: Sequence[str], interval: str, bar_count: int
    ) -> tuple[VenueRequest, ...]:
        """One public call per symbol -- this venue bulk-serves no interval shorter than 24h."""
        venue_interval = CANDLE_INTERVAL_BY_CANONICAL_NAME[interval]
        return tuple(
            VenueRequest(
                url=KLINE_URL.format(symbol=symbol, interval=venue_interval, limit=bar_count),
                describes=symbol,
            )
            for symbol in symbols
        )

    def read_short_window_klines(
        self, symbol_responses: Sequence[tuple[str, object]]
    ) -> Mapping[str, tuple[float, ...]]:
        """Each response's closes, oldest first -- one response is one symbol here.

        The symbol travels beside the response rather than being read back out
        of it, the same convention Binance's reader uses, even though this
        venue's own response does carry `result.symbol` -- one pairing rule for
        both venues is what keeps this method from needing to know which venue
        it is reading. This venue's kline rows are not documented as arriving in
        a fixed order, so they are sorted by start time here rather than trusted
        as already chronological.
        """
        closes: dict[str, tuple[float, ...]] = {}
        for symbol, response in symbol_responses:
            rows = (response.get("result") or {}).get("list") or []
            if not rows:
                continue
            ordered = sorted(rows, key=lambda row: int(row[0]))
            closes[symbol] = tuple(float(row[4]) for row in ordered)
        return closes

    def read_symbol_listings(self, catalogue_response: object) -> tuple[SymbolListing, ...]:
        """Every instrument the venue lists, from an instruments-info response.

        Both perpetual and dated contracts come back under `category=linear`, and
        both are kept with their type recorded -- the same rule as the other
        venue, so a symbol's tape does not depend on which venue listed it.
        """
        instruments = catalogue_response["result"]["list"]
        return tuple(
            SymbolListing(
                symbol=entry["symbol"],
                contract_type=entry.get("contractType", ""),
                status=entry["status"],
                price_increment=_read_price_increment(entry),
                funding_settlements_per_day=_read_settlements_per_day(entry),
                instrument_kind=INSTRUMENT_KIND_OF_CONTRACT_TYPE.get(
                    entry.get("contractType", "")
                ),
            )
            for entry in instruments
        )

    def funding_request_urls(self) -> tuple[str, ...]:
        """None. This venue already put both funding figures in what we fetch.

        `fundingRate` is on the tickers response the reader reads volumes from,
        and `fundingInterval` is on the instruments-info response it reads
        listings from. Asking again would be a request against a rate limit that
        bought a number already in hand.
        """
        return ()

    def read_funding_facts(
        self,
        listings: Sequence[SymbolListing],
        ticker_response: object,
        funding_responses: Sequence[object],
    ) -> Mapping[str, ContractFunding]:
        """Each perpetual's funding: the rate off the tickers, the interval off the
        catalogue.

        The two come from different responses, which is why the join happens here
        rather than in the reader -- on the other venue they come from two
        endpoints that neither response knows about, and a reader that knew which
        was which would be a reader with a venue inside it.

        A symbol quoting an empty `fundingRate` -- this venue writes "" rather
        than omitting the field, and does so on its dated contracts, which pay no
        funding at all -- is left out of the mapping rather than recorded as zero.
        """
        if funding_responses:
            raise ValueError(
                f"{VENUE_ID} asks for no funding endpoint of its own and was handed "
                f"{len(funding_responses)} response(s). A response nothing here reads is a "
                f"request paid for against a rate limit and then discarded."
            )
        settlements_per_day = {
            listing.symbol: listing.funding_settlements_per_day
            for listing in listings
            if listing.funding_settlements_per_day is not None
        }
        facts = {}
        for entry in ticker_response["result"]["list"]:
            rate = entry.get("fundingRate")
            if rate is None or rate == "":
                continue
            symbol = entry["symbol"]
            # `fundingCap` rides on the same tickers message the rate does --
            # measured 2026-08-24, it differs by symbol (0.00333 on BTCUSDT,
            # 0.005 on a lower-volume listing in the same capture), so it is
            # read per symbol rather than assumed shared. This venue documents
            # the bound as symmetric and publishes only the one figure -- the
            # floor is its negative by that documented convention, not a
            # second reading, which is why it carries no separate source note.
            # No `interestRate`-equivalent field appears anywhere on this
            # response; this venue's formula does not expose one the way
            # Binance's does, so it stays undeclared rather than guessed.
            cap = entry.get("fundingCap")
            facts[symbol] = ContractFunding(
                symbol=symbol,
                rate_per_settlement=float(rate),
                settlements_per_day=settlements_per_day.get(symbol),
                rate_cap=None if cap is None or cap == "" else float(cap),
                rate_floor=None if cap is None or cap == "" else -float(cap),
                interest_rate_per_interval=None,
                source=(
                    f"{TICKER_URL} fundingRate"
                    + (
                        f", {CATALOGUE_URL} fundingInterval"
                        if symbol in settlements_per_day
                        else ", interval undeclared by this venue"
                    )
                    + (
                        f", {TICKER_URL} fundingCap (floor taken as its negative)"
                        if cap not in (None, "")
                        else ", cap undeclared by this venue"
                    )
                ),
            )
        return facts

    def margin_schedule_requests(self, symbols) -> tuple[VenueRequest, ...]:
        """One public call per captured symbol. This venue needs no key at all."""
        return tuple(
            VenueRequest(url=RISK_LIMIT_URL.format(symbol=symbol), describes=symbol)
            for symbol in symbols
        )

    def read_margin_tiers(self, responses):
        """Each symbol's ladder, from the risk-limit responses.

        This venue states a tier as "up to `riskLimitValue`, this maintenance
        margin", so `riskLimitValue` is a **ceiling** and the floor of a tier is
        the ceiling of the one below it. Read as a floor directly, the lowest
        tier's rate would apply only above the first ceiling and every position
        under it would be priced by the wrong rate -- which is every position this
        bot takes.

        `mmDeduction` is not used. It is the venue's arithmetic shortcut for
        charging one blended rate across tiers rather than a rate per tier, and
        this ladder is read tier by tier.
        """
        ladders: dict[str, list] = {}
        for response in responses:
            if not isinstance(response, Mapping):
                continue
            rows = (response.get("result") or {}).get("list") or ()
            for row in rows:
                symbol = row.get("symbol")
                ceiling = row.get("riskLimitValue")
                rate = row.get("maintenanceMargin")
                if not symbol or ceiling in (None, "") or rate in (None, ""):
                    continue
                ladders.setdefault(symbol, []).append(
                    (float(ceiling), float(rate), float(row.get("maxLeverage") or 0.0))
                )

        schedule = {}
        for symbol, rows in ladders.items():
            rows.sort()
            floor = 0.0
            tiers = []
            for ceiling, rate, leverage in rows:
                tiers.append(
                    MarginTier(
                        notional_floor=floor,
                        maintenance_margin_rate=rate,
                        maximum_leverage=leverage,
                    )
                )
                floor = ceiling
            schedule[symbol] = tuple(tiers)
        return schedule

    def is_symbol_capturable(self, listing: SymbolListing) -> bool:
        """Trading and nothing else. The venue spells it with one capital letter."""
        return listing.status == _LIMITS["capturable_status"].value


# This venue's two words for a linear contract, translated into the system's own.
# Measured 2026-08-22: 793 LinearPerpetual and 40 LinearFutures, and nothing else
# under category=linear. The inverse contracts live under a category this adapter
# does not read, so they are absent here rather than unrecognised.
INSTRUMENT_KIND_OF_CONTRACT_TYPE = {
    "LinearPerpetual": PERPETUAL_FUTURE,
    "LinearFutures": DATED_FUTURE,
}

MINUTES_PER_DAY = 1440.0


def _read_settlements_per_day(entry: Mapping[str, object]) -> float | None:
    """How often this contract settles funding per day, from `fundingInterval`.

    The venue states the interval in minutes -- 240 and 480 are what its linear
    perpetuals carry, measured 2026-08-22. A dated contract pays no funding and
    declares no interval, and comes back None rather than zero: nothing settles
    is not the same fact as settles zero times.
    """
    interval_minutes = entry.get("fundingInterval")
    if not interval_minutes:
        return None
    return MINUTES_PER_DAY / float(interval_minutes)


def _read_price_increment(entry: Mapping[str, object]) -> float | None:
    """The tick size out of an instrument's priceFilter, or None if it declares none."""
    price_filter = entry.get("priceFilter") or {}
    tick_size = price_filter.get("tickSize")
    return float(tick_size) if tick_size is not None else None
