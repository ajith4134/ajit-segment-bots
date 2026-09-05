"""broker-symbol-universe-bridge: republishes a broker's own instrument master
as `symbol-universe` -- the crypto-era type eleven parts already consume and
which no running part has produced since `symbol-catalogue-reader` came off the
live spine in the 2026-09-02 cutover (docs/proposals/broker-symbol-universe-
bridge.md).

The measured consequence, 2026-09-04: `universal-symbol-sweeper` had run 3,114
sweeps over an empty list -- every skip counter reading 0, which is the
signature of iterating nothing rather than of rejecting things -- and the whole
chain below it was at zero: no `entry-candidate`, no `bull-side-candidate`, no
feature vector, no conviction, no intent, no order.

Not a change to what any consumer reads. `InstrumentListing`'s own docstring
records why the two types were never merged -- "reusing that type id would wire
this reader into every existing crypto consumer of symbol-universe" -- so this
part is the explicit, named crossing R-01 requires, the same shape
`broker-underlying-price-frame-bridge` already is.

**`symbol` is the `trading_symbol`.** `universal-symbol-sweeper` keys its
universe `(venue_id, symbol)` and matches it against `symbol-price-frame`, which
`broker-market-data-bridge` publishes under `listing.trading_symbol`. A universe
keyed on the `instrument_key` would be a universe nothing could ever price, and
would show up only as `skipped_unmeasurable` climbing -- a working-looking
bridge feeding a sweeper that finds nothing. The key format is Upstox's own
implementation detail (T-4); the trading symbol is the shared name.

**The chain is capped, and the cap is a standing ruling rather than a
convenience.** The operator's note on `captured_symbol_count` (2026-08-25) reads
"hold at 50 and stop walking. The count is not raised again until the project is
100% built", and the reason recorded there is structural:
`cointegration-pair-finder` and `spread-reversion-detector` fail as the *square*
of the universe -- at 205 symbols the pair finder had tested 3,277,888 pairs and
the spread detector had dropped 332,858 inputs and was still climbing. NIFTY's
nearest expiry alone is 174 contracts and its whole listed chain is 1,580, so
publishing a chain whole would reproduce that failure on a segment that has
never completed a trade.

Ranking by distance from the money is what makes a small cap the *right* small
cap: measured the same day
(measurements/2026-09-04-why-detector-windows-never-fill/), only 76 of 891
traded NSE_FO instruments -- 8.5% -- had traded often enough to fill a detector
window, and the ones that had not are the contracts far from the money.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.symbol_universe import CapturableSymbol
from runtime.trading_types import OPTION

PART_ID = "broker-symbol-universe-bridge"

# The venue_id stamped on every entry -- the broker whose instrument master is
# being republished. Not read from settings for the same reason
# broker-underlying-price-frame-bridge does not read its own: this part is only
# ever instantiated against Upstox until a second broker adapter exists.
UPSTOX_VENUE_ID = "upstox"

# Upstox's own words for the two option kinds in its instrument master, read off
# the real file (assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz,
# 2026-09-04). Named here rather than inferred from the presence of a strike:
# `instrument_type` is what the master actually states.
CALL = "CE"
PUT = "PE"
OPTION_INSTRUMENT_TYPES = (CALL, PUT)

PART_DECLARATION = PartDeclaration(
    part_id="broker-symbol-universe-bridge",
    consumes=("broker-instrument-listing", "broker-price-frame"),
    produces=("symbol-universe", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="delays",
)


@dataclass
class BridgeStanding:
    listings_seen: int = 0
    underlyings_resolved: int = 0
    option_contracts_known: int = 0
    price_frames_seen: int = 0
    underlyings_priced: int = 0
    underlyings_without_a_price: int = 0
    underlyings_with_no_live_expiry: int = 0
    contracts_published: int = 0
    underlyings_published: int = 0
    nearest_expiry_ms: dict = field(default_factory=dict)


class BrokerSymbolUniverseBridge:
    """Holds the master and answers what this segment's universe currently is."""

    def __init__(
        self,
        tracked_trading_symbols: tuple[str, ...],
        option_contracts_per_underlying: int | dict[str, int],
        now_ms=lambda: int(time.time() * 1000),
    ) -> None:
        if not tracked_trading_symbols:
            raise ValueError(
                "the bridge needs at least one underlying trading_symbol to track; an "
                "empty list publishes an empty universe while looking like a working "
                "bridge -- which is exactly the state that left universal-symbol-sweeper "
                "sweeping nothing for a day without anything reporting a fault"
            )
        # One width, or one per underlying. Three segments on one spine want three
        # different chains -- 50 contracts on an index, 20 on a single stock, and
        # none at all on cash equity -- and one number for all of them would either
        # truncate the index chain or spend the connection on stock strikes nothing
        # trades (2026-09-05). An int means the same width for every underlying,
        # which is what a one-segment spine states.
        if isinstance(option_contracts_per_underlying, dict):
            widths = {
                str(symbol): int(width)
                for symbol, width in option_contracts_per_underlying.items()
            }
            missing = sorted(set(tracked_trading_symbols) - set(widths))
            if missing:
                raise ValueError(
                    f"no chain width is stated for {', '.join(missing)}; every tracked "
                    f"underlying needs one, because an underlying whose width defaulted "
                    f"would publish a chain nobody chose the size of (RL-061)"
                )
            # Zero is a real width here and not a refusal: a cash-equity underlying
            # is tracked for its own price and no segment on this spine trades its
            # options, so publishing a chain for it would spend the connection's
            # instrument budget on contracts nothing can act on. A segment that DOES
            # trade options cannot reach zero -- its width comes from its own
            # settings file, which refuses to be missing.
            negative = sorted(symbol for symbol, width in widths.items() if width < 0)
            if negative:
                raise ValueError(
                    f"the chain width for {', '.join(negative)} is negative, which is "
                    f"not a number of contracts"
                )
        else:
            if option_contracts_per_underlying < 1:
                raise ValueError(
                    f"option_contracts_per_underlying is {option_contracts_per_underlying}; a "
                    f"universe of underlyings with no chain cannot produce an option trade, "
                    f"and this segment trades options"
                )
            widths = {
                symbol: int(option_contracts_per_underlying)
                for symbol in tracked_trading_symbols
            }
        self._tracked = frozenset(tracked_trading_symbols)
        self._contracts_per_underlying = widths
        self._now_ms = now_ms
        # The tracked underlyings, by the key the master gives them.
        self._underlying_by_key: dict[str, object] = {}
        # Every option contract on a tracked underlying, by that underlying's key.
        self._contracts_by_underlying_key: dict[str, dict[str, object]] = {}
        # What each underlying last traded at, by its own key.
        self._price_by_underlying_key: dict[str, float] = {}
        self.standing = BridgeStanding()

    def observe_listing(self, listing) -> None:
        """One instrument listing: a tracked underlying, one of its contracts, or neither."""
        self.standing.listings_seen += 1

        if listing.trading_symbol in self._tracked and listing.underlying_key is None:
            if listing.instrument_key not in self._underlying_by_key:
                self.standing.underlyings_resolved += 1
            self._underlying_by_key[listing.instrument_key] = listing
            return

        if listing.instrument_type not in OPTION_INSTRUMENT_TYPES:
            return
        if listing.underlying_key is None or listing.expiry_ms is None:
            # A contract with no underlying or no expiry cannot be placed on a
            # chain. Carried as absent rather than guessed at (the master states
            # both for every real option; this is the shape, not a default).
            return
        contracts = self._contracts_by_underlying_key.setdefault(listing.underlying_key, {})
        if listing.instrument_key not in contracts:
            self.standing.option_contracts_known += 1
        contracts[listing.instrument_key] = listing

    def observe_price_frame(self, price_frame) -> None:
        """One broker-price-frame. Only the tracked underlyings' levels are kept."""
        self.standing.price_frames_seen += 1
        for level in price_frame.levels:
            if level.instrument_key in self._underlying_by_key:
                self._price_by_underlying_key[level.instrument_key] = level.price

    def nearest_expiry_for(self, underlying_key: str) -> int | None:
        """The soonest expiry on this underlying that has not already passed.

        An expiry in the past is not a chain to trade: the master lists only live
        contracts, but "live" is as of the file, and a file read before an expiry
        and used after it would put a settled chain into the universe.
        """
        contracts = self._contracts_by_underlying_key.get(underlying_key)
        if not contracts:
            return None
        now = self._now_ms()
        live = [c.expiry_ms for c in contracts.values() if c.expiry_ms > now]
        return min(live) if live else None

    def _entry_for_underlying(self, listing) -> CapturableSymbol:
        # instrument_kind is None rather than any known kind: trading_types has
        # no INDEX, and None already means "unknown kind, treat as no kind"
        # rather than being read as a particular one.
        return CapturableSymbol(
            venue_id=UPSTOX_VENUE_ID,
            symbol=listing.trading_symbol,
            contract_type=listing.instrument_type,
            quote_volume_24h=None,
            price_increment=listing.tick_size,
            instrument_kind=None,
            lot_size=listing.lot_size,
            venue_instrument_id=listing.instrument_key,
        )

    def _entry_for_contract(
        self, listing, underlying_symbol: str, underlying_key: str,
    ) -> CapturableSymbol:
        return CapturableSymbol(
            venue_id=UPSTOX_VENUE_ID,
            symbol=listing.trading_symbol,
            contract_type=listing.instrument_type,
            # Upstox's instrument master states no traded volume at all -- this
            # is the file that says what exists, not what has traded. None is
            # that absence; a zero would read as "listed and untraded", which is
            # a different and measurable fact this part has not measured.
            quote_volume_24h=None,
            price_increment=listing.tick_size,
            instrument_kind=OPTION,
            strike_price=listing.strike_price,
            expiry_ms=listing.expiry_ms,
            lot_size=listing.lot_size,
            venue_instrument_id=listing.instrument_key,
            underlying_symbol=underlying_symbol,
            underlying_venue_instrument_id=underlying_key,
        )

    def contracts_for(self, underlying_key: str) -> tuple:
        """This underlying's nearest-expiry contracts, nearest the money first.

        Empty when nothing has priced the underlying yet: the ranking *is* the
        distance from its price, so without one there is no ranking to do. The
        alternative -- falling back to whatever order the master lists strikes
        in -- would publish a different universe while every counter on the
        board read the same (Rule 8), and the strikes the master happens to list
        first are the ones furthest from the money.
        """
        price = self._price_by_underlying_key.get(underlying_key)
        if price is None:
            return ()
        expiry = self.nearest_expiry_for(underlying_key)
        if expiry is None:
            return ()
        on_the_chain = [
            listing
            for listing in self._contracts_by_underlying_key[underlying_key].values()
            if listing.expiry_ms == expiry and listing.strike_price is not None
        ]
        # Distance from the money first, then the strike and the contract's own
        # key, so a call and a put on the same strike order the same way on
        # every read rather than by dictionary order.
        on_the_chain.sort(
            key=lambda listing: (
                abs(listing.strike_price - price),
                listing.strike_price,
                listing.instrument_key,
            )
        )
        underlying = self._underlying_by_key.get(underlying_key)
        width = self._contracts_per_underlying.get(
            getattr(underlying, "trading_symbol", None)
        )
        if width is None:
            # An underlying that is not tracked has no width and no chain. It
            # cannot be reached from universe(), which iterates the tracked ones;
            # returning nothing here says so rather than inventing a width.
            return ()
        return tuple(on_the_chain[:width])

    def universe(self) -> tuple[CapturableSymbol, ...]:
        """Every entry this segment's universe currently holds.

        The tracked underlyings always, because they are what the bots form an
        opinion about and the only symbols whose prints are dense enough to fill
        a detector window (measured 2026-09-04: 3 of 3 index instruments reached
        the 256-observation floor, p99 gap between prints 0.6s). Their nearest
        expiry's contracts with them, because those are what the segment buys.
        """
        entries: list[CapturableSymbol] = []
        priced = without_price = no_expiry = contracts = 0

        for key, listing in sorted(self._underlying_by_key.items()):
            entries.append(self._entry_for_underlying(listing))
            if key not in self._price_by_underlying_key:
                without_price += 1
                continue
            priced += 1
            if self.nearest_expiry_for(key) is None:
                no_expiry += 1
                continue
            for contract in self.contracts_for(key):
                entries.append(
                    self._entry_for_contract(contract, listing.trading_symbol, key)
                )
                contracts += 1
            self.standing.nearest_expiry_ms[listing.trading_symbol] = self.nearest_expiry_for(key)

        self.standing.underlyings_published = len(self._underlying_by_key)
        self.standing.underlyings_priced = priced
        self.standing.underlyings_without_a_price = without_price
        self.standing.underlyings_with_no_live_expiry = no_expiry
        self.standing.contracts_published = contracts
        return tuple(entries)


def describe_bridge(bridge: BrokerSymbolUniverseBridge) -> dict:
    return {
        "part_id": PART_ID,
        "listings_seen": bridge.standing.listings_seen,
        "underlyings_resolved": bridge.standing.underlyings_resolved,
        "option_contracts_known": bridge.standing.option_contracts_known,
        "price_frames_seen": bridge.standing.price_frames_seen,
        "underlyings_published": bridge.standing.underlyings_published,
        "underlyings_priced": bridge.standing.underlyings_priced,
        "underlyings_without_a_price": bridge.standing.underlyings_without_a_price,
        "underlyings_with_no_live_expiry": bridge.standing.underlyings_with_no_live_expiry,
        "contracts_published": bridge.standing.contracts_published,
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch
    from runtime.level_publishing import LevelPublisher

    listings = Batch(read=context.bus.reader("broker-instrument-listing"))
    price_frames = Batch(read=context.bus.reader("broker-price-frame"))
    publish_universe = context.bus.publisher_for("symbol-universe")

    # The segment's own universe, not the machine's. `segment_id` already
    # decides which capital file is read, so a universe in machine scope meant
    # pointing this spine at stock-options would move the money and leave the
    # chains on NIFTY, BANKNIFTY and SENSEX -- healthy on every counter, and
    # trading the wrong segment.
    # Every built segment's, not one segment's: three bots run on this spine
    # (2026-09-05) and each underlying is subscribed once however many of them
    # want it, with the chain width of whichever segment trades its options.
    from runtime.segment_settings import (
        option_chain_width_by_underlying, underlyings_every_built_segment_trades,
    )

    bridge = BrokerSymbolUniverseBridge(
        tracked_trading_symbols=underlyings_every_built_segment_trades(context),
        option_contracts_per_underlying=option_chain_width_by_underlying(context),
    )

    # `symbol-universe` is a level: these are the symbols this system captures,
    # now. Restated on an interval as well as on change, because a consumer that
    # started after the last change has no way to ask for what it missed, and
    # nothing else can tell it that its empty universe is missing rather than
    # empty -- the reasoning symbol-catalogue-reader's own restatement carries.
    restated = LevelPublisher(
        publish=publish_universe,
        refresh_interval_seconds=context.number("symbol_universe_restatement_interval"),
    )

    def tick() -> None:
        for listing in listings.payloads():
            bridge.observe_listing(listing)
        for price_frame in price_frames.payloads():
            bridge.observe_price_frame(price_frame)
        universe = bridge.universe()
        if universe:
            # An empty universe is never published: never read and lists nothing
            # are different facts, and only one of them means a consumer should
            # stop looking for instruments (Rule 8).
            restated.publish_level(universe)

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
    "BrokerSymbolUniverseBridge",
    "CALL",
    "OPTION_INSTRUMENT_TYPES",
    "PART_DECLARATION",
    "PART_ID",
    "PUT",
    "UPSTOX_VENUE_ID",
    "describe_bridge",
    "start_part",
]
