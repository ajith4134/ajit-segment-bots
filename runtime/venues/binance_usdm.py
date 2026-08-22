"""Binance USDⓈ-M futures, and every oddity that venue has.

Nothing outside this module knows any of it. The facts below are quoted from
Binance's own documentation, fetched raw on 2026-08-21 and saved with their URLs
in `~/research/segment-bots-phase1/binance-usdm-stream-limits.md`, or measured
live against the venue on 2026-08-22 and saved under `tests/captured/`.

Three of them decide the shape of everything here:

**The routed path.** Binance has split the futures websocket into `/public`,
`/market` and `/private`. `@aggTrade` and `@kline_*` live under `/market`;
`@depth*` lives under `/public`. Their own text: streams belonging to `/market`
"will not push data on unrouted connections" -- and an unrouted connection does
not error, does not close, and delivers nothing. A tape with no trades in it
looks exactly like a quiet market. That hazard is confined to
`stream_endpoint_url` and is why the endpoint is asked per stream kind rather
than once per venue.

**There is no raw trade stream.** Futures offers only `@aggTrade`, which merges
fills at the same price and side within a 100 ms window into one event. That is a
fidelity limit rather than a setting, so this adapter declares its trades
venue-aggregated and the tape carries that alongside them.

**Every limit is per IP, not per key.** Public market data uses no key at all, so
holding none exempts nothing: 429 escalates to a 418 IP ban of two minutes to
three days, and it outlasts any restart of the part that caused it.
"""

from __future__ import annotations

import json
import re
import time
from typing import Mapping, Sequence

from runtime.tape import NOT_SENT, StreamKind, TradeFidelity
from runtime.trading_types import BUY, DATED_FUTURE, PERPETUAL_FUTURE, SELL
from runtime.venues.venue_adapter import (
    BanSignal,
    ConnectionDiscipline,
    ContractFunding,
    HeartbeatDiscipline,
    MessageFacts,
    NormalisedTrade,
    SequenceContinuity,
    StreamRequest,
    SymbolListing,
    VenueAdapter,
    VenueFact,
    VenueMessageNotRecognised,
)

VENUE_ID = "binance-usdm"

# The routed websocket endpoints, and the REST host the catalogue comes from.
STREAM_HOST = "wss://fstream.binance.com"
MARKET_ROUTE = f"{STREAM_HOST}/market/ws"
PUBLIC_ROUTE = f"{STREAM_HOST}/public/ws"
REST_HOST = "https://fapi.binance.com"
CATALOGUE_URL = f"{REST_HOST}/fapi/v1/exchangeInfo"
# Every symbol's rolling 24-hour statistics in one call. Weight 40 for the whole
# market against a 2400-per-minute budget, versus weight 1 per symbol asked for
# individually -- at 570 symbols the per-symbol form would cost fourteen times
# the entire minute's budget.
TICKER_URL = f"{REST_HOST}/fapi/v1/ticker/24hr"
# Where a perpetual's funding comes from, which is neither of the two above.
# `premiumIndex` carries `lastFundingRate` for every listed symbol -- 875 of 875
# on 2026-08-22 -- and `fundingInfo` carries `fundingIntervalHours` for the 760
# symbols this venue states an interval for. Two calls because the rate and the
# interval genuinely live apart here; a rate without its interval prices nothing.
FUNDING_RATE_URL = f"{REST_HOST}/fapi/v1/premiumIndex"
FUNDING_INTERVAL_URL = f"{REST_HOST}/fapi/v1/fundingInfo"
HOURS_PER_DAY = 24.0

_DOCS = "https://developers.binance.com/docs/derivatives/usds-margined-futures"
_CONNECT_PAGE = f"{_DOCS}/websocket-market-streams/Connect"
_ROUTING_PAGE = f"{_DOCS}/websocket-market-streams/Important-WebSocket-Change-Notice"
_STREAMS_PAGE = f"{_DOCS}/websocket-market-streams"
_GENERAL_PAGE = f"{_DOCS}/general-info"
_LIVE_CAPTURE = "live capture 2026-08-22, tests/captured/binance-usdm/"

# What the venue calls each thing on the wire. Names, not decision numbers.
AGGREGATED_TRADE_STREAM = "aggTrade"
CANDLE_STREAM_PREFIX = "kline"
PARTIAL_BOOK_STREAM_PREFIX = "depth"
SUBSCRIBE_METHOD = "SUBSCRIBE"
UNSUBSCRIBE_METHOD = "UNSUBSCRIBE"

# The event names its data messages carry in field "e".
TRADE_EVENT = "aggTrade"
CANDLE_EVENT = "kline"
BOOK_EVENT = "depthUpdate"

# Binance's own error code for a rate-limit breach, and the shape its ban message
# takes: "Way too many requests; IP banned until %s." -- the %s is an epoch in
# milliseconds, which is the only place the ban duration is stated at all. No
# Retry-After header is documented for futures, so this text is the fallback and
# the header is read when present rather than relied on.
TOO_MANY_REQUESTS_CODE = -1003
_BANNED_UNTIL_PATTERN = re.compile(r"banned until (\d{10,})", re.IGNORECASE)

MILLISECONDS_TO_NANOSECONDS = 1_000_000

_LIMITS: dict[str, VenueFact] = {
    "streams_per_connection": VenueFact(
        name="streams_per_connection",
        value=1024,
        unit="streams",
        source=f'{_CONNECT_PAGE} -- "A single connection can listen to a maximum of 1024 streams."',
    ),
    "incoming_messages_per_second": VenueFact(
        name="incoming_messages_per_second",
        value=10,
        unit="messages per second",
        source=f'{_CONNECT_PAGE} -- "WebSocket connections have a limit of 10 incoming messages '
        f'per second." Bounds what we send, not what arrives.',
    ),
    "connection_lifetime_seconds": VenueFact(
        name="connection_lifetime_seconds",
        value=86_400,
        unit="seconds",
        source=f'{_CONNECT_PAGE} -- "A single connection is only valid for 24 hours; expect to be '
        f'disconnected at the 24 hour mark." Routine, not a fault.',
    ),
    "server_ping_interval_seconds": VenueFact(
        name="server_ping_interval_seconds",
        value=180,
        unit="seconds",
        source=f"{_CONNECT_PAGE} -- the server sends a ping frame every 3 minutes.",
    ),
    "pong_deadline_seconds": VenueFact(
        name="pong_deadline_seconds",
        value=600,
        unit="seconds",
        source=f"{_CONNECT_PAGE} -- no pong within 10 minutes and the connection is dropped.",
    ),
    "rest_request_weight_per_minute": VenueFact(
        name="rest_request_weight_per_minute",
        value=2400,
        unit="request weight per minute per IP",
        source=f"{_GENERAL_PAGE} and the live exchangeInfo.rateLimits array, 2026-08-21.",
    ),
    "trade_aggregation_window_ms": VenueFact(
        name="trade_aggregation_window_ms",
        value=100,
        unit="milliseconds",
        source=f"{_STREAMS_PAGE} -- aggTrade merges fills at the same price and taking side "
        f"every 100 ms. This is why trade fidelity here is venue-aggregated.",
    ),
    "partial_book_depth_levels": VenueFact(
        name="partial_book_depth_levels",
        value=(5, 10, 20),
        unit="levels per side",
        source=f"{_STREAMS_PAGE} -- the partial book depth stream's levels enum.",
    ),
    "partial_book_update_speeds_ms": VenueFact(
        name="partial_book_update_speeds_ms",
        value=(100, 500),
        unit="milliseconds",
        source=f"{_STREAMS_PAGE} -- the updateSpeed enum for partial depth. The page's prose "
        f"also mentions 250ms, contradicting its own enum, so 250 is not offered here.",
    ),
    "subscribe_frame_is_text": VenueFact(
        name="subscribe_frame_is_text",
        value="text",
        unit="websocket frame type",
        source=f"{_LIVE_CAPTURE} -- measured: the same JSON sent as a binary frame is refused "
        f"with close code 1008 'Invalid request'; as a text frame it subscribes.",
    ),
    "capturable_status": VenueFact(
        name="capturable_status",
        value="TRADING",
        unit="exchangeInfo status",
        source=f"{_LIVE_CAPTURE} -- of 872 listed contracts, 570 PERPETUAL and 170 "
        f"TRADIFI_PERPETUAL are TRADING; 127 are SETTLING and 1 PENDING_TRADING.",
    ),
}


def build_venue_adapter() -> "BinanceUsdmAdapter":
    """The factory `adapter_registry` looks for. Every venue module exposes this."""
    return BinanceUsdmAdapter()


class BinanceUsdmAdapter(VenueAdapter):
    """Answers the venue question set for Binance USDⓈ-M futures."""

    @property
    def venue_id(self) -> str:
        return VENUE_ID

    @property
    def trade_fidelity(self) -> TradeFidelity:
        """Aggregated, because the venue offers nothing else.

        Futures has no `@trade` stream at all -- spot does, futures does not --
        so this is a property of what can be captured here rather than of what
        was configured, and it travels with the data so a later phase computing
        across both venues can see which is which.
        """
        return TradeFidelity.VENUE_AGGREGATED

    def declared_limits(self) -> Mapping[str, VenueFact]:
        return dict(_LIMITS)

    def stream_endpoint_url(self, stream_kind: StreamKind) -> str:
        """The routed endpoint for this stream kind. The route is the whole point.

        `@aggTrade` and `@kline_*` are `/market` streams and `@depth*` is a
        `/public` stream, and a connection carrying a `/market` stream on an
        unrouted or wrongly-routed path stays open and silent.
        """
        if stream_kind in (StreamKind.TRADE, StreamKind.CANDLE):
            return MARKET_ROUTE
        if stream_kind is StreamKind.BOOK:
            return PUBLIC_ROUTE
        raise VenueMessageNotRecognised(
            f"{VENUE_ID} has no endpoint for stream kind {stream_kind!r}"
        )

    def subscription_topic(self, request: StreamRequest) -> str:
        symbol = request.symbol.lower()
        if request.stream_kind is StreamKind.TRADE:
            return f"{symbol}@{AGGREGATED_TRADE_STREAM}"
        if request.stream_kind is StreamKind.CANDLE:
            if not request.candle_interval:
                raise ValueError(
                    f"{VENUE_ID} candle subscriptions name an interval; the request for "
                    f"{request.symbol} named none, and a default here would be a timeframe "
                    f"nobody chose being written to the tape"
                )
            return f"{symbol}@{CANDLE_STREAM_PREFIX}_{request.candle_interval}"
        if request.stream_kind is StreamKind.BOOK:
            levels = self.resolve_book_depth_levels(request.book_depth_levels)
            speed = self.slowest_partial_book_update_speed_ms()
            return f"{symbol}@{PARTIAL_BOOK_STREAM_PREFIX}{levels}@{speed}ms"
        raise VenueMessageNotRecognised(f"{VENUE_ID} has no stream for {request.stream_kind!r}")

    def resolve_book_depth_levels(self, requested_levels: int | None) -> int:
        """The venue's shallowest offered depth that is at least as deep as asked.

        The operator asks for a depth once, in `book_depth_levels`, and the two
        venues offer different ladders -- 5/10/20 here against Bybit's
        1/50/200/1000. Rounding up rather than down means a book is never
        shallower than what was asked for, and the level actually used is
        returned rather than hidden inside the topic string.
        """
        if requested_levels is None:
            raise ValueError(
                f"{VENUE_ID} book subscriptions name a depth; a default here would be a book "
                f"depth nobody chose, and depth couples to push rate"
            )
        offered = _LIMITS["partial_book_depth_levels"].value
        deep_enough = [levels for levels in offered if levels >= requested_levels]
        if not deep_enough:
            raise ValueError(
                f"{VENUE_ID} partial book depth offers {offered} levels; {requested_levels} was "
                f"asked for. Going deeper here means the diff-depth stream and a local book, "
                f"which is a different part with a resync procedure of its own."
            )
        return min(deep_enough)

    def slowest_partial_book_update_speed_ms(self) -> int:
        """The least chatty push rate the venue offers for partial depth.

        The book reader writes a snapshot on its own `book_snapshot_interval`,
        which is seconds; every push between two snapshots is discarded. So the
        slowest offered speed captures exactly as much and costs the least
        bandwidth and the least of the 10-messages-per-second budget.

        Slowest is the largest interval. The enum is in milliseconds between
        pushes, so `max` is the quiet end of it and `min` is the loud one.
        """
        return max(_LIMITS["partial_book_update_speeds_ms"].value)

    def does_topic_fit_connection(self, existing_topics: Sequence[str], candidate: str) -> bool:
        """Binance's cap is a count of streams, so this counts streams.

        Bybit's is a character count of the subscribe payload. That the two are
        answered by the same question and different arithmetic is exactly why the
        caller is not allowed to do the arithmetic.
        """
        if candidate in existing_topics:
            return True
        return len(existing_topics) + 1 <= _LIMITS["streams_per_connection"].value

    def subscribe_frame(self, topics: Sequence[str]) -> bytes:
        return self._control_frame(SUBSCRIBE_METHOD, topics)

    def unsubscribe_frame(self, topics: Sequence[str]) -> bytes:
        return self._control_frame(UNSUBSCRIBE_METHOD, topics)

    def _control_frame(self, method: str, topics: Sequence[str]) -> bytes:
        """One subscribe or unsubscribe request.

        The id is the number of topics rather than a counter: the venue echoes it
        back on the acknowledgement, and a stateless value keeps a reconnecting
        connection from having to remember where its counter had got to. Nothing
        in this project matches an acknowledgement to a request by id -- the
        acknowledgement is a control frame and goes nowhere near the tape.
        """
        return json.dumps({"method": method, "params": list(topics), "id": len(topics)}).encode(
            "utf-8"
        )

    def heartbeat_discipline(self) -> HeartbeatDiscipline:
        """Binance pings us; we only have to answer.

        The server sends a ping frame every 3 minutes and drops the connection if
        no pong arrives within 10. Those are websocket control frames, which the
        client library answers automatically -- so there is no application-level
        ping to send here, and sending one would count against the
        10-incoming-messages-per-second budget for nothing.
        """
        return HeartbeatDiscipline(expects_client_ping=False)

    def connection_discipline(self) -> ConnectionDiscipline:
        """A 24-hour close is scheduled here, and the connection budget is unstated.

        Their own text: "expect to be disconnected at the 24 hour mark". A reader
        that treated that as a fault would double its backoff every day.

        No connections-per-IP or connection-rate figure exists anywhere in the
        futures documentation -- the widely repeated "300 attempts per 5 minutes"
        is a spot number, and carrying it across products would be a limit this
        project invented. None says the venue does not say, which leaves the
        reconnect backoff as the only guard here rather than pretending to a
        budget nobody published.
        """
        return ConnectionDiscipline(
            lifetime_seconds=float(_LIMITS["connection_lifetime_seconds"].value)
        )

    def read_message_facts(self, payload: bytes) -> MessageFacts | None:
        """The index fields inside one message, or None when it carries no data.

        A subscribe acknowledgement is `{"result":null,"id":N}` -- it has no `e`
        field at all, which is how a control frame is told from a data one here.
        """
        message = json.loads(payload)
        if not isinstance(message, dict):
            raise VenueMessageNotRecognised(f"{VENUE_ID} sent a non-object message: {payload[:120]!r}")
        event = message.get("e")
        if event is None:
            return None

        if event == TRADE_EVENT:
            return MessageFacts(
                stream_kind=StreamKind.TRADE,
                symbol=message["s"],
                # T is when the trade happened; E is when the venue emitted the
                # event. The trade's own time is the one a later phase reasons
                # about, and the difference between them is venue latency we do
                # not want folded into it.
                venue_time_ns=int(message["T"]) * MILLISECONDS_TO_NANOSECONDS,
                # The aggregate trade id, which increments per aggregated event,
                # so a break in it is a genuinely missed trade rather than a
                # missed message.
                sequence=int(message["a"]),
            )

        if event == CANDLE_EVENT:
            candle = message["k"]
            return MessageFacts(
                stream_kind=StreamKind.CANDLE,
                symbol=message["s"],
                # The event time, not the candle's close time: an open candle's
                # close time is in the future, and an index sorted on it would
                # order updates by when their minute ends rather than when they
                # arrived.
                venue_time_ns=int(message["E"]) * MILLISECONDS_TO_NANOSECONDS,
                sequence=NOT_SENT,
                is_closed_candle=bool(candle["x"]),
            )

        if event == BOOK_EVENT:
            return MessageFacts(
                stream_kind=StreamKind.BOOK,
                symbol=message["s"],
                venue_time_ns=int(message["T"]) * MILLISECONDS_TO_NANOSECONDS,
                # The final update id in this event. Measured 2026-08-22: the
                # partial-depth stream carries U/u/pu just as the diff stream
                # does, so continuity is checkable here even though each message
                # is a self-contained top-N snapshot.
                sequence=int(message["u"]),
            )

        raise VenueMessageNotRecognised(
            f"{VENUE_ID} sent event {event!r}, which this adapter has no reading for. "
            f"That is this adapter being out of date, not a message to drop quietly."
        )

    def book_stream_delivers_full_depth(self) -> bool:
        """True: the partial-depth stream sends a fresh top-N snapshot every push.

        Measured 2026-08-22 -- every captured depth20 message carried 20 bids and
        20 asks, not a difference against a previous one. So a reader may record
        one every few seconds and lose only how finely it can see the book move.

        This is the partial-depth stream specifically. Binance also offers a diff
        stream, which is not what this adapter subscribes to precisely because it
        would need a REST snapshot and a re-initialisation procedure to be
        readable at all.
        """
        return True

    def sequence_continuity(self, stream_kind: StreamKind) -> SequenceContinuity:
        """What each of this venue's streams promises about its own numbering.

        The book is the strong case and the one that matters: Binance states the
        rule itself -- each event's `pu` equals the previous event's `u`, and
        otherwise the local book must be re-initialised -- so continuity is
        checked against what the message claims rather than against arithmetic.
        Aggregate trade ids increment by one per event, measured across the whole
        2026-08-22 capture. Candles carry no sequence at all, so silence is the
        only detector there and `feed_gap_threshold` is what catches them.
        """
        if stream_kind is StreamKind.BOOK:
            return SequenceContinuity.CHAINED_TO_PREVIOUS
        if stream_kind is StreamKind.TRADE:
            return SequenceContinuity.INCREMENTS_BY_ONE
        return SequenceContinuity.NOT_NUMBERED

    def read_trades(self, payload: bytes) -> tuple[NormalisedTrade, ...]:
        """One aggregate trade per message, or none if this is not a trade message.

        `m` is whether the *buyer* was the market maker, so the aggressor is the
        seller when it is true. Binance is the only one of the two venues that
        phrases the side that way round, and this is the one place that has to know.

        Fidelity travels with the trade because these are 100 ms aggregates: this
        venue has no raw trade stream at all, so a consumer counting prints here is
        counting something coarser than the same count on Bybit.
        """
        message = json.loads(payload)
        if not isinstance(message, dict) or message.get("e") != TRADE_EVENT:
            return ()
        return (
            NormalisedTrade(
                venue_id=VENUE_ID,
                symbol=message["s"],
                price=float(message["p"]),
                quantity=float(message["q"]),
                side=SELL if message["m"] else BUY,
                venue_time_ns=int(message["T"]) * MILLISECONDS_TO_NANOSECONDS,
                sequence=int(message["a"]),
                fidelity=self.trade_fidelity,
            ),
        )

    def read_previous_sequence(self, payload: bytes) -> int | None:
        """The `pu` a book message says the previous message's `u` was.

        None for anything else: only the book chains here, and returning a number
        for a stream that does not chain would invent a continuity claim the
        venue never made.
        """
        message = json.loads(payload)
        if message.get("e") != BOOK_EVENT:
            return None
        return int(message["pu"])

    def read_http_ban_signal(
        self, status_code: int, headers: Mapping[str, str]
    ) -> BanSignal | None:
        """429 is "back off"; 418 is "you did not, and you are banned".

        Both are per IP. Escalating for repeat offenders from 2 minutes to 3
        days, so the cost of ignoring the first is not linear in the second.
        """
        if status_code not in (429, 418):
            return None
        lowered = {name.lower(): value for name, value in headers.items()}
        retry_after = lowered.get("retry-after")
        reason = (
            "rate limit exceeded; back off now or the next step is an IP ban"
            if status_code == 429
            else "IP banned for continuing after 429s; 2 minutes to 3 days, escalating"
        )
        return BanSignal(
            venue_id=VENUE_ID,
            observed_code=str(status_code),
            reason=reason,
            retry_after_seconds=float(retry_after) if retry_after is not None else None,
        )

    def read_stream_ban_signal(self, payload: bytes) -> BanSignal | None:
        """A rate-limit error the venue sent over the websocket rather than REST.

        The ban's end is stated only inside the message text -- "IP banned until
        <epoch ms>" -- because no Retry-After header is documented for futures.
        Reading it is the difference between waiting the stated time and guessing.
        """
        try:
            message = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(message, dict):
            return None
        envelope = message.get("error") if isinstance(message.get("error"), dict) else message
        if envelope.get("code") != TOO_MANY_REQUESTS_CODE:
            return None
        text = str(envelope.get("msg", ""))
        match = _BANNED_UNTIL_PATTERN.search(text)
        retry_after = None
        if match:
            banned_until_seconds = int(match.group(1)) / 1000
            retry_after = max(0.0, banned_until_seconds - time.time())
        return BanSignal(
            venue_id=VENUE_ID,
            observed_code=str(TOO_MANY_REQUESTS_CODE),
            reason=text or "rate limit exceeded on the websocket",
            retry_after_seconds=retry_after,
        )

    def read_used_request_weight(self, headers: Mapping[str, str]) -> int | None:
        """How much of this minute's REST weight the venue says we have used.

        Read rather than counted. A local counter is a second opinion about a
        number the venue is already reporting, and it is wrong whenever anything
        else on this IP makes a request -- which, since the limit is per IP and
        not per key, includes every other process on this box.
        """
        for name, value in headers.items():
            if name.lower() == "x-mbx-used-weight-1m":
                return int(value)
        return None

    def catalogue_url(self, cursor: str | None = None) -> str:
        """One page, always. This venue returns every contract in a single call.

        A cursor here would be a caller believing it had more pages to fetch than
        this venue has, so it is refused rather than ignored.
        """
        if cursor is not None:
            raise ValueError(f"{VENUE_ID} paginates nothing; there is no page after {cursor!r}")
        return CATALOGUE_URL

    def read_catalogue_cursor(self, catalogue_response: object) -> str | None:
        """None, always: 872 contracts arrived in one response, measured 2026-08-22."""
        return None

    def ticker_url(self) -> str:
        return TICKER_URL

    def read_quote_volumes(self, ticker_response: object) -> Mapping[str, float]:
        """Each symbol's 24-hour quote volume, which this venue calls quoteVolume.

        `volume` on the same record is the base-asset count, and ordering by it
        would rank a symbol quoted in millions of a cheap coin above one quoted
        in thousands of an expensive one. The two fields differ by orders of
        magnitude, so picking the wrong one produces a plausible ordering that is
        simply the wrong 30 symbols -- captured irreversibly.
        """
        return {
            entry["symbol"]: float(entry["quoteVolume"])
            for entry in ticker_response
            if entry.get("quoteVolume") is not None
        }

    def funding_request_urls(self) -> tuple[str, ...]:
        """Two: the rate, then the interval. Neither is on the catalogue or ticker.

        The order is what `read_funding_facts` expects to be handed back, and it
        is the only thing that needs to know it.
        """
        return (FUNDING_RATE_URL, FUNDING_INTERVAL_URL)

    def read_funding_facts(
        self,
        listings: Sequence[SymbolListing],
        ticker_response: object,
        funding_responses: Sequence[object],
    ) -> Mapping[str, ContractFunding]:
        """Each perpetual's funding, from premiumIndex joined to fundingInfo.

        The rate is `lastFundingRate`: the rate the venue last actually charged,
        rather than the running `estimatedSettlePrice` premium, because a carry a
        position will be billed is the one worth pricing against.

        A symbol absent from fundingInfo gets `settlements_per_day=None` -- 132 of
        872 listed symbols on 2026-08-22 -- rather than this venue's documented
        eight-hour default. Of the 760 it does state, 444 settle four-hourly and 2
        hourly, so a blanket eight would be wrong for the majority of the symbols
        the venue bothered to mention, and would be wrong silently. Whoever wants
        one of those 132 priced can settle it in one call to /fapi/v1/fundingRate,
        whose history gives the interval between two consecutive settlements as a
        measurement rather than an assumption.

        Neither the listings nor the ticker response is read: this venue puts no
        funding on its exchangeInfo or its 24-hour statistics.
        """
        if len(funding_responses) != len(self.funding_request_urls()):
            raise ValueError(
                f"{VENUE_ID} asked for {len(self.funding_request_urls())} funding responses "
                f"and was handed {len(funding_responses)}. Reading them in the wrong order "
                f"would attach one symbol's rate to another's interval, which is a wrong "
                f"carry cost that looks exactly like a right one."
            )
        rate_response, interval_response = funding_responses
        settlements_per_day = {
            entry["symbol"]: HOURS_PER_DAY / float(entry["fundingIntervalHours"])
            for entry in interval_response
            if entry.get("fundingIntervalHours")
        }
        facts = {}
        for entry in rate_response:
            rate = entry.get("lastFundingRate")
            if rate is None:
                continue
            symbol = entry["symbol"]
            facts[symbol] = ContractFunding(
                symbol=symbol,
                rate_per_settlement=float(rate),
                settlements_per_day=settlements_per_day.get(symbol),
                source=(
                    f"{FUNDING_RATE_URL} lastFundingRate"
                    + (
                        f", {FUNDING_INTERVAL_URL} fundingIntervalHours"
                        if symbol in settlements_per_day
                        else ", interval undeclared by this venue"
                    )
                ),
            )
        return facts

    def read_symbol_listings(self, catalogue_response: object) -> tuple[SymbolListing, ...]:
        """Every contract the venue lists, as listings, from an exchangeInfo response.

        Every contract type is kept, tokenised equities included. Their symbols
        look exactly like crypto perpetuals -- AAPLUSDT is quoted in USDT and
        marked active -- and only `contractType` separates them, so it is carried
        through rather than filtered on here (spec §1.1, the user's ruling of
        2026-08-21: capture is irreversible, a filter at order time is not).
        """
        symbols = catalogue_response["symbols"]
        return tuple(
            SymbolListing(
                symbol=entry["symbol"],
                contract_type=entry.get("contractType", ""),
                status=entry["status"],
                price_increment=_read_price_increment(entry),
                instrument_kind=INSTRUMENT_KIND_OF_CONTRACT_TYPE.get(
                    entry.get("contractType", "")
                ),
            )
            for entry in symbols
        )

    def is_symbol_capturable(self, listing: SymbolListing) -> bool:
        """TRADING and nothing else.

        SETTLING contracts are on their way to delisting and PENDING_TRADING ones
        have not started, so neither has a tape worth opening. This is not a
        judgement about what is worth trading -- that decision belongs to the
        phase that has something to trade with.
        """
        return listing.status == _LIMITS["capturable_status"].value


# This venue's word for a contract type, translated into the system's own. The
# tokenised equities are perpetuals in every mechanical sense -- they are quoted
# in USDT, they never expire and they settle funding on the same schedule -- so
# they carry the same kind; whether one should be traded is a segment question,
# not a pricing one. The quarterlies are dated: measured 2026-08-22, all four of
# them are absent from fundingInfo and quote lastFundingRate 0.00000000, which is
# exactly the shape that would price as a free perpetual if the kind were
# inferred from funding instead of stated here.
INSTRUMENT_KIND_OF_CONTRACT_TYPE = {
    "PERPETUAL": PERPETUAL_FUTURE,
    "TRADIFI_PERPETUAL": PERPETUAL_FUTURE,
    "CURRENT_QUARTER": DATED_FUTURE,
    "NEXT_QUARTER": DATED_FUTURE,
}


def _read_price_increment(entry: Mapping[str, object]) -> float | None:
    """The tick size out of a symbol's PRICE_FILTER, or None if it declares none.

    None rather than a guess: `tick-size-resolver` infers an increment from live
    bid/ask spacing when the venue does not declare one, and it can only know to
    do that if the absence is reported as an absence.
    """
    for symbol_filter in entry.get("filters", ()):
        if symbol_filter.get("filterType") == "PRICE_FILTER":
            tick_size = symbol_filter.get("tickSize")
            if tick_size is not None:
                return float(tick_size)
    return None
