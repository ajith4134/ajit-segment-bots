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
from dataclasses import dataclass
from typing import Mapping, Sequence

from runtime.tape import NOT_SENT, StreamKind, TradeFidelity

__all__ = [
    "BanSignal",
    "HeartbeatDiscipline",
    "MessageFacts",
    "QUESTIONS_ANSWERED_FROM_A_VENUE_MESSAGE",
    "QUESTIONS_ANSWERED_WITHOUT_VENUE_DATA",
    "StreamRequest",
    "SymbolListing",
    "VenueAdapter",
    "VenueFact",
    "VenueFactWithoutSource",
]


class VenueFactWithoutSource(ValueError):
    """A venue limit was declared without saying where it was read from."""


@dataclass(frozen=True)
class VenueFact:
    """One number the venue itself fixes, and where that number was read.

    The operator owns settings; the venue owns these. Both carry provenance,
    because RL-061 is about a number being answerable for, not about which side
    of the boundary it came from.
    """

    name: str
    value: int | float | str
    unit: str
    source: str

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise VenueFactWithoutSource(
                f"venue fact '{self.name}' carries no source. A capacity figure a part "
                f"acts on must say which document or measurement it came from -- RL-061 "
                f"does not stop at the venue boundary."
            )


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
    def read_message_facts(self, payload: bytes) -> MessageFacts | None:
        """The index fields inside one raw venue message, or None if it carries no data.

        None means a control frame -- a subscribe acknowledgement, a pong, an
        error envelope. Those are not tape records, and telling them apart is
        venue knowledge, so the adapter decides rather than the caller guessing
        from a missing field.
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
    def read_symbol_listings(self, catalogue_response: object) -> tuple[SymbolListing, ...]:
        """Every contract this venue lists, as listings, from its own catalogue response."""

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
)

# The questions that need a real message or a real catalogue response to ask.
# RL-063 forbids inventing one, so the conformance test checks these are answered
# by the adapter rather than inherited unimplemented, and each venue's own tests
# exercise them against payloads that venue actually sent.
QUESTIONS_ANSWERED_FROM_A_VENUE_MESSAGE = (
    "read_message_facts",
    "read_http_ban_signal",
    "read_stream_ban_signal",
    "read_symbol_listings",
    "is_symbol_capturable",
)
