"""bull-feature-builder: what the model is allowed to look at, and what was missing.

The features are the bot. A conviction model is a way of weighing evidence, and
which evidence it is given decides what it can possibly learn -- so this part is
where the bull bot's actual view of the market is expressed.

Every feature here is a **fraction or a ratio**, never a price and never a
notional. A model trained on absolute prices learns the price level it was
trained at and stops working when the market moves; a model trained on "how far
above its own recent mean" transfers between symbols and across a year.

**A feature that cannot be measured is named as missing, never defaulted.** This
is the rule that costs the most and matters the most. Filling an unavailable
funding rate with zero teaches the model that unavailable means neutral, and
nothing downstream can undo that -- the model will have learned it, and it will
trade on it. So the vector says what it has, says what it does not, and lets the
parts below decide whether that is enough.

The build is bandwidth-bound rather than compute-bound: the work is reading the
book and the recent tape for a symbol, not the arithmetic on them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import FeatureVector
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow

PART_ID = "bull-feature-builder"
BOT = "bull-bot"

PART_DECLARATION = PartDeclaration(
    part_id="bull-feature-builder",
    consumes=(
        "bull-side-candidate", "market-data", "order-book-snapshot",
        "symbol-profile", "funding-forecast",
    ),
    produces=("bull-feature-vector", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

# Every feature this bot can produce, named once. A model asked for a feature
# that is not in this tuple is asking for something nothing builds, which is a
# defect rather than a missing value.
FEATURE_NAMES = (
    "price_z_score",
    "return_over_window",
    "realised_volatility_fraction",
    "volatility_ratio_short_to_long",
    "book_imbalance",
    "spread_fraction",
    "depth_to_size_ratio",
    "funding_rate",
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
    profiles_absent: int = 0


@dataclass
class SymbolObservations:
    """Everything the builder has been told about one symbol."""

    short_window: RollingWindow
    long_window: RollingWindow
    book: tuple | None = None
    funding_rate: float | None = None
    funding_forecast: float | None = None
    quote_step: float | None = None


class BullFeatureBuilder:
    """Turns a candidate plus the recent market into a vector, with its gaps named."""

    def __init__(
        self,
        short_window: int,
        long_window: int,
        minimum_observations: int,
        reference_order_size_quote: float,
        now_ns=time.time_ns,
    ) -> None:
        if short_window >= long_window:
            raise ValueError(
                "the short window must be shorter than the long one, or their ratio "
                "carries no information about whether volatility is rising"
            )
        if reference_order_size_quote <= 0:
            raise ValueError("depth is measured against a size, and a size of zero has no depth")
        self._short = short_window
        self._long = long_window
        self._minimum = minimum_observations
        self._reference_size = reference_order_size_quote
        self._now_ns = now_ns
        self._symbols: dict[tuple[str, str], SymbolObservations] = {}
        self.standing = BuilderStanding()

    def observe_price(self, venue_id: str, symbol: str, price: float) -> None:
        observations = self._observations_for(venue_id, symbol)
        observations.short_window.observe(price)
        observations.long_window.observe(price)

    def observe_book(self, venue_id: str, symbol: str, bids, asks) -> None:
        self._observations_for(venue_id, symbol).book = (tuple(bids), tuple(asks))

    def observe_funding(self, venue_id: str, symbol: str, rate: float) -> None:
        self._observations_for(venue_id, symbol).funding_rate = rate

    def observe_funding_forecast(self, venue_id: str, symbol: str, forecast: float) -> None:
        self._observations_for(venue_id, symbol).funding_forecast = forecast

    def observe_symbol_profile(self, venue_id: str, symbol: str, quote_step: float) -> None:
        self._observations_for(venue_id, symbol).quote_step = quote_step

    def build(self, candidate) -> FeatureVector:
        """One vector for one candidate, with everything it could not measure named."""
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

        # The detector's own claim. Always available -- it came in with the
        # candidate -- and it is what the model is being asked to second-guess.
        record("detector_strength", candidate.signal_strength, f"detector:{candidate.detector}")
        record(
            "detector_hit_rate",
            candidate.detector_confidence.value if candidate.detector_confidence.is_fitted else None,
            "detector's own calibrated record",
        )
        record("setup_weight", candidate.setup_weight, "bull-setup-weight-learner")

        if observations is None:
            for name in (
                "price_z_score", "return_over_window", "realised_volatility_fraction",
                "volatility_ratio_short_to_long", "book_imbalance", "spread_fraction",
                "depth_to_size_ratio", "funding_rate", "funding_forecast_change",
            ):
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
            self._volatility_fraction(observations.long_window),
            f"{self._long}-observation returns",
        )
        record(
            "volatility_ratio_short_to_long",
            self._volatility_ratio(observations),
            "short over long realised volatility",
        )

        if observations.book is None:
            self.standing.books_absent += 1
            record("book_imbalance", None, "no book snapshot")
            record("spread_fraction", None, "no book snapshot")
            record("depth_to_size_ratio", None, "no book snapshot")
        else:
            bids, asks = observations.book
            record("book_imbalance", self._book_imbalance(bids, asks), "order-book-snapshot")
            record("spread_fraction", self._spread_fraction(bids, asks), "order-book-snapshot")
            record(
                "depth_to_size_ratio",
                self._depth_to_size(asks),
                f"ask depth against {self._reference_size:g} quote",
            )

        record("funding_rate", observations.funding_rate, "venue funding rate")
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
                short_window=RollingWindow(length=self._short),
                long_window=RollingWindow(length=self._long),
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

    def _volatility_fraction(self, window: RollingWindow) -> float | None:
        returns = window.returns()
        if len(returns) < self._minimum:
            return None
        mean = sum(returns) / len(returns)
        variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
        return variance ** 0.5

    def _volatility_ratio(self, observations: SymbolObservations) -> float | None:
        """Rising volatility looks different from falling volatility at the same level."""
        short = self._volatility_fraction(observations.short_window)
        long = self._volatility_fraction(observations.long_window)
        if short is None or long is None or long == 0:
            return None
        return short / long

    def _book_imbalance(self, bids, asks) -> float | None:
        """Bid size less ask size over their total: -1 all offer, +1 all bid."""
        bid_size = sum(size for _, size in bids)
        ask_size = sum(size for _, size in asks)
        total = bid_size + ask_size
        if total <= 0:
            return None
        return (bid_size - ask_size) / total

    def _spread_fraction(self, bids, asks) -> float | None:
        if not bids or not asks:
            return None
        best_bid = max(price for price, _ in bids)
        best_ask = min(price for price, _ in asks)
        mid = (best_bid + best_ask) / 2
        if mid <= 0:
            return None
        return (best_ask - best_bid) / mid

    def _depth_to_size(self, asks) -> float | None:
        """How many reference orders the offer side could absorb before running out."""
        if not asks:
            return None
        available = sum(price * size for price, size in asks)
        return available / self._reference_size


def describe_feature_building(builder: BullFeatureBuilder) -> dict:
    return {
        "part_id": PART_ID,
        "vectors_built": builder.standing.vectors_built,
        "complete_vectors": builder.standing.complete_vectors,
        "incomplete_vectors": builder.standing.vectors_built - builder.standing.complete_vectors,
        "symbols_tracked": builder.standing.symbols_tracked,
        "missing_by_feature": dict(sorted(builder.standing.missing_by_feature.items())),
        "book_snapshots_absent": builder.standing.books_absent,
        "feature_names": list(FEATURE_NAMES),
    }


def run_bull_feature_builder(
    builder: BullFeatureBuilder, control_socket, read_candidates_and_market,
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

    trades = Batch(read=context.bus.reader("market-data"))
    candidates = Batch(read=context.bus.reader("bull-side-candidate"))
    books = Batch(read=context.bus.reader("order-book-snapshot"))
    profiles = Batch(read=context.bus.reader("symbol-profile"))
    funding = Batch(read=context.bus.reader("funding-forecast"))
    publish_vectors = context.bus.publisher_for("bull-feature-vector")

    def read_candidates_and_market(builder):
        for trade in trades.payloads():
            builder.observe_price(trade.venue_id, trade.symbol, trade.price)
        for book in books.payloads():
            builder.observe_book(book.venue_id, book.symbol, book.bids, book.asks)
        for profile in profiles.payloads():
            builder.observe_symbol_profile(profile)
        for forecast in funding.payloads():
            builder.observe_funding_forecast(forecast)
        return candidates.payloads()

    return run_bull_feature_builder(
        builder=BullFeatureBuilder(
            short_window=int(context.number("bull_feature_short_window")),
            long_window=int(context.number("bull_feature_long_window")),
            minimum_observations=int(context.number("bull_feature_minimum_observations")),
            reference_order_size_quote=context.number("bull_reference_order_size_quote"),
        ),
        control_socket=context.control_socket,
        read_candidates_and_market=read_candidates_and_market,
        publish_vectors=publish_vectors,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
