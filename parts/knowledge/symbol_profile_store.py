"""symbol-profile-store: everything one symbol is, in one place.

Half the parts in this system need the same handful of facts about a symbol --
its tick size, its typical spread, how much it normally moves, when its funding
settles. Without a profile each of them measures its own, and they disagree
quietly: two parts sizing against two different ideas of "normal" produce a book
nobody designed.

So the profile is assembled once and read everywhere, and its rules are about
staying honest:

- **Measured facts beat published ones.** A venue's documented tick size is what
  it says; the tick size in the fills is what it does, and where they differ the
  fills are right.
- **Every field says whether it is measured or assumed.** A profile where the
  two are indistinguishable is a profile whose defaults become beliefs.
- **A field with no value is absent, not defaulted.** A "typical spread" of zero
  makes every symbol look free to trade, and it makes the cheapest ones
  indistinguishable from the ones nobody has measured.
- **A new listing is a state.** A symbol with a week of history has a profile
  whose every field is thin, and the parts that size against it should know that
  rather than infer it from the numbers.

**The profile is rebuilt from its inputs, never edited in place.** An edited
profile has no history, and the question after every surprise is what the system
thought this symbol was.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.knowledge_types import (
    FUNDING_INTERVAL, LISTING_AGE, NORMAL_MOVE, TICK_SIZE, TYPICAL_SPREAD, TYPICAL_VOLUME,
)
from runtime.learned_estimator import QuantileEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "symbol-profile-store"

PART_DECLARATION = PartDeclaration(
    part_id="symbol-profile-store",
    consumes=("market-data", "semantic-fact", "order-book-snapshot"),
    produces=("symbol-profile", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

MEASURED = "measured"
PUBLISHED = "published"
ABSENT = "absent"

ESTABLISHED = "established"
NEW_LISTING = "a-new-listing-whose-every-field-is-thin"


@dataclass(frozen=True)
class ProfileField:
    """One field, and whether it is measured, published or simply absent."""

    name: str
    value: float | None
    source: str
    observations: int

    @property
    def is_measured(self) -> bool:
        return self.source == MEASURED

    @property
    def is_known(self) -> bool:
        return self.value is not None


@dataclass(frozen=True)
class SymbolProfile:
    """Everything one symbol is, assembled once and read everywhere."""

    venue_id: str
    symbol: str
    state: str
    fields: dict
    observations: int
    listing_age_seconds: float | None
    reason: str
    built_at_ns: int

    def field(self, name: str) -> ProfileField | None:
        return self.fields.get(name)

    def value_of(self, name: str) -> float | None:
        entry = self.fields.get(name)
        return entry.value if entry is not None else None

    @property
    def is_established(self) -> bool:
        return self.state == ESTABLISHED

    @property
    def measured_fields(self) -> tuple:
        return tuple(sorted(name for name, entry in self.fields.items() if entry.is_measured))


@dataclass
class StoreStanding:
    symbols_tracked: int = 0
    profiles_built: int = 0
    new_listings: int = 0
    measured_over_published: int = 0
    absent_fields: dict = field(default_factory=dict)
    observations: int = 0


class SymbolProfileStore:
    """Assembles one profile per symbol, preferring what was measured."""

    def __init__(
        self,
        window: int,
        minimum_observations: int,
        new_listing_seconds: float,
        now_ns=time.time_ns,
    ) -> None:
        if minimum_observations < 2:
            raise ValueError("a typical value from one observation is that observation")
        if new_listing_seconds <= 0:
            raise ValueError(
                "a new listing is a state parts should know rather than infer from the numbers"
            )
        self._window = window
        self._minimum = minimum_observations
        self._new_listing_seconds = new_listing_seconds
        self._now_ns = now_ns
        self._spreads: dict[tuple[str, str], QuantileEstimator] = {}
        self._volumes: dict[tuple[str, str], QuantileEstimator] = {}
        self._moves: dict[tuple[str, str], QuantileEstimator] = {}
        self._ticks: dict[tuple[str, str], float] = {}
        self._published: dict[tuple[str, str, str], float] = {}
        self._first_seen: dict[tuple[str, str], int] = {}
        self._counts: dict[tuple[str, str], int] = {}
        self.standing = StoreStanding()

    def observe_market(
        self, venue_id: str, symbol: str, spread_fraction: float,
        quote_volume: float | None, move_fraction: float,
        observed_tick: float | None = None,
    ) -> None:
        """One observation of what this symbol actually does.

        `quote_volume` is None when the print stated no size -- Upstox states
        one on about a quarter of its LTP updates and on no index at all. The
        spread and the move are still real observations, so they are still
        made; only the volume estimator sits this one out. TYPICAL_VOLUME then
        reads absent rather than zero, which is the distinction `build`'s own
        reason string already exists to keep ('a typical spread of zero makes
        every symbol look free to trade').
        """
        key = (venue_id, symbol)
        self._estimator(self._spreads, key).observe(abs(spread_fraction))
        if quote_volume is not None:
            self._estimator(self._volumes, key).observe(quote_volume)
        self._estimator(self._moves, key).observe(abs(move_fraction))
        if observed_tick is not None and observed_tick > 0:
            # What it does, not what the venue says it does.
            self._ticks[key] = observed_tick
        self._first_seen.setdefault(key, self._now_ns())
        self._counts[key] = self._counts.get(key, 0) + 1
        self.standing.observations += 1
        self.standing.symbols_tracked = len(self._counts)

    def observe_published_fact(self, venue_id: str, symbol: str, key_name: str, value: float) -> None:
        """What the venue documents. Beaten by measurement where they differ."""
        self._published[(venue_id, symbol, key_name)] = value

    def build(self, venue_id: str, symbol: str) -> SymbolProfile:
        """One profile, rebuilt from its inputs rather than edited in place."""
        self.standing.profiles_built += 1
        key = (venue_id, symbol)
        observations = self._counts.get(key, 0)

        fields = {}
        for name, table in (
            (TYPICAL_SPREAD, self._spreads),
            (TYPICAL_VOLUME, self._volumes),
            (NORMAL_MOVE, self._moves),
        ):
            fields[name] = self._field_from(name, table.get(key), venue_id, symbol)

        fields[TICK_SIZE] = self._tick_field(venue_id, symbol)
        fields[FUNDING_INTERVAL] = self._published_field(FUNDING_INTERVAL, venue_id, symbol)

        for name, entry in fields.items():
            if not entry.is_known:
                self.standing.absent_fields[name] = (
                    self.standing.absent_fields.get(name, 0) + 1
                )

        first_seen = self._first_seen.get(key)
        age = None if first_seen is None else (self._now_ns() - first_seen) / 1e9
        is_new = age is not None and age < self._new_listing_seconds
        if is_new:
            self.standing.new_listings += 1

        measured = [name for name, entry in fields.items() if entry.is_measured]
        absent = [name for name, entry in fields.items() if not entry.is_known]

        return SymbolProfile(
            venue_id=venue_id,
            symbol=symbol,
            state=NEW_LISTING if is_new else ESTABLISHED,
            fields=fields,
            observations=observations,
            listing_age_seconds=age,
            reason=(
                f"{len(measured)} field(s) measured here over {observations} observation(s)"
                + (f", {len(absent)} absent: {', '.join(sorted(absent))}" if absent else "")
                + ". An absent field is absent rather than zero -- a typical spread of zero "
                "makes every symbol look free to trade"
                + (
                    f". This is a new listing at {age / 86400:.1f} day(s) old, so every field "
                    f"is thin and the parts sizing against it should know that rather than "
                    f"infer it from the numbers"
                    if is_new
                    else ""
                )
            ),
            built_at_ns=self._now_ns(),
        )

    def _field_from(self, name: str, estimator, venue_id: str, symbol: str) -> ProfileField:
        if estimator is not None:
            estimate = estimator.estimate(0.5, self._minimum)
            if estimate.is_fitted:
                published = self._published.get((venue_id, symbol, name))
                if published is not None:
                    self.standing.measured_over_published += 1
                return ProfileField(name, estimate.value, MEASURED, estimate.observations)
        return self._published_field(name, venue_id, symbol)

    def _tick_field(self, venue_id: str, symbol: str) -> ProfileField:
        """The tick size in the fills beats the tick size in the documentation."""
        observed = self._ticks.get((venue_id, symbol))
        if observed is not None:
            published = self._published.get((venue_id, symbol, TICK_SIZE))
            if published is not None and published != observed:
                self.standing.measured_over_published += 1
            return ProfileField(TICK_SIZE, observed, MEASURED, 1)
        return self._published_field(TICK_SIZE, venue_id, symbol)

    def _published_field(self, name: str, venue_id: str, symbol: str) -> ProfileField:
        value = self._published.get((venue_id, symbol, name))
        if value is None:
            return ProfileField(name, None, ABSENT, 0)
        return ProfileField(name, value, PUBLISHED, 0)

    def _estimator(self, table, key) -> QuantileEstimator:
        estimator = table.get(key)
        if estimator is None:
            estimator = QuantileEstimator(window=self._window, prior=0.0)
            table[key] = estimator
        return estimator


def describe_symbol_profiles(store: SymbolProfileStore) -> dict:
    return {
        "part_id": PART_ID,
        "symbols_tracked": store.standing.symbols_tracked,
        "observations": store.standing.observations,
        "profiles_built": store.standing.profiles_built,
        "new_listings": store.standing.new_listings,
        "fields_where_measurement_beat_publication": store.standing.measured_over_published,
        "absent_fields": dict(sorted(store.standing.absent_fields.items())),
        "defaults_absent_fields": False,
    }


def run_symbol_profile_store(
    store: SymbolProfileStore, control_socket, read_market, publish_profiles,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        symbols = read_market(store)
        publish_profiles(tuple(store.build(venue_id, symbol) for venue_id, symbol in symbols))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_symbol_profiles(store),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A print gives a quote volume and, against the previous print on the
    same venue and symbol, a move; a book update gives a spread. A
    published fact with a numeric value is observed under its key. Every
    venue and symbol touched in a tick is rebuilt.
    """
    from runtime.input_assembly import Batch
    from runtime.venues.venue_adapter import NormalisedTrade

    market = Batch(read=context.bus.reader("market-data"))
    books = Batch(read=context.bus.reader("order-book-snapshot"))
    facts = Batch(read=context.bus.reader("semantic-fact"))
    publish_profiles = context.bus.publisher_for("symbol-profile")
    store = SymbolProfileStore(
        window=int(context.number("learning_window")),
        minimum_observations=int(context.number("decoding_minimum_trades")),
        new_listing_seconds=context.number("symbol_profile_new_listing_seconds"),
    )
    last_price: dict[tuple[str, str], float] = {}
    last_spread: dict[tuple[str, str], float] = {}

    def read_market(_store):
        touched: set[tuple[str, str]] = set()
        # The spread comes off the book's own wire since 2026-08-25. It was taken
        # out of market-data by isinstance and no book has ever travelled there,
        # so every profile this part wrote carried a spread of zero -- a symbol
        # that costs nothing to cross, which is the one thing no symbol is.
        for book in books.payloads():
            if not (book.bids and book.asks):
                continue
            bid, ask = float(book.bids[0][0]), float(book.asks[0][0])
            mid = (bid + ask) / 2
            if mid > 0:
                last_spread[(book.venue_id, book.symbol)] = (ask - bid) / mid
        for item in market.payloads():
            key = (item.venue_id, item.symbol)
            if isinstance(item, NormalisedTrade) and item.price > 0:
                previous = last_price.get(key)
                last_price[key] = item.price
                if previous is None or previous <= 0:
                    continue
                store.observe_market(
                    item.venue_id, item.symbol,
                    spread_fraction=last_spread.get(key, 0.0),
                    quote_volume=item.quote_volume,
                    move_fraction=(item.price - previous) / previous,
                )
                touched.add(key)
        for fact in facts.payloads():
            if isinstance(fact.value, (int, float)) and not isinstance(fact.value, bool):
                store.observe_published_fact(fact.venue_id, fact.symbol, fact.key, float(fact.value))
                touched.add((fact.venue_id, fact.symbol))
        return tuple(sorted(touched))

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_profiles(kept)

    return run_symbol_profile_store(
        store=store,
        control_socket=context.control_socket,
        read_market=read_market,
        publish_profiles=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
