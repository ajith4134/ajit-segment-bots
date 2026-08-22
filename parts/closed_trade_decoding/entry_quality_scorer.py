"""entry-quality-scorer: how good the entry was, against what was actually reachable.

The tempting comparison is against the best price of the session, and it is wrong in
a way that quietly poisons everything downstream: no decision could have reached that
price, so every entry scores badly, the score carries no information, and a system
optimising against it learns to wait forever.

So the benchmark is the window the decision could have acted in -- from when the
signal existed to a bounded moment after it -- and the entry is placed as a
percentile within the prices available in that window. An entry at the median of what
was reachable is an ordinary entry, which is the correct verdict for most of them.

Two things are separated that a single score conflates:

- **Chasing.** An entry taken after the price has already moved a long way in the
  intended direction is a different failure from a merely mediocre fill: the setup
  may have been right and the edge already spent. It is flagged on its own, measured
  against the symbol's typical movement rather than a fixed percentage, because 1%
  is a large move in one symbol and noise in another.
- **Unmeasurable.** If the market data for the window is missing, the entry is
  unscored rather than scored neutrally. A neutral default is indistinguishable from
  a genuinely average entry and pollutes every average computed over it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.trade_decoding_types import EntryQuality
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "entry-quality-scorer"

PART_DECLARATION = PartDeclaration(
    part_id="entry-quality-scorer",
    consumes=("closed-trade", "market-data", "journal-entry"),
    produces=("entry-quality", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

SCORED = "scored"
NO_WINDOW = "no-market-data-covers-the-window-the-decision-could-have-acted-in"
NO_SIGNAL_TIME = "the-moment-the-signal-existed-was-never-recorded"
TOO_FEW_PRICES = "too-few-prices-in-the-window-to-place-the-entry-among-them"


@dataclass(frozen=True)
class EntryScore:
    trade_id: str
    state: str
    quality: EntryQuality | None
    prices_in_window: int
    reason: str
    scored_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == SCORED and self.quality is not None


@dataclass
class EntryScorerStanding:
    entries_examined: int = 0
    scored: int = 0
    unmeasurable_no_window: int = 0
    unmeasurable_no_signal_time: int = 0
    unmeasurable_too_few_prices: int = 0
    chasing_entries: int = 0
    entries_better_than_median: int = 0


class EntryQualityScorer:
    """Places an achieved entry among the prices the decision could have reached."""

    def __init__(
        self,
        window_seconds: float,
        minimum_prices: int,
        chasing_in_typical_movements: float,
        now_ns=time.time_ns,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError(
                "the benchmark is the window the decision could have acted in; comparing "
                "against the session's best price scores every entry badly and teaches "
                "the system to wait forever"
            )
        if minimum_prices < 2:
            raise ValueError("a percentile among one price is not a percentile")
        if chasing_in_typical_movements <= 0:
            raise ValueError(
                "chasing is measured in the symbol's own typical movement, because one "
                "percent is a large move in one symbol and noise in another"
            )
        self._window_seconds = window_seconds
        self._minimum_prices = minimum_prices
        self._chasing_threshold = chasing_in_typical_movements
        self._now_ns = now_ns
        self._prices: dict[tuple, list] = {}
        self._signal_times: dict[str, int] = {}
        self._typical_movement: dict[tuple, float] = {}
        self.standing = EntryScorerStanding()

    def observe_price(self, venue_id: str, symbol: str, price: float, at_ns: int) -> None:
        self._prices.setdefault((venue_id, symbol), []).append((at_ns, price))

    def observe_signal_time(self, trade_id: str, at_ns: int) -> None:
        """When the signal existed. The window starts here, not at the fill."""
        self._signal_times[trade_id] = at_ns

    def observe_typical_movement(self, venue_id: str, symbol: str, movement: float) -> None:
        self._typical_movement[(venue_id, symbol)] = movement

    def prices_in_window(self, venue_id, symbol, start_ns) -> list:
        end_ns = start_ns + int(self._window_seconds * 1e9)
        return [
            price
            for at_ns, price in self._prices.get((venue_id, symbol), [])
            if start_ns <= at_ns <= end_ns
        ]

    def score(self, trade_id: str, closed_trade) -> EntryScore:
        self.standing.entries_examined += 1
        signal_at = self._signal_times.get(trade_id)
        if signal_at is None:
            self.standing.unmeasurable_no_signal_time += 1
            return self._score(
                trade_id, NO_SIGNAL_TIME, None, 0,
                "the moment the signal existed was never recorded, so the window the "
                "decision could have acted in is unknown",
            )

        window = self.prices_in_window(closed_trade.venue_id, closed_trade.symbol, signal_at)
        if not window:
            self.standing.unmeasurable_no_window += 1
            return self._score(
                trade_id, NO_WINDOW, None, 0,
                f"no market data covers the {self._window_seconds:.0f}s after the signal. "
                f"This is unscored rather than scored neutrally: a neutral default is "
                f"indistinguishable from a genuinely average entry",
            )

        if len(window) < self._minimum_prices:
            self.standing.unmeasurable_too_few_prices += 1
            return self._score(
                trade_id, TOO_FEW_PRICES, None, len(window),
                f"{len(window)} price(s) in the window, below the {self._minimum_prices} "
                f"needed to place the entry among them",
            )

        achieved = closed_trade.entry_price
        is_long = closed_trade.direction == "long"
        # For a long, lower is better; for a short, higher is.
        better = sum(
            1 for price in window if (price > achieved if is_long else price < achieved)
        )
        percentile = better / len(window)
        best = min(window) if is_long else max(window)
        worst = max(window) if is_long else min(window)

        typical = self._typical_movement.get((closed_trade.venue_id, closed_trade.symbol))
        was_chasing = False
        if typical and typical > 0:
            first_price = window[0]
            moved = (achieved - first_price) if is_long else (first_price - achieved)
            was_chasing = moved / typical >= self._chasing_threshold
        if was_chasing:
            self.standing.chasing_entries += 1
        if percentile > 0.5:
            self.standing.entries_better_than_median += 1

        self.standing.scored += 1
        return self._score(
            trade_id, SCORED,
            EntryQuality(
                trade_id=trade_id,
                achieved_price=achieved,
                best_reachable=best,
                worst_reachable=worst,
                percentile=percentile,
                seconds_of_window=self._window_seconds,
                was_chasing=was_chasing,
                is_measurable=True,
                reason=(
                    f"better than {percentile:.0%} of the {len(window)} price(s) reachable "
                    f"in the {self._window_seconds:.0f}s after the signal"
                    + (
                        f". The price had already moved {self._chasing_threshold:.1f} "
                        f"typical movement(s) in the intended direction before this entry "
                        f"-- the setup may have been right and the edge already spent"
                        if was_chasing
                        else ""
                    )
                ),
                scored_at_ns=self._now_ns(),
            ),
            len(window),
            f"percentile {percentile:.2f} of {len(window)} reachable price(s)",
        )

    def _score(self, trade_id, state, quality, prices, reason) -> EntryScore:
        return EntryScore(
            trade_id=trade_id, state=state, quality=quality, prices_in_window=prices,
            reason=reason, scored_at_ns=self._now_ns(),
        )


def describe_entry_scoring(scorer: EntryQualityScorer) -> dict:
    return {
        "part_id": PART_ID,
        "entries_examined": scorer.standing.entries_examined,
        "scored": scorer.standing.scored,
        "unmeasurable_no_window": scorer.standing.unmeasurable_no_window,
        "unmeasurable_no_signal_time": scorer.standing.unmeasurable_no_signal_time,
        "unmeasurable_too_few_prices": scorer.standing.unmeasurable_too_few_prices,
        "chasing_entries": scorer.standing.chasing_entries,
        "entries_better_than_median": scorer.standing.entries_better_than_median,
        "benchmarks_against_the_sessions_best_price": False,
        "scores_an_unmeasurable_entry_as_neutral": False,
    }


def run_entry_quality_scorer(
    scorer: EntryQualityScorer, control_socket, read_closed_trades, publish_qualities,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        for trade_id, closed_trade in read_closed_trades():
            scored = scorer.score(trade_id, closed_trade)
            if scored.is_usable:
                publish_qualities(scored.quality)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
