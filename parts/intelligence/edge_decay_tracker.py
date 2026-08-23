"""edge-decay-tracker: how long an edge lasts before it stops working.

Every edge this system finds decays. Someone else finds it, the flow that created
it changes, or it was never there. What separates a system that compounds from
one that gives it all back is knowing **which** -- and the only way to know is to
measure how each edge's performance ages.

The measurement is a half-life: the number of trades after which an
instruction's excess hit rate has fallen by half. That form is chosen because:

- **It is comparable across instructions.** A raw hit rate says one is better;
  a half-life says one will still be working next month and the other will not.
- **It separates decay from noise.** A hit rate that wobbles has no half-life; a
  hit rate that falls monotonically over successive blocks has a short one, and
  the fit is what tells them apart.
- **It is actionable.** An instruction with a half-life of forty trades and
  thirty-five behind it should be retired now, not when the record finally turns
  negative -- by which time it has been losing for a while.

**Excess over the system's own base rate, not raw.** A market getting easier
lifts every instruction's hit rate, and an edge measured raw would look durable
because everything was.

**An instruction with no measurable decay is reported as such**, and that is a
different statement from a long half-life. Most edges will be here for most of
their lives, and calling that "durable" is how a system convinces itself that
what has not yet decayed never will.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import linear_fit

PART_ID = "edge-decay-tracker"

PART_DECLARATION = PartDeclaration(
    part_id="edge-decay-tracker",
    consumes=("instruction-scorecard", "trade-episode"),
    produces=("edge-half-life", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

DECAYING = "decaying"
NOT_DECAYING = "no-decay-measurable-yet"
NEVER_HAD_AN_EDGE = "there-was-no-excess-to-decay"
TOO_FEW_BLOCKS = "too-few-blocks-of-trades-to-fit-a-decay"


@dataclass(frozen=True)
class EdgeHalfLife:
    """How long an instruction's excess edge lasts, in trades."""

    instruction_id: str
    state: str
    half_life_trades: float | None
    initial_excess: float | None
    current_excess: float | None
    trades_behind_it: int
    blocks_fitted: int
    slope: float | None
    fit_quality: float | None
    reason: str
    measured_at_ns: int

    @property
    def is_measured(self) -> bool:
        return self.state == DECAYING

    def trades_remaining(self) -> float | None:
        """How many more trades before the excess halves again."""
        if self.half_life_trades is None:
            return None
        return max(0.0, self.half_life_trades - self.trades_behind_it)

    @property
    def should_be_retired(self) -> bool:
        """Retire before the record turns negative, not after."""
        remaining = self.trades_remaining()
        return remaining is not None and remaining <= 0


@dataclass
class TrackerStanding:
    instructions_tracked: int = 0
    trades_recorded: int = 0
    measurements: int = 0
    decaying: int = 0
    never_had_an_edge: int = 0
    ready_to_retire: int = 0
    shortest_half_life_seen: float | None = None


class EdgeDecayTracker:
    """Fits how each instruction's excess over the base rate ages."""

    def __init__(
        self,
        block_size: int,
        minimum_blocks: int,
        minimum_initial_excess: float,
        now_ns=time.time_ns,
    ) -> None:
        if block_size < 5:
            raise ValueError(
                "a block of fewer than five trades has a hit rate that is mostly noise, and "
                "fitting decay through noise finds decay everywhere"
            )
        if minimum_blocks < 3:
            raise ValueError(
                "two points fit any line; distinguishing decay from a wobble needs three"
            )
        self._block_size = block_size
        self._minimum_blocks = minimum_blocks
        self._minimum_excess = minimum_initial_excess
        self._now_ns = now_ns
        self._outcomes: dict[str, list] = {}
        self._base_rate_outcomes: list = []
        self.standing = TrackerStanding()

    def observe_closed_trade(self, instruction_id: str, was_win: bool) -> None:
        """One resolved trade, in order. Order is the whole measurement."""
        self._outcomes.setdefault(instruction_id, []).append(1.0 if was_win else 0.0)
        self._base_rate_outcomes.append(1.0 if was_win else 0.0)
        self.standing.trades_recorded += 1
        self.standing.instructions_tracked = len(self._outcomes)

    def base_rate(self) -> float | None:
        """The system's own hit rate, which is what an excess is excess over.

        Raw hit rate would make every instruction look durable in a market that
        was getting easier.
        """
        if not self._base_rate_outcomes:
            return None
        return sum(self._base_rate_outcomes) / len(self._base_rate_outcomes)

    def blocks_for(self, instruction_id: str) -> list:
        """Successive blocks of trades, each with its excess over the base rate."""
        outcomes = self._outcomes.get(instruction_id, [])
        base = self.base_rate()
        if base is None:
            return []
        blocks = []
        for start in range(0, len(outcomes) - self._block_size + 1, self._block_size):
            block = outcomes[start : start + self._block_size]
            blocks.append(sum(block) / len(block) - base)
        return blocks

    def measure(self, instruction_id: str) -> EdgeHalfLife:
        self.standing.measurements += 1
        blocks = self.blocks_for(instruction_id)
        trades = len(self._outcomes.get(instruction_id, []))

        if len(blocks) < self._minimum_blocks:
            return self._half_life(
                instruction_id, TOO_FEW_BLOCKS, None, None, None, trades, len(blocks), None, None,
                f"{len(blocks)} block(s) of {self._block_size} trade(s), below the "
                f"{self._minimum_blocks} needed; two points fit any line",
            )

        initial = blocks[0]
        current = blocks[-1]

        if initial <= self._minimum_excess:
            self.standing.never_had_an_edge += 1
            return self._half_life(
                instruction_id, NEVER_HAD_AN_EDGE, None, initial, current, trades,
                len(blocks), None, None,
                f"the first block's excess over the base rate was {initial:+.1%}, at or below "
                f"the {self._minimum_excess:+.1%} that counts as an edge; there was nothing "
                f"to decay",
            )

        # Fit the excess against block index. A falling line is decay; a flat or
        # rising one is not, and the fit's own quality is what separates decay
        # from a wobble.
        points = [(float(index), excess) for index, excess in enumerate(blocks)]
        fit = linear_fit(points)
        if fit is None:
            return self._half_life(
                instruction_id, NOT_DECAYING, None, initial, current, trades, len(blocks),
                None, None, "the blocks do not vary enough to fit anything",
            )

        slope, intercept = fit
        quality = self._fit_quality(points, slope, intercept)

        if slope >= 0:
            return self._half_life(
                instruction_id, NOT_DECAYING, None, initial, current, trades, len(blocks),
                slope, quality,
                f"the excess is not falling ({slope:+.4f} per block of {self._block_size} "
                f"trades). That is not the same statement as a long half-life: most edges are "
                f"here for most of their lives, and calling that durable is how a system "
                f"convinces itself that what has not decayed never will",
            )

        # Half-life: the block count at which the fitted excess reaches half its
        # initial value, converted to trades.
        blocks_to_half = (initial / 2 - intercept) / slope if slope != 0 else None
        half_life = None if blocks_to_half is None else max(0.0, blocks_to_half * self._block_size)

        self.standing.decaying += 1
        if half_life is not None and (
            self.standing.shortest_half_life_seen is None
            or half_life < self.standing.shortest_half_life_seen
        ):
            self.standing.shortest_half_life_seen = half_life

        result = self._half_life(
            instruction_id, DECAYING, half_life, initial, current, trades, len(blocks),
            slope, quality,
            f"the excess has fallen from {initial:+.1%} to {current:+.1%} over {len(blocks)} "
            f"block(s), a slope of {slope:+.4f} per block with a fit quality of "
            f"{quality:.2f}. Half-life is {half_life:.0f} trade(s) and {trades} are behind it"
            + (
                " -- this should be retired now rather than when the record turns negative, "
                "by which time it has been losing for a while"
                if half_life is not None and trades >= half_life
                else ""
            ),
        )
        if result.should_be_retired:
            self.standing.ready_to_retire += 1
        return result

    def measure_all(self) -> tuple:
        return tuple(self.measure(instruction_id) for instruction_id in sorted(self._outcomes))

    def _fit_quality(self, points, slope: float, intercept: float) -> float:
        """R-squared: how much of the movement the line explains rather than noise."""
        mean = sum(value for _, value in points) / len(points)
        total = sum((value - mean) ** 2 for _, value in points)
        if total <= 0:
            return 0.0
        residual = sum((value - (slope * index + intercept)) ** 2 for index, value in points)
        return max(0.0, 1.0 - residual / total)

    def _half_life(
        self, instruction_id, state, half_life, initial, current, trades, blocks,
        slope, quality, reason,
    ) -> EdgeHalfLife:
        return EdgeHalfLife(
            instruction_id=instruction_id,
            state=state,
            half_life_trades=half_life,
            initial_excess=initial,
            current_excess=current,
            trades_behind_it=trades,
            blocks_fitted=blocks,
            slope=slope,
            fit_quality=quality,
            reason=reason,
            measured_at_ns=self._now_ns(),
        )


def describe_edge_decay(tracker: EdgeDecayTracker) -> dict:
    return {
        "part_id": PART_ID,
        "instructions_tracked": tracker.standing.instructions_tracked,
        "trades_recorded": tracker.standing.trades_recorded,
        "measurements": tracker.standing.measurements,
        "instructions_decaying": tracker.standing.decaying,
        "instructions_that_never_had_an_edge": tracker.standing.never_had_an_edge,
        "instructions_ready_to_retire": tracker.standing.ready_to_retire,
        "shortest_half_life_seen": tracker.standing.shortest_half_life_seen,
        "base_rate": tracker.base_rate(),
    }


def run_edge_decay_tracker(
    tracker: EdgeDecayTracker, control_socket, read_scorecards, publish_half_lives,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_scorecards(tracker)
        publish_half_lives(tracker.measure_all())

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

    A scorecard carries cumulative trades and wins; the difference from the
    last one seen for the same instruction is the closed trades since, fed
    as the wins then the losses it counted. Episodes that name their
    instruction in their conditions are observed one by one instead, which
    keeps their order; an instruction is tracked one way or the other,
    never both, so no trade counts twice.
    """
    from runtime.input_assembly import Batch

    scorecards = Batch(read=context.bus.reader("instruction-scorecard"))
    episodes = Batch(read=context.bus.reader("trade-episode"))
    publish_half_lives = context.bus.publisher_for("edge-half-life")
    tracker = EdgeDecayTracker(
        block_size=int(context.number("edge_decay_block_size")),
        minimum_blocks=int(context.number("edge_decay_minimum_blocks")),
        minimum_initial_excess=context.number("edge_decay_minimum_initial_excess"),
    )
    last_counts: dict[str, tuple[int, int]] = {}
    tracked_by_episode: set[str] = set()

    def read_scorecards(_tracker) -> None:
        for episode in episodes.payloads():
            conditions = episode.conditions if isinstance(episode.conditions, dict) else {}
            instruction_id = conditions.get("instruction_id")
            if instruction_id is None:
                continue
            tracked_by_episode.add(str(instruction_id))
            tracker.observe_closed_trade(str(instruction_id), episode.realised > 0)
        for card in scorecards.payloads():
            if card.instruction_id in tracked_by_episode:
                continue
            trades_before, wins_before = last_counts.get(card.instruction_id, (0, 0))
            new_trades, new_wins = card.trades - trades_before, card.wins - wins_before
            if new_trades <= 0:
                continue
            last_counts[card.instruction_id] = (card.trades, card.wins)
            for _ in range(max(0, new_wins)):
                tracker.observe_closed_trade(card.instruction_id, True)
            for _ in range(max(0, new_trades - max(0, new_wins))):
                tracker.observe_closed_trade(card.instruction_id, False)

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_half_lives(kept)

    return run_edge_decay_tracker(
        tracker=tracker,
        control_socket=context.control_socket,
        read_scorecards=read_scorecards,
        publish_half_lives=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
