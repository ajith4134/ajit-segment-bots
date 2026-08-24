"""market-anomaly-detector: telling a real market move from a broken feed.

Every part downstream treats what arrives as what happened. When a feed lies --
a stale price, a crossed book, a venue printing through a gap -- the whole system
acts on fiction confidently, and the failure looks exactly like a fast market.

This part is the one that distinguishes them, and the distinction is always the
same shape: **a real move shows up in more than one place.**

- **A price that moved on one venue and not another** is a data problem until
  proven otherwise. A genuine move arbitrages across venues in seconds; a stale
  or broken feed does not.
- **A move that arrives during a known feed gap** is not a move, it is the
  reconnection. The gap detector already knows; this part refuses to treat the
  first post-gap print as information.
- **A crossed or locked book** -- best bid at or above best ask -- is not a market
  state, it is a snapshot assembled from two moments.
- **A move without volume** is a print, not a trade. Price moving 3% on a
  handful of contracts is either a wick nobody could have traded or a feed
  reporting an index rather than a market.

**An anomaly is not a signal to trade.** It is a reason to distrust the input,
and the parts that act on it should act by refusing rather than by positioning.

**A symbol with only one venue cannot be cross-checked**, and that is reported
rather than assumed clean: a single-venue symbol is precisely where a bad feed
goes unnoticed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow

PART_ID = "market-anomaly-detector"

PART_DECLARATION = PartDeclaration(
    part_id="market-anomaly-detector",
    consumes=("market-data", "feed-gap", "consolidated-price", "feed-coverage"),
    produces=("market-anomaly", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

NO_ANOMALY = "no-anomaly"
VENUES_DISAGREE = "one-venue-moved-and-the-others-did-not"
DURING_A_FEED_GAP = "this-print-is-the-reconnection-not-a-move"
CROSSED_BOOK = "the-book-is-crossed-or-locked"
MOVE_WITHOUT_VOLUME = "price-moved-without-trades-behind-it"
STALE_FEED = "this-venue-has-stopped-updating"
CANNOT_CROSS_CHECK = "only-one-venue-carries-this-symbol"


@dataclass(frozen=True)
class MarketAnomaly:
    """A reason to distrust an input, named and evidenced."""

    venue_id: str
    symbol: str
    anomaly: str
    is_anomalous: bool
    observed_price: float | None
    consolidated_price: float | None
    disagreement_fraction: float | None
    venues_compared: int
    reason: str
    detected_at_ns: int

    @property
    def should_be_traded_on(self) -> bool:
        """Never. An anomaly is a reason to refuse, not a reason to position."""
        return False


@dataclass
class DetectorStanding:
    checks: int = 0
    anomalies: int = 0
    by_anomaly: dict = field(default_factory=dict)
    single_venue_symbols: int = 0
    largest_disagreement_seen: float | None = None


class MarketAnomalyDetector:
    """Cross-checks a venue's prints against everything else that saw the same market."""

    def __init__(
        self,
        disagreement_threshold: float,
        stale_after_seconds: float,
        minimum_volume_for_a_move: float,
        move_threshold: float,
        window_length: int,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < disagreement_threshold < 1.0:
            raise ValueError(
                "the threshold is a fraction of price; outside (0, 1) it flags everything or "
                "nothing"
            )
        if stale_after_seconds <= 0:
            raise ValueError("a feed with no staleness bound is never stale, which is false")
        self._disagreement = disagreement_threshold
        self._stale_after_ns = int(stale_after_seconds * 1e9)
        self._minimum_volume = minimum_volume_for_a_move
        self._move_threshold = move_threshold
        self._window = window_length
        self._now_ns = now_ns
        self._prices: dict[tuple[str, str], RollingWindow] = {}
        self._last_update: dict[tuple[str, str], int] = {}
        self._consolidated: dict[str, tuple] = {}
        self._books: dict[tuple[str, str], tuple] = {}
        self._volumes: dict[tuple[str, str], float] = {}
        self._gaps: set[tuple[str, str]] = set()
        self.standing = DetectorStanding()

    def observe_price(self, venue_id: str, symbol: str, price: float, at_ns: int | None = None) -> None:
        key = (venue_id, symbol)
        window = self._prices.get(key)
        if window is None:
            window = RollingWindow(length=self._window)
            self._prices[key] = window
        window.observe(price)
        self._last_update[key] = at_ns if at_ns is not None else self._now_ns()

    def observe_consolidated_price(self, symbol: str, price: float, venues: int) -> None:
        """What every venue together says this symbol is worth."""
        self._consolidated[symbol] = (price, venues)

    def observe_book(self, venue_id: str, symbol: str, best_bid: float, best_ask: float) -> None:
        self._books[(venue_id, symbol)] = (best_bid, best_ask)

    def observe_volume(self, venue_id: str, symbol: str, quote_volume: float) -> None:
        self._volumes[(venue_id, symbol)] = quote_volume

    def observe_feed_gap(self, venue_id: str, symbol: str, in_a_gap: bool) -> None:
        key = (venue_id, symbol)
        if in_a_gap:
            self._gaps.add(key)
        else:
            self._gaps.discard(key)

    def check(self, venue_id: str, symbol: str) -> MarketAnomaly:
        self.standing.checks += 1
        key = (venue_id, symbol)
        window = self._prices.get(key)
        price = None if window is None else window.latest

        if key in self._gaps:
            # The first print after a reconnection is not information about the
            # market; it is information about the connection.
            return self._anomaly(
                venue_id, symbol, DURING_A_FEED_GAP, True, price, None, None, 0,
                "this print arrived during a known feed gap, so it is the reconnection "
                "rather than a move",
            )

        book = self._books.get(key)
        if book is not None and book[0] >= book[1] > 0:
            return self._anomaly(
                venue_id, symbol, CROSSED_BOOK, True, price, None, None, 0,
                f"best bid {book[0]:.8g} is at or above best ask {book[1]:.8g}; that is not a "
                f"market state, it is a snapshot assembled from two moments",
            )

        last_update = self._last_update.get(key)
        if last_update is not None and self._now_ns() - last_update > self._stale_after_ns:
            return self._anomaly(
                venue_id, symbol, STALE_FEED, True, price, None, None, 0,
                f"{venue_id} has not updated {symbol} for "
                f"{(self._now_ns() - last_update) / 1e9:.0f}s, past the "
                f"{self._stale_after_ns / 1e9:.0f}s this detector treats as live",
            )

        consolidated = self._consolidated.get(symbol)
        if consolidated is None or consolidated[1] < 2:
            # Reported rather than assumed clean: a single-venue symbol is
            # precisely where a bad feed goes unnoticed.
            self.standing.single_venue_symbols += 1
            return self._anomaly(
                venue_id, symbol, CANNOT_CROSS_CHECK, False, price,
                None if consolidated is None else consolidated[0], None,
                0 if consolidated is None else consolidated[1],
                f"only {0 if consolidated is None else consolidated[1]} venue(s) carry "
                f"{symbol}, so nothing can be cross-checked; that is not the same as being "
                f"clean, and a single-venue symbol is where a bad feed goes unnoticed",
            )

        consolidated_price, venues = consolidated
        if price is None or consolidated_price <= 0:
            return self._anomaly(
                venue_id, symbol, NO_ANOMALY, False, price, consolidated_price, None, venues,
                "no price to check",
            )

        disagreement = abs(price - consolidated_price) / consolidated_price
        if (
            self.standing.largest_disagreement_seen is None
            or disagreement > self.standing.largest_disagreement_seen
        ):
            self.standing.largest_disagreement_seen = disagreement

        if disagreement > self._disagreement:
            return self._anomaly(
                venue_id, symbol, VENUES_DISAGREE, True, price, consolidated_price,
                disagreement, venues,
                f"{venue_id} has {symbol} at {price:.8g} against a consolidated "
                f"{consolidated_price:.8g} across {venues} venue(s) -- {disagreement:.2%} apart, "
                f"past the {self._disagreement:.2%} that separates a real move from a data "
                f"problem. A genuine move arbitrages across venues in seconds",
            )

        move = self._recent_move(window)
        volume = self._volumes.get(key, 0.0)
        if move is not None and move > self._move_threshold and volume < self._minimum_volume:
            return self._anomaly(
                venue_id, symbol, MOVE_WITHOUT_VOLUME, True, price, consolidated_price,
                disagreement, venues,
                f"price moved {move:.2%} on {volume:,.0f} of quote volume, below the "
                f"{self._minimum_volume:,.0f} this detector treats as a market. That is a "
                f"print rather than a trade: a wick nobody could have traded, or a feed "
                f"reporting an index",
            )

        return self._anomaly(
            venue_id, symbol, NO_ANOMALY, False, price, consolidated_price, disagreement, venues,
            f"{venue_id} agrees with {venues} venue(s) to within {disagreement:.3%}, the book "
            f"is uncrossed, and the feed is live",
        )

    def _recent_move(self, window: RollingWindow) -> float | None:
        series = list(window.values)
        if len(series) < 2 or series[-2] <= 0:
            return None
        return abs(series[-1] - series[-2]) / series[-2]

    def _anomaly(
        self, venue_id, symbol, anomaly, is_anomalous, price, consolidated,
        disagreement, venues, reason,
    ) -> MarketAnomaly:
        if is_anomalous:
            self.standing.anomalies += 1
            self.standing.by_anomaly[anomaly] = self.standing.by_anomaly.get(anomaly, 0) + 1
        return MarketAnomaly(
            venue_id=venue_id,
            symbol=symbol,
            anomaly=anomaly,
            is_anomalous=is_anomalous,
            observed_price=price,
            consolidated_price=consolidated,
            disagreement_fraction=disagreement,
            venues_compared=venues,
            reason=reason,
            detected_at_ns=self._now_ns(),
        )


def describe_anomalies(detector: MarketAnomalyDetector) -> dict:
    return {
        "part_id": PART_ID,
        "checks": detector.standing.checks,
        "anomalies": detector.standing.anomalies,
        "by_anomaly": dict(sorted(detector.standing.by_anomaly.items())),
        "single_venue_symbols_that_could_not_be_cross_checked": detector.standing.single_venue_symbols,
        "largest_disagreement_seen": detector.standing.largest_disagreement_seen,
        "venue_symbols_watched": len(detector._prices),
        "produces_a_trading_signal": False,
    }


def run_market_anomaly_detector(
    detector: MarketAnomalyDetector, control_socket, read_market, publish_anomalies,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        symbols = read_market(detector)
        publish_anomalies(tuple(detector.check(venue_id, symbol) for venue_id, symbol in symbols))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_anomalies(detector),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Every print is a price and a volume; a book update is a top of book; a
    feed gap marks the venue and symbol as inside one until its next print;
    a consolidated price is the cross-venue reference with how many venues
    stood behind it. Every venue and symbol touched in a tick is checked.
    Coverage is read and drained: which venues carry a symbol is already in
    the consolidated price's contributor count.
    """
    from runtime.input_assembly import Batch
    from runtime.venues.venue_adapter import BookUpdate, NormalisedTrade

    market = Batch(read=context.bus.reader("market-data"))
    gaps = Batch(read=context.bus.reader("feed-gap"))
    consolidated = Batch(read=context.bus.reader("consolidated-price"))
    coverage = Batch(read=context.bus.reader("feed-coverage"))
    publish_anomalies = context.bus.publisher_for("market-anomaly")
    detector = MarketAnomalyDetector(
        disagreement_threshold=context.number("anomaly_disagreement_threshold"),
        stale_after_seconds=context.number("feed_coverage_window"),
        minimum_volume_for_a_move=context.number("anomaly_minimum_quote_volume_for_a_move"),
        move_threshold=context.number("anomaly_move_threshold"),
        window_length=int(context.number("anomaly_window_length")),
    )

    def read_market(_detector):
        coverage.payloads()
        touched: set[tuple[str, str]] = set()
        for gap in gaps.payloads():
            detector.observe_feed_gap(gap.venue_id, gap.symbol, True)
            touched.add((gap.venue_id, gap.symbol))
        for item in market.payloads():
            if isinstance(item, NormalisedTrade):
                detector.observe_feed_gap(item.venue_id, item.symbol, False)
                detector.observe_price(item.venue_id, item.symbol, item.price, item.venue_time_ns)
                detector.observe_volume(item.venue_id, item.symbol, item.price * item.quantity)
                touched.add((item.venue_id, item.symbol))
            elif isinstance(item, BookUpdate) and item.bids and item.asks:
                detector.observe_book(item.venue_id, item.symbol, float(item.bids[0][0]), float(item.asks[0][0]))
                touched.add((item.venue_id, item.symbol))
        for price in consolidated.payloads():
            detector.observe_consolidated_price(price.symbol, price.price, len(price.contributing_venues))
        return tuple(sorted(touched))

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_anomalies(kept)

    return run_market_anomaly_detector(
        detector=detector,
        control_socket=context.control_socket,
        read_market=read_market,
        publish_anomalies=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
