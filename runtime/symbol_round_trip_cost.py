"""What a round trip in one symbol actually costs, measured from its own book.

Every gate that computes a break-even needs the same number and needs it for the
same reason, so it is defined once here rather than four times in four bots.

The number is `liquidity-grade.round_trip_cost_fraction` -- `liquidity-grader`'s
reading of that symbol's own book, the spread crossed once plus the walk paid on
each side -- plus the broker's charge stack, which a book cannot see. Both are
fractions of that symbol's own price, which is the whole point: a conviction
floor divides a cost by a risk, and the ratio means nothing unless the two are
measured on the same instrument.

**This replaces one global rate that was charged to every plan.** Until
2026-09-08 every gate passed `per_side_trading_cost_fraction`, derived for an NSE
option premium, against risk fractions that were often measured on an underlying
stock. A stock's range over a minute is a much smaller number than an option's,
so the ratio was meaningless and the floor it produced was uncrossable: the live
spine formed 9,085 trade intents that day and every single one was `stand-aside`
for `conviction-below-threshold`. See
measurements/2026-09-08-conviction-floor-name-mismatch/.

**A grade goes stale and is then not this symbol's cost any more.** The grader
re-grades only symbols whose book it is still receiving, so a grade that stops
being restated is a symbol that went quiet, and a quiet book's last spread is not
what an order would cross now. Past `maximum_age_seconds` this reports nothing
and the caller falls back to its own rate -- which is the same discipline
`LatestByKey`'s `maximum_age_seconds` exists for, and the same trap this project
has paid for three times when a level was allowed to be true for ever.
"""

from __future__ import annotations

import time


class SymbolRoundTripCost:
    """Holds the latest liquidity grade per symbol and reports what it costs."""

    def __init__(
        self,
        charge_stack_round_trip_fraction: float,
        maximum_age_seconds: float,
        now_ns=time.time_ns,
    ) -> None:
        if charge_stack_round_trip_fraction < 0.0:
            raise ValueError(
                f"a charge stack of {charge_stack_round_trip_fraction} is not a cost"
            )
        if maximum_age_seconds <= 0.0:
            raise ValueError(
                "a grade with no age bound is true for ever, and a cost that is true for "
                "ever is a cost nobody measured"
            )
        self._charge_stack = charge_stack_round_trip_fraction
        self._maximum_age_ns = int(maximum_age_seconds * 1e9)
        self._now_ns = now_ns
        self._graded: dict[tuple[str, str], object] = {}
        self.costs_read = 0
        self.costs_from_a_grade = 0
        self.grades_too_old = 0
        self.symbols_graded = 0

    def observe_liquidity_grade(self, grade) -> None:
        """One `liquidity-grade`. Last one per symbol wins, as a level does."""
        self._graded[(grade.venue_id, grade.symbol)] = grade
        self.symbols_graded = len(self._graded)

    def for_symbol(self, venue_id: str, symbol: str) -> float | None:
        """This symbol's whole round trip, or None where nothing recent measured it."""
        self.costs_read += 1
        grade = self._graded.get((venue_id, symbol))
        if grade is None or grade.round_trip_cost_fraction is None:
            return None
        if self._now_ns() - grade.graded_at_ns > self._maximum_age_ns:
            self.grades_too_old += 1
            return None
        self.costs_from_a_grade += 1
        return grade.round_trip_cost_fraction + self._charge_stack

    def describe(self) -> dict:
        """What a board shows, so "no grade" and "a stale grade" stay apart."""
        return {
            "round_trip_costs_read": self.costs_read,
            "round_trip_costs_from_a_measured_grade": self.costs_from_a_grade,
            "round_trip_grades_too_old_to_use": self.grades_too_old,
            "symbols_with_a_liquidity_grade": self.symbols_graded,
        }


__all__ = ["SymbolRoundTripCost"]
