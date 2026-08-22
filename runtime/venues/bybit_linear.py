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
from typing import Mapping, Sequence

from runtime.tape import NOT_SENT, StreamKind, TradeFidelity
from runtime.venues.venue_adapter import (
    BanSignal,
    HeartbeatDiscipline,
    MessageFacts,
    SequenceContinuity,
    StreamRequest,
    SymbolListing,
    VenueAdapter,
    VenueFact,
    VenueMessageNotRecognised,
)

VENUE_ID = "bybit-linear"

# One public endpoint carries every stream kind here -- unlike Binance, where the
# routed path differs per stream and getting it wrong delivers silence.
LINEAR_PUBLIC_STREAM = "wss://stream.bybit.com/v5/public/linear"
REST_HOST = "https://api.bybit.com"
CATALOGUE_URL = f"{REST_HOST}/v5/market/instruments-info?category=linear"

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
        if stream_kind in (StreamKind.TRADE, StreamKind.CANDLE, StreamKind.BOOK):
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
        return SequenceContinuity.NOT_NUMBERED

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
            )
            for entry in instruments
        )

    def is_symbol_capturable(self, listing: SymbolListing) -> bool:
        """Trading and nothing else. The venue spells it with one capital letter."""
        return listing.status == _LIMITS["capturable_status"].value


def _read_price_increment(entry: Mapping[str, object]) -> float | None:
    """The tick size out of an instrument's priceFilter, or None if it declares none."""
    price_filter = entry.get("priceFilter") or {}
    tick_size = price_filter.get("tickSize")
    return float(tick_size) if tick_size is not None else None
