"""broker-underlying-price-frame-bridge: republishes one broker's underlying-
instrument prices as symbol-price-frame -- the type regime-classifier,
mean-reversion-detector, cointegration-pair-finder, spread-reversion-detector
and momentum-burst-detector already consume (docs/superpowers/specs/
2026-09-01-options-segment-bots-design.md section 4, "survives unchanged").

Not a swap of those detectors' consumes: broker-price-frame was deliberately
kept as its own data type, not merged into symbol-price-frame, specifically
so R-01 would not auto-wire it into every crypto consumer of that type
(docs/proposals/broker-price-quote-samplers.md). This part is the explicit,
named crossing R-01 requires -- it consumes broker-price-frame and produces
symbol-price-frame on purpose, nothing else changes.

Resolves which instrument_key is which underlying from real
broker-instrument-listing data rather than a hardcoded key, because the exact
key format (e.g. "NSE_INDEX|Nifty 50") is Upstox's own implementation detail
(T-4) -- only the trading_symbol (NIFTY, BANKNIFTY, SENSEX) is this part's
business, and that is a named setting with its own provenance.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from parts.market_data_feed.price_level_sampler import SymbolPriceFrame, SymbolPriceLevel

PART_ID = "broker-underlying-price-frame-bridge"

# The venue_id this part stamps on every frame it publishes -- the broker
# whose InstrumentListing/BrokerPriceFrame it reads. Not read from settings:
# this part is only ever instantiated against Upstox until a second broker
# adapter exists, at which point the broker_id becomes a real parameter, the
# same way UpstoxAdapter.broker_id already carries it on the consuming side.
UPSTOX_VENUE_ID = "upstox"

PART_DECLARATION = PartDeclaration(
    part_id="broker-underlying-price-frame-bridge",
    consumes=("broker-subscribed-instrument-listing", "broker-price-frame"),
    produces=("symbol-price-frame", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="delays",
)


@dataclass
class BridgeStanding:
    listings_seen: int = 0
    underlyings_resolved: int = 0
    price_frames_seen: int = 0
    levels_matched: int = 0
    frames_published: int = 0


class BrokerUnderlyingPriceFrameBridge:
    """Holds the resolved underlyings and republishes their prices."""

    def __init__(self, tracked_trading_symbols: tuple[str, ...], now_ns=time.time_ns) -> None:
        if not tracked_trading_symbols:
            raise ValueError(
                "the bridge needs at least one underlying trading_symbol to track; an "
                "empty list publishes nothing while looking like a working bridge"
            )
        self._tracked = frozenset(tracked_trading_symbols)
        self._now_ns = now_ns
        self._instrument_key_to_symbol: dict[str, str] = {}
        self.standing = BridgeStanding()

    def observe_listing(self, listing) -> None:
        """One instrument listing. Remembered only if it is a tracked underlying."""
        self.standing.listings_seen += 1
        if listing.trading_symbol in self._tracked:
            if listing.instrument_key not in self._instrument_key_to_symbol:
                self.standing.underlyings_resolved += 1
            self._instrument_key_to_symbol[listing.instrument_key] = listing.trading_symbol

    def levels_for(self, price_frame) -> tuple[SymbolPriceLevel, ...]:
        """The tracked underlyings' levels in one broker-price-frame, if any resolved yet."""
        self.standing.price_frames_seen += 1
        matched = [
            SymbolPriceLevel(symbol=symbol, price=level.price, observed_at_ns=level.observed_at_ns)
            for level in price_frame.levels
            if (symbol := self._instrument_key_to_symbol.get(level.instrument_key)) is not None
        ]
        matched.sort(key=lambda level: level.symbol)
        self.standing.levels_matched += len(matched)
        return tuple(matched)

    def frame_for(self, levels: tuple[SymbolPriceLevel, ...]) -> SymbolPriceFrame | None:
        """One symbol-price-frame for these levels, or None if there is nothing to publish."""
        if not levels:
            return None
        self.standing.frames_published += 1
        return SymbolPriceFrame(
            venue_id=UPSTOX_VENUE_ID, levels=levels,
            published_at_ns=self._now_ns(), part_number=1, of_parts=1,
        )


def describe_bridge(bridge: BrokerUnderlyingPriceFrameBridge) -> dict:
    return {
        "part_id": PART_ID,
        "listings_seen": bridge.standing.listings_seen,
        "underlyings_resolved": bridge.standing.underlyings_resolved,
        "price_frames_seen": bridge.standing.price_frames_seen,
        "levels_matched": bridge.standing.levels_matched,
        "frames_published": bridge.standing.frames_published,
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch

    listings = Batch(read=context.bus.reader("broker-subscribed-instrument-listing"))
    price_frames = Batch(read=context.bus.reader("broker-price-frame"))
    publish_frames = context.bus.publisher_for("symbol-price-frame")

    tracked = tuple(
        str(symbol)
        for symbol in context.setting("underlying_price_bridge_index_trading_symbols").value
    )
    bridge = BrokerUnderlyingPriceFrameBridge(tracked_trading_symbols=tracked)

    def tick() -> None:
        for listing in listings.payloads():
            bridge.observe_listing(listing)
        frames = tuple(
            frame
            for price_frame in price_frames.payloads()
            if (frame := bridge.frame_for(bridge.levels_for(price_frame))) is not None
        )
        if frames:
            publish_frames(frames)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_bridge(bridge),
    )


__all__ = [
    "BridgeStanding",
    "BrokerUnderlyingPriceFrameBridge",
    "PART_DECLARATION",
    "PART_ID",
    "UPSTOX_VENUE_ID",
    "describe_bridge",
    "start_part",
]
