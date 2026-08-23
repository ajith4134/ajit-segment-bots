"""sample-weight-assigner: how much each labelled example should count.

Weight is not a tuning knob. Three trades that all closed profitably can be
entirely different evidence -- one from a regime that ended, one whose label came
from a partial fill in a thin book, one that resolved cleanly yesterday -- and a
learner given them equally learns the average of three different things and
believes it has three observations of one.

Each reason is applied separately and reported separately, because they have
different fixes:

- **Age.** An example decays toward irrelevance as the market moves on. Halved by
  a half-life rather than cut off by a window: a cliff makes the model lurch
  whenever an example crosses it.
- **Fill quality.** A label derived from a partial or badly slipped fill is a
  label about execution as much as about the setup, and it should teach the
  conviction model less.
- **Regime.** An example from a regime that has broken describes a market that no
  longer exists. It is down-weighted rather than deleted, because it will matter
  again if that regime returns.
- **Rarity.** The rare classes are where a learner has least data and most to
  learn, and unweighted training makes the model excellent at the common case and
  useless at the one that costs money.

**Weights are capped.** An uncapped weight lets a single example dominate a batch,
and one badly labelled trade then moves the model more than a month of good ones.

**A weight of zero is never assigned.** Zero deletes the example, and deleting is
irreversible in a way down-weighting is not.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.learning_types import SampleWeight
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "sample-weight-assigner"

PART_DECLARATION = PartDeclaration(
    part_id="sample-weight-assigner",
    consumes=("training-label", "trade-episode"),
    produces=("sample-weight", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FOR_AGE = "age"
FOR_FILL_QUALITY = "fill-quality"
FOR_A_BROKEN_REGIME = "the-regime-it-came-from-has-broken"
FOR_RARITY = "class-rarity"
FOR_NOT_RESOLVING = "it-did-not-resolve-inside-its-horizon"


@dataclass
class AssignerStanding:
    weights_assigned: int = 0
    clamped_at_the_cap: int = 0
    raised_to_the_floor: int = 0
    from_broken_regimes: int = 0
    rare_classes_upweighted: int = 0
    largest_weight: float = 0.0
    smallest_weight: float | None = None
    by_reason: dict = field(default_factory=dict)


class SampleWeightAssigner:
    """Weighs each example by why it is more or less informative than the rest."""

    def __init__(
        self,
        half_life_seconds: float,
        minimum_weight: float,
        maximum_weight: float,
        partial_fill_multiple: float,
        broken_regime_multiple: float,
        unresolved_multiple: float,
        rarity_power: float,
        now_ns=time.time_ns,
    ) -> None:
        if minimum_weight <= 0:
            raise ValueError(
                "a weight of zero deletes the example, and deleting is irreversible in a way "
                "down-weighting is not"
            )
        if maximum_weight <= minimum_weight:
            raise ValueError(
                "an uncapped weight lets one badly labelled trade move the model more than a "
                "month of good ones"
            )
        if half_life_seconds <= 0:
            raise ValueError("age must decay over some time, or every example is equally fresh")
        for name, multiple in (
            ("partial fill", partial_fill_multiple),
            ("broken regime", broken_regime_multiple),
            ("unresolved", unresolved_multiple),
        ):
            if not 0.0 < multiple <= 1.0:
                raise ValueError(f"the {name} multiple reduces a weight and must be in (0, 1]")
        self._half_life = half_life_seconds
        self._minimum = minimum_weight
        self._maximum = maximum_weight
        self._partial_multiple = partial_fill_multiple
        self._broken_multiple = broken_regime_multiple
        self._unresolved_multiple = unresolved_multiple
        self._rarity_power = rarity_power
        self._now_ns = now_ns
        self._broken_regimes: set[str] = set()
        self._class_counts: dict[tuple[str, bool], int] = {}
        self.standing = AssignerStanding()

    def observe_regime_break(self, regime: str, has_broken: bool) -> None:
        if has_broken:
            self._broken_regimes.add(regime)
        else:
            self._broken_regimes.discard(regime)

    def observe_class(self, component: str, label: bool) -> None:
        """Count how common each class is, so the rare ones can be lifted."""
        self._class_counts[(component, label)] = self._class_counts.get((component, label), 0) + 1

    def rarity_multiple(self, component: str, label: bool) -> float:
        """How much to lift a class the learner has little of.

        The rare classes are where the learner has least data and most to learn;
        unweighted training makes it excellent at the common case and useless at
        the one that costs money.
        """
        this_class = self._class_counts.get((component, label), 0)
        other_class = self._class_counts.get((component, not label), 0)
        total = this_class + other_class
        if total == 0 or this_class == 0:
            return 1.0
        share = this_class / total
        return (0.5 / share) ** self._rarity_power

    def assign(
        self, label, fill_quality: float = 1.0, primary_component: str | None = None
    ) -> SampleWeight:
        """One label's weight, with each reason applied and reported separately."""
        self.standing.weights_assigned += 1
        reasons: dict[str, float] = {}

        age_seconds = max(0.0, (self._now_ns() - label.built_at_ns) / 1e9)
        # Halved by a half-life rather than cut off by a window: a cliff makes
        # the model lurch whenever an example crosses it.
        age_multiple = 0.5 ** (age_seconds / self._half_life)
        reasons[FOR_AGE] = age_multiple

        if fill_quality < 1.0:
            # A label from a partial or badly slipped fill is a label about
            # execution as much as about the setup.
            quality_multiple = self._partial_multiple + (1.0 - self._partial_multiple) * max(
                0.0, min(1.0, fill_quality)
            )
            reasons[FOR_FILL_QUALITY] = quality_multiple

        if label.regime in self._broken_regimes:
            # Down-weighted rather than deleted: it will matter again if that
            # regime returns.
            reasons[FOR_A_BROKEN_REGIME] = self._broken_multiple
            self.standing.from_broken_regimes += 1

        if not label.resolved_within_horizon:
            reasons[FOR_NOT_RESOLVING] = self._unresolved_multiple

        if primary_component is not None:
            value = label.label_for(primary_component)
            if value is not None:
                rarity = self.rarity_multiple(primary_component, value)
                if rarity > 1.0:
                    self.standing.rare_classes_upweighted += 1
                reasons[FOR_RARITY] = rarity

        weight = 1.0
        for multiple in reasons.values():
            weight *= multiple

        clamped = weight > self._maximum
        raised = weight < self._minimum
        if clamped:
            self.standing.clamped_at_the_cap += 1
        if raised:
            self.standing.raised_to_the_floor += 1
        weight = min(self._maximum, max(self._minimum, weight))

        self.standing.largest_weight = max(self.standing.largest_weight, weight)
        if self.standing.smallest_weight is None or weight < self.standing.smallest_weight:
            self.standing.smallest_weight = weight
        for reason in reasons:
            self.standing.by_reason[reason] = self.standing.by_reason.get(reason, 0) + 1

        return SampleWeight(
            venue_id=label.venue_id,
            symbol=label.symbol,
            weight=weight,
            age_seconds=age_seconds,
            reasons=reasons,
            was_clamped=clamped or raised,
            reason=(
                f"{weight:.3f} from "
                + ", ".join(f"{name} x{multiple:.3f}" for name, multiple in sorted(reasons.items()))
                + (
                    f"; clamped at the {self._maximum:.2f} cap so one badly labelled trade "
                    f"cannot move the model more than a month of good ones"
                    if clamped
                    else ""
                )
                + (
                    f"; held at the {self._minimum:.3f} floor -- zero would delete the example, "
                    f"and deleting is irreversible in a way down-weighting is not"
                    if raised
                    else ""
                )
            ),
            assigned_at_ns=self._now_ns(),
        )


def describe_sample_weights(assigner: SampleWeightAssigner) -> dict:
    return {
        "part_id": PART_ID,
        "weights_assigned": assigner.standing.weights_assigned,
        "clamped_at_the_cap": assigner.standing.clamped_at_the_cap,
        "raised_to_the_floor": assigner.standing.raised_to_the_floor,
        "examples_from_broken_regimes": assigner.standing.from_broken_regimes,
        "rare_class_examples_upweighted": assigner.standing.rare_classes_upweighted,
        "largest_weight": assigner.standing.largest_weight,
        "smallest_weight": assigner.standing.smallest_weight,
        "by_reason": dict(sorted(assigner.standing.by_reason.items())),
        "assigns_zero": False,
    }


def run_sample_weight_assigner(
    assigner: SampleWeightAssigner, control_socket, read_labels, publish_weights,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_weights(
            tuple(
                assigner.assign(label, fill_quality, component)
                for label, fill_quality, component in read_labels(assigner)
            )
        )

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
