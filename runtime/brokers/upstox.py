"""UpstoxAdapter: everything Upstox-specific, and the only place it is allowed
to live (docs/superpowers/specs/2026-09-01-upstox-adapter-design.md).
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import urllib.parse
import uuid
from typing import Mapping, Sequence

from runtime.brokers import upstox_market_data_feed_pb2 as feed_pb2
from runtime.brokers.broker_adapter import (
    BanSignal,
    BrokerAdapter,
    BrokerCandle,
    BrokerFact,
    BrokerOpenInterest,
    BrokerOptionGreeks,
    BrokerOrderBookLevel,
    BrokerOrderBookUpdate,
    BrokerTokenPolicy,
    ConnectionDiscipline,
    DecodedFeedMessage,
    HeartbeatDiscipline,
    InstrumentListing,
    LtpUpdate,
    SubscriptionMode,
    SubscriptionRequest,
    TokenExpiryPolicy,
)

UPSTOX_BROKER_ID = "upstox"

# Order placement and per-order margin -- UpstoxAdapter methods, not yet a
# declared part (spec section 6a). Both need an order intent as input and
# nothing in this project produces one for Indian markets yet, so no
# broker-order-router is declared: it would be an R-01 dangling input, not
# a formality. These types and methods exist so declaring that part, the
# day something produces broker-order-request, is a thin wiring layer
# rather than a redesign.


class OrderPlacementRefused(ValueError):
    """Upstox's own response said the order was not accepted."""


@dataclasses.dataclass(frozen=True)
class OrderRequest:
    """One order, in the fields Upstox's place-order API actually takes.

    `product` is `I` (intraday), `D` (delivery) or `MTF` -- no plain `CNC`
    string, delivery is `D`. `market_protection` of `-1` means "exchange
    default"; `0` means none, which the exchange itself refuses for a
    MARKET order placed through the API.
    """

    instrument_key: str
    quantity: int
    product: str
    order_type: str
    transaction_type: str
    validity: str = "DAY"
    price: float = 0.0
    trigger_price: float = 0.0
    disclosed_quantity: int = 0
    is_amo: bool = False
    market_protection: int = -1
    tag: str | None = None


@dataclasses.dataclass(frozen=True)
class ModifyRequest:
    """One change to a live order, in the fields Upstox's modify API takes.

    Source: upstox.com/developer/api-documentation/v3/modify-order, fetched
    2026-09-12. `order_id` is the BROKER's id, not the client order id -- the
    caller has to have kept it from the place response, which is why
    broker-order-router remembers the mapping.

    Upstox's own required/optional split is preserved rather than tidied:
    `validity`, `price`, `order_type` and `trigger_price` are all REQUIRED even
    when unchanged, because the API assumes the original order's value only for
    fields left out entirely and these four are not among them. A modify that
    omitted `price` on a limit order would not keep the old price; it would be
    refused.
    """

    order_id: str
    order_type: str
    validity: str
    price: float = 0.0
    trigger_price: float = 0.0
    quantity: int | None = None
    disclosed_quantity: int | None = None
    market_protection: int = 0


@dataclasses.dataclass(frozen=True)
class OrderResult:
    order_id: str


@dataclasses.dataclass(frozen=True)
class MarginQuoteRequest:
    """One candidate order to price margin for -- not yet placed."""

    instrument_key: str
    quantity: int
    transaction_type: str
    product: str
    price: float | None = None


@dataclasses.dataclass(frozen=True)
class MarginQuote:
    """What Upstox says this candidate order would cost in margin."""

    span_margin: float
    exposure_margin: float
    equity_margin: float
    net_buy_premium: float
    additional_margin: float


MAXIMUM_MARGIN_QUOTE_INSTRUMENTS = 20  # Upstox's own documented cap per call

# Historical candles. Source: upstox.com/developer/api-documentation/v3/
# get-historical-candle-data, fetched 2026-09-02, and one real response taken
# from the live API the same day.
HISTORICAL_CANDLE_HOST = "https://api.upstox.com/v3/historical-candle"
# Upstox's own five units, written out rather than accepted freely: a unit it
# does not publish is answered with a 400 at best and an empty series at worst,
# and an empty series is indistinguishable from a market that did not trade.
HISTORICAL_CANDLE_UNITS = ("minutes", "hours", "days", "weeks", "months")
# What Upstox serves, per unit -- the bound a caller has to plan requests
# against rather than discover by being refused.
HISTORICAL_MINUTE_DATA_BEGINS = "2022-01-01"
HISTORICAL_MINUTE_WINDOW_DAYS = 30  # one month per request, for 1-15 minute intervals


# Upstox states an instrument's tick size in paise while quoting every price in
# rupees, so the master's 5.0 for an NSE option is the standard 0.05-rupee tick.
#
# **Upstox's documentation does not say this.** The instruments page
# (upstox.com/developer/api-documentation/instruments/, raw-fetched 2026-09-04)
# describes the field only as "The minimum price movement of the equity" and
# gives no unit; its own EQ sample carries `tick_size: 5.0` for a cash equity,
# whose NSE tick is 0.05 rupees. The conclusion is from measurement, not from the
# document -- see `tick_size_in_rupees` and
# `measurements/2026-09-04-why-no-paper-order-ever-fills/`.
#
# Not a settings question under RL-061: this is a unit conversion between two
# statements of one physical fact, fixed by what the exchange quotes in, and a
# number an operator could tune would only let the two disagree again.
PAISE_PER_RUPEE = 100.0


def tick_size_in_rupees(declared) -> float | None:
    """One instrument's tick, in the unit its prices are quoted in.

    Absence stays absence: a listing Upstox does not carry a tick for reports
    None, never a zero and never a guessed default. A zero would read as "this
    instrument has no minimum increment", and position-sizer refuses a
    non-positive increment by name rather than dividing by it.
    """
    if declared is None:
        return None
    return float(declared) / PAISE_PER_RUPEE

# Source for every figure below: upstox.com/developer/api-documentation/v3/get-market-data-feed,
# fetched 2026-09-01. Free-tier limits -- Upstox Plus limits are a settings
# question for whenever that tier is actually bought, not baked in here.
_LIMITS_SOURCE = "upstox v3 market-data-feed docs, fetched 2026-09-01"


class UpstoxAdapter(BrokerAdapter):
    @property
    def broker_id(self) -> str:
        return UPSTOX_BROKER_ID

    def declared_limits(self) -> Mapping[str, BrokerFact]:
        return {
            "connections_per_user": BrokerFact(
                name="connections_per_user", value=2, unit="count", source=_LIMITS_SOURCE
            ),
            "ltpc_individual_limit": BrokerFact(
                name="ltpc_individual_limit", value=5000, unit="instrument keys", source=_LIMITS_SOURCE
            ),
            "ltpc_combined_limit": BrokerFact(
                name="ltpc_combined_limit", value=2000, unit="instrument keys", source=_LIMITS_SOURCE
            ),
            "option_greeks_individual_limit": BrokerFact(
                name="option_greeks_individual_limit", value=3000, unit="instrument keys", source=_LIMITS_SOURCE
            ),
            "option_greeks_combined_limit": BrokerFact(
                name="option_greeks_combined_limit", value=2000, unit="instrument keys", source=_LIMITS_SOURCE
            ),
            "full_individual_limit": BrokerFact(
                name="full_individual_limit", value=2000, unit="instrument keys", source=_LIMITS_SOURCE
            ),
            "full_combined_limit": BrokerFact(
                name="full_combined_limit", value=1500, unit="instrument keys", source=_LIMITS_SOURCE
            ),
        }

    def token_policy(self) -> BrokerTokenPolicy:
        return BrokerTokenPolicy(
            expiry=TokenExpiryPolicy.DAILY_AT_FIXED_TIME,
            daily_expiry_time_ist=datetime.time(3, 30),
            # True because upstox-totp automates the whole login -> code ->
            # token exchange without a human. Not true of Upstox's own
            # documented flows on their own.
            auto_refreshable=True,
            source="upstox.com/developer/api-documentation/get-token, fetched 2026-09-01; "
                   "auto_refreshable via upstox-totp 1.0.8 (PyPI, MIT)",
        )

    def instrument_listing_urls(self) -> tuple[str, ...]:
        # NSE and BSE only -- MCX (commodities) is out of scope until that
        # segment is built (docs/goal.md #6, deferred).
        return (
            "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz",
            "https://assets.upstox.com/market-quote/instruments/exchange/BSE.json.gz",
        )

    def read_instrument_listings(self, response: object) -> tuple[InstrumentListing, ...]:
        """The instrument master, in this project's terms.

        `tick_size` is converted from paise to rupees here, which is the one
        place this venue's unit meets the rest of the system. Measured on the
        real NSE master and the real tape for 2026-09-04
        (`measurements/2026-09-04-why-no-paper-order-ever-fills/`): every one of
        the 194 option rows declares `tick_size` 5.0, including contracts trading
        at 0.82 rupees, while the prices Upstox streams for those same contracts
        move in 0.01 to 0.05. A 5-rupee tick cannot quote a 0.82-rupee contract
        at all, so 5.0 is five paise -- the standard NSE option tick of 0.05
        rupees -- stated in a unit the rest of the feed does not use.

        Taken as rupees it cost every trade this segment tried to open.
        `position-sizer` snaps an entry and its stop to a multiple of the
        increment, so any stop inside 5 rupees of the entry snapped onto it, and
        a stop equal to its entry is refused as being on the wrong side of it:
        707 of 843 actionable intents, 84%, with entry and stop printed as the
        same number (750.0/750.0, 80.0/80.0, 20.0/20.0). `tick-size-resolver`
        had already noticed -- it counted 967,114 disagreements between this
        declared tick and the one it inferred from the book -- but a declared
        tick beats an inferred one, correctly, so the disagreement was recorded
        and never acted on.
        """
        listings = []
        for row in response:
            listings.append(
                InstrumentListing(
                    instrument_key=row["instrument_key"],
                    exchange=row["exchange"],
                    segment=row["segment"],
                    instrument_type=row["instrument_type"],
                    trading_symbol=row.get("trading_symbol", ""),
                    # Upstox's own `name`: "RELIANCE INDUSTRIES LTD" beside
                    # "RELIANCE". Carried rather than dropped because it is the
                    # only thing in this master that a news item's "Reliance
                    # Industries" can be matched against.
                    name=row.get("name", ""),
                    lot_size=row.get("lot_size"),
                    tick_size=tick_size_in_rupees(row.get("tick_size")),
                    freeze_quantity=row.get("freeze_quantity"),
                    expiry_ms=row.get("expiry"),
                    strike_price=row.get("strike_price"),
                    underlying_key=row.get("underlying_key"),
                    underlying_symbol=row.get("underlying_symbol"),
                    intraday_margin_percent=row.get("intraday_margin"),
                    intraday_leverage=row.get("intraday_leverage"),
                    # NORMAL, SME, PCA, IPO or RELIST in Upstox's own master.
                    # Measured on the real file 2026-09-05: 9,126 NORMAL, 558
                    # SME, 36 PCA, 3 IPO, 1 RELIST across 9,724 NSE_EQ rows.
                    security_type=row.get("security_type"),
                )
            )
        return tuple(listings)

    def stream_endpoint_url(self) -> str:
        return "wss://api.upstox.com/v3/feed/market-data-feed"

    def stream_authorize_url(self) -> str:
        """Where to GET the real, signed feed URL before connecting.

        Real bug, 2026-09-02: `stream_endpoint_url()` above is not something
        a client connects to directly -- Upstox's V3 feed requires this
        separate authorize call first (Authorization: Bearer + Accept:
        application/json), and the actual connection target is whatever
        `parse_authorized_stream_url` extracts from its response. Source:
        upstox.com/developer/api-documentation/get-market-data-feed-
        authorize-v3, fetched 2026-09-02 (<endpoint-path>/feed/market-data-
        feed/authorize).
        """
        return "https://api.upstox.com/v3/feed/market-data-feed/authorize"

    @staticmethod
    def parse_authorized_stream_url(response: dict) -> str:
        """The signed, single-use wss:// URL from the authorize call's own
        response shape -- same doc page's 200 sample:
        `{"status": "success", "data": {"authorized_redirect_uri": "wss://..."}}`.
        """
        uri = response.get("data", {}).get("authorized_redirect_uri")
        if not uri:
            raise ValueError(
                f"the authorize response carried no authorized_redirect_uri: {response!r}"
            )
        return uri

    def heartbeat_discipline(self) -> HeartbeatDiscipline:
        # Standard WS ping/pong, handled by most client libraries -- Upstox
        # sends the ping frame itself, simpler than a venue requiring an
        # application-level ping.
        return HeartbeatDiscipline(expects_client_ping=False)

    def connection_discipline(self) -> ConnectionDiscipline:
        return ConnectionDiscipline(concurrent_connections=2)

    def encode_subscribe_frame(self, requests: Sequence[SubscriptionRequest]) -> bytes:
        return self._encode_frame("sub", requests)

    def encode_unsubscribe_frame(self, requests: Sequence[SubscriptionRequest]) -> bytes:
        return self._encode_frame("unsub", requests)

    @staticmethod
    def _encode_frame(method: str, requests: Sequence[SubscriptionRequest]) -> bytes:
        by_mode: dict[str, list[str]] = {}
        for request in requests:
            by_mode.setdefault(request.mode.value, []).append(request.instrument_key)
        # One frame per mode -- the documented request shape carries one
        # "mode" per message, so a caller asking for two modes at once is
        # encoded as two frames, never guessed into one.
        if len(by_mode) != 1:
            raise ValueError(
                f"encode_subscribe_frame got {len(by_mode)} distinct modes in one "
                f"call; the documented request shape carries exactly one mode per "
                f"frame, so the caller must send one frame per mode."
            )
        [(mode, instrument_keys)] = by_mode.items()
        frame = {
            "guid": str(uuid.uuid4()),
            "method": method,
            "data": {"mode": mode, "instrumentKeys": instrument_keys},
        }
        return json.dumps(frame).encode("utf-8")

    def does_subscription_fit_connection(
        self,
        existing: Sequence[SubscriptionRequest],
        candidate: SubscriptionRequest,
    ) -> bool:
        limits = self.declared_limits()
        by_mode: dict[SubscriptionMode, int] = {}
        for request in existing:
            by_mode[request.mode] = by_mode.get(request.mode, 0) + 1
        individual_key = {
            SubscriptionMode.LTPC: "ltpc_individual_limit",
            SubscriptionMode.OPTION_GREEKS: "option_greeks_individual_limit",
            SubscriptionMode.FULL: "full_individual_limit",
        }.get(candidate.mode)
        combined_key = {
            SubscriptionMode.LTPC: "ltpc_combined_limit",
            SubscriptionMode.OPTION_GREEKS: "option_greeks_combined_limit",
            SubscriptionMode.FULL: "full_combined_limit",
        }.get(candidate.mode)
        if individual_key is None:
            # FULL_D30 is an Upstox Plus mode -- no free-tier limit is
            # declared for it, so a candidate asking for it is refused rather
            # than silently allowed past a check that has nothing to check.
            return False
        candidate_count_in_mode = by_mode.get(candidate.mode, 0) + 1
        if candidate_count_in_mode > int(limits[individual_key].value):
            return False
        modes_in_use = set(by_mode) | {candidate.mode}
        if len(modes_in_use) > 1:
            total_after = len(existing) + 1
            if total_after > int(limits[combined_key].value):
                return False
        return True

    def decode_feed_message(self, payload: bytes) -> DecodedFeedMessage:
        response = feed_pb2.FeedResponse()
        response.ParseFromString(payload)

        kind = feed_pb2.Type.Name(response.type)
        segment_status = None
        if response.HasField("marketInfo"):
            segment_status = {
                segment: feed_pb2.MarketStatus.Name(status)
                for segment, status in response.marketInfo.segmentStatus.items()
            }

        broker_time_ns = response.currentTs * 1_000_000 if response.currentTs else 0

        ltp_updates: list[LtpUpdate] = []
        candles: list[BrokerCandle] = []
        book_updates: list[BrokerOrderBookUpdate] = []
        open_interest: list[BrokerOpenInterest] = []
        option_greeks: list[BrokerOptionGreeks] = []

        for instrument_key, feed in response.feeds.items():
            which = feed.WhichOneof("FeedUnion")
            if which == "ltpc":
                ltp_updates.append(self._read_ltpc(instrument_key, feed.ltpc, broker_time_ns))
            elif which == "firstLevelWithGreeks":
                first = feed.firstLevelWithGreeks
                ltp_updates.append(self._read_ltpc(instrument_key, first.ltpc, broker_time_ns))
                book_updates.append(
                    BrokerOrderBookUpdate(
                        instrument_key=instrument_key,
                        levels=(self._read_quote_level(first.firstDepth),),
                        broker_time_ns=broker_time_ns,
                    )
                )
                option_greeks.append(
                    self._read_greeks(instrument_key, first.optionGreeks, first.iv, broker_time_ns)
                )
                open_interest.append(
                    BrokerOpenInterest(
                        instrument_key=instrument_key,
                        open_interest=first.oi,
                        volume_traded_today=first.vtt,
                        total_buy_quantity=0.0,
                        total_sell_quantity=0.0,
                        average_traded_price=0.0,
                        broker_time_ns=broker_time_ns,
                    )
                )
            elif which == "fullFeed":
                full = feed.fullFeed
                full_which = full.WhichOneof("FullFeedUnion")
                if full_which == "marketFF":
                    market = full.marketFF
                    ltp_updates.append(self._read_ltpc(instrument_key, market.ltpc, broker_time_ns))
                    if market.HasField("marketLevel"):
                        book_updates.append(
                            BrokerOrderBookUpdate(
                                instrument_key=instrument_key,
                                levels=tuple(
                                    self._read_quote_level(level)
                                    for level in market.marketLevel.bidAskQuote
                                ),
                                broker_time_ns=broker_time_ns,
                            )
                        )
                    for bar in market.marketOHLC.ohlc:
                        candles.append(self._read_ohlc(instrument_key, bar))
                    if market.HasField("optionGreeks"):
                        option_greeks.append(
                            self._read_greeks(
                                instrument_key, market.optionGreeks, market.iv, broker_time_ns
                            )
                        )
                    open_interest.append(
                        BrokerOpenInterest(
                            instrument_key=instrument_key,
                            open_interest=market.oi,
                            volume_traded_today=market.vtt,
                            total_buy_quantity=market.tbq,
                            total_sell_quantity=market.tsq,
                            average_traded_price=market.atp,
                            broker_time_ns=broker_time_ns,
                        )
                    )
                elif full_which == "indexFF":
                    index = full.indexFF
                    ltp_updates.append(self._read_ltpc(instrument_key, index.ltpc, broker_time_ns))
                    for bar in index.marketOHLC.ohlc:
                        candles.append(self._read_ohlc(instrument_key, bar))

        return DecodedFeedMessage(
            kind=kind,
            market_segment_status=segment_status,
            ltp_updates=tuple(ltp_updates),
            candles=tuple(candles),
            book_updates=tuple(book_updates),
            open_interest=tuple(open_interest),
            option_greeks=tuple(option_greeks),
            broker_time_ns=broker_time_ns,
        )

    @staticmethod
    def _read_ltpc(instrument_key: str, ltpc, broker_time_ns: int) -> LtpUpdate:
        return LtpUpdate(
            instrument_key=instrument_key,
            last_traded_price=ltpc.ltp,
            last_traded_quantity=ltpc.ltq or None,
            last_traded_time_ms=ltpc.ltt,
            close_price=ltpc.cp or None,
            broker_time_ns=broker_time_ns,
        )

    @staticmethod
    def _read_quote_level(quote) -> BrokerOrderBookLevel:
        return BrokerOrderBookLevel(
            bid_price=quote.bidP, bid_quantity=quote.bidQ,
            ask_price=quote.askP, ask_quantity=quote.askQ,
        )

    @staticmethod
    def _read_ohlc(instrument_key: str, bar) -> BrokerCandle:
        return BrokerCandle(
            instrument_key=instrument_key,
            interval=bar.interval,
            open=bar.open, high=bar.high, low=bar.low, close=bar.close,
            volume=bar.vol,
            bar_time_ms=bar.ts,
            # Upstox states no closed/live flag on any OHLC entry (verified
            # against the committed .proto -- OHLC carries no such field).
            # None here is the honest reading, not a guess.
            is_closed=None,
        )

    @staticmethod
    def _read_greeks(
        instrument_key: str, greeks, implied_volatility: float, broker_time_ns: int
    ) -> BrokerOptionGreeks:
        # `iv` lives on the parent message (MarketFullFeed/FirstLevelWithGreeks),
        # not inside the OptionGreeks submessage itself -- verified against the
        # committed .proto -- so every caller passes its own sibling `iv` field
        # in rather than this method reading a field that doesn't exist on `greeks`.
        return BrokerOptionGreeks(
            instrument_key=instrument_key,
            delta=greeks.delta, theta=greeks.theta, gamma=greeks.gamma,
            vega=greeks.vega, rho=greeks.rho,
            implied_volatility=implied_volatility,
            broker_time_ns=broker_time_ns,
        )

    def read_http_ban_signal(
        self, status_code: int, headers: Mapping[str, str]
    ) -> BanSignal | None:
        if status_code == 429:
            retry_after = headers.get("Retry-After")
            return BanSignal(
                broker_id=UPSTOX_BROKER_ID,
                observed_code=str(status_code),
                reason="rate limited",
                retry_after_seconds=float(retry_after) if retry_after else None,
            )
        return None

    # -- order placement and per-order margin (spec section 6/6a) --------
    # Not part of the BrokerAdapter ABC: order fields are the most
    # broker-specific part of this whole contract, and standardising them
    # into the shared interface before a second broker's real API has been
    # read against it would be exactly the premature abstraction T-6 warns
    # against. These live on UpstoxAdapter alone for now.

    def order_endpoint_url(self) -> str:
        # Separate low-latency host from the rest of the API, per Upstox's
        # own docs -- worth preserving as a fact this method states rather
        # than a URL a caller hardcodes.
        return "https://api-hft.upstox.com/v2/order/place"

    def build_order_request_payload(self, order: OrderRequest) -> dict:
        return {
            "quantity": order.quantity,
            "product": order.product,
            "validity": order.validity,
            "price": order.price,
            "tag": order.tag,
            # Upstox's own place-order body names this field
            # instrument_token, not instrument_key -- its own
            # inconsistency, preserved rather than "fixed" here.
            "instrument_token": order.instrument_key,
            "order_type": order.order_type,
            "transaction_type": order.transaction_type,
            "disclosed_quantity": order.disclosed_quantity,
            "trigger_price": order.trigger_price,
            "is_amo": order.is_amo,
            "market_protection": order.market_protection,
        }

    def cancel_endpoint_url(self, order_id: str) -> str:
        """Where one open order is cancelled.

        Source: upstox.com/developer/api-documentation/v3/cancel-order, fetched
        2026-09-12. A DELETE with the order id as a QUERY PARAMETER and no body
        -- not a JSON field, which is what a caller reaching for the place-order
        shape would assume. Percent-encoded because an order id is the broker's
        string and this method is what states that it goes in the query.

        **v3 here while place is v2**, which is Upstox's own shape rather than
        an oversight of this project's: /v2/order/place is the documented
        low-latency place endpoint and cancel and modify are documented at v3 on
        the same host. Both take the same broker order id.
        """
        return (
            "https://api-hft.upstox.com/v3/order/cancel"
            f"?order_id={urllib.parse.quote(str(order_id), safe='')}"
        )

    def modify_endpoint_url(self) -> str:
        """Where one live order is changed. A PUT with a JSON body.

        Source: upstox.com/developer/api-documentation/v3/modify-order, fetched
        2026-09-12.
        """
        return "https://api-hft.upstox.com/v3/order/modify"

    def build_modify_request_payload(self, request: ModifyRequest) -> dict:
        """The body Upstox's modify API actually takes.

        The optional fields are omitted when unset rather than sent as zero:
        Upstox assumes the original order's value for a field left out, and
        `disclosed_quantity` in particular "must be non-zero if provided", so a
        defaulted 0 is not the same message as saying nothing.
        """
        payload = {
            "order_id": request.order_id,
            "order_type": request.order_type,
            "validity": request.validity,
            "price": request.price,
            "trigger_price": request.trigger_price,
            "market_protection": request.market_protection,
        }
        if request.quantity is not None:
            payload["quantity"] = request.quantity
        if request.disclosed_quantity is not None:
            payload["disclosed_quantity"] = request.disclosed_quantity
        return payload

    def read_order_result(self, response: dict) -> OrderResult:
        if response.get("status") != "success":
            raise OrderPlacementRefused(f"Upstox refused the order: {response}")
        return OrderResult(order_id=response["data"]["order_id"])

    def historical_candle_url(
        self, instrument_key: str, unit: str, interval: int,
        from_date: str, to_date: str,
    ) -> str:
        """Upstox's own v3 path, in Upstox's own order: to_date before from_date.

        Source: upstox.com/developer/api-documentation/v3/get-historical-candle-data,
        fetched 2026-09-02. The instrument key carries a pipe and must be
        percent-encoded or the path is not the path that was asked for.
        """
        if unit not in HISTORICAL_CANDLE_UNITS:
            raise ValueError(
                f"Upstox publishes the units {', '.join(HISTORICAL_CANDLE_UNITS)}; "
                f"got {unit!r}. Guessing a unit name is answered with a 400 at best "
                f"and an empty series at worst, which reads as a quiet market"
            )
        if interval < 1:
            raise ValueError(f"an interval is a count of {unit} and must be positive; got {interval!r}")
        return (
            f"{HISTORICAL_CANDLE_HOST}/{urllib.parse.quote(instrument_key, safe='')}"
            f"/{unit}/{interval}/{to_date}/{from_date}"
        )

    def read_historical_candles(
        self, instrument_key: str, unit: str, interval: int, response: object
    ) -> tuple[BrokerCandle, ...]:
        """One already-fetched historical response, oldest bar first.

        Two things this converts once, here at the edge, because every reader
        downstream would otherwise have to know them:

        **The rows arrive newest first.** A series read in the order Upstox
        sends it runs backwards through time, and every window builder, every
        return and every gap measured over it would be reversed.

        **The stamp is +05:30, not UTC.** 15:39 IST is 10:09 UTC. Read as UTC,
        every bar of the Indian session lands outside it -- the same trap
        market-session-calendar carries its own warning about.

        `is_closed` is True, unlike the live feed's OHLC where it is None: a bar
        Upstox serves as history is finished by construction, and that is a fact
        rather than the guess the live path refuses to make.
        """
        if not isinstance(response, Mapping) or response.get("status") != "success":
            raise ValueError(
                f"Upstox did not return a successful historical series for "
                f"{instrument_key}: {response!r}. No candles and a failed request are "
                f"different facts, and reading one as the other is how a feed that "
                f"stopped looks like a market that went quiet"
            )
        rows = response.get("data", {}).get("candles", ()) or ()
        candles = [
            BrokerCandle(
                instrument_key=instrument_key,
                interval=f"{interval}{unit}",
                open=float(row[1]), high=float(row[2]),
                low=float(row[3]), close=float(row[4]),
                volume=float(row[5]),
                bar_time_ms=int(
                    datetime.datetime.fromisoformat(row[0]).timestamp() * 1000
                ),
                is_closed=True,
            )
            for row in rows
        ]
        candles.sort(key=lambda candle: candle.bar_time_ms)
        return tuple(candles)

    def margin_endpoint_url(self) -> str:
        return "https://api.upstox.com/v2/charges/margin"

    def build_margin_quote_request_payload(
        self, requests: Sequence[MarginQuoteRequest]
    ) -> dict:
        if len(requests) > MAXIMUM_MARGIN_QUOTE_INSTRUMENTS:
            raise ValueError(
                f"asked to price margin for {len(requests)} instruments; Upstox's own "
                f"margin endpoint accepts at most {MAXIMUM_MARGIN_QUOTE_INSTRUMENTS} per call"
            )
        instruments = []
        for request in requests:
            entry = {
                "instrument_key": request.instrument_key,
                "quantity": request.quantity,
                "product": request.product,
                "transaction_type": request.transaction_type,
            }
            if request.price is not None:
                entry["price"] = request.price
            instruments.append(entry)
        return {"instruments": instruments}

    def read_margin_quotes(
        self, response: dict, instrument_keys: Sequence[str]
    ) -> Mapping[str, MarginQuote]:
        # Upstox returns margins as an ordered array, not keyed by
        # instrument -- the order matches the request's own instrument
        # order, which is why the caller's own request order is threaded
        # back in here rather than read from the response itself.
        margins = response.get("data", {}).get("margins", [])
        return {
            instrument_key: MarginQuote(
                span_margin=entry["span_margin"],
                exposure_margin=entry["exposure_margin"],
                equity_margin=entry["equity_margin"],
                net_buy_premium=entry["net_buy_premium"],
                additional_margin=entry["additional_margin"],
            )
            for instrument_key, entry in zip(instrument_keys, margins)
        }


__all__ = [
    "MAXIMUM_MARGIN_QUOTE_INSTRUMENTS",
    "MarginQuote",
    "MarginQuoteRequest",
    "OrderPlacementRefused",
    "OrderRequest",
    "OrderResult",
    "UPSTOX_BROKER_ID",
    "UpstoxAdapter",
]
