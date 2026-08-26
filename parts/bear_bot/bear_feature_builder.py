"""bear-feature-builder: what a short is allowed to look at, which is not the bull's list.

The same mechanics as any feature builder and a deliberately different list. A
short is not a long with the signs flipped, and three of these features exist
only because the short side is asymmetric:

- **`downside_volatility_ratio`.** Volatility rises when price falls and falls
  when price rises. One realised-volatility number treats a 3% drop and a 3%
  rally as the same event; a short cares which, because the move that pays it is
  the one that also widens every spread it will have to exit through.
- **`funding_carry_over_horizon`.** A perpetual short is paid when funding is
  positive and charged when it is negative, every settlement, whatever the price
  does. It is a feature rather than only a filter because its size should change
  how sure the model is, not merely whether it looks.
- **`squeeze_room`.** How much of the visible offer side would have to be lifted
  to move price against this short by its own recent volatility. A long's worst
  case is bounded at zero; a short's is not, and it arrives fastest in exactly
  the thin symbols where the setup looks best.

**Book imbalance is signed for the short**, not reused from the bull's view: the
resting bids that comfort a long are what a short has to sell into and then buy
back from, and a feature whose sign means one thing to one bot and the opposite
to another is a feature that teaches both models wrong.

**A feature that cannot be measured is named as missing, never defaulted.**
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
        "bear-side-candidate", "symbol-price-frame", "order-book-snapshot",
        "symbol-profile", "funding-forecast",
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
    "funding_rate",
    "funding_carry_over_horizon",
    "funding_forecast_change",
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
    book: tuple | None = None
    funding_rate: float | None = None
    funding_forecast: float | None = None
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
        settlements_per_day: float,
        maximum_gap_seconds: float | None = None,
        gap_patience_multiple: float | None = None,
        now_ns=time.time_ns,
    ) -> None:
        if short_window >= long_window:
            raise ValueError(
                "the short window must be shorter than the long one, or their ratio carries "
                "no information about whether volatility is rising"
            )
        if settlements_per_day <= 0:
            raise ValueError("funding settles on a schedule; carry cannot be projected without it")
        self._short = short_window
        self._long = long_window
        self._minimum = minimum_observations
        self._settlements_per_day = settlements_per_day
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

    def observe_funding(self, venue_id: str, symbol: str, rate: float) -> None:
        self._observations_for(venue_id, symbol).funding_rate = rate

    def observe_funding_forecast(self, venue_id: str, symbol: str, forecast: float) -> None:
        self._observations_for(venue_id, symbol).funding_forecast = forecast

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
            self._volatility(observations.long_window.returns()),
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

        record("funding_rate", observations.funding_rate, "venue funding rate")
        record(
            "funding_carry_over_horizon",
            None
            if observations.funding_rate is None
            else -observations.funding_rate
            * (candidate.horizon_seconds / 86400.0 * self._settlements_per_day),
            "funding rate over the setup's own horizon, signed as a cost to the short",
        )
        record(
            "funding_forecast_change",
            None
            if observations.funding_forecast is None or observations.funding_rate is None
            else observations.funding_forecast - observations.funding_rate,
            "funding-forecast minus current",
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

    def _volatility(self, returns) -> float | None:
        if len(returns) < self._minimum:
            return None
        mean = sum(returns) / len(returns)
        variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
        return variance ** 0.5

    def _downside_ratio(self, window: RollingWindow) -> float | None:
        """How much of this symbol's movement is downward, which is what pays a short.

        One realised-volatility number treats a 3% drop and a 3% rally as the
        same event, and the short only gets paid by one of them.
        """
        returns = window.returns()
        if len(returns) < self._minimum:
            return None
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
        volatility = self._volatility(observations.long_window.returns())
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

    Prices, books, profiles and funding are all levels the builder accumulates; the
    side candidates are the events that ask for a vector. Prices are taken in first
    within a tick, so a vector is built from the market as of the candidate rather
    than as of the last tick.

    Four of its five inputs will be empty in the first run -- nothing is producing
    order books, symbol profiles or funding forecasts yet. That is why the vector
    names what it could not measure instead of substituting zeros: a missing
    feature and a feature that measured zero are different, and the composer counts
    the first.
    """
    from runtime.input_assembly import Batch

    trades = Batch(read=context.bus.reader("symbol-price-frame"))
    candidates = Batch(read=context.bus.reader("bear-side-candidate"))
    books = Batch(read=context.bus.reader("order-book-snapshot"))
    profiles = Batch(read=context.bus.reader("symbol-profile"))
    funding = Batch(read=context.bus.reader("funding-forecast"))
    publish_vectors = context.bus.publisher_for("bear-feature-vector")

    def read_candidates_and_market(builder):
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
        for forecast in funding.payloads():
            builder.observe_funding_forecast(forecast)
        return candidates.payloads()

    return run_bear_feature_builder(
        builder=BearFeatureBuilder(
            short_window=int(context.number("bear_feature_short_window")),
            long_window=int(context.number("bear_feature_long_window")),
            minimum_observations=int(context.number("bear_feature_minimum_observations")),
            settlements_per_day=context.number("bear_settlements_per_day"),
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
