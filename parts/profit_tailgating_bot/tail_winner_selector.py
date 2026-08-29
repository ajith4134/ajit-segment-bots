"""tail-winner-selector: adding to this segment's own winning leg, and when not to.

RL-047 made concrete. When an exploration pair has been run and one leg is
working, the working leg is the best-evidenced opportunity this system has: it is
not a forecast, it is a position that is already right, in a symbol this segment
already holds and already understands.

That is also exactly why it is dangerous, and every check here exists because of
it:

- **Concentration.** Adding to a winner is how a diversified book becomes a
  single bet. The most reliable signal the system produces is the one most likely
  to be over-weighted, and the check against it has to sit here rather than being
  left to the risk gate -- by the time the gate sees it, the bot has already
  claimed the capital.
- **The verdict must be in.** A leg that is merely up is not a winning leg; the
  pair verdict is what says the experiment has resolved. Adding on unrealised
  profit before the verdict is adding on noise.
- **Peak excursion, not current profit** (RL-042). A position up 3% that was up
  8% is a position giving profit back, and that is the shape of a move that has
  finished. Current profit alone cannot see it.

**It never adds to a loser and never reverses one.** A losing leg is the other
experiment's evidence, not this bot's opportunity.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import FROM_OUR_OWN_WINNER, LONG, SHORT, FollowCandidate
# The verdict shape as `exploration-pair-decoder` publishes it. It was declared a
# second time in this file until 2026-08-28, with four fields the wire payload has
# never carried -- so the reads below type-checked against a class nobody sends,
# the tests built that class and passed, and the first real verdict to arrive
# would have killed this part with AttributeError.
from runtime.trade_decoding_types import PairVerdict
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "tail-winner-selector"
BOT = "profit-tailgating-bot"

PART_DECLARATION = PartDeclaration(
    part_id="tail-winner-selector",
    consumes=("position", "market-data", "peak-excursion", "pair-verdict"),
    produces=("follow-candidate", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

SELECTED = "selected"
NO_VERDICT = "the-pair-experiment-has-not-resolved"
NOT_THE_WINNING_SIDE = "this-position-is-on-the-side-the-pair-found-against"
SETTLED_ON_NEITHER_SIDE = "the-pair-resolved-and-found-for-neither-side"
NOT_IN_PROFIT = "the-leg-is-not-actually-ahead"
GIVING_PROFIT_BACK = "price-has-retraced-from-its-peak"
ALREADY_CONCENTRATED = "this-symbol-already-holds-too-much-of-the-book"
NO_PEAK_RECORD = "no-peak-excursion-recorded-for-this-position"
NO_COST_BASIS = "position-has-no-cost-basis-to-measure-profit-against"


@dataclass(frozen=True)
class PeakExcursion:
    """The furthest a position has been in profit, and where it is now (RL-042).

    Matches what `peak-excursion-tracker` actually publishes: unrealised
    account-currency amounts, not a fraction of the position. A fraction is
    computed in `select()`, against the notional of the position it is being
    judged for, because the tracker itself has no notional to divide by --
    only the cost basis it was given (T-4).
    """

    venue_id: str
    symbol: str
    best_unrealised: float
    worst_unrealised: float
    best_price: float
    worst_price: float
    current_unrealised: float
    samples: int
    observed_at_ns: int


@dataclass
class SelectorStanding:
    positions_examined: int = 0
    selected: int = 0
    by_rejection: dict = field(default_factory=dict)
    largest_retracement_refused: float = 0.0
    concentration_refusals: int = 0
    verdicts_without_an_instrument: int = 0


class TailWinnerSelector:
    """Selects this segment's own winning leg to add to, and refuses to concentrate."""

    def __init__(
        self,
        maximum_retraced_fraction: float,
        maximum_symbol_share_of_book: float,
        minimum_profit_fraction: float,
        default_setup_weight: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < maximum_retraced_fraction < 1.0:
            raise ValueError(
                "the retracement ceiling is a fraction of the peak and must be inside (0, 1); "
                "without one this bot adds to moves that have already finished"
            )
        if not 0.0 < maximum_symbol_share_of_book < 1.0:
            raise ValueError(
                "adding to a winner is how a diversified book becomes a single bet, so the "
                "share one symbol may hold must be bounded"
            )
        self._maximum_retraced = maximum_retraced_fraction
        self._maximum_share = maximum_symbol_share_of_book
        self._minimum_profit = minimum_profit_fraction
        self._default_weight = default_setup_weight
        self._now_ns = now_ns
        self._verdicts: dict[tuple[str, str], PairVerdict] = {}
        self._peaks: dict[tuple[str, str], PeakExcursion] = {}
        self._exposure: dict[tuple[str, str], float] = {}
        self._book_value: float = 0.0
        self.standing = SelectorStanding()

    def observe_pair_verdict(self, verdict: PairVerdict) -> None:
        """One verdict, filed under the instrument the pair was run on.

        A pair is two directions on one instrument, so a verdict names one
        instrument and one winning side -- never a winning symbol and a losing
        one. A verdict that reached this part without naming its instrument
        cannot be matched to a position and is counted rather than filed.
        """
        if verdict.venue_id is None or verdict.symbol is None:
            self.standing.verdicts_without_an_instrument += 1
            return
        self._verdicts[(verdict.venue_id, verdict.symbol)] = verdict

    def observe_peak_excursion(self, peak: PeakExcursion) -> None:
        self._peaks[(peak.venue_id, peak.symbol)] = peak

    def observe_exposure(self, venue_id: str, symbol: str, notional: float) -> None:
        self._exposure[(venue_id, symbol)] = notional

    def observe_book_value(self, book_value: float) -> None:
        """What the whole book is worth, so one symbol's share can be measured."""
        if book_value < 0:
            raise ValueError("a book cannot be worth less than nothing")
        self._book_value = book_value

    def symbol_share(self, venue_id: str, symbol: str) -> float | None:
        if self._book_value <= 0:
            return None
        return self._exposure.get((venue_id, symbol), 0.0) / self._book_value

    def select(self, position) -> tuple[FollowCandidate | None, str]:
        """One held position, judged as something to add to rather than to hold."""
        self.standing.positions_examined += 1
        key = (position.venue_id, position.symbol)

        direction = LONG if position.quantity > 0 else SHORT

        verdict = self._verdicts.get(key)
        if verdict is None or not verdict.is_conclusive:
            return None, self._reject(NO_VERDICT)
        # A pair that settled on neither side settled the question -- it found no
        # directional edge -- and that is a reason not to add rather than a reason
        # to wait. Kept apart from "no verdict yet" because the two say opposite
        # things about whether more evidence is coming.
        if verdict.winning_side is None:
            return None, self._reject(SETTLED_ON_NEITHER_SIDE)
        if verdict.winning_side != direction:
            return None, self._reject(NOT_THE_WINNING_SIDE)

        peak = self._peaks.get(key)
        if peak is None:
            return None, self._reject(NO_PEAK_RECORD)

        notional = abs(position.quantity) * position.average_entry_price
        if notional <= 0:
            return None, self._reject(NO_COST_BASIS)

        peak_fraction = peak.best_unrealised / notional
        current_fraction = peak.current_unrealised / notional
        retraced_fraction = (
            max(0.0, (peak_fraction - current_fraction) / peak_fraction)
            if peak_fraction > 0
            else 0.0
        )

        if current_fraction < self._minimum_profit:
            return None, self._reject(NOT_IN_PROFIT)

        if retraced_fraction > self._maximum_retraced:
            self.standing.largest_retracement_refused = max(
                self.standing.largest_retracement_refused, retraced_fraction
            )
            return None, self._reject(GIVING_PROFIT_BACK)

        share = self.symbol_share(*key)
        if share is not None and share > self._maximum_share:
            self.standing.concentration_refusals += 1
            return None, self._reject(ALREADY_CONCENTRATED)

        self.standing.selected += 1
        return (
            FollowCandidate(
                bot=BOT,
                source=FROM_OUR_OWN_WINNER,
                venue_id=position.venue_id,
                symbol=position.symbol,
                direction=direction,
                move_so_far=current_fraction,
                move_normal=peak_fraction,
                observations_in_move=peak.samples,
                entry_cost_fraction=None,
                setup_weight=self._default_weight,
                detector=PART_ID,
                evidence={
                    "peak_fraction": peak_fraction,
                    "current_fraction": current_fraction,
                    "retraced_fraction": retraced_fraction,
                    "symbol_share_of_book": share,
                    "pair_verdict": verdict.reason,
                },
                reason=(
                    f"{position.symbol} is held on the {verdict.winning_side} side, which "
                    f"is the side a resolved pair found for "
                    f"({verdict.reason}), up {current_fraction:.2%} against a peak of "
                    f"{peak_fraction:.2%} -- {retraced_fraction:.0%} given back, "
                    f"inside the {self._maximum_retraced:.0%} this bot will add through"
                    + (
                        f"; this symbol holds {share:.0%} of the book, inside the "
                        f"{self._maximum_share:.0%} ceiling that keeps the most reliable "
                        f"signal from becoming the only position"
                        if share is not None
                        else "; the book's value is unknown, so no share could be checked"
                    )
                ),
                qualified_at_ns=self._now_ns(),
            ),
            SELECTED,
        )

    def select_all(self, positions) -> tuple[FollowCandidate, ...]:
        selected = []
        for position in positions:
            candidate, _ = self.select(position)
            if candidate is not None:
                selected.append(candidate)
        return tuple(selected)

    def _reject(self, reason: str) -> str:
        self.standing.by_rejection[reason] = self.standing.by_rejection.get(reason, 0) + 1
        return reason


def describe_winner_selection(selector: TailWinnerSelector) -> dict:
    return {
        "part_id": PART_ID,
        "positions_examined": selector.standing.positions_examined,
        "selected": selector.standing.selected,
        "rejected_by_reason": dict(selector.standing.by_rejection),
        "refused_for_concentration": selector.standing.concentration_refusals,
        "largest_retracement_refused": selector.standing.largest_retracement_refused,
        "verdicts_held": len(selector._verdicts),
        "verdicts_without_an_instrument": selector.standing.verdicts_without_an_instrument,
    }


def run_tail_winner_selector(
    selector: TailWinnerSelector, control_socket, read_positions_and_verdicts,
    publish_follow_candidates, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        positions = read_positions_and_verdicts(selector)
        publish_follow_candidates(selector.select_all(positions))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_winner_selection(selector),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch, LatestByKey
    from runtime.venues.venue_adapter import NormalisedTrade

    positions = LatestByKey(read=context.bus.reader("position"), key_of=lambda p: (p.venue_id, p.symbol))
    trades = Batch(read=context.bus.reader("market-data"))
    peaks = Batch(read=context.bus.reader("peak-excursion"))
    verdicts = Batch(read=context.bus.reader("pair-verdict"))
    publish_follow_candidates = context.bus.publisher_for("follow-candidate")
    selector = TailWinnerSelector(
        maximum_retraced_fraction=context.number("tail_winner_maximum_retraced_fraction"),
        maximum_symbol_share_of_book=context.number("tail_winner_maximum_symbol_share_of_book"),
        minimum_profit_fraction=context.number("tail_winner_minimum_profit_fraction"),
        default_setup_weight=context.number("tail_default_setup_weight"),
    )

    def read_positions_and_verdicts(_selector):
        for peak in peaks.payloads():
            selector.observe_peak_excursion(peak)
        for verdict in verdicts.payloads():
            selector.observe_pair_verdict(verdict)
        trades.payloads()
        held = [p for p in positions.mapping().values() if not p.is_flat]
        book_value = 0.0
        for position in held:
            notional = abs(position.quantity) * position.average_entry_price
            selector.observe_exposure(position.venue_id, position.symbol, notional)
            book_value += notional
        selector.observe_book_value(book_value)
        return tuple(held)

    def publish(items) -> None:
        if items:
            publish_follow_candidates(items)

    return run_tail_winner_selector(
        selector=selector,
        control_socket=context.control_socket,
        read_positions_and_verdicts=read_positions_and_verdicts,
        publish_follow_candidates=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
