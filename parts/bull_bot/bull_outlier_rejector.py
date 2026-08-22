"""bull-outlier-rejector: whether this vector looks like anything the model has seen.

A model is only a claim about the region it was trained on. Outside it a logistic
model does not fail loudly -- it extrapolates, confidently, and its most extreme
outputs are exactly where it is least entitled to them. The 2020 crash and every
exchange outage since produced feature values orders of magnitude outside normal,
and a model handed those returns a number rather than a refusal.

So this part exists to say **"I do not recognise this"**, and it is separate from
the model on purpose (T-1, T-6): the same judgement is needed by the bear bot and
the tailgater, and a model that policed its own inputs could not be replaced
without replacing the policing too.

The measure is per-feature distance in the feature's own decayed units. Not a
single joint distance -- one feature ten deviations out is the case that matters
and an average over twelve features hides it. The flag names **which** feature,
because "out of distribution" is not actionable and "funding is 14 deviations
from normal because the venue is settling" is.

A feature with too little history is **unjudgeable**, not normal. Counting it as
in-distribution would make a brand-new symbol -- the one nothing is known about --
the one that passes most easily.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import OutOfDistributionFlag
from runtime.online_learner import RunningMoments
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "bull-outlier-rejector"
BOT = "bull-bot"

PART_DECLARATION = PartDeclaration(
    part_id="bull-outlier-rejector",
    consumes=("bull-feature-vector",),
    produces=("bull-feature-out-of-distribution-flag", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)


@dataclass
class RejectorStanding:
    vectors_judged: int = 0
    flagged: int = 0
    unjudgeable_vectors: int = 0
    by_worst_feature: dict = field(default_factory=dict)
    largest_deviation_seen: float = 0.0


class BullOutlierRejector:
    """Learns what normal looks like per feature, and says when a vector is not it."""

    def __init__(
        self,
        deviation_threshold: float,
        minimum_observations: int,
        half_life_observations: float,
        maximum_unjudgeable_fraction: float,
        now_ns=time.time_ns,
    ) -> None:
        if deviation_threshold <= 0:
            raise ValueError("a threshold of zero flags every vector including the normal ones")
        if not 0.0 <= maximum_unjudgeable_fraction <= 1.0:
            raise ValueError("the unjudgeable fraction is a fraction of the vector's features")
        self._threshold = deviation_threshold
        self._minimum = minimum_observations
        self._half_life = half_life_observations
        self._maximum_unjudgeable = maximum_unjudgeable_fraction
        self._now_ns = now_ns
        self._moments: dict[str, RunningMoments] = {}
        self.standing = RejectorStanding()

    def observe_vector(self, vector) -> None:
        """Learn what normal is. Called on every vector, flagged or not.

        Including flagged ones: today's outlier is next month's normal, and a
        rejector that only learned from what it accepted would keep rejecting a
        regime that has already become the market.
        """
        for name, value in vector.features.items():
            self._moment_for(name).observe(value)

    def judge(self, vector) -> OutOfDistributionFlag:
        self.standing.vectors_judged += 1

        judged = 0
        unjudgeable = []
        worst_feature = None
        worst_deviation = 0.0

        for name, value in sorted(vector.features.items()):
            moments = self._moments.get(name)
            standardised = (
                None if moments is None else moments.standardise(value, self._minimum)
            )
            if standardised is None:
                unjudgeable.append(name)
                continue
            judged += 1
            distance = abs(standardised)
            if distance > worst_deviation:
                worst_deviation = distance
                worst_feature = name

        total = judged + len(unjudgeable)
        unjudgeable_fraction = len(unjudgeable) / total if total else 1.0

        if judged == 0:
            self.standing.unjudgeable_vectors += 1
            return self._flag(
                vector, True, None, None, judged, unjudgeable,
                "nothing in this vector has enough history to say what normal is, so it "
                "cannot be recognised; an unrecognisable vector is refused rather than "
                "waved through, because a brand-new symbol would otherwise pass most easily",
            )

        if unjudgeable_fraction > self._maximum_unjudgeable:
            self.standing.unjudgeable_vectors += 1
            self.standing.flagged += 1
            return self._flag(
                vector, True, worst_feature, worst_deviation, judged, unjudgeable,
                f"{len(unjudgeable)} of {total} features have no established normal "
                f"({unjudgeable_fraction:.0%}, above the {self._maximum_unjudgeable:.0%} "
                f"allowed); too much of this vector is unrecognised to judge the rest",
            )

        self.standing.largest_deviation_seen = max(
            self.standing.largest_deviation_seen, worst_deviation
        )

        if worst_deviation > self._threshold:
            self.standing.flagged += 1
            self.standing.by_worst_feature[worst_feature] = (
                self.standing.by_worst_feature.get(worst_feature, 0) + 1
            )
            return self._flag(
                vector, True, worst_feature, worst_deviation, judged, unjudgeable,
                f"{worst_feature} is {worst_deviation:.1f} deviations from its own normal, "
                f"past the {self._threshold:.1f} this bot recognises; the model would "
                f"extrapolate rather than fail, and its most extreme output would come "
                f"from exactly the region it knows least",
            )

        return self._flag(
            vector, False, worst_feature, worst_deviation, judged, unjudgeable,
            f"the furthest feature is {worst_feature} at {worst_deviation:.1f} deviations, "
            f"inside the {self._threshold:.1f} this bot recognises, over {judged} judged "
            f"feature(s)",
        )

    def _flag(
        self, vector, is_out, worst_feature, worst_deviation, judged, unjudgeable, reason
    ) -> OutOfDistributionFlag:
        return OutOfDistributionFlag(
            bot=BOT,
            venue_id=vector.venue_id,
            symbol=vector.symbol,
            is_out_of_distribution=is_out,
            worst_feature=worst_feature,
            worst_deviation=worst_deviation,
            features_judged=judged,
            features_unjudgeable=tuple(unjudgeable),
            reason=reason,
            flagged_at_ns=self._now_ns(),
        )

    def _moment_for(self, name: str) -> RunningMoments:
        moments = self._moments.get(name)
        if moments is None:
            moments = RunningMoments(half_life_observations=self._half_life)
            self._moments[name] = moments
        return moments

    def normal_range(self, name: str) -> tuple[float, float] | None:
        """What this rejector currently considers normal for one feature."""
        moments = self._moments.get(name)
        if moments is None or moments.count < self._minimum or moments.deviation <= 0:
            return None
        return (
            moments.mean - self._threshold * moments.deviation,
            moments.mean + self._threshold * moments.deviation,
        )


def describe_rejection(rejector: BullOutlierRejector) -> dict:
    return {
        "part_id": PART_ID,
        "vectors_judged": rejector.standing.vectors_judged,
        "flagged_out_of_distribution": rejector.standing.flagged,
        "unjudgeable_vectors": rejector.standing.unjudgeable_vectors,
        "flagged_by_worst_feature": dict(sorted(rejector.standing.by_worst_feature.items())),
        "largest_deviation_seen": rejector.standing.largest_deviation_seen,
        "features_with_a_learned_normal": len(rejector._moments),
    }


def run_bull_outlier_rejector(
    rejector: BullOutlierRejector, control_socket, read_vectors, publish_flags,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        flags = []
        for vector in read_vectors():
            flags.append(rejector.judge(vector))
            rejector.observe_vector(vector)
        publish_flags(tuple(flags))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
