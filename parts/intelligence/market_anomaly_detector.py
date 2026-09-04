"""market-anomaly-detector: telling a real market move from a broken feed.

Every part downstream treats what arrives as what happened. When a feed lies --
a stale price, a crossed book, a venue printing through a gap -- the whole system
acts on fiction confidently, and the failure looks exactly like a fast market.

This part is the one that distinguishes them, and the distinction is always the
same shape: **a real move shows up in more than one place.**

- **A price that moved on one venue and not another** is a data problem until
  proven otherwise. A genuine move arbitrages across venues in seconds; a stale
  or broken feed does not. What is compared is the **departure from the basis
  this pair of venues normally holds**, never the distance from parity: two
  venues pricing a contract apart is a fact about the contract, and measured on
  the tape of 2026-08-26 it is the ordinary state. Across 36 symbols carried by
  both venues, 2,871,711 prints, the median disagreement is 0.0245% -- and
  BTRUSDT sits 1.38% apart on 98.9% of its prints with nothing wrong with either
  feed. Comparing levels flagged 3.75% of every print in the market; comparing
  departures from the learned basis flags 0.28%, and BTRUSDT falls from 98.9% to
  6.5%. The measurement is in
  `measurements/2026-08-26-anomaly-disagreement/`.
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
from collections import deque
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow

PART_ID = "market-anomaly-detector"

PART_DECLARATION = PartDeclaration(
    part_id="market-anomaly-detector",
    consumes=("market-data", "feed-gap", "consolidated-price", "feed-coverage", "order-book-snapshot"),
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
# Not yet knowing what two venues normally charge for a contract is its own
# state. Reported rather than folded into NO_ANOMALY, because "measured and
# clean" and "not measured" are different facts and only one of them is evidence
# (Rule 8), and rather than folded into an anomaly, because an unmeasured basis
# is not a reason to distrust a feed.
BASIS_NOT_MEASURED_YET = "the-basis-between-these-venues-is-not-measured-yet"
REFERENCE_IS_TOO_OLD = "the-cross-venue-reference-is-too-old-to-check-against"


@dataclass(frozen=True)
class MarketAnomaly:
    """A reason to distrust an input, named and evidenced."""

    venue_id: str
    symbol: str
    anomaly: str
    is_anomalous: bool
    observed_price: float | None
    consolidated_price: float | None
    # How far this print sits from the basis this venue normally holds against
    # the others -- not from the reference itself. Named `disagreement_fraction`
    # still because that is what it decides, but it is a departure since
    # 2026-08-26.
    disagreement_fraction: float | None
    venues_compared: int
    reason: str
    detected_at_ns: int
    # What the pair normally sits at, and on how many prints. Carried so a reader
    # can tell a venue that moved from a pair that has always been apart, and so
    # a flag can be argued with rather than only believed.
    learned_basis: float | None = None
    basis_observations: int = 0

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
    # Pairs whose normal basis is measured, and checks that could not be made
    # because it is not yet, or because the reference on hand was older than a
    # print now can be argued with.
    pairs_with_a_measured_basis: int = 0
    checks_against_a_stale_reference: int = 0
    widest_learned_basis: float | None = None
    # How many symbols have shown enough of their own rhythm for the silence
    # bound to widen past the floor, and how many checks ran inside a widened
    # bound. Reported because a patience nobody can see is indistinguishable
    # from one that never engages -- the way momentum-burst-detector's
    # series_breaks stayed invisible while it decided everything.
    symbols_with_a_measured_rhythm: int = 0
    checks_inside_a_widened_bound: int = 0


class MarketAnomalyDetector:
    """Cross-checks a venue's prints against everything else that saw the same market."""

    def __init__(
        self,
        disagreement_threshold: float,
        stale_after_seconds: float,
        minimum_volume_for_a_move: float,
        move_threshold: float,
        window_length: int,
        basis_window_observations: int = 200,
        minimum_basis_observations: int = 50,
        reference_maximum_age_seconds: float = 5.0,
        silence_patience_multiple: float | None = None,
        silence_gaps_needed: int = 8,
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
        if basis_window_observations < 2 or minimum_basis_observations < 2:
            raise ValueError(
                "a basis needs at least two observations to be a basis; got "
                f"{basis_window_observations!r} over a minimum of "
                f"{minimum_basis_observations!r}"
            )
        if minimum_basis_observations > basis_window_observations:
            raise ValueError(
                "a minimum above the window can never be reached, so the pair would never "
                f"be checked: {minimum_basis_observations!r} of {basis_window_observations!r}"
            )
        if reference_maximum_age_seconds <= 0:
            raise ValueError("a reference with no age bound is never old, which is false")
        if silence_patience_multiple is not None and silence_patience_multiple <= 0:
            raise ValueError(
                "the patience is a positive multiple of a symbol's own p99 gap between "
                f"prints, or None for the stated floor alone; got {silence_patience_multiple!r}"
            )
        if silence_gaps_needed < 2:
            raise ValueError(
                "a p99 estimated from fewer than two gaps is one gap wearing a percentile; "
                f"got {silence_gaps_needed!r}"
            )
        self._silence_patience_multiple = silence_patience_multiple
        self._silence_gaps_needed = silence_gaps_needed
        # Each symbol's own recent gaps between prints, bounded the same way
        # RollingWindow bounds its own: as many gaps as the window has values,
        # so the p99 describes the same stretch the prices do.
        self._gaps_between_prints: dict[tuple[str, str], deque] = {}
        self._basis_window = basis_window_observations
        self._minimum_basis_observations = minimum_basis_observations
        self._reference_maximum_age_ns = int(reference_maximum_age_seconds * 1e9)
        self._now_ns = now_ns
        self._prices: dict[tuple[str, str], RollingWindow] = {}
        self._last_update: dict[tuple[str, str], int] = {}
        self._consolidated: dict[str, tuple] = {}
        # What this venue normally prints against the others, per venue and
        # symbol. The learned half of the judgement (RL-060): the threshold says
        # how far a print may depart from what this pair does, and the pair's own
        # prints say what that is.
        self._basis: dict[tuple[str, str], RollingWindow] = {}
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
        at = at_ns if at_ns is not None else self._now_ns()
        previous = self._last_update.get(key)
        if previous is not None and at > previous:
            gaps = self._gaps_between_prints.get(key)
            if gaps is None:
                gaps = self._gaps_between_prints[key] = deque(maxlen=self._window)
            gaps.append((at - previous) / 1e9)
        self._last_update[key] = at

    def observe_consolidated_price(
        self, symbol: str, price: float, venues: int, contributing_prices: dict | None = None,
        observed_at_ns: int | None = None,
    ) -> None:
        """What every venue together says this symbol is worth, and who said what.

        The per-venue prices matter because the question this part asks is
        whether **one** venue moved when the others did not, and a blend that
        includes the venue being checked is partly that venue's own price. With
        two venues the blend is half of it, and the weight is the size of one
        print: a large trade on one side pulls the reference towards it and makes
        the other side read as anomalous.

        `observed_at_ns` is when the reference was taken. Kept because this map
        held a symbol's last reference forever: a reference is a level, and a
        level with no age bound is the trap this project keeps falling into --
        a fresh print compared against a reference from any time ago reads as a
        venue that moved alone, which is precisely the flag this part raises.
        """
        self._consolidated[symbol] = (
            price,
            venues,
            dict(contributing_prices or {}),
            self._now_ns() if observed_at_ns is None else observed_at_ns,
        )

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

    def silence_bound_ns(self, key) -> int:
        """How long this symbol may be silent before that is a fault, right now.

        The stated floor until this symbol has shown enough of its own rhythm to
        be measured against, then the larger of the floor and a multiple of its
        own p99 gap between prints. The same rule -- and the same reason --
        `RollingWindow._gap_bound_seconds` keeps: an estimate from a handful of
        gaps lets one early pause decide what ordinary looks like forever.

        This exists because the floor alone is a statement about one market. The
        bound this part used was `feed_coverage_window`, a setting belonging to
        `feed-coverage-auditor` and written about crypto perpetuals -- "the
        thinnest symbol in the captured thirty printed at least once a minute on
        2026-08-22". An NSE option chain is mostly contracts that do not, and on
        2026-09-04 that produced 3,282 of 3,289 anomalies, every one of them
        `this-venue-has-stopped-updating`, on instruments that were merely quiet.
        A detector that fires on ordinary quiet is not measuring the feed.
        """
        if self._silence_patience_multiple is None:
            return self._stale_after_ns
        gaps = self._gaps_between_prints.get(key)
        if gaps is None or len(gaps) < self._silence_gaps_needed:
            return self._stale_after_ns
        ordered = sorted(gaps)
        p99 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]
        widened = int(self._silence_patience_multiple * p99 * 1e9)
        if widened > self._stale_after_ns:
            self.standing.checks_inside_a_widened_bound += 1
            return widened
        return self._stale_after_ns

    def check(self, venue_id: str, symbol: str) -> MarketAnomaly:
        self.standing.checks += 1
        key = (venue_id, symbol)
        window_of_prints = self._prices.get(key)
        price = None if window_of_prints is None else window_of_prints.latest

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
        silence_bound_ns = self.silence_bound_ns(key)
        if last_update is not None and self._now_ns() - last_update > silence_bound_ns:
            return self._anomaly(
                venue_id, symbol, STALE_FEED, True, price, None, None, 0,
                f"{venue_id} has not updated {symbol} for "
                f"{(self._now_ns() - last_update) / 1e9:.0f}s, past the "
                f"{silence_bound_ns / 1e9:.0f}s this symbol's own rhythm allows",
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

        consolidated_price, venues = consolidated[0], consolidated[1]
        contributing_prices = consolidated[2] if len(consolidated) > 2 else {}
        reference_at_ns = consolidated[3] if len(consolidated) > 3 else None
        if (
            reference_at_ns is not None
            and self._now_ns() - reference_at_ns > self._reference_maximum_age_ns
        ):
            # A reference that old describes a market this print has left. It is
            # not evidence that this venue moved alone, and calling it that is how
            # a quiet symbol becomes a permanently anomalous one.
            self.standing.checks_against_a_stale_reference += 1
            return self._anomaly(
                venue_id, symbol, REFERENCE_IS_TOO_OLD, False, price, consolidated_price, None,
                venues,
                f"the cross-venue reference for {symbol} was taken "
                f"{(self._now_ns() - reference_at_ns) / 1e9:.1f}s ago, past the "
                f"{self._reference_maximum_age_ns / 1e9:.0f}s a reference is evidence about a "
                f"print now; nothing can be cross-checked until a fresh one arrives",
            )
        if price is None or consolidated_price <= 0:
            return self._anomaly(
                venue_id, symbol, NO_ANOMALY, False, price, consolidated_price, None, venues,
                "no price to check",
            )

        # The others, not the blend. A venue compared against a reference it is
        # part of is compared partly against itself, which is not what "a real
        # move shows up in more than one place" means.
        others = {
            other: other_price
            for other, other_price in contributing_prices.items()
            if other != venue_id and other_price > 0
        }
        if contributing_prices and not others:
            self.standing.single_venue_symbols += 1
            return self._anomaly(
                venue_id, symbol, CANNOT_CROSS_CHECK, False, price, consolidated_price, None, 1,
                f"{venue_id} is the only venue contributing a fresh price for {symbol}, so "
                f"there is nothing to cross-check it against; that is not the same as being "
                f"clean",
            )
        reference = sum(others.values()) / len(others) if others else consolidated_price
        venues_compared = len(others) if others else venues

        # What this venue prints against the others right now, signed, because the
        # side of the basis is what says which venue moved.
        basis = (price - reference) / reference
        window = self._basis.get(key)
        if window is None:
            window = RollingWindow(length=self._basis_window)
            self._basis[key] = window
        normal = window.quantile(0.5, self._minimum_basis_observations)
        observations = window.count
        # The median rather than the mean: the window is exactly where the outliers
        # this part is looking for land, and a mean moves towards them, so a single
        # broken print would raise the bar for the next one.
        window.observe(basis)

        move = self._recent_move(window_of_prints)
        volume = self._volumes.get(key, 0.0)
        moved_without_volume = (
            move is not None and move > self._move_threshold and volume < self._minimum_volume
        )

        if normal is None:
            # The pair's basis cannot judge anything yet, but a move nobody traded
            # is a fact about this venue alone and needs no cross-venue reference
            # at all -- so it is still named. Checked here rather than after the
            # disagreement, because gating it on the basis would leave the first
            # fifty prints of every pair unwatched for it.
            if moved_without_volume:
                return self._anomaly(
                    venue_id, symbol, MOVE_WITHOUT_VOLUME, True, price, reference, None,
                    venues_compared,
                    f"price moved {move:.2%} on {volume:,.0f} of quote volume, below the "
                    f"{self._minimum_volume:,.0f} this detector treats as a market. That is a "
                    f"print rather than a trade: a wick nobody could have traded, or a feed "
                    f"reporting an index",
                    learned_basis=None, basis_observations=observations,
                )
            return self._anomaly(
                venue_id, symbol, BASIS_NOT_MEASURED_YET, False, price, reference, None,
                venues_compared,
                f"{venue_id} is {basis:+.3%} from the {venues_compared} other venue(s) on "
                f"{symbol}, and this pair has {observations} of the "
                f"{self._minimum_basis_observations} prints needed before that number means "
                f"anything. Not measured is not clean",
                learned_basis=None, basis_observations=observations,
            )

        departure = abs(basis - normal)
        if (
            self.standing.largest_disagreement_seen is None
            or departure > self.standing.largest_disagreement_seen
        ):
            self.standing.largest_disagreement_seen = departure
        if (
            self.standing.widest_learned_basis is None
            or abs(normal) > self.standing.widest_learned_basis
        ):
            self.standing.widest_learned_basis = abs(normal)

        if departure > self._disagreement:
            return self._anomaly(
                venue_id, symbol, VENUES_DISAGREE, True, price, reference,
                departure, venues_compared,
                f"{venue_id} has {symbol} at {price:.8g} against {reference:.8g} across the "
                f"{venues_compared} other venue(s) -- {basis:+.2%}, where this pair normally "
                f"sits at {normal:+.2%} over its last {observations} prints. That is "
                f"{departure:.2%} of departure, past the {self._disagreement:.2%} that "
                f"separates a real move from a data problem. A genuine move arbitrages across "
                f"venues in seconds; a basis does not move at all",
                learned_basis=normal, basis_observations=observations,
            )

        if moved_without_volume:
            return self._anomaly(
                venue_id, symbol, MOVE_WITHOUT_VOLUME, True, price, reference,
                departure, venues_compared,
                f"price moved {move:.2%} on {volume:,.0f} of quote volume, below the "
                f"{self._minimum_volume:,.0f} this detector treats as a market. That is a "
                f"print rather than a trade: a wick nobody could have traded, or a feed "
                f"reporting an index",
                learned_basis=normal, basis_observations=observations,
            )

        return self._anomaly(
            venue_id, symbol, NO_ANOMALY, False, price, reference, departure, venues_compared,
            f"{venue_id} is {basis:+.3%} from {venues_compared} other venue(s) on {symbol}, "
            f"{departure:.3%} from the {normal:+.3%} this pair normally holds over its last "
            f"{observations} prints; the book is uncrossed and the feed is live",
            learned_basis=normal, basis_observations=observations,
        )

    def _recent_move(self, window: RollingWindow) -> float | None:
        series = list(window.values)
        if len(series) < 2 or series[-2] <= 0:
            return None
        return abs(series[-1] - series[-2]) / series[-2]

    def _anomaly(
        self, venue_id, symbol, anomaly, is_anomalous, price, consolidated,
        disagreement, venues, reason, learned_basis=None, basis_observations=0,
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
            learned_basis=learned_basis,
            basis_observations=basis_observations,
        )


def describe_anomalies(detector: MarketAnomalyDetector) -> dict:
    return {
        "part_id": PART_ID,
        "checks": detector.standing.checks,
        # Counted here rather than per check: this walks every tracked symbol,
        # and a check runs thousands of times a second while health is read
        # once. Pacing the work, not only the publish (2026-08-26).
        "symbols_with_a_measured_rhythm": sum(
            1 for gaps in detector._gaps_between_prints.values()
            if len(gaps) >= detector._silence_gaps_needed
        ),
        "checks_inside_a_widened_bound": detector.standing.checks_inside_a_widened_bound,
        "anomalies": detector.standing.anomalies,
        "by_anomaly": dict(sorted(detector.standing.by_anomaly.items())),
        # One counter per kind as well as the map, because only numbers survive
        # onto part-health: 162,773 anomalies in 1,206,824 checks on 2026-08-26
        # said nothing about which of the five this part detects was firing, and
        # each one means a different fault.
        **{
            f"anomaly_{kind.replace('-', '_')}": detector.standing.by_anomaly.get(kind, 0)
            for kind in (
                VENUES_DISAGREE, DURING_A_FEED_GAP, CROSSED_BOOK, MOVE_WITHOUT_VOLUME,
                STALE_FEED,
            )
        },
        "single_venue_symbols_that_could_not_be_cross_checked": detector.standing.single_venue_symbols,
        "largest_disagreement_seen": detector.standing.largest_disagreement_seen,
        "pairs_with_a_measured_basis": sum(
            1
            for window in detector._basis.values()
            if window.count >= detector._minimum_basis_observations
        ),
        "pairs_watched_for_a_basis": len(detector._basis),
        "widest_learned_basis": detector.standing.widest_learned_basis,
        "checks_against_a_stale_reference": detector.standing.checks_against_a_stale_reference,
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
    from runtime.venues.venue_adapter import NormalisedTrade

    market = Batch(read=context.bus.reader("market-data"))
    # Since 2026-08-25 the book has its own wire here. It was picked out of
    # market-data by isinstance and no book has ever travelled there, so the
    # crossed-book and wide-spread anomalies this part exists to catch were
    # unreachable.
    books = Batch(read=context.bus.reader("order-book-snapshot"))
    gaps = Batch(read=context.bus.reader("feed-gap"))
    consolidated = Batch(read=context.bus.reader("consolidated-price"))
    coverage = Batch(read=context.bus.reader("feed-coverage"))
    publish_anomalies = context.bus.publisher_for("market-anomaly")
    detector = MarketAnomalyDetector(
        disagreement_threshold=context.number("anomaly_disagreement_threshold"),
        # Its own setting since 2026-09-04. This used to read
        # feed_coverage_window, which belongs to feed-coverage-auditor and
        # answers a different question with that part's provenance.
        stale_after_seconds=context.number("anomaly_feed_silent_after_seconds"),
        silence_patience_multiple=context.number("anomaly_feed_silence_patience_multiple"),
        silence_gaps_needed=int(context.number("anomaly_feed_silence_gaps_needed")),
        minimum_volume_for_a_move=context.number("anomaly_minimum_quote_volume_for_a_move"),
        move_threshold=context.number("anomaly_move_threshold"),
        window_length=int(context.number("anomaly_window_length")),
        basis_window_observations=int(context.number("anomaly_basis_window_observations")),
        minimum_basis_observations=int(context.number("anomaly_minimum_basis_observations")),
        reference_maximum_age_seconds=context.number("anomaly_reference_maximum_age_seconds"),
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
        for book in books.payloads():
            if not (book.bids and book.asks):
                continue
            detector.observe_book(
                book.venue_id, book.symbol, float(book.bids[0][0]), float(book.asks[0][0])
            )
            touched.add((book.venue_id, book.symbol))
        for price in consolidated.payloads():
            detector.observe_consolidated_price(
                price.symbol,
                price.price,
                len(price.contributing_venues),
                getattr(price, "contributing_prices", None),
                # The consolidator's own stamp for when it took the reading, not
                # this part's clock: how old a reference is has to be measured
                # from when it was taken.
                price.observed_at_ns,
            )
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
