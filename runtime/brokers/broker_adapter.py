"""The shape every Indian-market broker adapter is, and nothing else has to know.

Fresh contract, not an extension of runtime/venues/venue_adapter.py -- that one
is shaped for crypto perpetuals (funding, mark price, premium) and none of it
exists for NSE/BSE cash and derivatives. Retiring it is a separate piece of
work; this module imports nothing from it (docs/superpowers/specs/
2026-09-01-upstox-adapter-design.md section 1).

Upstox is the first of six brokers docs/goal.md commits to. Every method here
is broker-agnostic on purpose -- which broker answers is a settings choice
resolved through an adapter registry, never a part's identity (T-1, T-4), the
same discipline venue_adapter.py already used for its two crypto venues.
"""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass
from typing import Mapping, Sequence

__all__ = [
    "BanSignal",
    "BrokerAdapter",
    "BrokerCandle",
    "BrokerFact",
    "BrokerFactWithoutSource",
    "BrokerOpenInterest",
    "BrokerOptionGreeks",
    "BrokerOrderBookLevel",
    "BrokerOrderBookUpdate",
    "BrokerTokenPolicy",
    "ConnectionDiscipline",
    "DecodedFeedMessage",
    "HeartbeatDiscipline",
    "InstrumentListing",
    "LtpUpdate",
    "QUESTIONS_ANSWERED_FROM_BROKER_DATA",
    "QUESTIONS_ANSWERED_WITHOUT_BROKER_DATA",
    "SubscriptionMode",
    "SubscriptionRequest",
    "TokenExpiryPolicy",
]


class BrokerFactWithoutSource(ValueError):
    """A broker limit was declared without saying where it was read from."""


@dataclass(frozen=True)
class BrokerFact:
    """One number the broker itself fixes, and where that number was read.

    Same shape and same reasoning as venue_adapter.VenueFact (RL-061): the
    operator owns settings, the broker owns these, and both carry provenance.
    """

    name: str
    value: int | float | str | tuple[int | str, ...]
    unit: str
    source: str

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise BrokerFactWithoutSource(
                f"broker fact '{self.name}' carries no source. A capacity figure a "
                f"part acts on must say which document or measurement it came "
                f"from -- RL-061 does not stop at the broker boundary."
            )


class TokenExpiryPolicy(enum.StrEnum):
    """What kind of session expiry a broker imposes -- T-5, an explicit set.

    Crypto had no equivalent: an API key signs each request and does not
    expire on its own schedule. A broker session here can, so this is the one
    genuinely new concept versus venue_adapter's contract.
    """

    NEVER = "never"
    DAILY_AT_FIXED_TIME = "daily-at-fixed-time"


@dataclass(frozen=True)
class BrokerTokenPolicy:
    """How this broker's session token expires, and whether refresh can be automated.

    `daily_expiry_time_ist` is only meaningful under DAILY_AT_FIXED_TIME.
    `auto_refreshable` says whether a scheduler can renew the token without a
    human -- Upstox's documented flows all need one, but TOTP-based headless
    login (spec section 3) makes it true in practice, which is a fact worth
    a part being able to read rather than assume.
    """

    expiry: TokenExpiryPolicy
    daily_expiry_time_ist: object | None  # datetime.time, kept loosely typed to avoid a stdlib import here
    auto_refreshable: bool
    source: str

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise BrokerFactWithoutSource(
                f"a token expiry policy carries no source -- when a session dies is "
                f"a fact a scheduler acts on, the same as any other broker fact."
            )


@dataclass(frozen=True)
class InstrumentListing:
    """One contract as a broker's own instrument master lists it.

    Deliberately not venue_adapter.SymbolListing -- the fields genuinely
    differ (instrument_key, lot_size, strike/expiry/option-type) and reusing
    that type id would wire this reader into every existing crypto consumer
    of symbol-universe (docs/proposals/upstox-broker-adapter.md).

    `expiry_ms`, `strike_price`, `underlying_key` are None for a plain equity
    listing (Upstox's own JSON omits them there, not zeroes them -- absence is
    carried as absence, never filled in).
    """

    instrument_key: str
    exchange: str
    segment: str
    instrument_type: str
    trading_symbol: str
    lot_size: int | None
    tick_size: float | None
    freeze_quantity: float | None
    expiry_ms: int | None
    strike_price: float | None
    underlying_key: str | None
    # Only present in Upstox's MIS-specific instrument file, per symbol. None
    # for a listing read from a file that doesn't carry it, never zero.
    intraday_margin_percent: float | None
    intraday_leverage: float | None


class SubscriptionMode(enum.StrEnum):
    """The four ways to ask Upstox's v3 feed for one instrument's data.

    Values match the .proto's own RequestMode enum names exactly (the spec's
    docs-page reading of "full" was corrected to the real "full_d5" against
    the actual wire schema).
    """

    LTPC = "ltpc"
    OPTION_GREEKS = "option_greeks"
    FULL = "full_d5"
    FULL_D30 = "full_d30"


@dataclass(frozen=True)
class SubscriptionRequest:
    """One instrument, at one mode, that a connection should carry."""

    instrument_key: str
    mode: SubscriptionMode


@dataclass(frozen=True)
class LtpUpdate:
    """One last-traded-price update. See TradeFidelity.LAST_TRADED_PRICE_ONLY --
    this is never a print, it is the exchange's own last-traded-price ticker."""

    instrument_key: str
    last_traded_price: float
    last_traded_quantity: float | None
    last_traded_time_ms: int
    close_price: float | None
    broker_time_ns: int


@dataclass(frozen=True)
class BrokerCandle:
    """One OHLC bar. `is_closed` is None when the broker does not say --
    Upstox never does; a reader that guessed would be stating a fact the
    broker itself never stated."""

    instrument_key: str
    interval: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    bar_time_ms: int
    is_closed: bool | None


@dataclass(frozen=True)
class BrokerOrderBookLevel:
    bid_price: float
    bid_quantity: float
    ask_price: float
    ask_quantity: float


@dataclass(frozen=True)
class BrokerOrderBookUpdate:
    instrument_key: str
    levels: tuple[BrokerOrderBookLevel, ...]
    broker_time_ns: int


@dataclass(frozen=True)
class BrokerOpenInterest:
    instrument_key: str
    open_interest: float
    volume_traded_today: float
    total_buy_quantity: float
    total_sell_quantity: float
    average_traded_price: float
    broker_time_ns: int


@dataclass(frozen=True)
class BrokerOptionGreeks:
    instrument_key: str
    delta: float
    theta: float
    gamma: float
    vega: float
    rho: float
    implied_volatility: float
    broker_time_ns: int


@dataclass(frozen=True)
class DecodedFeedMessage:
    """One decoded WebSocket message, decomposed into this project's own
    record kinds. `kind` matches the proto Type enum's own names, so a reader
    never has to translate an integer back into what it means.

    Every tuple is empty rather than None when this message carried none of
    that kind -- a market_info message has empty everything except
    market_segment_status, for instance."""

    kind: str  # "market_info" | "initial_feed" | "live_feed"
    market_segment_status: Mapping[str, str] | None
    ltp_updates: tuple[LtpUpdate, ...]
    candles: tuple[BrokerCandle, ...]
    book_updates: tuple[BrokerOrderBookUpdate, ...]
    open_interest: tuple[BrokerOpenInterest, ...]
    option_greeks: tuple[BrokerOptionGreeks, ...]
    broker_time_ns: int


@dataclass(frozen=True)
class BanSignal:
    broker_id: str
    observed_code: str
    reason: str
    retry_after_seconds: float | None = None


@dataclass(frozen=True)
class HeartbeatDiscipline:
    expects_client_ping: bool
    interval_seconds: float | None = None
    ping_frame: bytes | None = None


@dataclass(frozen=True)
class ConnectionDiscipline:
    lifetime_seconds: float | None = None
    new_connections_per_window: int | None = None
    rate_window_seconds: float | None = None
    concurrent_connections: int | None = None


class BrokerAdapter(abc.ABC):
    """Every question a broker has to answer, and nothing a part has to know.

    Subclasses are the only code permitted to know a broker's URLs, message
    shape or limits. An adapter registry resolves them by id from settings --
    no part import ever names a broker.
    """

    @property
    @abc.abstractmethod
    def broker_id(self) -> str:
        """The id this broker is named by in settings and on the tape."""

    @abc.abstractmethod
    def declared_limits(self) -> Mapping[str, BrokerFact]:
        """Every capacity figure this adapter acts on, each with its source."""

    @abc.abstractmethod
    def token_policy(self) -> BrokerTokenPolicy:
        """How this broker's session token expires, and whether it can auto-refresh."""

    @abc.abstractmethod
    def instrument_listing_urls(self) -> tuple[str, ...]:
        """Where this broker's instrument master is fetched from, one URL per file."""

    @abc.abstractmethod
    def read_instrument_listings(self, response: object) -> tuple[InstrumentListing, ...]:
        """Every contract in one already-fetched, already-decoded listing file."""

    @abc.abstractmethod
    def stream_endpoint_url(self) -> str:
        """The websocket URL for this broker's market feed."""

    @abc.abstractmethod
    def heartbeat_discipline(self) -> HeartbeatDiscipline:
        """Who pings whom, how often, and with what."""

    @abc.abstractmethod
    def connection_discipline(self) -> ConnectionDiscipline:
        """What this broker says about opening, holding and losing connections."""

    @abc.abstractmethod
    def encode_subscribe_frame(self, requests: Sequence[SubscriptionRequest]) -> bytes:
        """The bytes that subscribe to these instrument/mode pairs."""

    @abc.abstractmethod
    def encode_unsubscribe_frame(self, requests: Sequence[SubscriptionRequest]) -> bytes:
        """The bytes that unsubscribe from these instrument/mode pairs."""

    @abc.abstractmethod
    def does_subscription_fit_connection(
        self,
        existing: Sequence[SubscriptionRequest],
        candidate: SubscriptionRequest,
    ) -> bool:
        """Whether one more subscription fits alongside what a connection already carries.

        Upstox's limits are two-layered -- an individual cap per mode and a
        lower combined cap across modes on one connection -- so this cannot be
        a single counter the way a character-count or stream-count check
        would be."""

    @abc.abstractmethod
    def decode_feed_message(self, payload: bytes) -> DecodedFeedMessage:
        """One WebSocket message, decomposed into this project's own record kinds."""

    @abc.abstractmethod
    def read_http_ban_signal(
        self, status_code: int, headers: Mapping[str, str]
    ) -> BanSignal | None:
        """A ban this broker signalled over REST, or None if it did not."""


# Split the same way venue_adapter.py's are: these need no live broker data,
# so a conformance test can ask every adapter all of them with no network.
QUESTIONS_ANSWERED_WITHOUT_BROKER_DATA = (
    "broker_id",
    "declared_limits",
    "token_policy",
    "instrument_listing_urls",
    "stream_endpoint_url",
    "heartbeat_discipline",
    "connection_discipline",
    "encode_subscribe_frame",
    "encode_unsubscribe_frame",
    "does_subscription_fit_connection",
)

# These need a real payload or a real listing response to ask meaningfully.
QUESTIONS_ANSWERED_FROM_BROKER_DATA = (
    "read_instrument_listings",
    "decode_feed_message",
    "read_http_ban_signal",
)
