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

**Phase 1 has no options feed**, and this part says so rather than producing
anything: options are a segment this system has not built (RL-050), and a reader
that returned a flat surface would put an invented forward view into the vol
feature builder and from there into every sizing decision.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "implied-vol-reader"

PART_DECLARATION = PartDeclaration(
    part_id="implied-vol-reader",
    consumes=("market-data",),
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
        self._quotes: dict[str, list] = {}
        self._feed_connected = False
        self.standing = ReaderStanding()

    def set_feed_connected(self, connected: bool) -> None:
        """Whether an options feed exists at all. Phase 1 has none (RL-050)."""
        self._feed_connected = connected

    def observe_quote(self, venue_id: str, quote: OptionQuote) -> None:
        self.standing.quotes_seen += 1
        self._quotes.setdefault(f"{venue_id}:{quote.underlying}", []).append(quote)

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

        for quote in self._quotes.get(key, []):
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

    No options feed is connected in phase 1 and market-data carries no
    options quote; the reader is told so and publishes surfaces that say
    the feed is not connected, never an empty surface that reads as a flat
    market.
    """
    # `market-data` carries trades AND candles: venue-trade-stream-reader
    # publishes the first, ccxt-venue-reader the second, and both have always
    # declared it. This part wants trades and now says so, rather than assuming
    # the wire holds only what it happens to want -- a part that dies on an
    # unexpected shape is a part the wiring can kill.
    from runtime.market_data_stream import trades_in
    from runtime.input_assembly import Batch

    trades = Batch(read=context.bus.reader("market-data"))
    publish_surfaces = context.bus.publisher_for("implied-vol-surface")
    reader = ImpliedVolReader(
        maximum_quote_age_seconds=context.number("implied_vol_maximum_quote_age"),
        minimum_strikes_per_expiry=int(context.number("implied_vol_minimum_strikes_per_expiry")),
    )
    reader.set_feed_connected(False)

    def read_quotes(_reader):
        trades_in(trades.payloads())
        return ()

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
