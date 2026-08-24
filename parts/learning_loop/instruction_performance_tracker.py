"""instruction-performance-tracker: what each learned instruction has actually produced.

The scoreboard for everything the hypothesis block writes. An instruction that is
never scored is a belief, and a system full of unscored beliefs converges on
whichever ones were written most recently.

Three things it measures that a naive tracker misses:

- **Live against replay.** An instruction that performed well in backtest and
  badly live has a gap, and the gap is the measurement -- not the live number
  alone. A large gap means the backtest was optimistic in a way that will repeat,
  and that is worth knowing before the next instruction is written the same way.
- **Expectancy, not just hit rate.** An instruction right 70% of the time that
  loses more on its misses than it makes on its hits is a losing instruction, and
  a hit-rate-only tracker calls it the best one.
- **Trades since the last regime break, separately.** An instruction's record
  spanning a regime change is two records, and reporting their average describes
  neither.

**Costs are included.** An instruction's edge before costs is not an edge, and
the desk pays the costs.

**Nothing is retired here.** This measures; retirement is a decision belonging to
the part whose job that is, with the edge half-life and the falsification
criterion in front of it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, RateEstimator
from runtime.learning_types import InstructionScorecard
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "instruction-performance-tracker"

PART_DECLARATION = PartDeclaration(
    part_id="instruction-performance-tracker",
    consumes=("trade-episode", "opportunity-instruction"),
    produces=("instruction-scorecard", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

LIVE = "live"
REPLAY = "replay"


@dataclass
class InstructionRecord:
    hit_rate: RateEstimator
    trades: int = 0
    wins: int = 0
    realised: float = 0.0
    wins_total: float = 0.0
    losses_total: float = 0.0
    replay_trades: int = 0
    replay_realised: float = 0.0
    trades_since_break: int = 0
    realised_since_break: float = 0.0


@dataclass
class TrackerStanding:
    instructions_tracked: int = 0
    trades_recorded: int = 0
    replay_trades_recorded: int = 0
    regime_breaks_seen: int = 0
    instructions_with_a_live_replay_gap: int = 0
    largest_gap_seen: float | None = None


class InstructionPerformanceTracker:
    """Scores every instruction on what it has produced, live and in replay."""

    def __init__(
        self,
        prior_hit_rate: float,
        prior_weight: float,
        half_life_observations: float,
        minimum_trades: int,
        gap_threshold: float,
        now_ns=time.time_ns,
    ) -> None:
        if gap_threshold <= 0:
            raise ValueError(
                "with no threshold every instruction has a gap, and the measurement stops "
                "distinguishing anything"
            )
        self._prior_hit_rate = prior_hit_rate
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._minimum = minimum_trades
        self._gap_threshold = gap_threshold
        self._now_ns = now_ns
        self._records: dict[str, InstructionRecord] = {}
        self.standing = TrackerStanding()

    def observe_trade(
        self, instruction_id: str, was_win: bool, realised_after_costs: float, source: str = LIVE
    ) -> None:
        """One resolved trade this instruction produced, after costs.

        After costs because an instruction's edge before them is not an edge and
        the desk pays them.
        """
        record = self._record_for(instruction_id)
        if source == REPLAY:
            record.replay_trades += 1
            record.replay_realised += realised_after_costs
            self.standing.replay_trades_recorded += 1
            return

        record.hit_rate.observe(was_win)
        record.trades += 1
        record.wins += 1 if was_win else 0
        record.realised += realised_after_costs
        record.trades_since_break += 1
        record.realised_since_break += realised_after_costs
        if realised_after_costs >= 0:
            record.wins_total += realised_after_costs
        else:
            record.losses_total += -realised_after_costs
        self.standing.trades_recorded += 1
        self.standing.instructions_tracked = len(self._records)

    def observe_regime_break(self) -> None:
        """A record spanning a regime change is two records; the average describes neither."""
        self.standing.regime_breaks_seen += 1
        for record in self._records.values():
            record.trades_since_break = 0
            record.realised_since_break = 0.0

    def expectancy(self, instruction_id: str) -> float | None:
        """Average result per trade, which is what a hit rate alone cannot say.

        An instruction right 70% of the time that loses more on its misses than
        it makes on its hits is a losing instruction, and a hit-rate-only
        tracker calls it the best one.
        """
        record = self._records.get(instruction_id)
        if record is None or record.trades == 0:
            return None
        return record.realised / record.trades

    def live_versus_replay_gap(self, instruction_id: str) -> float | None:
        """How much worse it has been live than in replay.

        The gap is the measurement, not the live number alone: a large one means
        the backtest was optimistic in a way that will repeat.
        """
        record = self._records.get(instruction_id)
        if record is None or record.trades == 0 or record.replay_trades == 0:
            return None
        live = record.realised / record.trades
        replay = record.replay_realised / record.replay_trades
        return replay - live

    def score(self, instruction_id: str) -> InstructionScorecard:
        record = self._record_for(instruction_id)
        hit_rate = record.hit_rate.estimate(self._minimum)
        expectancy = self.expectancy(instruction_id)
        gap = self.live_versus_replay_gap(instruction_id)

        if gap is not None and gap > self._gap_threshold:
            self.standing.instructions_with_a_live_replay_gap += 1
            if self.standing.largest_gap_seen is None or gap > self.standing.largest_gap_seen:
                self.standing.largest_gap_seen = gap

        return InstructionScorecard(
            instruction_id=instruction_id,
            trades=record.trades,
            wins=record.wins,
            realised=record.realised,
            hit_rate=hit_rate,
            expectancy=expectancy,
            live_versus_replay_gap=gap,
            is_measured=hit_rate.is_fitted,
            reason=(
                f"{record.wins} of {record.trades} trade(s) after costs, "
                + (
                    f"expectancy {expectancy:+.4%} per trade"
                    if expectancy is not None
                    else "no expectancy yet"
                )
                + (
                    f"; {record.trades_since_break} of them since the last regime break"
                    if self.standing.regime_breaks_seen
                    else ""
                )
                + (
                    f"; replay was {gap:+.4%} per trade better than live, which says the "
                    f"backtest was optimistic in a way that will repeat"
                    if gap is not None and gap > self._gap_threshold
                    else ""
                )
                + (
                    ". A hit rate alone would call an instruction that wins often and loses "
                    "big the best one"
                    if expectancy is not None and expectancy < 0 and hit_rate.value > 0.5
                    else ""
                )
            ),
            scored_at_ns=self._now_ns(),
        )

    def score_all(self) -> tuple:
        return tuple(self.score(instruction_id) for instruction_id in sorted(self._records))

    def _record_for(self, instruction_id: str) -> InstructionRecord:
        record = self._records.get(instruction_id)
        if record is None:
            record = InstructionRecord(
                hit_rate=RateEstimator(
                    prior=self._prior_hit_rate, prior_weight=self._prior_weight,
                    half_life_observations=self._half_life,
                )
            )
            self._records[instruction_id] = record
            self.standing.instructions_tracked = len(self._records)
        return record


def describe_instruction_performance(tracker: InstructionPerformanceTracker) -> dict:
    return {
        "part_id": PART_ID,
        "instructions_tracked": tracker.standing.instructions_tracked,
        "trades_recorded": tracker.standing.trades_recorded,
        "replay_trades_recorded": tracker.standing.replay_trades_recorded,
        "regime_breaks_seen": tracker.standing.regime_breaks_seen,
        "instructions_with_a_live_replay_gap": tracker.standing.instructions_with_a_live_replay_gap,
        "largest_gap_seen": tracker.standing.largest_gap_seen,
        "retires_anything": False,
    }


def run_instruction_performance_tracker(
    tracker: InstructionPerformanceTracker, control_socket, read_episodes, publish_scorecards,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_episodes(tracker)
        publish_scorecards(tracker.score_all())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_instruction_performance(tracker),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A closed trade's episode names the detector that raised it; when that
    detector is a learned instruction -- one the writer has published -- the
    trade is observed against the instruction. Scorecards go out once per
    health interval.
    """
    import time as _time

    from runtime.input_assembly import Batch

    episodes = Batch(read=context.bus.reader("trade-episode"))
    instructions = Batch(read=context.bus.reader("opportunity-instruction"))
    publish_scorecards = context.bus.publisher_for("instruction-scorecard")
    tracker = InstructionPerformanceTracker(
        prior_hit_rate=context.number("learning_prior_hit_rate"),
        prior_weight=context.number("learning_prior_weight"),
        half_life_observations=context.number("learning_half_life_observations"),
        minimum_trades=int(context.number("learning_minimum_observations")),
        gap_threshold=context.number("instruction_replay_gap_threshold"),
    )
    known_instructions: set[str] = set()
    last_publish = [float("-inf")]

    def read_episodes(_tracker) -> None:
        for instruction in instructions.payloads():
            known_instructions.add(instruction.instruction_id)
        for episode in episodes.payloads():
            if episode.detector in known_instructions:
                tracker.observe_trade(episode.detector, episode.realised > 0, episode.realised)

    def tick() -> None:
        read_episodes(tracker)
        now = _time.monotonic()
        if now - last_publish[0] < context.health_interval_seconds:
            return
        cards = tracker.score_all()
        if cards:
            publish_scorecards(cards)
        last_publish[0] = now

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
    )
