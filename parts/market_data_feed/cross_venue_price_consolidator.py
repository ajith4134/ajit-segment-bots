"""cross-venue-price-consolidator: one price per symbol, with weight and staleness."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "cross-venue-price-consolidator"

PART_DECLARATION = PartDeclaration(
    part_id="cross-venue-price-consolidator",
    consumes=("market-data", "venue-standing"),
    produces=("consolidated-price", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

BANNED = "banned"
THROTTLED = "throttled"

# A quote older than this contributes nothing. It is not a market fact: it is how
# long a price may be believed after the venue that gave it went quiet, and a
# stale quote pulling a consolidated price is worse than a missing one because
# nothing downstream can see it happening.
DEFAULT_MAXIMUM_QUOTE_AGE_SECONDS = 5.0


@dataclass(frozen=True)
class VenueQuote:
    venue_id: str
    symbol: str
    price: float
    quantity: float
    observed_at_ns: int


@dataclass(frozen=True)
class ConsolidatedPrice:
    """One price, and everything needed to distrust it."""

    symbol: str
    price: float | None
    contributing_venues: tuple[str, ...]
    excluded_venues: tuple[str, ...]
    weights: dict[str, float]
    oldest_contribution_seconds: float | None
    reason: str
    observed_at_ns: int


@dataclass
class ConsolidatorStanding:
    quotes_seen: int = 0
    prices_published: int = 0
    symbols_without_price: int = 0
    excluded_for_staleness: int = 0
    excluded_for_standing: int = 0
    symbols: set[str] = field(default_factory=set)


class CrossVenuePriceConsolidator:
    """Volume-weights each venue's latest quote, dropping stale and refused ones.

    Volume weight rather than a plain mean: a thin venue's print moves a mean as
    much as a deep venue's, and the resulting price is one nobody could trade at.
    Weights and ages are published alongside the number, because a consolidated
    price with no way to see what went into it cannot be checked.

    A symbol with no usable quote gets a price of None and the reason. Rule 8 --
    absence renders as absence, never as the last price anyone happened to see.
    """

    def __init__(
        self,
        maximum_quote_age_seconds: float = DEFAULT_MAXIMUM_QUOTE_AGE_SECONDS,
        now_ns=time.time_ns,
    ) -> None:
        self._maximum_age_ns = int(maximum_quote_age_seconds * 1_000_000_000)
        self._now_ns = now_ns
        self._quotes: dict[str, dict[str, VenueQuote]] = {}
        self._standing: dict[str, str] = {}
        self.standing = ConsolidatorStanding()

    def set_venue_standing(self, venue_id: str, state: str) -> None:
        self._standing[venue_id] = state

    def observe_quote(self, quote: VenueQuote) -> None:
        self.standing.quotes_seen += 1
        self.standing.symbols.add(quote.symbol)
        self._quotes.setdefault(quote.symbol, {})[quote.venue_id] = quote

    def consolidate(self, symbol: str) -> ConsolidatedPrice:
        now = self._now_ns()
        quotes = self._quotes.get(symbol, {})
        contributing: dict[str, VenueQuote] = {}
        excluded: list[str] = []

        for venue_id, quote in quotes.items():
            if self._standing.get(venue_id) == BANNED:
                excluded.append(venue_id)
                self.standing.excluded_for_standing += 1
                continue
            if now - quote.observed_at_ns > self._maximum_age_ns:
                excluded.append(venue_id)
                self.standing.excluded_for_staleness += 1
                continue
            if quote.price <= 0 or quote.quantity <= 0:
                excluded.append(venue_id)
                continue
            contributing[venue_id] = quote

        if not contributing:
            self.standing.symbols_without_price += 1
            return ConsolidatedPrice(
                symbol=symbol,
                price=None,
                contributing_venues=(),
                excluded_venues=tuple(sorted(excluded)),
                weights={},
                oldest_contribution_seconds=None,
                reason="no venue has a fresh, usable quote for this symbol",
                observed_at_ns=now,
            )

        total_quantity = sum(quote.quantity for quote in contributing.values())
        weights = {
            venue_id: quote.quantity / total_quantity for venue_id, quote in contributing.items()
        }
        price = sum(quote.price * weights[venue_id] for venue_id, quote in contributing.items())
        oldest = max((now - quote.observed_at_ns) for quote in contributing.values()) / 1e9
        self.standing.prices_published += 1
        return ConsolidatedPrice(
            symbol=symbol,
            price=price,
            contributing_venues=tuple(sorted(contributing)),
            excluded_venues=tuple(sorted(excluded)),
            weights=weights,
            oldest_contribution_seconds=oldest,
            reason=f"volume-weighted across {len(contributing)} venue(s)",
            observed_at_ns=now,
        )

    def consolidate_all(self) -> tuple[ConsolidatedPrice, ...]:
        return tuple(self.consolidate(symbol) for symbol in sorted(self._quotes))


def describe_consolidation(consolidator: CrossVenuePriceConsolidator) -> dict:
    standing = consolidator.standing
    return {
        "part_id": PART_ID,
        "quotes_seen": standing.quotes_seen,
        "symbols_seen": len(standing.symbols),
        "prices_published": standing.prices_published,
        "symbols_without_price": standing.symbols_without_price,
        "excluded_for_staleness": standing.excluded_for_staleness,
        "excluded_for_standing": standing.excluded_for_standing,
    }


def run_cross_venue_price_consolidator(
    consolidator: CrossVenuePriceConsolidator, control_socket, read_quotes, publish_prices,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        for quote in read_quotes():
            consolidator.observe_quote(quote)
        publish_prices(consolidator.consolidate_all())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
