"""broker-market-data-bridge: republishes a broker's own LTP updates as
market-data -- the crypto-era type most of the detector/execution layer
still reads, so a fill can be simulated for an option contract the same way
crypto's paper-fill-simulator already simulates one for a perpetual.

Not a swap of every `market-data` consumer's own crypto-shaped logic
(that is real, per-consumer work, already partly done elsewhere this
session for symbol-price-frame). This is the missing producer: nothing
published `market-data` for Indian instruments at all, the same gap
broker-underlying-price-frame-bridge closed for symbol-price-frame.

**`quantity` is `None` when Upstox states no size, never a fabricated 0
and never a dropped print.** Measured on the live tape of Friday
2026-09-04, a real trading day, across 1,500 sampled instruments: **75.1%
of LTP updates carry no `last_traded_quantity` at all** -- 100% of
NSE_COM, NCD_FO, BCD_FO and NSE_INDEX, ~50% of NSE_FO and NSE_EQ. This
bridge used to return None for every one of them, so no index price could
ever reach `market-data` and three quarters of every other instrument's
prints were thrown away. That is a crypto-shaped assumption: a crypto
trade stream is a stream of prints and every print has a size, while
Upstox's LTPC is a *price* ticker that states a size only when it has one.
The price is the fact the 26 consumers of this type overwhelmingly want;
the three that want size (`order-flow-state-encoder`,
`market-anomaly-detector`, `symbol-profile-store`, plus
`liquidity-grader` via `quote_volume`) skip a print that states none,
which is the same shape `side` already carries.

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

from runtime.pending_instrument_updates import UpdatesAwaitingInstrumentListing
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

    def __init__(self, *, held_instrument_limit: int) -> None:
        self._trading_symbol_by_key: dict[str, str] = {}
        # The feed's whole snapshot arrives the instant the socket connects,
        # while the listings arrive on a 300 s restatement conveyor, so the
        # first burst always meets an empty map. Held rather than dropped --
        # runtime/pending_instrument_updates.py carries the measurement.
        self._awaiting_listing: UpdatesAwaitingInstrumentListing = (
            UpdatesAwaitingInstrumentListing(held_instrument_limit=held_instrument_limit)
        )

    def observe_listing(self, listing) -> None:
        self._trading_symbol_by_key[listing.instrument_key] = listing.trading_symbol

    def trade_for(self, update) -> NormalisedTrade | None:
        """One LTP update, as market-data -- or None while its instrument is
        unknown, in which case the update is held until the listing arrives."""
        symbol = self._trading_symbol_by_key.get(update.instrument_key)
        if symbol is None:
            self._awaiting_listing.hold(update.instrument_key, update)
            return None
        return NormalisedTrade(
            venue_id=UPSTOX_VENUE_ID, symbol=symbol, price=update.last_traded_price,
            # None, not 0.0: Upstox states a size on a quarter of its LTP
            # updates and on no index at all (module docstring).
            quantity=update.last_traded_quantity, side=None,
            venue_time_ns=update.last_traded_time_ms * 1_000_000,
            sequence=NOT_SENT, fidelity=TradeFidelity.LAST_TRADED_PRICE_ONLY,
        )

    def trades_now_resolvable(self) -> tuple[NormalisedTrade, ...]:
        """Every held update whose listing has since arrived, as market-data."""
        released = tuple(
            self._awaiting_listing.release_resolvable(
                lambda key: key in self._trading_symbol_by_key
            )
        )
        return tuple(
            trade for update in released if (trade := self.trade_for(update)) is not None
        )

    def trades_from(self, updates) -> tuple[NormalisedTrade, ...]:
        """One tick's worth of market-data: what the listings just unblocked,
        then this tick's own updates.

        The order is the point and it is why this is a method rather than a
        line in `start_part`. A released update is by definition older than one
        that arrived this tick, so publishing it afterwards would put a stale
        price on the wire behind the current one -- and `market-data` is read
        as a level by everything that keeps a last price.
        """
        return self.trades_now_resolvable() + tuple(
            trade for update in updates if (trade := self.trade_for(update)) is not None
        )


def describe_bridge(bridge: BrokerMarketDataBridge) -> dict:
    return {
        "part_id": PART_ID,
        "instruments_resolved": len(bridge._trading_symbol_by_key),
        **bridge._awaiting_listing.describe(),
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch

    listings = Batch(read=context.bus.reader("broker-subscribed-instrument-listing"))
    updates = Batch(read=context.bus.reader("broker-market-data"))
    publish_trades = context.bus.publisher_for("market-data")
    bridge = BrokerMarketDataBridge(
        held_instrument_limit=int(
            context.setting("unresolved_broker_update_hold_limit").value
        ),
    )

    def tick() -> None:
        for listing in listings.payloads():
            bridge.observe_listing(listing)
        trades = bridge.trades_from(updates.payloads())
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
