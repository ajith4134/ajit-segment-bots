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
from runtime.trading_types import OPTION, SPOT

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
    consumes=(
        "broker-instrument-listing", "broker-price-frame", "cash-equity-shortlist",
        "position",
    ),
    produces=("symbol-universe", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="delays",
)


# What an ordinary NSE share looks like in the broker's own master, and nothing
# else does. Measured on the real file 2026-09-05: of 9,724 NSE_EQ rows only
# 2,655 are instrument_type EQ -- the rest are sovereign gold bonds (4,311),
# government securities, treasury bills, NCDs, SME listings and 242 BE-series
# shares, which are trade-for-trade and cannot be traded intraday at all. An
# intraday bot that treated any of those as an ordinary share would place orders
# the exchange rejects.
NSE_EQUITY_SEGMENT = "NSE_EQ"
ORDINARY_SHARE = "EQ"
ORDINARY_SECURITY = "NORMAL"


class EquityWithoutADerivative:
    """Which shares the cash-equity segment may trade, by exclusion.

    The operator's instruction, 2026-09-05: every NSE share the derivatives
    segments do not already cover, so the bots never hold the same underlying at
    once. Two segments holding one name is exposure nothing bounds -- each stays
    inside its own risk limits while the machine as a whole is twice as long as
    either believes.

    Stated as a rule rather than a list of symbols. A list of 2,444 names typed
    into a settings file is fiction the day NSE adds an F&O name, and it cannot
    be audited; this is one sentence, evaluated against the broker's own master
    every time it is restated.

    `admits` answers only the first half -- is this an ordinary share -- because
    the second half needs the whole master: an equity is excluded when some
    contract is written on it, and that contract may not have been spoken yet.
    """

    def admits(self, listing) -> bool:
        return (
            getattr(listing, "segment", None) == NSE_EQUITY_SEGMENT
            and getattr(listing, "instrument_type", None) == ORDINARY_SHARE
            and getattr(listing, "security_type", None) == ORDINARY_SECURITY
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
    # The derived cash-equity universe (2026-09-05). Every one of these is a
    # count of something measured off the broker's own master, and the first two
    # together are what says whether the exclusion has anything to exclude yet.
    catalogue_cycles_heard: int = 0
    derivative_underlyings_known: int = 0
    equities_listed: int = 0
    equities_covered_by_a_derivative: int = 0
    equities_published: int = 0
    # True while the master has not been heard through once. The exclusion set is
    # incomplete until then, so nothing is published rather than publishing an
    # F&O name into the cash segment -- which is the double-exposure the rule
    # exists to prevent, and would last up to a full 30-minute catalogue cycle.
    equity_universe_is_waiting_for_a_full_catalogue_cycle: bool = True
    # True while cash-equity-shortlist-ranker has never yet said anything.
    # Publishing the whole excluded set while waiting would be exactly the
    # 2,444-name, unranked, uncapped universe this shortlist exists to replace
    # (2026-09-05) -- so nothing is published for the equity branch until a
    # shortlist has been heard, the same reasoning the catalogue-cycle wait
    # already uses one line above.
    equity_universe_is_waiting_for_a_shortlist: bool = True
    equities_outside_the_shortlist: int = 0
    # A position survives the money-distance ranking and the nearest-expiry
    # filter that bound everything else this bridge publishes (2026-09-08).
    # Real incident: ten open stock-options positions went unpriced for their
    # entire remaining life because contracts_for() only ever publishes the
    # CURRENT nearest-expiry chain, ranked by distance from the underlying's
    # price -- a position opened weeks earlier, on a strike the price has since
    # moved away from or an expiry that has since rolled, falls out of that
    # window and stays out permanently. Nothing evicts a held position from the
    # book, so nothing should be allowed to evict its listing from the universe.
    held_positions_forced_in: int = 0
    # A held symbol whose listing has not arrived yet, or whose venue is not
    # this bridge's (paper positions on a venue this broker never listed). Not
    # an error -- the same "absence is its own state" the rest of this file
    # already applies to underlyings_without_a_price.
    held_positions_without_a_listing: int = 0
    # Whether start_part's own direct read of the master (see
    # warm_start_from_the_masters_own_file) succeeded, and how many listings it
    # loaded -- 0 while it has not run yet or failed, which reads identically to
    # "never tried" (Rule 8 -- see warm_start_failure for which one it was).
    warm_start_listings_loaded: int = 0
    warm_start_failure: str | None = None


class BrokerSymbolUniverseBridge:
    """Holds the master and answers what this segment's universe currently is."""

    def __init__(
        self,
        tracked_trading_symbols: tuple[str, ...],
        option_contracts_per_underlying: int | dict[str, int],
        now_ms=lambda: int(time.time() * 1000),
        equity_selection=None,
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
        # How the cash-equity universe is decided: by exclusion, from the
        # broker's own master, rather than by a list somebody typed. The
        # operator's instruction (2026-09-05) is every NSE share the derivatives
        # segments do not already cover, so the two never hold the same
        # underlying at once -- separate segments have separate risk limits, and
        # one name held in both is exposure nothing bounds.
        #
        # None means this bridge publishes only what it was handed, which is what
        # a spine trading only derivatives states.
        self._equity_selection = equity_selection
        # The most recent cash-equity-shortlist, or None while none has arrived
        # yet. None while the equity_selection wants shares at all is what
        # holds the equity branch back (see universe()) -- publishing every
        # excluded share while waiting is exactly the unranked, uncapped
        # universe the shortlist replaces (2026-09-05).
        self._shortlist_symbols: frozenset[str] | None = None
        # Every NSE_EQ share the master has listed, by instrument_key.
        self._equity_by_key: dict[str, object] = {}
        # Every instrument_key the master has named as a derivative's underlying.
        # An equity in here is covered by the F&O segments and is not this
        # segment's. Measured on the real master 2026-09-05: 210 stock
        # underlyings and 6 index ones, and each of the 210 is exactly an NSE_EQ
        # instrument_key, so the exclusion is a set difference and never a
        # symbol-string match.
        self._derivative_underlying_keys: set[str] = set()
        # The master is spoken over a 30-minute cycle, not handed over at once,
        # so the exclusion set is incomplete until a full cycle has been heard --
        # and publishing early would put an F&O name in the equity universe for
        # up to half an hour, which is the double-exposure this rule exists to
        # prevent. A cycle is complete when the first listing ever seen comes
        # round again: the conveyor restates the table in order, forever.
        self._first_listing_key: str | None = None
        self._catalogue_cycles_heard = 0
        self._now_ms = now_ms
        # The tracked underlyings, by the key the master gives them.
        self._underlying_by_key: dict[str, object] = {}
        # Every option contract on a tracked underlying, by that underlying's key.
        self._contracts_by_underlying_key: dict[str, dict[str, object]] = {}
        # What each underlying last traded at, by its own key.
        self._price_by_underlying_key: dict[str, float] = {}
        # Every listing seen, by its trading_symbol -- the same name a Position
        # carries, so a held position can be looked up without knowing its
        # instrument_key up front. Last listing wins, matching the whole file's
        # instrument_key-keyed dicts; two exchanges listing the same trading
        # symbol is the same rare case those already accept.
        self._listing_by_trading_symbol: dict[str, object] = {}
        # The latest Position per (venue_id, symbol), replaced whole on every
        # message -- a level, not an event, matching how fill-reconciler and
        # position-close-detector publish it. A closed position (is_flat) stays
        # in this dict rather than being removed: keeping the entry read-only
        # is simpler than deleting on the specific message shape that means
        # flat, and universe() already skips flat positions by reading
        # is_flat itself.
        self._position_by_key: dict[tuple[str, str], object] = {}
        self.standing = BridgeStanding()

    def observe_listing(self, listing) -> None:
        """One instrument listing: a tracked underlying, one of its contracts, or neither."""
        self.standing.listings_seen += 1
        # Indexed by trading_symbol unconditionally, before any of the filters
        # below -- a held position's listing may be an equity, an option whose
        # underlying is not tracked, or a contract whose expiry has already
        # rolled past nearest, and universe() must still be able to find it.
        self._listing_by_trading_symbol[listing.trading_symbol] = listing

        if self._first_listing_key is None:
            self._first_listing_key = listing.instrument_key
        elif listing.instrument_key == self._first_listing_key:
            self._catalogue_cycles_heard += 1
            self.standing.catalogue_cycles_heard = self._catalogue_cycles_heard

        # Learned from every derivative, option or future alike: what matters is
        # that some contract is written on this underlying, not which kind.
        if listing.underlying_key is not None:
            self._derivative_underlying_keys.add(listing.underlying_key)
            self.standing.derivative_underlyings_known = len(
                self._derivative_underlying_keys
            )
        elif self._equity_selection is not None and self._equity_selection.admits(listing):
            self._equity_by_key[listing.instrument_key] = listing
            self.standing.equities_listed = len(self._equity_by_key)

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

    def observe_shortlist(self, shortlist) -> None:
        """The latest cash-equity-shortlist. Replaces the previous one whole --
        this is a level, and yesterday's top 50 has no standing once today's
        has arrived."""
        self._shortlist_symbols = frozenset(shortlist.symbols)

    def observe_position(self, position) -> None:
        """One position, replacing whatever this bridge last knew about it.

        Read for one thing only: which symbols currently have real capital in
        them and must stay priced no matter where the ranking would otherwise
        put them.
        """
        self._position_by_key[(position.venue_id, position.symbol)] = position

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

    def _entry_for_underlying(self, listing, instrument_kind=None) -> CapturableSymbol:
        """One symbol that is not a contract: an option's underlying, or a share.

        `instrument_kind` is None for an option underlying, because trading_types
        has no INDEX and None already means "unknown kind, treat as no kind"
        rather than being read as a particular one. NIFTY is a number, not
        something anyone can buy, and `instrument-selector` correctly registers a
        kindless listing only as the thing options hang off.

        A **share** is SPOT, and passing None for one is what stopped
        cash-equity-intraday trading at all. Measured on the live spine
        2026-09-08: this bridge published 47 shares and
        `instrument-selector.chosen_by_kind` held nothing but `option` -- 17,089
        of them -- because every share arrived kindless, matched the "an
        underlying: what every contract on it resolves through" branch, and went
        into the ATM tracker instead of into the listed-instrument book. The
        selector's own SPOT branch, written 2026-09-07 precisely so an equity
        intent could be expressed, was unreachable. `paper-account-cash-equity-
        intraday` read `fills_applied 0` while the other two segments held real
        positions.

        The two groups are disjoint by construction, so no symbol needs both
        kinds: `entries()` skips any share that is a derivative's underlying
        (`equities_covered_by_a_derivative`), which is the double-exposure rule
        that already governs this universe.
        """
        return CapturableSymbol(
            venue_id=UPSTOX_VENUE_ID,
            symbol=listing.trading_symbol,
            contract_type=listing.instrument_type,
            quote_volume_24h=None,
            price_increment=listing.tick_size,
            instrument_kind=instrument_kind,
            lot_size=listing.lot_size,
            freeze_quantity=listing.freeze_quantity,
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
            # The exchange's single-order limit for this contract, carried so
            # trade-capital-bounds-gate can refuse a size no venue would take
            # (2026-09-12). NIFTY's is 1,755; the orders of 2026-09-08 were up
            # to 6,823,286.
            freeze_quantity=listing.freeze_quantity,
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

        # The derived cash-equity universe: every ordinary share no derivative is
        # written on. Held back until the master has been heard through once,
        # because a share whose options have not been spoken yet reads as having
        # none -- and publishing an F&O name here is the double-exposure this
        # whole rule exists to prevent.
        waiting = self._catalogue_cycles_heard < 1
        self.standing.equity_universe_is_waiting_for_a_full_catalogue_cycle = waiting
        # Held back the same way the catalogue-cycle wait is: publishing every
        # excluded share while no shortlist has ever arrived would be the
        # 2,444-name, unranked, uncapped universe the shortlist exists to
        # replace (2026-09-05), not a smaller version of the correct answer.
        waiting_for_a_shortlist = self._shortlist_symbols is None
        self.standing.equity_universe_is_waiting_for_a_shortlist = waiting_for_a_shortlist
        covered = equities = outside_shortlist = 0
        if self._equity_selection is not None and not waiting and not waiting_for_a_shortlist:
            for key, listing in sorted(self._equity_by_key.items()):
                if key in self._derivative_underlying_keys:
                    covered += 1
                    continue
                if listing.trading_symbol not in self._shortlist_symbols:
                    outside_shortlist += 1
                    continue
                # SPOT, not None: this is a share and the cash-equity segment
                # buys it directly. See _entry_for_underlying.
                entries.append(self._entry_for_underlying(listing, instrument_kind=SPOT))
                equities += 1
        self.standing.equities_covered_by_a_derivative = covered
        self.standing.equities_outside_the_shortlist = outside_shortlist
        self.standing.equities_published = equities

        self.standing.underlyings_published = len(self._underlying_by_key)
        self.standing.underlyings_priced = priced
        self.standing.underlyings_without_a_price = without_price
        self.standing.underlyings_with_no_live_expiry = no_expiry
        self.standing.contracts_published = contracts

        already_covered = {entry.symbol for entry in entries}
        held_entries, forced_in, without_a_listing = self._held_entries(already_covered)
        entries.extend(held_entries)
        self.standing.held_positions_forced_in = forced_in
        self.standing.held_positions_without_a_listing = without_a_listing

        return tuple(entries)

    def _held_entries(
        self, already_covered: set[str],
    ) -> tuple[list[CapturableSymbol], int, int]:
        """Every currently-held symbol the ranking above did not already publish.

        Unlike `contracts_for`, this reads a position's own listing directly --
        no distance-from-the-money rank, no nearest-expiry filter, no chain
        width cap. Those three bound what a segment might *newly* buy, which is
        legitimately small; a position already holding real capital is not a
        candidate to be ranked, it is a fact this bridge is not allowed to stop
        stating just because the market moved since it opened.
        """
        out: list[CapturableSymbol] = []
        forced_in = without_a_listing = 0
        for (venue_id, symbol), position in sorted(self._position_by_key.items()):
            if venue_id != UPSTOX_VENUE_ID or position.is_flat or symbol in already_covered:
                continue
            listing = self._listing_by_trading_symbol.get(symbol)
            if listing is None:
                without_a_listing += 1
                continue
            if listing.instrument_type in OPTION_INSTRUMENT_TYPES and listing.underlying_key is not None:
                underlying_listing = self._underlying_by_key.get(listing.underlying_key)
                underlying_symbol = (
                    underlying_listing.trading_symbol if underlying_listing is not None else ""
                )
                out.append(self._entry_for_contract(listing, underlying_symbol, listing.underlying_key))
            else:
                # A held non-contract is a share this segment bought, unless it
                # is one of the tracked option underlyings -- which are not
                # buyable and are never held. Same kinds as `entries()`, so a
                # position forced back in is registered the way it was opened.
                out.append(self._entry_for_underlying(
                    listing,
                    instrument_kind=(
                        None if listing.instrument_key in self._underlying_by_key else SPOT
                    ),
                ))
            already_covered.add(symbol)
            forced_in += 1
        return out, forced_in, without_a_listing


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
        "catalogue_cycles_heard": bridge.standing.catalogue_cycles_heard,
        "equities_listed": bridge.standing.equities_listed,
        "equities_covered_by_a_derivative": bridge.standing.equities_covered_by_a_derivative,
        "equities_outside_the_shortlist": bridge.standing.equities_outside_the_shortlist,
        "equities_published": bridge.standing.equities_published,
        "equity_universe_is_waiting_for_a_full_catalogue_cycle": (
            bridge.standing.equity_universe_is_waiting_for_a_full_catalogue_cycle
        ),
        "equity_universe_is_waiting_for_a_shortlist": (
            bridge.standing.equity_universe_is_waiting_for_a_shortlist
        ),
        "held_positions_forced_in": bridge.standing.held_positions_forced_in,
        "held_positions_without_a_listing": bridge.standing.held_positions_without_a_listing,
        "warm_start_listings_loaded": bridge.standing.warm_start_listings_loaded,
        "warm_start_failure": bridge.standing.warm_start_failure,
    }


def warm_start_from_the_masters_own_file(bridge: BrokerSymbolUniverseBridge) -> None:
    """Feed the whole instrument master into the bridge once, synchronously,
    before the first tick -- the same static gzip file
    broker-instrument-catalogue-reader fetches, read a second time here rather
    than waited for.

    That reader already holds every listing in memory the instant it starts;
    what is slow is its *paced* republish onto the bus, deliberately paced
    (`RestatementConveyor`) to protect the bounded bus buffer from exactly the
    103,078-listing flood that dropped 13.6% of `broker-instrument-listing` on
    2026-09-05. That protection is correct for the steady state and wrong for
    the one thing that cannot wait for it: a position already holding real
    capital, whose price a full conveyor cycle can take twenty-plus minutes to
    reach after every restart. Measured live 2026-09-08: 10 open stock-options
    positions, 8 minutes in, only 2 had found their listing through the paced
    bus alone.

    A second parse of the same ~15 MB gzip is a one-time, part-start cost, not
    a per-tick one, and it changes nothing about what gets published --
    observe_listing() is the same call the paced bus drives, so a listing that
    arrives twice (once here, once again later off the bus) is simply written
    twice, not double-counted incorrectly (BridgeStanding.listings_seen sees
    both, honestly, since both really were listings observed).

    Best-effort: a fetch failure here leaves the bridge exactly as it would
    have been without this function -- still fed by the paced bus, just
    without the head start. Recorded rather than raised, because a symbol
    universe with no warm start is a real, running bridge and a crash here
    would make it not one.
    """
    from runtime.brokers.instrument_master import fetch_and_parse_listings
    from runtime.brokers.upstox import UpstoxAdapter

    try:
        listings = fetch_and_parse_listings(UpstoxAdapter())
    except Exception as failure:
        bridge.standing.warm_start_failure = f"{type(failure).__name__}: {failure}"
        return
    for listing in listings:
        bridge.observe_listing(listing)
    bridge.standing.warm_start_listings_loaded = len(listings)


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch
    from runtime.level_publishing import LevelPublisher

    listings = Batch(read=context.bus.reader("broker-instrument-listing"))
    price_frames = Batch(read=context.bus.reader("broker-price-frame"))
    shortlists = Batch(read=context.bus.reader("cash-equity-shortlist"))
    positions = Batch(read=context.bus.reader("position"))
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
        any_segment_takes_shares_without_a_derivative,
        option_chain_width_by_underlying,
        underlyings_every_built_segment_trades,
    )

    bridge = BrokerSymbolUniverseBridge(
        tracked_trading_symbols=underlyings_every_built_segment_trades(context),
        option_contracts_per_underlying=option_chain_width_by_underlying(context),
        # The cash-equity segment takes every ordinary NSE share the derivatives
        # segments do not cover, derived from the master rather than listed
        # (2026-09-05). None when no segment on this spine asks for it, and then
        # this bridge publishes only what it was handed.
        equity_selection=(
            EquityWithoutADerivative()
            if any_segment_takes_shares_without_a_derivative(context)
            else None
        ),
    )
    # Held positions cannot wait for the paced conveyor -- see
    # warm_start_from_the_masters_own_file's own docstring for why this is a
    # second read of the same file rather than a change to that pacing.
    warm_start_from_the_masters_own_file(bridge)

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
        for shortlist in shortlists.payloads():
            bridge.observe_shortlist(shortlist)
        for position in positions.payloads():
            bridge.observe_position(position)
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
    "EquityWithoutADerivative",
    "NSE_EQUITY_SEGMENT",
    "ORDINARY_SECURITY",
    "ORDINARY_SHARE",
    "OPTION_INSTRUMENT_TYPES",
    "PART_DECLARATION",
    "PART_ID",
    "PUT",
    "UPSTOX_VENUE_ID",
    "warm_start_from_the_masters_own_file",
    "describe_bridge",
    "start_part",
]
