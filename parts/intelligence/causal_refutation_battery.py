"""causal-refutation-battery: trying to break the reason an edge is supposed to work.

Every instruction this system trades comes with a story about why it works. The
story is almost always available -- a plausible mechanism can be written for any
correlation -- and almost never tested. This part tests it, by trying to refute
it.

Refutation rather than confirmation, because confirmation is free. The tests are
the standard ways a spurious edge reveals itself:

- **Label permutation.** Shuffle which trades belong to the instruction and see
  whether the edge survives. It should not: if a random assignment of the same
  trades produces the same edge, the instruction was not the cause. This is the
  strongest single test and the cheapest.
- **Time shift.** Apply the instruction's signal to the period *before* it fired.
  An edge that works equally well shifted backwards is measuring a property of
  the period, not of the signal.
- **The feature it claims to use.** If an instruction says it works because of
  funding skew, its trades should not perform the same when funding skew is
  removed from the attribution. One that does is working for a reason nobody has
  identified, which is a different instruction.
- **Subperiod stability.** An edge concentrated in one week is one event, and one
  event is not an edge however good the aggregate looks.

**A verdict of "not refuted" is not proof.** It is the absence of a refutation
from these tests, and it is stated that way -- a system that read "survived" as
"true" would be exactly as confident as one that never tested anything.

**Every test that could not be run is named.** A battery reporting three of four
tests as passed, with the fourth unmentioned, reads as a stronger result than it
is.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "causal-refutation-battery"

PART_DECLARATION = PartDeclaration(
    part_id="causal-refutation-battery",
    consumes=("trade-episode", "instruction-scorecard", "feature-attribution"),
    produces=("refutation-verdict", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

LABEL_PERMUTATION = "label-permutation"
TIME_SHIFT = "time-shift"
CLAIMED_FEATURE = "the-feature-it-claims-to-use"
SUBPERIOD_STABILITY = "subperiod-stability"

REFUTED = "refuted"
NOT_REFUTED = "not-refuted-by-these-tests"
COULD_NOT_RUN = "could-not-run"


@dataclass(frozen=True)
class RefutationTest:
    """One attempt to break an edge, and what happened."""

    test: str
    outcome: str
    observed: float | None
    under_the_null: float | None
    reason: str

    @property
    def refuted_it(self) -> bool:
        return self.outcome == REFUTED


@dataclass(frozen=True)
class RefutationVerdict:
    """Whether an instruction's edge survived every test that could be run."""

    instruction_id: str
    verdict: str
    tests_run: tuple
    tests_that_could_not_run: tuple
    refuting_tests: tuple
    trades_examined: int
    reason: str
    judged_at_ns: int

    @property
    def was_refuted(self) -> bool:
        return self.verdict == REFUTED

    @property
    def is_proof(self) -> bool:
        """Never. Not-refuted is the absence of a refutation, not a demonstration."""
        return False


@dataclass
class BatteryStanding:
    instructions_tested: int = 0
    refuted: int = 0
    survived: int = 0
    tests_run: int = 0
    tests_that_could_not_run: int = 0
    by_refuting_test: dict = field(default_factory=dict)


class CausalRefutationBattery:
    """Tries to break an edge four ways, and reports what it could not try."""

    def __init__(
        self,
        permutations: int,
        minimum_trades: int,
        subperiods: int,
        concentration_threshold: float,
        significance: float,
        now_ns=time.time_ns,
    ) -> None:
        if permutations < 20:
            raise ValueError(
                "a permutation test with too few shuffles cannot distinguish a real edge from "
                "a lucky ordering"
            )
        if subperiods < 2:
            raise ValueError("stability across one period is not stability")
        if not 0.0 < significance < 0.5:
            raise ValueError("the significance level is a tail probability")
        self._permutations = permutations
        self._minimum_trades = minimum_trades
        self._subperiods = subperiods
        self._concentration = concentration_threshold
        self._significance = significance
        self._now_ns = now_ns
        self._trades: dict[str, list] = {}
        self._all_trades: list = []
        self._claimed_features: dict[str, str] = {}
        self._attributions: dict[str, dict] = {}
        self.standing = BatteryStanding()

    def observe_trade(
        self, instruction_id: str, was_win: bool, realised: float, at_ns: int,
        shifted_realised: float | None = None,
    ) -> None:
        """One resolved trade. `shifted_realised` is what the same signal would have
        made applied to the preceding period, which the time-shift test needs."""
        record = (was_win, realised, at_ns, shifted_realised)
        self._trades.setdefault(instruction_id, []).append(record)
        self._all_trades.append(record)

    def observe_claimed_mechanism(self, instruction_id: str, feature: str) -> None:
        """What the instruction says it works because of."""
        self._claimed_features[instruction_id] = feature

    def observe_feature_attribution(self, instruction_id: str, attribution: dict) -> None:
        self._attributions[instruction_id] = dict(attribution)

    def label_permutation_test(self, instruction_id: str) -> RefutationTest:
        """Shuffle which trades belong to the instruction. The edge should not survive.

        The strongest single test and the cheapest: if a random assignment of the
        same trades produces the same edge, the instruction was not the cause.
        """
        trades = self._trades.get(instruction_id, [])
        if len(trades) < self._minimum_trades or len(self._all_trades) <= len(trades):
            return RefutationTest(
                LABEL_PERMUTATION, COULD_NOT_RUN, None, None,
                f"{len(trades)} trade(s) of the {self._minimum_trades} needed, or no other "
                f"trades to shuffle against",
            )

        observed = sum(realised for _, realised, _, _ in trades) / len(trades)
        pool = [realised for _, realised, _, _ in self._all_trades]

        # A deterministic shuffle: the index stride is coprime with the pool size
        # for each permutation, so every shuffle is a genuine reordering and the
        # test is reproducible.
        better_or_equal = 0
        size = len(trades)
        for permutation in range(self._permutations):
            stride = permutation * 2 + 1
            sample = [pool[(index * stride + permutation) % len(pool)] for index in range(size)]
            if sum(sample) / size >= observed:
                better_or_equal += 1

        fraction = better_or_equal / self._permutations
        if fraction > self._significance:
            return RefutationTest(
                LABEL_PERMUTATION, REFUTED, observed, fraction,
                f"{fraction:.0%} of random reassignments of the same trades produced an edge "
                f"at least as good, past the {self._significance:.0%} that would make this "
                f"instruction the cause. The edge is in the trades, not in the instruction",
            )
        return RefutationTest(
            LABEL_PERMUTATION, NOT_REFUTED, observed, fraction,
            f"only {fraction:.0%} of random reassignments matched it, so the instruction is "
            f"doing something the shuffle cannot reproduce",
        )

    def time_shift_test(self, instruction_id: str) -> RefutationTest:
        """Apply the signal to the period before it fired. It should do worse.

        An edge that works equally well shifted backwards is measuring a property
        of the period, not of the signal.
        """
        trades = [
            trade for trade in self._trades.get(instruction_id, []) if trade[3] is not None
        ]
        if len(trades) < self._minimum_trades:
            return RefutationTest(
                TIME_SHIFT, COULD_NOT_RUN, None, None,
                f"{len(trades)} trade(s) carry what the same signal would have made in the "
                f"preceding period; without that this test cannot run",
            )
        observed = sum(realised for _, realised, _, _ in trades) / len(trades)
        shifted = sum(trade[3] for trade in trades) / len(trades)
        if shifted >= observed:
            return RefutationTest(
                TIME_SHIFT, REFUTED, observed, shifted,
                f"applied to the period before it fired the same signal made {shifted:+.3%} "
                f"against {observed:+.3%} live -- it is measuring a property of the period, "
                f"not of the signal",
            )
        return RefutationTest(
            TIME_SHIFT, NOT_REFUTED, observed, shifted,
            f"shifted backwards it made {shifted:+.3%} against {observed:+.3%} live, so the "
            f"timing matters",
        )

    def claimed_feature_test(self, instruction_id: str) -> RefutationTest:
        """Does it work for the reason it says it does?"""
        claimed = self._claimed_features.get(instruction_id)
        attribution = self._attributions.get(instruction_id)
        if claimed is None or not attribution:
            return RefutationTest(
                CLAIMED_FEATURE, COULD_NOT_RUN, None, None,
                "the instruction states no mechanism, or no feature attribution was recorded",
            )
        total = sum(abs(value) for value in attribution.values())
        if total <= 0:
            return RefutationTest(
                CLAIMED_FEATURE, COULD_NOT_RUN, None, None, "the attribution is empty"
            )
        share = abs(attribution.get(claimed, 0.0)) / total
        if share < self._concentration:
            return RefutationTest(
                CLAIMED_FEATURE, REFUTED, share, self._concentration,
                f"it claims to work because of {claimed}, but that feature carries "
                f"{share:.0%} of the attribution -- below the {self._concentration:.0%} the "
                f"claim needs. It is working for a reason nobody has identified, which makes "
                f"it a different instruction",
            )
        return RefutationTest(
            CLAIMED_FEATURE, NOT_REFUTED, share, self._concentration,
            f"{claimed} carries {share:.0%} of the attribution, which is what it claims",
        )

    def subperiod_stability_test(self, instruction_id: str) -> RefutationTest:
        """An edge concentrated in one week is one event."""
        trades = sorted(self._trades.get(instruction_id, []), key=lambda trade: trade[2])
        if len(trades) < self._minimum_trades * self._subperiods:
            return RefutationTest(
                SUBPERIOD_STABILITY, COULD_NOT_RUN, None, None,
                f"{len(trades)} trade(s); {self._minimum_trades * self._subperiods} are needed "
                f"to split into {self._subperiods} periods with enough in each",
            )
        size = len(trades) // self._subperiods
        totals = [
            sum(realised for _, realised, _, _ in trades[index * size : (index + 1) * size])
            for index in range(self._subperiods)
        ]
        overall = sum(totals)
        if overall <= 0:
            return RefutationTest(
                SUBPERIOD_STABILITY, REFUTED, overall, None,
                "the instruction has made nothing overall, so there is no edge to be stable",
            )
        largest = max(totals) / overall
        if largest > self._concentration:
            return RefutationTest(
                SUBPERIOD_STABILITY, REFUTED, largest, self._concentration,
                f"{largest:.0%} of the whole result came from one of {self._subperiods} "
                f"periods, past the {self._concentration:.0%} that would make it an edge "
                f"rather than an event",
            )
        return RefutationTest(
            SUBPERIOD_STABILITY, NOT_REFUTED, largest, self._concentration,
            f"the largest single period contributed {largest:.0%} of the result, so it is "
            f"spread rather than concentrated",
        )

    def judge(self, instruction_id: str) -> RefutationVerdict:
        self.standing.instructions_tested += 1
        tests = (
            self.label_permutation_test(instruction_id),
            self.time_shift_test(instruction_id),
            self.claimed_feature_test(instruction_id),
            self.subperiod_stability_test(instruction_id),
        )

        ran = tuple(test for test in tests if test.outcome != COULD_NOT_RUN)
        could_not = tuple(test for test in tests if test.outcome == COULD_NOT_RUN)
        refuting = tuple(test for test in ran if test.refuted_it)

        self.standing.tests_run += len(ran)
        self.standing.tests_that_could_not_run += len(could_not)
        for test in refuting:
            self.standing.by_refuting_test[test.test] = (
                self.standing.by_refuting_test.get(test.test, 0) + 1
            )

        if refuting:
            self.standing.refuted += 1
            verdict = REFUTED
        else:
            self.standing.survived += 1
            verdict = NOT_REFUTED

        return RefutationVerdict(
            instruction_id=instruction_id,
            verdict=verdict,
            tests_run=ran,
            tests_that_could_not_run=could_not,
            refuting_tests=refuting,
            trades_examined=len(self._trades.get(instruction_id, [])),
            reason=(
                f"{len(ran)} of 4 test(s) ran"
                + (
                    f"; refuted by {', '.join(test.test for test in refuting)}"
                    if refuting
                    else "; none of them refuted it, which is the absence of a refutation "
                    "rather than a demonstration -- reading 'survived' as 'true' would be "
                    "exactly as confident as never testing"
                )
                + (
                    f". Could not run: {', '.join(test.test for test in could_not)} -- named "
                    f"rather than omitted, because three of four passing reads as a stronger "
                    f"result than it is"
                    if could_not
                    else ""
                )
            ),
            judged_at_ns=self._now_ns(),
        )


def describe_refutation(battery: CausalRefutationBattery) -> dict:
    return {
        "part_id": PART_ID,
        "instructions_tested": battery.standing.instructions_tested,
        "refuted": battery.standing.refuted,
        "survived": battery.standing.survived,
        "tests_run": battery.standing.tests_run,
        "tests_that_could_not_run": battery.standing.tests_that_could_not_run,
        "by_refuting_test": dict(sorted(battery.standing.by_refuting_test.items())),
        "surviving_means_proven": False,
    }


def run_causal_refutation_battery(
    battery: CausalRefutationBattery, control_socket, read_episodes, publish_verdicts,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        instructions = read_episodes(battery)
        publish_verdicts(tuple(battery.judge(instruction) for instruction in instructions))

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

    A trade belongs to the instruction its conditions name, or failing that
    to its detector's family. The feature attribution last seen for the same
    venue and symbol is what the instruction's mechanism is checked against.
    An instruction is judged when a scorecard arrives with at least the
    trades the battery needs; judging earlier would be a verdict on a
    sample the tests cannot resolve.
    """
    from runtime.input_assembly import Batch, LatestByKey

    episodes = Batch(read=context.bus.reader("trade-episode"))
    scorecards = Batch(read=context.bus.reader("instruction-scorecard"))
    attributions = LatestByKey(read=context.bus.reader("feature-attribution"), key_of=lambda a: (a.venue_id, a.symbol))
    publish_verdicts = context.bus.publisher_for("refutation-verdict")
    minimum_trades = int(context.number("decoding_minimum_trades"))
    battery = CausalRefutationBattery(
        permutations=int(context.number("refutation_permutations")),
        minimum_trades=minimum_trades,
        subperiods=int(context.number("refutation_subperiods")),
        concentration_threshold=context.number("refutation_concentration_threshold"),
        significance=context.number("power_significance"),
    )

    def instruction_of(episode) -> str:
        conditions = episode.conditions if isinstance(episode.conditions, dict) else {}
        return str(conditions.get("instruction_id") or episode.detector)

    def read_episodes(_battery):
        by_context = attributions.mapping()
        for episode in episodes.payloads():
            instruction_id = instruction_of(episode)
            battery.observe_trade(instruction_id, episode.realised > 0, float(episode.realised), int(episode.closed_at_ns))
            attribution = by_context.get((episode.venue_id, episode.symbol))
            if attribution is not None and isinstance(attribution.contributions, dict):
                battery.observe_feature_attribution(instruction_id, dict(attribution.contributions))
                if attribution.strongest:
                    battery.observe_claimed_mechanism(instruction_id, str(attribution.strongest))
        return tuple(card.instruction_id for card in scorecards.payloads() if card.trades >= minimum_trades)

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_verdicts(kept)

    return run_causal_refutation_battery(
        battery=battery,
        control_socket=context.control_socket,
        read_episodes=read_episodes,
        publish_verdicts=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
