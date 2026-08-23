"""liquidity-grader: how tradeable each symbol is at the size this bot uses.

Tradeability is not a property of a symbol; it is a property of a symbol *and a
size*. A symbol that is deep for a hundred-dollar order and untradeable for a
hundred-thousand-dollar one has one grade in a naive system and two here, and
which one applies depends entirely on what the bot is trying to do.

So the grade is computed against the bot's actual order size, and it combines the
three things that decide what a trade costs:

- **Spread**, paid on every round trip.
- **Depth at the size**, which decides how far past the touch the order walks.
- **Turnover**, because a symbol can be momentarily deep and still be one where a
  position cannot be closed.

**Refreshed on an interval, not every tick.** Grading every symbol on every book
update would spend most of the machine on a number that changes slowly, and the
blueprint says so explicitly.

An ungradeable symbol is **ungradeable**, never given a middling grade. A default
grade would let a symbol nobody could measure pass a liquidity filter.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow

PART_ID = "liquidity-grader"

PART_DECLARATION = PartDeclaration(
    part_id="liquidity-grader",
    consumes=("order-book-snapshot", "market-data"),
    produces=("liquidity-grade", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

DEEP = "deep"
TRADEABLE = "tradeable"
THIN = "thin"
UNTRADEABLE = "untradeable"
UNGRADEABLE = "ungradeable"

GRADES = (DEEP, TRADEABLE, THIN, UNTRADEABLE, UNGRADEABLE)


@dataclass(frozen=True)
class LiquidityGrade:
    """What it would cost to trade this symbol at this size, right now."""

    venue_id: str
    symbol: str
    grade: str
    order_size_quote: float
    spread_fraction: float | None
    depth_at_size_fraction: float | None
    round_trip_cost_fraction: float | None
    turnover_quote: float | None
    graded_at_ns: int
    reason: str

    @property
    def is_tradeable(self) -> bool:
        return self.grade in (DEEP, TRADEABLE)


@dataclass
class GraderStanding:
    grades_computed: int = 0
    refreshes_skipped: int = 0
    ungradeable: int = 0
    symbols_graded: int = 0
    by_grade: dict = field(default_factory=dict)
    worst_round_trip_cost: float = 0.0


class LiquidityGrader:
    """Grades each symbol against the bot's own order size, on a fixed interval."""

    def __init__(
        self,
        refresh_interval_seconds: float,
        deep_cost_fraction: float,
        tradeable_cost_fraction: float,
        thin_cost_fraction: float,
        turnover_window: int,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        if not deep_cost_fraction < tradeable_cost_fraction < thin_cost_fraction:
            raise ValueError("the cost bands must widen: deep, then tradeable, then thin")
        self._refresh = refresh_interval_seconds
        self._deep = deep_cost_fraction
        self._tradeable = tradeable_cost_fraction
        self._thin = thin_cost_fraction
        self._turnover_window = turnover_window
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._books: dict[tuple[str, str], tuple[tuple, tuple]] = {}
        self._turnover: dict[tuple[str, str], RollingWindow] = {}
        self._graded_at: dict[tuple[str, str], float] = {}
        self._grades: dict[tuple[str, str], LiquidityGrade] = {}
        self.standing = GraderStanding()

    def observe_book(self, venue_id: str, symbol: str, bids, asks) -> None:
        self._books[(venue_id, symbol)] = (
            tuple(sorted(bids, key=lambda level: -level[0])),
            tuple(sorted(asks, key=lambda level: level[0])),
        )

    def observe_turnover(self, venue_id: str, symbol: str, quote_volume: float) -> None:
        key = (venue_id, symbol)
        window = self._turnover.get(key)
        if window is None:
            window = RollingWindow(length=self._turnover_window)
            self._turnover[key] = window
        window.observe(quote_volume)

    def grade(self, venue_id: str, symbol: str, order_size_quote: float, force: bool = False):
        """Grade one symbol, or return the held grade if the interval has not passed."""
        key = (venue_id, symbol)
        now = self._monotonic()
        last = self._graded_at.get(key)

        if not force and last is not None and now - last < self._refresh:
            self.standing.refreshes_skipped += 1
            return self._grades[key]

        book = self._books.get(key)
        if book is None or not book[0] or not book[1]:
            self.standing.ungradeable += 1
            return self._record(
                key, venue_id, symbol, UNGRADEABLE, order_size_quote, None, None, None, None,
                "no two-sided book for this symbol; a default grade would let a symbol nobody "
                "could measure pass a liquidity filter",
            )

        bids, asks = book
        best_bid, best_ask = bids[0][0], asks[0][0]
        mid = (best_bid + best_ask) / 2
        if mid <= 0:
            self.standing.ungradeable += 1
            return self._record(
                key, venue_id, symbol, UNGRADEABLE, order_size_quote, None, None, None, None,
                "the book's mid price is not positive",
            )

        spread = (best_ask - best_bid) / mid
        walk = self._walk_cost(asks, order_size_quote, best_ask)
        turnover_window = self._turnover.get(key)
        turnover = turnover_window.mean(1) if turnover_window else None

        if walk is None:
            self.standing.ungradeable += 1
            return self._record(
                key, venue_id, symbol, UNTRADEABLE, order_size_quote, spread, None, None, turnover,
                f"the book cannot absorb {order_size_quote:,.0f} at any price shown; this symbol "
                f"is untradeable at this size whatever its spread looks like",
            )

        # A round trip pays the spread once and the walk on both sides.
        cost = spread + 2 * walk
        self.standing.worst_round_trip_cost = max(self.standing.worst_round_trip_cost, cost)

        if cost <= self._deep:
            grade = DEEP
        elif cost <= self._tradeable:
            grade = TRADEABLE
        elif cost <= self._thin:
            grade = THIN
        else:
            grade = UNTRADEABLE

        return self._record(
            key, venue_id, symbol, grade, order_size_quote, spread, walk, cost, turnover,
            f"a round trip of {order_size_quote:,.0f} costs {cost:.3%}: {spread:.3%} spread plus "
            f"{walk:.3%} walking the book on each side",
        )

    def _walk_cost(self, levels, order_size_quote: float, touch: float) -> float | None:
        """How far past the touch an order of this size would fill, as a fraction."""
        remaining = order_size_quote
        cost = 0.0
        filled = 0.0
        for price, quantity in levels:
            available = price * quantity
            taken = min(available, remaining)
            cost += taken
            filled += taken / price
            remaining -= taken
            if remaining <= 0:
                break
        if remaining > 0 or filled <= 0:
            return None
        average = cost / filled
        return abs(average - touch) / touch

    def _record(
        self, key, venue_id, symbol, grade, size, spread, walk, cost, turnover, reason
    ) -> LiquidityGrade:
        self.standing.grades_computed += 1
        self.standing.by_grade[grade] = self.standing.by_grade.get(grade, 0) + 1
        graded = LiquidityGrade(
            venue_id=venue_id, symbol=symbol, grade=grade, order_size_quote=size,
            spread_fraction=spread, depth_at_size_fraction=walk,
            round_trip_cost_fraction=cost, turnover_quote=turnover,
            graded_at_ns=self._now_ns(), reason=reason,
        )
        self._grades[key] = graded
        self._graded_at[key] = self._monotonic()
        self.standing.symbols_graded = len(self._grades)
        return graded

    def grade_of(self, venue_id: str, symbol: str) -> LiquidityGrade | None:
        return self._grades.get((venue_id, symbol))


def describe_liquidity(grader: LiquidityGrader) -> dict:
    return {
        "part_id": PART_ID,
        "grades_computed": grader.standing.grades_computed,
        "symbols_graded": grader.standing.symbols_graded,
        "refreshes_skipped": grader.standing.refreshes_skipped,
        "ungradeable": grader.standing.ungradeable,
        "by_grade": dict(grader.standing.by_grade),
        "worst_round_trip_cost": grader.standing.worst_round_trip_cost,
    }


def run_liquidity_grader(
    grader: LiquidityGrader, control_socket, read_books, publish_grades,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        symbols = read_books(grader)
        publish_grades(tuple(grader.grade(**symbol) for symbol in symbols))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
