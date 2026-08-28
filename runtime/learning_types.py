"""What the learning loop passes around, and the mistakes each type prevents.

Substrate, not a part. Fifteen learning-loop parts and eleven hypothesis parts
share these shapes and none may import another (T-4).

The types here encode the two mistakes that ruin a learning system:

**Learning from an outcome instead of from a label.** A closed trade's profit is
not a training label. Whether the setup was right, whether the exit was timed,
whether the size was appropriate, whether the market simply moved -- profit is
their sum, and a model trained on the sum learns whichever component happened to
dominate the sample. So `TrainingLabel` carries what it is a label *of*, and
`ExpectancyBreakdown` separates the components that profit conflates.

**Counting a result before knowing how many were tried.** A `Hypothesis` carries
the number of trials in its family and what would refute it, because a hypothesis
without either is a story about the past.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate

# What a label is a label of. Profit is the sum of these, and a model trained on
# the sum learns whichever component dominated the sample.
THE_SETUP_WAS_RIGHT = "the-setup-was-right"
THE_ENTRY_WAS_TIMED = "the-entry-was-timed"
THE_EXIT_WAS_TIMED = "the-exit-was-timed"
THE_SIZE_WAS_RIGHT = "the-size-was-right"

# Where a trade's result actually came from.
FROM_THE_SETUP = "the-setup"
FROM_ENTRY_TIMING = "entry-timing"
FROM_EXIT_TIMING = "exit-timing"
FROM_SIZING = "sizing"
FROM_COSTS = "costs"
FROM_THE_MARKET = "the-market-moving-anyway"


@dataclass(frozen=True)
class TrainingLabel:
    """One labelled example, and what it is a label of.

    `labels` is what this trade says about each component separately. A single
    boolean would make every model in the system learn the same conflated thing.
    """

    venue_id: str
    symbol: str
    detector: str
    regime: str
    labels: dict
    horizon_seconds: float
    seconds_to_resolve: float
    resolved_within_horizon: bool
    features: dict
    built_at_ns: int
    # When the claim this label judges was made. Defaulted because a label built
    # from a closed trade has the trade's own times; a label built from a signal
    # needs it, because the model has to find the feature vector that was current
    # when the detector spoke rather than the one current when the label arrived --
    # and by then the market has moved on, which is the whole point of the horizon.
    claimed_at_ns: int = 0
    # How far price actually travelled while this claim was open, as fractions of
    # the price at the moment it was made, signed towards what the claim said:
    # favourable is positive, adverse is negative.
    #
    # Carried rather than discarded because the claim already measured them on
    # every tick, and throwing them away was what left the system unable to place
    # a stop before its first trade had closed. `signal-excursion-profiler` turns
    # them into the distribution a stop and a target are set from
    # (docs/proposals/live-excursion-and-horizon-profiling.md).
    #
    # `direction` is here for the same reason: an excursion means the opposite
    # thing for a long and a short, so a profile keyed without it would average
    # the two into a number that describes neither.
    direction: str = ""
    best_favourable_fraction: float = 0.0
    worst_adverse_fraction: float = 0.0
    # The key the detector that made this claim calibrates on, carried back
    # unchanged so its estimator can be found again. Empty on a label built from
    # a closed trade, and that emptiness is load-bearing: `label-builder` sets
    # `THE_SETUP_WAS_RIGHT` too, from realised PnL after costs, and
    # `SignalCalibrator` must not be trained on it -- a trade sized badly,
    # entered late or stopped early is not evidence about the setup. A detector
    # takes only labels that carry a key, which says what it means rather than
    # relying on `claimed_at_ns` being non-zero.
    calibration_key: str = ""

    def label_for(self, component: str) -> bool | None:
        return self.labels.get(component)

    @property
    def is_complete(self) -> bool:
        return all(
            component in self.labels
            for component in (THE_SETUP_WAS_RIGHT, THE_ENTRY_WAS_TIMED, THE_EXIT_WAS_TIMED)
        )


@dataclass(frozen=True)
class SampleWeight:
    """How much one labelled example should count, and why.

    Weight is not a tuning knob. A trade from a regime that has ended, one whose
    label came from a partial fill, and one that resolved cleanly last week are
    different evidence, and a learner given them equally learns the average of
    three different things.
    """

    venue_id: str
    symbol: str
    weight: float
    age_seconds: float
    reasons: dict
    was_clamped: bool
    reason: str
    assigned_at_ns: int


@dataclass(frozen=True)
class ExpectancyBreakdown:
    """Where a strategy's expectancy actually came from.

    The decomposition exists because every number here can be positive while the
    total is negative, and the fix for each is different. A strategy whose setup
    is excellent and whose exits give it all back needs a new exit, not a new
    setup -- and expectancy alone says only that it loses money.
    """

    detector: str
    regime: str
    total_expectancy: float
    by_component: dict
    trades: int
    win_rate: Estimate
    average_win: float
    average_loss: float
    is_measured: bool
    reason: str
    decomposed_at_ns: int

    @property
    def largest_drag(self) -> tuple | None:
        """The component costing the most, which is where the fix belongs."""
        negatives = {
            name: value for name, value in self.by_component.items() if value < 0
        }
        if not negatives:
            return None
        name = min(negatives, key=lambda key: negatives[key])
        return name, negatives[name]

    @property
    def would_be_profitable_without_its_worst_component(self) -> bool:
        drag = self.largest_drag
        return drag is not None and self.total_expectancy - drag[1] > 0


@dataclass(frozen=True)
class Hypothesis:
    """One testable claim, with what would refute it and how many were tried.

    Both fields are required rather than optional. A hypothesis with no
    refutation criterion is a story about the past, and one whose trial count is
    unknown cannot have its significance judged.
    """

    hypothesis_id: str
    statement: str
    what_would_refute_it: str
    family: str
    trials_in_family: int
    source: str
    context: dict
    required_sample_size: int | None
    regime_tag: str | None
    novelty: float | None
    evidence: dict
    proposed_at_ns: int

    @property
    def is_testable(self) -> bool:
        return bool(self.what_would_refute_it.strip())

    @property
    def has_a_sample_size(self) -> bool:
        return self.required_sample_size is not None


@dataclass(frozen=True)
class OpportunityInstruction:
    """A hypothesis that survived, compiled into something the scanner can watch for.

    It carries its own retirement condition. An instruction that cannot expire
    outlives the market it was learned in, and nothing else in the system is
    positioned to notice.
    """

    instruction_id: str
    hypothesis_id: str
    measurement: str
    comparison: str
    threshold: float
    direction: str
    expectation: str
    horizon_seconds: float
    regime_tag: str | None
    retire_when: str
    required_sample_size: int | None
    trials_in_family: int
    evidence: dict
    reason: str
    written_at_ns: int


@dataclass(frozen=True)
class InstructionScorecard:
    """What an instruction has actually produced since it was written."""

    instruction_id: str
    trades: int
    wins: int
    realised: float
    hit_rate: Estimate
    expectancy: float | None
    live_versus_replay_gap: float | None
    is_measured: bool
    reason: str
    scored_at_ns: int

    @property
    def has_enough_evidence(self) -> bool:
        return self.hit_rate.is_fitted


@dataclass(frozen=True)
class ModelVersion:
    """One model artefact, with what justified promoting it."""

    model_name: str
    version: str
    role: str
    trained_on_windows: int
    validation_score: float
    refutation_verdict: str | None
    trials_in_family: int
    corrected_significance: float | None
    promoted: bool
    reason: str
    registered_at_ns: int

    @property
    def was_promoted_on_evidence(self) -> bool:
        return self.promoted and self.refutation_verdict is not None
