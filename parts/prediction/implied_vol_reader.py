"""implied-vol-reader: the options market's view of future volatility, or nothing.

Implied volatility is the only forward-looking volatility measure the system can
observe: everything else is computed from what already happened. That makes it
valuable and makes it the easiest number in the system to fake, because a
plausible surface is trivially constructible and nothing downstream could tell.

So this part reads and does not model. It takes quoted option prices and the
implied volatilities the venue publishes, and:

- **A strike with no two-sided quote is not a data point.** A surface built from
  one-sided marks is a surface built from where nobody will trade, and the skew
  it produces is an artefact of who happened to be quoting.
- **A stale quote is dropped, not carried.** Options quotes go stale in minutes
  on crypto venues, and a surface holding yesterday's wing is more wrong than a
  surface with a hole in it.
- **A surface too thin to interpolate is published as thin**, never smoothed into
  completeness. Interpolating across a missing wing invents exactly the part of
  the surface that carries the information.

**It reads the broker's own chain, and still never computes an implied
volatility itself.** Upstox publishes greeks per subscribed contract, implied
volatility among them, so the number this part serves is the one the market
made rather than one solved locally from a price -- which is the same rule the
three bullets above are: read, do not model.

Rewired 2026-09-06 (docs/proposals/implied-vol-reader-reads-the-broker-option-
chain.md). Until then `start_part` drained `market-data`, threw it away and
returned no read requests, so this part had `reads 0 / quotes_seen 0 /
surfaces_published 0` for its entire life. That was deliberate and cited RL-050
-- the **crypto** build order, under which options were a segment this system
had not built. That ordering is retired: `docs/goal.md`'s Phase A is index
options and stock options, both of them, fully, before anything else moves, so
the one segment this part was told not to serve is now the first two in the
plan. Nothing new is fetched; all four inputs were already carrying.

`options_feed_connected` stays a reachable state rather than a line that can no
longer be reached: it becomes true only once greeks have actually arrived, and
a surface read before that still says the feed is not connected rather than
returning a flat one. A flat surface is an invented forward view, and it would
reach every sizing decision through the vol features.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "implied-vol-reader"

PART_DECLARATION = PartDeclaration(
    part_id="implied-vol-reader",
    consumes=(
        "broker-option-greeks",
        "broker-subscribed-instrument-listing",
        "market-quote",
        "symbol-price-frame",
    ),
    produces=("implied-vol-surface", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

READABLE = "readable"
NO_FEED = "no-options-feed-is-connected"
TOO_THIN = "too-few-two-sided-quotes-to-form-a-surface"
ALL_STALE = "every-quote-is-older-than-this-reader-trusts"

CALL = "call"
PUT = "put"

UPSTOX_VENUE_ID = "upstox"
MILLISECONDS_TO_NANOSECONDS = 1_000_000

# Upstox's own instrument_type codes for the two option kinds, in its own
# instrument master. Anything else in the subscription -- an equity, a future,
# an index -- is not an option and contributes no point to a surface.
OPTION_KIND_BY_INSTRUMENT_TYPE = {"CE": CALL, "PE": PUT}


@dataclass(frozen=True)
class OptionQuote:
    """One option's market, as quoted. Both sides or it is not a quote."""

    symbol: str
    underlying: str
    kind: str
    strike: float
    seconds_to_expiry: float
    bid: float | None
    ask: float | None
    implied_volatility: float | None
    quoted_at_ns: int

    @property
    def is_two_sided(self) -> bool:
        return self.bid is not None and self.ask is not None and self.ask > self.bid > 0

    @property
    def mid(self) -> float | None:
        return None if not self.is_two_sided else (self.bid + self.ask) / 2


@dataclass(frozen=True)
class ImpliedVolSurface:
    """What the options market implies, per expiry and strike, with its holes visible."""

    venue_id: str
    underlying: str
    state: str
    by_expiry: dict
    at_the_money: dict
    skew: dict
    quotes_used: int
    quotes_dropped: dict
    read_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == READABLE

    def volatility_at(self, seconds_to_expiry: float, strike: float) -> float | None:
        """The implied volatility at one point, or None if nothing was quoted there.

        None rather than an interpolation across a missing wing: the wing is
        where the information is, and inventing it produces a confident number
        about exactly the region nobody would trade.
        """
        expiry = self.by_expiry.get(seconds_to_expiry)
        return None if expiry is None else expiry.get(strike)


@dataclass
class ReaderStanding:
    reads: int = 0
    surfaces_published: int = 0
    no_feed: int = 0
    too_thin: int = 0
    quotes_seen: int = 0
    dropped_one_sided: int = 0
    dropped_stale: int = 0
    dropped_no_implied: int = 0


class OptionChainListings:
    """The subscribed instrument master, as the three lookups a chain needs.

    An object rather than three dicts in `start_part` because it is real state
    with real rules -- and because every other consumer of
    `broker-subscribed-instrument-listing` reads the payload inside a method
    exactly like this one, which is the shape the payload checker can follow
    (the filter publishes a conveyor slice, so the type is not inferable at the
    publish site).
    """

    def __init__(self) -> None:
        self._listing_by_key: dict[str, object] = {}
        self._symbol_by_key: dict[str, str] = {}

    def observe_listing(self, listing) -> None:
        instrument_key = getattr(listing, "instrument_key", None)
        if instrument_key is None:
            return
        self._listing_by_key[instrument_key] = listing
        symbol = getattr(listing, "trading_symbol", None)
        if symbol:
            self._symbol_by_key[instrument_key] = symbol

    def listing_of(self, instrument_key: str):
        return self._listing_by_key.get(instrument_key)

    def symbol_of(self, instrument_key: str) -> str | None:
        """The contract's own trading symbol, or None until its listing arrives."""
        return self._symbol_by_key.get(instrument_key)

    @property
    def instruments_known(self) -> int:
        return len(self._listing_by_key)


def option_quote_from(listing, symbol: str, quote, greek, now_ns: int) -> "OptionQuote | None":
    """One contract, as an OptionQuote -- or None where anything is missing.

    Never a partial quote with a guessed leg: every None here is a contract this
    part genuinely cannot say anything about, which is a different fact from a
    contract whose market is one-sided (that one becomes a quote and is dropped,
    and counted, by `read`).

    The underlying is taken from the contract's own `underlying_symbol` rather
    than by resolving `underlying_key` against the underlying's listing.
    Measured 2026-09-06: only **26 of 1,707** subscribed options had their
    underlying subscribed as well, so a reader that waited for that listing
    would wait for ever on 98% of the chain, while every one of the 94,352
    options in Upstox's master states this field outright.
    """
    kind = OPTION_KIND_BY_INSTRUMENT_TYPE.get(getattr(listing, "instrument_type", None))
    if kind is None:
        return None  # not an option; a subscription carries plenty that are not
    strike = getattr(listing, "strike_price", None)
    expiry_ms = getattr(listing, "expiry_ms", None)
    underlying = getattr(listing, "underlying_symbol", None)
    if strike is None or expiry_ms is None or not underlying:
        return None
    return OptionQuote(
        symbol=symbol,
        underlying=underlying,
        kind=kind,
        strike=float(strike),
        # Seconds from now, so a contract that has already expired reads
        # negative rather than being silently treated as the nearest expiry.
        seconds_to_expiry=(expiry_ms * MILLISECONDS_TO_NANOSECONDS - now_ns) / 1e9,
        bid=quote.bid_price,
        ask=quote.ask_price,
        implied_volatility=greek.implied_volatility,
        # The broker's own stamp for the greeks, never arrival time: the
        # staleness bound is about the market's age, not our latency.
        quoted_at_ns=greek.broker_time_ns,
    )


class ImpliedVolReader:
    """Reads a surface from quoted options, and publishes its holes rather than filling them."""

    def __init__(
        self,
        maximum_quote_age_seconds: float,
        minimum_strikes_per_expiry: int,
        now_ns=time.time_ns,
    ) -> None:
        if maximum_quote_age_seconds <= 0:
            raise ValueError(
                "a reader with no staleness bound carries yesterday's wing, which is more "
                "wrong than a hole"
            )
        if minimum_strikes_per_expiry < 3:
            raise ValueError(
                "a skew needs at least three strikes: one either side of the money and the "
                "money itself"
            )
        self._maximum_age_ns = int(maximum_quote_age_seconds * 1e9)
        self._minimum_strikes = minimum_strikes_per_expiry
        self._now_ns = now_ns
        # underlying -> contract symbol -> its newest quote. See observe_quote.
        self._quotes: dict[str, dict[str, OptionQuote]] = {}
        self._feed_connected = False
        self.standing = ReaderStanding()

    def set_feed_connected(self, connected: bool) -> None:
        """Whether an options feed exists at all.

        Set from whether greeks have actually arrived, never from configuration:
        a reader told it has a feed it does not have publishes `too-thin` where
        the truth is `no-feed`, and those are different facts about the system.
        """
        self._feed_connected = connected

    def observe_quote(self, venue_id: str, quote: OptionQuote) -> None:
        """Hold this contract's newest quote for its underlying.

        Newest per contract, not appended. A quote is a level: a contract's
        newer quote supersedes its older one, and the by-strike surface is
        last-write-wins anyway, so keeping both cannot change the answer while
        it can exhaust the machine. Appending was harmless for as long as this
        part received nothing at all (measured 2026-09-06: quotes_seen 0 for
        its whole life); on a live chain at hundreds of quotes a second it
        grows without bound for the life of the process, and the staleness
        filter does not help because it runs at read time and leaves what it
        dropped in the list.
        """
        self.standing.quotes_seen += 1
        self._quotes.setdefault(f"{venue_id}:{quote.underlying}", {})[quote.symbol] = quote

    def read(self, venue_id: str, underlying: str, spot: float) -> ImpliedVolSurface:
        self.standing.reads += 1
        key = f"{venue_id}:{underlying}"

        if not self._feed_connected:
            # Not a flat surface. A flat surface is an invented forward view, and
            # it would reach every sizing decision through the vol features.
            self.standing.no_feed += 1
            return self._surface(
                venue_id, underlying, NO_FEED, {}, {}, {}, 0,
                {"no-feed": self.standing.quotes_seen},
            )

        now = self._now_ns()
        dropped = {"one-sided": 0, "stale": 0, "no-implied-volatility": 0}
        usable = []

        for quote in self._quotes.get(key, {}).values():
            if now - quote.quoted_at_ns > self._maximum_age_ns:
                dropped["stale"] += 1
                self.standing.dropped_stale += 1
                continue
            if not quote.is_two_sided:
                dropped["one-sided"] += 1
                self.standing.dropped_one_sided += 1
                continue
            if quote.implied_volatility is None or quote.implied_volatility <= 0:
                dropped["no-implied-volatility"] += 1
                self.standing.dropped_no_implied += 1
                continue
            usable.append(quote)

        if not usable:
            self.standing.too_thin += 1
            return self._surface(venue_id, underlying, ALL_STALE, {}, {}, {}, 0, dropped)

        by_expiry: dict[float, dict] = {}
        for quote in usable:
            by_expiry.setdefault(quote.seconds_to_expiry, {})[quote.strike] = (
                quote.implied_volatility
            )

        complete = {
            expiry: strikes
            for expiry, strikes in by_expiry.items()
            if len(strikes) >= self._minimum_strikes
        }
        if not complete:
            self.standing.too_thin += 1
            return self._surface(
                venue_id, underlying, TOO_THIN, by_expiry, {}, {}, len(usable), dropped
            )

        at_the_money = {
            expiry: strikes[min(strikes, key=lambda strike: abs(strike - spot))]
            for expiry, strikes in complete.items()
        }
        skew = {
            expiry: self._skew_of(strikes, spot) for expiry, strikes in complete.items()
        }

        self.standing.surfaces_published += 1
        return self._surface(
            venue_id, underlying, READABLE, complete, at_the_money, skew, len(usable), dropped
        )

    def _skew_of(self, strikes: dict, spot: float) -> float | None:
        """Downside implied volatility less upside, at equal distance from spot.

        The number that matters for a directional book: a market pricing puts far
        above calls is pricing a crash, and that is a different fact from a high
        overall level.
        """
        below = [strike for strike in strikes if strike < spot]
        above = [strike for strike in strikes if strike > spot]
        if not below or not above:
            return None
        lowest = min(below)
        highest = max(above)
        return strikes[lowest] - strikes[highest]

    def release(self, venue_id: str, underlying: str) -> None:
        """Drop held quotes. T-3."""
        self._quotes.pop(f"{venue_id}:{underlying}", None)

    def _surface(
        self, venue_id, underlying, state, by_expiry, at_the_money, skew, used, dropped
    ) -> ImpliedVolSurface:
        return ImpliedVolSurface(
            venue_id=venue_id,
            underlying=underlying,
            state=state,
            by_expiry=by_expiry,
            at_the_money=at_the_money,
            skew=skew,
            quotes_used=used,
            quotes_dropped=dict(dropped),
            read_at_ns=self._now_ns(),
        )


def describe_implied_vol(reader: ImpliedVolReader) -> dict:
    return {
        "part_id": PART_ID,
        "options_feed_connected": reader._feed_connected,
        "reads": reader.standing.reads,
        "surfaces_published": reader.standing.surfaces_published,
        "reads_with_no_feed": reader.standing.no_feed,
        "reads_too_thin_for_a_surface": reader.standing.too_thin,
        "quotes_seen": reader.standing.quotes_seen,
        "dropped_one_sided": reader.standing.dropped_one_sided,
        "dropped_stale": reader.standing.dropped_stale,
        "dropped_without_implied_volatility": reader.standing.dropped_no_implied,
    }


def run_implied_vol_reader(
    reader: ImpliedVolReader, control_socket, read_quotes, publish_surfaces,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        requests = read_quotes(reader)
        publish_surfaces(
            tuple(reader.read(venue_id, underlying, spot) for venue_id, underlying, spot in requests)
        )

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_implied_vol(reader),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Four inputs, joined on the broker's own `instrument_key`:

        broker-option-greeks                  the implied volatility, per contract
        broker-subscribed-instrument-listing  strike, expiry, CE/PE, underlying, symbol
        market-quote                          the two-sided market, per contract symbol
        symbol-price-frame                    the underlying's spot

    A contract is quoted only when all of the first three agree about it. The
    listing is what ties the other two together -- greeks arrive keyed by
    instrument key and quotes by trading symbol, and only the listing knows they
    are the same contract.
    """
    import time as _time

    from runtime.input_assembly import Batch, LatestByKey
    from runtime.price_frames import levels_in

    greeks = LatestByKey(
        read=context.bus.reader("broker-option-greeks"),
        key_of=lambda item: item.instrument_key,
        maximum_age_seconds=context.number("implied_vol_maximum_quote_age"),
    )
    listings = Batch(read=context.bus.reader("broker-subscribed-instrument-listing"))
    quotes = Batch(read=context.bus.reader("market-quote"))
    spots = Batch(read=context.bus.reader("symbol-price-frame"))
    publish_surfaces = context.bus.publisher_for("implied-vol-surface")

    reader = ImpliedVolReader(
        maximum_quote_age_seconds=context.number("implied_vol_maximum_quote_age"),
        minimum_strikes_per_expiry=int(context.number("implied_vol_minimum_strikes_per_expiry")),
    )

    listings_held = OptionChainListings()
    newest_quote_by_symbol: dict[str, object] = {}
    spot_by_symbol: dict[str, float] = {}

    def read_quotes(_reader):
        for listing in listings.payloads():
            listings_held.observe_listing(listing)
        now_ns = _time.time_ns()

        # `market-quote` carries NormalisedQuote directly, one per contract --
        # not a frame of levels, which is what `symbol-quote-frame` is. Anything
        # not shaped like a quote is skipped rather than raised on: an inbox
        # carries what the wiring delivers, and a part that died on an
        # unexpected shape is a part the wiring could kill.
        for quote in quotes.payloads():
            symbol = getattr(quote, "symbol", None)
            if symbol is not None and getattr(quote, "bid_price", None) is not None:
                newest_quote_by_symbol[symbol] = quote
        for level in levels_in(spots.payloads()):
            spot_by_symbol[level.symbol] = level.price

        known_greeks = greeks.mapping(now_ns)
        # Greeks arriving at all is what "the options feed is connected" means.
        # Read from the data rather than configured, so `no-feed` stays a
        # reachable state instead of a line nobody can get to.
        _reader.set_feed_connected(bool(known_greeks))

        underlyings: set[tuple[str, str]] = set()
        for instrument_key, greek in known_greeks.items():
            listing = listings_held.listing_of(instrument_key)
            if listing is None:
                continue
            symbol = listings_held.symbol_of(instrument_key)
            if symbol is None:
                continue
            quote = newest_quote_by_symbol.get(symbol)
            if quote is None:
                continue
            option = option_quote_from(listing, symbol, quote, greek, now_ns)
            if option is None:
                continue
            _reader.observe_quote(UPSTOX_VENUE_ID, option)
            underlyings.add((UPSTOX_VENUE_ID, option.underlying))

        # One read per underlying that has a spot. Without a spot there is no
        # at-the-money and no skew, and reading against a spot of zero would put
        # every strike on the same side of the money.
        return tuple(
            (venue_id, underlying, spot_by_symbol[underlying])
            for venue_id, underlying in sorted(underlyings)
            if spot_by_symbol.get(underlying)
        )

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_surfaces(kept)

    return run_implied_vol_reader(
        reader=reader,
        control_socket=context.control_socket,
        read_quotes=read_quotes,
        publish_surfaces=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
