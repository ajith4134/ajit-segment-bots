"""bear-feature-builder: what a short is allowed to look at, which is not the bull's list.

The same mechanics as any feature builder and a deliberately different list. A
short is not a long with the signs flipped, and three of these features exist
only because the short side is asymmetric:

- **`downside_volatility_ratio`.** Volatility rises when price falls and falls
  when price rises. One realised-volatility number treats a 3% drop and a 3%
  rally as the same event; a short cares which, because the move that pays it is
  the one that also widens every spread it will have to exit through.
- **`squeeze_room`.** How much of the visible offer side would have to be lifted
  to move price against this short by its own recent volatility. A long's worst
  case is bounded at zero; a short's is not, and it arrives fastest in exactly
  the thin symbols where the setup looks best.

**Book imbalance is signed for the short**, not reused from the bull's view: the
resting bids that comfort a long are what a short has to sell into and then buy
back from, and a feature whose sign means one thing to one bot and the opposite
to another is a feature that teaches both models wrong.

**A feature that cannot be measured is named as missing, never defaulted.**

**`open_interest_change` and `sell_flow_imbalance` replace `funding_rate`,
`funding_carry_over_horizon` and `funding_forecast_change`** (2026-09-01,
options-segment-bots conversion). Funding rate is a crypto perpetual
mechanic with no Indian equivalent. `funding_carry_over_horizon` -- the
actual cost of holding a short -- has no honest replacement yet either: that
needs an index/stock futures basis, which is not built (Phase B, per
docs/goal.md). Retired without substitute rather than guessed. What
`funding_rate`/`funding_forecast_change` were a proxy for -- crowd
positioning pressure -- does have an honest Indian analogue already in
`broker-open-interest`: `open_interest_change` (shared with the bull
builder, an unsigned fact about the chain) and `sell_flow_imbalance`,
signed for the short the same way `offer_side_imbalance` already is --
positive when sell-side flow is heavier, which favours the short.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.price_frames import levels_in
from runtime.bot_opinion import FeatureVector
from runtime.knowledge_types import TYPICAL_SPREAD
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow

PART_ID = "bear-feature-builder"
BOT = "bear-bot"

PART_DECLARATION = PartDeclaration(
    part_id="bear-feature-builder",
    consumes=(
        "bear-side-candidate", "broker-instrument-listing", "broker-open-interest",
        "order-book-snapshot", "symbol-price-frame", "symbol-profile", "symbol-universe",
    ),
    produces=("bear-feature-vector", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FEATURE_NAMES = (
    "price_z_score",
    "return_over_window",
    "realised_volatility_fraction",
    "downside_volatility_ratio",
    "offer_side_imbalance",
    "spread_fraction",
    "squeeze_room",
    "open_interest_change",
    "sell_flow_imbalance",
    "detector_strength",
    "detector_hit_rate",
    "setup_weight",
)


@dataclass
class BuilderStanding:
    vectors_built: int = 0
    complete_vectors: int = 0
    symbols_tracked: int = 0
    missing_by_feature: dict = field(default_factory=dict)
    books_absent: int = 0
    thinnest_squeeze_room: float | None = None


@dataclass
class SymbolObservations:
    short_window: RollingWindow
    long_window: RollingWindow
    open_interest_window: RollingWindow
    book: tuple | None = None
    sell_flow_imbalance: float | None = None
    # What this symbol's spread usually is, from its profile. Used only when no
    # book snapshot has arrived: the spread now is unknown then, and the spread it
    # usually has is a fact about the symbol rather than about this moment.
    typical_spread: float | None = None


class BearFeatureBuilder:
    """Builds the short's view of a symbol, with its gaps named rather than filled."""

    def __init__(
        self,
        short_window: int,
        long_window: int,
        minimum_observations: int,
        maximum_gap_seconds: float | None = None,
        gap_patience_multiple: float | None = None,
        now_ns=time.time_ns,
    ) -> None:
        if short_window >= long_window:
            raise ValueError(
                "the short window must be shorter than the long one, or their ratio carries "
                "no information about whether volatility is rising"
            )
        self._short = short_window
        self._long = long_window
        self._minimum = minimum_observations
        self._now_ns = now_ns
        # How long this symbol may be silent before its window is judged to have a
        # hole in it rather than a series. None means the caller stated no bound,
        # and this part does not invent one (RL-061).
        self._maximum_gap_seconds = maximum_gap_seconds
        self._gap_patience_multiple = gap_patience_multiple
        self._symbols: dict[tuple[str, str], SymbolObservations] = {}
        self.standing = BuilderStanding()

    def observe_price(self, venue_id: str, symbol: str, price: float, at_ns: int) -> None:
        """One print into both windows, with the venue's own time for it.

        `at_ns` has no default. Every feature about volatility and return is
        computed from these windows, and a window that cannot see time computes
        them straight across a hole in the feed -- the first print after the gap
        sits beside the last one before it, and the difference becomes a return no
        market produced.
        """
        observations = self._observations_for(venue_id, symbol)
        observations.short_window.observe(price, at_ns)
        observations.long_window.observe(price, at_ns)

    def observe_book(self, venue_id: str, symbol: str, bids, asks) -> None:
        self._observations_for(venue_id, symbol).book = (tuple(bids), tuple(asks))

    def observe_open_interest(
        self, venue_id: str, symbol: str, open_interest: float,
        total_buy_quantity: float, total_sell_quantity: float, at_ns: int,
    ) -> None:
        """One underlying's option-chain open interest and order flow, already
        summed across its contracts (runtime.underlying_open_interest)."""
        observations = self._observations_for(venue_id, symbol)
        observations.open_interest_window.observe(open_interest, at_ns)
        total = total_buy_quantity + total_sell_quantity
        observations.sell_flow_imbalance = (
            (total_sell_quantity - total_buy_quantity) / total if total > 0 else None
        )

    def observe_symbol_profile(self, venue_id: str, symbol: str, typical_spread: float) -> None:
        """This symbol's usual spread. The bull builder has had one since it was
        written; this part had no method at all, and the call crashed it the hour
        symbol-profile-store first ran."""
        self._observations_for(venue_id, symbol).typical_spread = typical_spread

    def build(self, candidate) -> FeatureVector:
        self.standing.vectors_built += 1
        key = (candidate.venue_id, candidate.symbol)
        observations = self._symbols.get(key)

        features: dict[str, float] = {}
        sources: dict[str, str] = {}
        missing: list[str] = []

        def record(name: str, value, source: str) -> None:
            if value is None:
                missing.append(name)
                self.standing.missing_by_feature[name] = (
                    self.standing.missing_by_feature.get(name, 0) + 1
                )
            else:
                features[name] = float(value)
                sources[name] = source

        record("detector_strength", candidate.signal_strength, f"detector:{candidate.detector}")
        record(
            "detector_hit_rate",
            candidate.detector_confidence.value if candidate.detector_confidence.is_fitted else None,
            "detector's own calibrated record",
        )
        record("setup_weight", candidate.setup_weight, "bear-setup-weight-learner")

        if observations is None:
            for name in FEATURE_NAMES:
                if name not in features:
                    record(name, None, "nothing observed for this symbol")
            return self._vector(candidate, features, missing, sources)

        record(
            "price_z_score",
            observations.long_window.z_score(observations.long_window.latest, self._minimum)
            if observations.long_window.latest is not None
            else None,
            f"{self._long}-observation price window",
        )
        record("return_over_window", self._return_over(observations.short_window), "short window")
        record(
            "realised_volatility_fraction",
            self._volatility(observations.long_window),
            f"{self._long}-observation returns",
        )
        record(
            "downside_volatility_ratio",
            self._downside_ratio(observations.long_window),
            "downside over total realised volatility",
        )

        if observations.book is None:
            self.standing.books_absent += 1
            record("offer_side_imbalance", None, "no book snapshot")
            if observations.typical_spread is None:
                record("spread_fraction", None, "no book snapshot")
            else:
                record(
                    "spread_fraction", observations.typical_spread,
                    "symbol-profile: what this symbol's spread usually is, not the spread now",
                )
            record("squeeze_room", None, "no book snapshot")
        else:
            bids, asks = observations.book
            record("offer_side_imbalance", self._offer_imbalance(bids, asks), "order-book-snapshot")
            record("spread_fraction", self._spread_fraction(bids, asks), "order-book-snapshot")
            record(
                "squeeze_room",
                self._squeeze_room(asks, observations),
                "offer-side notional against this symbol's own volatility",
            )

        record(
            "open_interest_change",
            self._return_over(observations.open_interest_window),
            "broker-open-interest summed across the underlying's option chain",
        )
        record(
            "sell_flow_imbalance", observations.sell_flow_imbalance,
            "broker-open-interest: sell quantity less buy quantity, over their total "
            "-- signed for the short, positive when sell-side flow is heavier",
        )

        return self._vector(candidate, features, missing, sources)

    def _vector(self, candidate, features, missing, sources) -> FeatureVector:
        if not missing:
            self.standing.complete_vectors += 1
        return FeatureVector(
            bot=BOT,
            venue_id=candidate.venue_id,
            symbol=candidate.symbol,
            features=features,
            missing=tuple(missing),
            sources=sources,
            built_at_ns=self._now_ns(),
        )

    def _observations_for(self, venue_id: str, symbol: str) -> SymbolObservations:
        key = (venue_id, symbol)
        observations = self._symbols.get(key)
        if observations is None:
            observations = SymbolObservations(
                short_window=RollingWindow(
                    length=self._short,
                    maximum_gap_seconds=self._maximum_gap_seconds,
                gap_patience_multiple=self._gap_patience_multiple,
                ),
                long_window=RollingWindow(
                    length=self._long,
                    maximum_gap_seconds=self._maximum_gap_seconds,
                gap_patience_multiple=self._gap_patience_multiple,
                ),
                open_interest_window=RollingWindow(
                    length=self._long,
                    maximum_gap_seconds=self._maximum_gap_seconds,
                    gap_patience_multiple=self._gap_patience_multiple,
                ),
            )
            self._symbols[key] = observations
            self.standing.symbols_tracked = len(self._symbols)
        return observations

    def _return_over(self, window: RollingWindow) -> float | None:
        if window.count < self._minimum:
            return None
        series = list(window.values)
        if series[0] == 0:
            return None
        return (series[-1] - series[0]) / series[0]

    def _volatility(self, window: RollingWindow) -> float | None:
        """Counted in observations, like the minimum itself. See the bull builder.

        Takes the window rather than its returns for exactly that reason: a
        window of N values yields N-1 returns, so a return count compared against
        an observation minimum refuses a window that is completely full. It is
        latent here -- this part only measures volatility over the long window,
        which is 512 against a minimum of 64 -- and it was not latent in
        `bull-feature-builder`, where the same comparison against the 64-length
        short window made `volatility_ratio_short_to_long` unmeasurable on every
        vector ever built.
        """
        if window.count < self._minimum:
            return None
        returns = window.returns()
        # Not a threshold: a sample variance divides by `len - 1`.
        if len(returns) < 2:
            return None
        mean = sum(returns) / len(returns)
        variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
        return variance ** 0.5

    def _downside_ratio(self, window: RollingWindow) -> float | None:
        """How much of this symbol's movement is downward, which is what pays a short.

        One realised-volatility number treats a 3% drop and a 3% rally as the
        same event, and the short only gets paid by one of them.
        """
        if window.count < self._minimum:
            return None
        returns = window.returns()
        total = sum(value * value for value in returns)
        if total <= 0:
            return None
        downside = sum(value * value for value in returns if value < 0)
        return downside / total

    def _offer_imbalance(self, bids, asks) -> float | None:
        """Signed for the short: positive when the offer side is heavier.

        Deliberately not the bull's `book_imbalance` under a different name. A
        feature whose sign means one thing to one bot and the opposite to
        another teaches both models wrong.
        """
        bid_size = sum(size for _, size in bids)
        ask_size = sum(size for _, size in asks)
        total = bid_size + ask_size
        if total <= 0:
            return None
        return (ask_size - bid_size) / total

    def _spread_fraction(self, bids, asks) -> float | None:
        if not bids or not asks:
            return None
        best_bid = max(price for price, _ in bids)
        best_ask = min(price for price, _ in asks)
        mid = (best_bid + best_ask) / 2
        if mid <= 0:
            return None
        return (best_ask - best_bid) / mid

    def _squeeze_room(self, asks, observations: SymbolObservations) -> float | None:
        """Offer-side notional per unit of this symbol's own volatility.

        Small means a short is standing in front of a thin book in a symbol that
        moves; that is the shape a squeeze takes, and it is a property of the
        pair of measurements rather than of either one.
        """
        if not asks:
            return None
        volatility = self._volatility(observations.long_window)
        price = observations.long_window.latest
        if volatility is None or volatility <= 0 or price is None or price <= 0:
            return None
        offer_notional = sum(ask_price * size for ask_price, size in asks)
        room = offer_notional / (price * volatility)
        if self.standing.thinnest_squeeze_room is None or room < self.standing.thinnest_squeeze_room:
            self.standing.thinnest_squeeze_room = room
        return room


def describe_feature_building(builder: BearFeatureBuilder) -> dict:
    return {
        "part_id": PART_ID,
        "vectors_built": builder.standing.vectors_built,
        "complete_vectors": builder.standing.complete_vectors,
        "incomplete_vectors": builder.standing.vectors_built - builder.standing.complete_vectors,
        "symbols_tracked": builder.standing.symbols_tracked,
        "missing_by_feature": dict(sorted(builder.standing.missing_by_feature.items())),
        # One counter per feature as well as the map, because only numbers reach
        # part-health: "0 complete vectors of 454" said nothing about which of
        # the twelve was absent, and each one is a different missing input.
        **{
            f"missing_{name}": builder.standing.missing_by_feature.get(name, 0)
            for name in FEATURE_NAMES
        },
        "book_snapshots_absent": builder.standing.books_absent,
        "thinnest_squeeze_room_seen": builder.standing.thinnest_squeeze_room,
        "feature_names": list(FEATURE_NAMES),
    }


def run_bear_feature_builder(
    builder: BearFeatureBuilder, control_socket, read_candidates_and_market,
    publish_vectors, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        candidates = read_candidates_and_market(builder)
        publish_vectors(tuple(builder.build(candidate) for candidate in candidates))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_feature_building(builder),
    )

def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Prices, books, profiles and open interest are all levels the builder
    accumulates; the side candidates are the events that ask for a vector.
    Prices and open interest are taken in first within a tick, so a vector is
    built from the market as of the candidate rather than as of the last tick.

    Open interest arrives per option contract (broker-open-interest); the
    aggregator resolves each contract to its underlying via
    broker-instrument-listing and sums the chain, so the builder itself only
    ever sees one figure per underlying (runtime.underlying_open_interest).

    Four of its inputs will be empty in the first run -- nothing is producing
    order books, symbol profiles or open interest yet. That is why the vector
    names what it could not measure instead of substituting zeros: a missing
    feature and a feature that measured zero are different, and the composer
    counts the first.
    """
    from runtime.input_assembly import Batch
    from runtime.underlying_open_interest import UnderlyingOpenInterestAggregator

    trades = Batch(read=context.bus.reader("symbol-price-frame"))
    candidates = Batch(read=context.bus.reader("bear-side-candidate"))
    books = Batch(read=context.bus.reader("order-book-snapshot"))
    profiles = Batch(read=context.bus.reader("symbol-profile"))
    listings = Batch(read=context.bus.reader("broker-instrument-listing"))
    open_interest = Batch(read=context.bus.reader("broker-open-interest"))
    publish_vectors = context.bus.publisher_for("bear-feature-vector")
    oi_aggregator = UnderlyingOpenInterestAggregator()

    def read_candidates_and_market(builder):
        for listing in listings.payloads():
            oi_aggregator.observe_listing(listing)
        for reading in open_interest.payloads():
            oi_aggregator.observe_open_interest(reading)
        for trade in levels_in(trades.payloads()):
            builder.observe_price(
                trade.venue_id, trade.symbol, trade.price, trade.observed_at_ns
            )
        for book in books.payloads():
            builder.observe_book(book.venue_id, book.symbol, book.bids, book.asks)
        for profile in profiles.payloads():
            # A symbol-profile is a bundle of measured fields under a closed key
            # set, not a number. Passing the bundle where a float was expected
            # crashed this part the hour symbol-profile-store first ran.
            typical_spread = profile.value_of(TYPICAL_SPREAD)
            if typical_spread is not None:
                builder.observe_symbol_profile(profile.venue_id, profile.symbol, typical_spread)
        pending = candidates.payloads()
        for candidate in pending:
            totals = oi_aggregator.totals_for(candidate.symbol)
            if totals is not None:
                builder.observe_open_interest(
                    candidate.venue_id, candidate.symbol, totals.open_interest,
                    totals.total_buy_quantity, totals.total_sell_quantity,
                    totals.observed_at_ns,
                )
        return pending

    return run_bear_feature_builder(
        builder=BearFeatureBuilder(
            short_window=int(context.number("bear_feature_short_window")),
            long_window=int(context.number("bear_feature_long_window")),
            minimum_observations=int(context.number("bear_feature_minimum_observations")),
            maximum_gap_seconds=context.number("price_series_maximum_gap_seconds"),
            gap_patience_multiple=context.number("price_gap_patience_multiple"),
        ),
        control_socket=context.control_socket,
        read_candidates_and_market=read_candidates_and_market,
        publish_vectors=publish_vectors,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
