"""broker-market-data-bridge: republishes a broker's own LTP updates as
market-data -- the crypto-era type most of the detector/execution layer
still reads, so a fill can be simulated for an option contract the same way
crypto's paper-fill-simulator already simulates one for a perpetual.

Not a swap of every `market-data` consumer's own crypto-shaped logic
(that is real, per-consumer work, already partly done elsewhere this
session for symbol-price-frame). This is the missing producer: nothing
published `market-data` for Indian instruments at all, the same gap
broker-underlying-price-frame-bridge closed for symbol-price-frame.

`side` is `None`, never fabricated -- Upstox's LTP ticker restates the
exchange's own last print, it does not stream individual prints with an
aggressor side (runtime/venues/venue_adapter.py's own NormalisedTrade
docstring, widened 2026-09-01 after an exhaustive search found zero real
consumers of `.side`/`.signed_quantity` on this type anywhere in the
codebase). `sequence` uses `NOT_SENT`, the project's own existing sentinel
for exactly this situation -- `runtime/venues/binance_usdm.py` already uses
it when a venue sends no sequence number, not a new convention invented
here.

Every tracked instrument's own LTP is bridged, not only underlyings:
paper-fill-simulator needs the price of the specific contract being
traded, not its underlying's.
"""

from __future__ import annotations

from runtime.tape import NOT_SENT, TradeFidelity
from runtime.venues.venue_adapter import NormalisedTrade
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "broker-market-data-bridge"
UPSTOX_VENUE_ID = "upstox"

PART_DECLARATION = PartDeclaration(
    part_id="broker-market-data-bridge",
    consumes=("broker-subscribed-instrument-listing", "broker-market-data"),
    produces=("market-data", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="delays",
)


class BrokerMarketDataBridge:
    """Resolves an instrument's own trading_symbol and republishes its LTP."""

    def __init__(self) -> None:
        self._trading_symbol_by_key: dict[str, str] = {}

    def observe_listing(self, listing) -> None:
        self._trading_symbol_by_key[listing.instrument_key] = listing.trading_symbol

    def trade_for(self, update) -> NormalisedTrade | None:
        """One LTP update, as market-data -- or None if unresolved or if the
        update carries no quantity (a real consumer needs a real number,
        never a fabricated zero)."""
        if update.last_traded_quantity is None:
            return None
        symbol = self._trading_symbol_by_key.get(update.instrument_key)
        if symbol is None:
            return None
        return NormalisedTrade(
            venue_id=UPSTOX_VENUE_ID, symbol=symbol, price=update.last_traded_price,
            quantity=update.last_traded_quantity, side=None,
            venue_time_ns=update.last_traded_time_ms * 1_000_000,
            sequence=NOT_SENT, fidelity=TradeFidelity.LAST_TRADED_PRICE_ONLY,
        )


def describe_bridge(bridge: BrokerMarketDataBridge) -> dict:
    return {
        "part_id": PART_ID,
        "instruments_resolved": len(bridge._trading_symbol_by_key),
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch

    listings = Batch(read=context.bus.reader("broker-subscribed-instrument-listing"))
    updates = Batch(read=context.bus.reader("broker-market-data"))
    publish_trades = context.bus.publisher_for("market-data")
    bridge = BrokerMarketDataBridge()

    def tick() -> None:
        for listing in listings.payloads():
            bridge.observe_listing(listing)
        trades = tuple(
            trade
            for update in updates.payloads()
            if (trade := bridge.trade_for(update)) is not None
        )
        if trades:
            publish_trades(trades)

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
    "BrokerMarketDataBridge",
    "PART_DECLARATION",
    "PART_ID",
    "UPSTOX_VENUE_ID",
    "describe_bridge",
    "start_part",
]
