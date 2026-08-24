"""Which price a part should act on: the trade if it is fresh, else the quote.

Every part here that judges a price was hardcoded to one source -- the last trade
-- and that is the single source which goes stale exactly when it is needed most,
because a symbol nobody is trading is a symbol with no recent trade and a
perfectly good resting market. Measured on the live run of 2026-08-24,
`spread-reversion-detector` refused 1,438,376 of 4,199,062 tests on the age of a
leg's last trade: 34% of its work, thrown away over symbols that were being
quoted the whole time.

The shape of the answer is taken from nautilus_trader, read from source on
2026-08-24 rather than from a summary of it: `crates/model/src/enums.rs`
defines `PriceType` as `Bid, Ask, Mid, Last, Mark`, and mature systems name which
one they mean instead of assuming. `Mark` is documented there as "a reference
price reflecting an instrument's fair value, often used for portfolio
calculations and risk management" -- the venues publish one, both of ours carry it
on streams this system already reads, and it is the natural next source after
this one.

**The trade wins whenever it is fresh enough.** A trade is what somebody actually
paid; a mid is what two people are asking for, and they are not the same fact. The
quote is a fallback, never a replacement.

**A wide quote is not a stand-in for a trade.** The mid of a market quoted a
percent wide is not within a percent of anything tradeable, and substituting it
into a spread would inject noise of that size into a statistic whose whole job is
to notice a two-sigma move. So a quote is only believed when its own spread is
immaterial by the yardstick this system already uses for materiality -- the round
trip cost, the same number that decides when a price is too old to act on. No new
threshold is invented here (RL-061); the one that exists is applied twice.

One rule in one place, because three parts need it and three copies of a rule is
three places for one bug to be fixed twice.
"""

from __future__ import annotations

from dataclasses import dataclass

TRADE = "trade"
QUOTE = "quote"


@dataclass(frozen=True)
class ChosenPrice:
    """A price to act on, when it was true, and which kind of fact it is."""

    price: float
    observed_at_ns: int
    source: str

    @property
    def came_from_a_quote(self) -> bool:
        return self.source == QUOTE

    def age_seconds(self, now_ns: int) -> float:
        return (now_ns - self.observed_at_ns) / 1e9


@dataclass(frozen=True)
class NoPrice:
    """Why no price could be given, in terms a refusal can be written from."""

    reason: str
    detail: str


NEVER_SEEN = "this-symbol-has-no-price-of-any-kind"
BOTH_TOO_OLD = "neither-the-trade-nor-the-quote-is-recent-enough"
QUOTE_TOO_WIDE = "the-quote-is-too-wide-to-stand-in-for-a-trade"


class ReferencePriceChooser:
    """Holds each symbol's last trade and last quote, and answers which to act on.

    It holds no clock of its own and asks nothing of the world: given the same
    observations and the same `now_ns`, it returns the same answer, which is what
    lets it be tested against a captured stream rather than against a live one.

    The staleness bound is not this class's business -- it is per symbol and
    learned, and it lives in `runtime.price_staleness`. This class decides which
    fact to measure against that bound, which is a different question.
    """

    def __init__(self, staleness, materiality_fraction: float) -> None:
        if not materiality_fraction > 0:
            raise ValueError(
                "the materiality fraction is how wide a quote may be before its mid stops "
                f"standing in for a trade, and must be positive; got {materiality_fraction!r}"
            )
        self._staleness = staleness
        self._materiality = materiality_fraction
        self._trades: dict[tuple[str, str], ChosenPrice] = {}
        self._quotes: dict[tuple[str, str], ChosenPrice] = {}
        self._quote_spread_fraction: dict[tuple[str, str], float] = {}
        self.chosen_the_trade = 0
        self.chosen_the_quote = 0
        self.refused_quote_too_wide = 0
        self.refused_both_too_old = 0
        self.refused_never_seen = 0

    def observe_trade(self, venue_id: str, symbol: str, price: float, at_ns: int) -> None:
        self._trades[(venue_id, symbol)] = ChosenPrice(price, at_ns, TRADE)

    def observe_quote(
        self,
        venue_id: str,
        symbol: str,
        bid_price: float,
        ask_price: float,
        at_ns: int,
    ) -> None:
        """One resting market. A crossed or non-positive quote is not one, and is dropped.

        Dropped rather than counted here: `quote-level-sampler` already refuses a
        crossed quote and counts it, and counting the same rejection twice in two
        parts would make one market look like two problems.
        """
        if bid_price <= 0 or ask_price <= 0 or bid_price > ask_price:
            return
        mid = (bid_price + ask_price) / 2.0
        self._quotes[(venue_id, symbol)] = ChosenPrice(mid, at_ns, QUOTE)
        self._quote_spread_fraction[(venue_id, symbol)] = (ask_price - bid_price) / mid

    def price_for(self, venue_id: str, symbol: str, now_ns: int) -> ChosenPrice | NoPrice:
        """The price to act on for this symbol, or why there is none."""
        key = (venue_id, symbol)
        traded = self._trades.get(key)
        quoted = self._quotes.get(key)
        if traded is None and quoted is None:
            self.refused_never_seen += 1
            return NoPrice(
                NEVER_SEEN,
                "no trade and no quote has ever arrived for this symbol here. Never seen "
                "is not a stale price and not a zero",
            )

        bound = self._staleness.believable_age_seconds(venue_id, symbol)
        if traded is not None and traded.age_seconds(now_ns) <= bound.value:
            self.chosen_the_trade += 1
            return traded

        if quoted is not None and quoted.age_seconds(now_ns) <= bound.value:
            width = self._quote_spread_fraction.get(key, 0.0)
            if width > self._materiality:
                self.refused_quote_too_wide += 1
                return NoPrice(
                    QUOTE_TOO_WIDE,
                    f"the last trade is too old and the resting market is {width:.3%} wide, "
                    f"past the {self._materiality:.3%} that makes a price difference matter "
                    f"here. Its mid is not within a round trip of anything tradeable",
                )
            self.chosen_the_quote += 1
            return quoted

        self.refused_both_too_old += 1
        traded_age = "never" if traded is None else f"{traded.age_seconds(now_ns):.0f}s ago"
        quoted_age = "never" if quoted is None else f"{quoted.age_seconds(now_ns):.0f}s ago"
        return NoPrice(
            BOTH_TOO_OLD,
            f"the last trade was {traded_age} and the last quote was {quoted_age}, both "
            f"past the {bound.value:.2f}s this symbol's own moves say a price may be "
            f"believed ({bound.reason}). Nobody is trading it and nobody is quoting it",
        )

    def describe(self) -> dict:
        """What this chooser has done, for the part's standing.

        `chosen_the_quote` is the measure of whether the quote feed is doing the job
        it was added for. A part whose refusals fall while this stays zero had its
        refusals fall for some other reason entirely.
        """
        return {
            "priced_from_a_trade": self.chosen_the_trade,
            "priced_from_a_quote": self.chosen_the_quote,
            "refused_quote_too_wide": self.refused_quote_too_wide,
            "refused_both_too_old": self.refused_both_too_old,
            "refused_never_seen": self.refused_never_seen,
            "symbols_with_a_trade": len(self._trades),
            "symbols_with_a_quote": len(self._quotes),
        }
