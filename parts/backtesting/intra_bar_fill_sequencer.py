"""intra-bar-fill-sequencer: which came first, the stop or the target.

A bar records open, high, low and close. It does not record the path between them, so
when a trade's stop and target both sit inside one bar's range, **which one was hit
first is genuinely unknown**. This is not a small ambiguity: it decides whether the
trade was a win or a loss, and it occurs disproportionately on the volatile bars that
dominate a result.

A backtest that assumes the favourable order is not optimistic, it is wrong, and it
is wrong by an amount that grows with volatility -- meaning the strategies it flatters
most are the ones taking the most risk. So this part:

- **Resolves ambiguity adversely by default.** When both levels are inside the bar,
  the adverse one is assumed to have been reached first. That understates the result,
  which is the safe direction to be wrong in.
- **Says when the order is actually known.** If only one level is inside the range,
  there is no ambiguity. If tick data covers the bar, the true order is used and
  marked as known -- and this is why the tape is recorded in the first place.
- **Reports how often it had to guess.** A backtest where most trades resolved
  ambiguously is a backtest about this assumption rather than about the strategy, and
  that fact belongs in the result.

The direction of the bar is used as evidence, not as an answer: a bar that closed up
having opened down probably went down first, but "probably" is not knowledge, and the
part records the inference separately from the assumption.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.backtest_types import FillSequence
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "intra-bar-fill-sequencer"

PART_DECLARATION = PartDeclaration(
    part_id="intra-bar-fill-sequencer",
    consumes=("historical-window", "cost-estimate"),
    produces=("fill-sequence", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

KNOWN_FROM_TICKS = "the-tape-covers-this-bar-so-the-order-is-known"
ONLY_ONE_LEVEL_INSIDE = "only-one-level-is-inside-the-bar-so-there-is-no-ambiguity"
AMBIGUOUS = "both-levels-are-inside-the-bar-and-the-order-is-unknown"
NEITHER_INSIDE = "neither-level-was-reached-in-this-bar"

ADVERSE_FIRST = "adverse-first"
KNOWN = "known"


@dataclass(frozen=True)
class SequenceOutcome:
    venue_id: str
    symbol: str
    at_ns: int
    state: str
    sequence: FillSequence
    stop_first: bool | None
    inferred_from_the_bar_shape: str | None
    reason: str
    sequenced_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state != NEITHER_INSIDE


@dataclass
class SequencerStanding:
    bars_sequenced: int = 0
    known_from_ticks: int = 0
    unambiguous: int = 0
    ambiguous: int = 0
    neither_reached: int = 0
    times_the_favourable_order_was_assumed: int = 0

    @property
    def ambiguous_fraction(self) -> float:
        total = self.bars_sequenced
        return self.ambiguous / total if total else 0.0


class IntraBarFillSequencer:
    """Decides the order two levels were reached in, or admits it cannot."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._ticks: dict[tuple, list] = {}
        self.standing = SequencerStanding()

    def observe_ticks(self, venue_id: str, symbol: str, at_ns: int, prices) -> None:
        """The recorded tape for this bar. This is why the tape exists."""
        self._ticks[(venue_id, symbol, at_ns)] = list(prices)

    def sequence(
        self, venue_id: str, symbol: str, at_ns: int, bar, stop_price: float,
        target_price: float, is_long: bool,
    ) -> SequenceOutcome:
        self.standing.bars_sequenced += 1
        stop_inside = bar.low_price <= stop_price <= bar.high_price
        target_inside = bar.low_price <= target_price <= bar.high_price

        if not stop_inside and not target_inside:
            self.standing.neither_reached += 1
            return self._outcome(
                venue_id, symbol, at_ns, NEITHER_INSIDE, bar, (), True, KNOWN, None, None,
                "neither level is inside the bar's range, so nothing was reached",
            )

        if stop_inside != target_inside:
            self.standing.unambiguous += 1
            order = ("stop",) if stop_inside else ("target",)
            return self._outcome(
                venue_id, symbol, at_ns, ONLY_ONE_LEVEL_INSIDE, bar, order, True, KNOWN,
                stop_inside, None,
                f"only the {'stop' if stop_inside else 'target'} is inside the bar, so "
                f"there is no ambiguity to resolve",
            )

        ticks = self._ticks.get((venue_id, symbol, at_ns))
        if ticks:
            self.standing.known_from_ticks += 1
            for price in ticks:
                reached_stop = price <= stop_price if is_long else price >= stop_price
                reached_target = price >= target_price if is_long else price <= target_price
                if reached_stop or reached_target:
                    order = ("stop", "target") if reached_stop else ("target", "stop")
                    return self._outcome(
                        venue_id, symbol, at_ns, KNOWN_FROM_TICKS, bar, order, True,
                        KNOWN, order[0] == "stop", None,
                        f"the tape shows the {order[0]} was reached first. This is why the "
                        f"tape is recorded rather than reconstructed from bars",
                    )

        # Genuinely unknown. The bar's shape is evidence, not an answer.
        went_down_first = bar.close_price >= bar.open_price
        inferred = (
            "the bar closed up having opened lower, so it probably fell first"
            if went_down_first
            else "the bar closed down having opened higher, so it probably rose first"
        )
        self.standing.ambiguous += 1
        return self._outcome(
            venue_id, symbol, at_ns, AMBIGUOUS, bar, ("stop", "target"), False,
            ADVERSE_FIRST, True, inferred,
            f"both levels are inside the bar and the tape does not cover it. The adverse "
            f"level is assumed first, which understates the result -- the safe direction "
            f"to be wrong in, since assuming the favourable order is wrong by an amount "
            f"that grows with volatility. Bar shape suggests: {inferred}",
        )

    def _outcome(
        self, venue_id, symbol, at_ns, state, bar, order, is_known, assumption,
        stop_first, inferred, reason,
    ) -> SequenceOutcome:
        return SequenceOutcome(
            venue_id=venue_id, symbol=symbol, at_ns=at_ns, state=state,
            sequence=FillSequence(
                venue_id=venue_id, symbol=symbol, at_ns=at_ns,
                open_price=bar.open_price, high_price=bar.high_price,
                low_price=bar.low_price, close_price=bar.close_price, order=order,
                is_known=is_known, assumption=assumption, reason=reason,
            ),
            stop_first=stop_first, inferred_from_the_bar_shape=inferred, reason=reason,
            sequenced_at_ns=self._now_ns(),
        )


def describe_sequencing(sequencer: IntraBarFillSequencer) -> dict:
    return {
        "part_id": PART_ID,
        "bars_sequenced": sequencer.standing.bars_sequenced,
        "known_from_the_tape": sequencer.standing.known_from_ticks,
        "unambiguous": sequencer.standing.unambiguous,
        "ambiguous": sequencer.standing.ambiguous,
        "ambiguous_fraction": sequencer.standing.ambiguous_fraction,
        "neither_level_reached": sequencer.standing.neither_reached,
        "assumes_the_favourable_order": False,
        "times_the_favourable_order_was_assumed": (
            sequencer.standing.times_the_favourable_order_was_assumed
        ),
    }


def run_intra_bar_fill_sequencer(
    sequencer: IntraBarFillSequencer, control_socket, read_jobs, publish_sequences,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for job in read_jobs():
            outcome = sequencer.sequence(**job)
            if outcome.is_usable:
                publish_sequences(outcome.sequence)

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

    The order the stop and target are touched inside one bar is sequenced
    for every bar of a window, with the bar's own extremes as the stop and
    target -- the worst case for each side -- so the replayer is handed the
    order a bar's path implies. Cost estimates are consumed to wake the part.
    """
    from runtime.input_assembly import Batch

    windows = Batch(read=context.bus.reader("historical-window"))
    estimates = Batch(read=context.bus.reader("cost-estimate"))
    publish_sequences = context.bus.publisher_for("fill-sequence")
    sequencer = IntraBarFillSequencer()

    def read_jobs():
        estimates.payloads()
        jobs = []
        for window in windows.payloads():
            for bar in window.bars:
                jobs.append({
                    "venue_id": window.venue_id, "symbol": window.symbol, "at_ns": bar.at_ns, "bar": bar,
                    "stop_price": bar.low_price, "target_price": bar.high_price, "is_long": True,
                })
        return tuple(jobs)

    def publish(item) -> None:
        if item is not None:
            publish_sequences((item,))

    return run_intra_bar_fill_sequencer(
        sequencer=sequencer,
        control_socket=context.control_socket,
        read_jobs=read_jobs,
        publish_sequences=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
