"""UpstoxAdapter: everything Upstox-specific, and the only place it is allowed
to live (docs/superpowers/specs/2026-09-01-upstox-adapter-design.md).
"""

from __future__ import annotations

import datetime
import json
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
        listings = []
        for row in response:
            listings.append(
                InstrumentListing(
                    instrument_key=row["instrument_key"],
                    exchange=row["exchange"],
                    segment=row["segment"],
                    instrument_type=row["instrument_type"],
                    trading_symbol=row.get("trading_symbol", ""),
                    lot_size=row.get("lot_size"),
                    tick_size=row.get("tick_size"),
                    freeze_quantity=row.get("freeze_quantity"),
                    expiry_ms=row.get("expiry"),
                    strike_price=row.get("strike_price"),
                    underlying_key=row.get("underlying_key"),
                    intraday_margin_percent=row.get("intraday_margin"),
                    intraday_leverage=row.get("intraday_leverage"),
                )
            )
        return tuple(listings)

    def stream_endpoint_url(self) -> str:
        return "wss://api.upstox.com/v3/feed/market-data-feed"

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


__all__ = ["UPSTOX_BROKER_ID", "UpstoxAdapter"]
