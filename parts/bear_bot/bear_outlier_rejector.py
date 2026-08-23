"""bear-outlier-rejector: whether this short's evidence resembles anything seen before.

Same job as its bull counterpart and a different threshold, because the two sides
are not exposed to the tail in the same way.

A long that meets an unrecognised market loses what it put in. A short that meets
one has no ceiling on what it loses, and the distributions that produce squeezes
are exactly the ones no model has enough of to have learned: they are rare, they
are fast, and their feature values sit far outside anything in the training
window. So this rejector is configured to be **harder to satisfy** than the bull's
-- refusing a good short costs a trade, and taking a short into an unrecognised
distribution has cost firms their existence.

Two refusals that are not in the bull's version:

- **A squeeze-shaped vector.** Thin offer side and rising volatility together is
  the shape a squeeze takes, and it is refused as a named case rather than left
  to the general distance test -- because each feature can sit inside its own
  normal range while the pair is a state nothing in the record resembles.
- **A feature that has never been this extreme in the direction that hurts.**
  Distance is symmetric; risk here is not. A funding rate far below normal
  charges the short every settlement, and one far above merely pays it.

**A feature with too little history is unjudgeable, not normal.**
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import OutOfDistributionFlag
from runtime.online_learner import RunningMoments
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "bear-outlier-rejector"
BOT = "bear-bot"

PART_DECLARATION = PartDeclaration(
    part_id="bear-outlier-rejector",
    consumes=("bear-feature-vector",),
    produces=("bear-feature-out-of-distribution-flag", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

# Features whose *low* end is what hurts a short, so being far below normal is
# treated as further out of distribution than being far above it.
DANGEROUS_WHEN_LOW = ("squeeze_room", "funding_rate", "offer_side_imbalance")

SQUEEZE_SHAPED = "squeeze-shaped: thin offer side with rising volatility"


@dataclass
class RejectorStanding:
    vectors_judged: int = 0
    flagged: int = 0
    unjudgeable_vectors: int = 0
    squeeze_shaped: int = 0
    by_worst_feature: dict = field(default_factory=dict)
    largest_deviation_seen: float = 0.0


class BearOutlierRejector:
    """Learns what normal looks like for a short, and is deliberately harder to satisfy."""

    def __init__(
        self,
        deviation_threshold: float,
        dangerous_side_threshold: float,
        minimum_observations: int,
        half_life_observations: float,
        maximum_unjudgeable_fraction: float,
        squeeze_room_deviation: float,
        rising_volatility_deviation: float,
        now_ns=time.time_ns,
    ) -> None:
        if deviation_threshold <= 0:
            raise ValueError("a threshold of zero flags every vector including the normal ones")
        if dangerous_side_threshold > deviation_threshold:
            raise ValueError(
                "the dangerous side must be at least as easy to trip as the ordinary one, or "
                "the asymmetry is stated and then not applied"
            )
        if not 0.0 <= maximum_unjudgeable_fraction <= 1.0:
            raise ValueError("the unjudgeable fraction is a fraction of the vector's features")
        self._threshold = deviation_threshold
        self._dangerous_threshold = dangerous_side_threshold
        self._minimum = minimum_observations
        self._half_life = half_life_observations
        self._maximum_unjudgeable = maximum_unjudgeable_fraction
        self._squeeze_room_deviation = squeeze_room_deviation
        self._rising_volatility_deviation = rising_volatility_deviation
        self._now_ns = now_ns
        self._moments: dict[str, RunningMoments] = {}
        self.standing = RejectorStanding()

    def observe_vector(self, vector) -> None:
        """Learn what normal is, from flagged vectors as well as accepted ones."""
        for name, value in vector.features.items():
            self._moment_for(name).observe(value)

    def judge(self, vector) -> OutOfDistributionFlag:
        self.standing.vectors_judged += 1

        judged = 0
        unjudgeable = []
        worst_feature = None
        worst_deviation = 0.0
        worst_exceeded_by = 0.0
        standardised_by_name = {}

        for name, value in sorted(vector.features.items()):
            moments = self._moments.get(name)
            standardised = None if moments is None else moments.standardise(value, self._minimum)
            if standardised is None:
                unjudgeable.append(name)
                continue
            judged += 1
            standardised_by_name[name] = standardised

            # Distance is symmetric; the risk it stands for is not. A feature
            # whose low end is what hurts a short trips at a nearer threshold.
            threshold = (
                self._dangerous_threshold
                if name in DANGEROUS_WHEN_LOW and standardised < 0
                else self._threshold
            )
            exceeded_by = abs(standardised) - threshold
            if exceeded_by > worst_exceeded_by or worst_feature is None:
                worst_exceeded_by = exceeded_by
                worst_deviation = abs(standardised)
                worst_feature = name

        total = judged + len(unjudgeable)
        unjudgeable_fraction = len(unjudgeable) / total if total else 1.0

        if judged == 0:
            self.standing.unjudgeable_vectors += 1
            return self._flag(
                vector, True, None, None, judged, unjudgeable,
                "nothing in this vector has enough history to say what normal is; an "
                "unrecognised short is refused rather than waved through, because the "
                "distributions that produce squeezes are exactly the ones no model has "
                "enough of to have learned",
            )

        if unjudgeable_fraction > self._maximum_unjudgeable:
            self.standing.unjudgeable_vectors += 1
            self.standing.flagged += 1
            return self._flag(
                vector, True, worst_feature, worst_deviation, judged, unjudgeable,
                f"{len(unjudgeable)} of {total} features have no established normal "
                f"({unjudgeable_fraction:.0%}, above the {self._maximum_unjudgeable:.0%} "
                f"allowed)",
            )

        self.standing.largest_deviation_seen = max(
            self.standing.largest_deviation_seen, worst_deviation
        )

        squeeze = self._is_squeeze_shaped(standardised_by_name)
        if squeeze is not None:
            self.standing.squeeze_shaped += 1
            self.standing.flagged += 1
            return self._flag(
                vector, True, "squeeze_room", worst_deviation, judged, unjudgeable, squeeze
            )

        if worst_exceeded_by > 0:
            self.standing.flagged += 1
            self.standing.by_worst_feature[worst_feature] = (
                self.standing.by_worst_feature.get(worst_feature, 0) + 1
            )
            dangerous = worst_feature in DANGEROUS_WHEN_LOW
            return self._flag(
                vector, True, worst_feature, worst_deviation, judged, unjudgeable,
                f"{worst_feature} is {worst_deviation:.1f} deviations from its own normal, past "
                f"the {self._dangerous_threshold if dangerous else self._threshold:.1f} this bot "
                f"recognises"
                + (
                    " -- a nearer threshold because this feature's low end is the one that "
                    "hurts a short, and a short's loss has no ceiling"
                    if dangerous
                    else ""
                ),
            )

        return self._flag(
            vector, False, worst_feature, worst_deviation, judged, unjudgeable,
            f"the furthest feature is {worst_feature} at {worst_deviation:.1f} deviations, "
            f"inside what this bot recognises, over {judged} judged feature(s)",
        )

    def _is_squeeze_shaped(self, standardised: dict) -> str | None:
        """Thin offer side and rising volatility together, each ordinary alone.

        Checked as a pair because each feature can sit inside its own normal
        range while the combination is a state nothing in the record resembles.
        """
        room = standardised.get("squeeze_room")
        volatility = standardised.get("realised_volatility_fraction")
        if room is None or volatility is None:
            return None
        if room <= -self._squeeze_room_deviation and volatility >= self._rising_volatility_deviation:
            return (
                f"{SQUEEZE_SHAPED}: squeeze_room is {room:.1f} deviations thin while realised "
                f"volatility is {volatility:.1f} deviations high. Each is ordinary alone; "
                f"together they are the shape a squeeze takes, and this bot has no record of "
                f"shorting into one"
            )
        return None

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
        moments = self._moments.get(name)
        if moments is None or moments.count < self._minimum or moments.deviation <= 0:
            return None
        low_threshold = (
            self._dangerous_threshold if name in DANGEROUS_WHEN_LOW else self._threshold
        )
        return (
            moments.mean - low_threshold * moments.deviation,
            moments.mean + self._threshold * moments.deviation,
        )


def describe_rejection(rejector: BearOutlierRejector) -> dict:
    return {
        "part_id": PART_ID,
        "vectors_judged": rejector.standing.vectors_judged,
        "flagged_out_of_distribution": rejector.standing.flagged,
        "flagged_squeeze_shaped": rejector.standing.squeeze_shaped,
        "unjudgeable_vectors": rejector.standing.unjudgeable_vectors,
        "flagged_by_worst_feature": dict(sorted(rejector.standing.by_worst_feature.items())),
        "largest_deviation_seen": rejector.standing.largest_deviation_seen,
        "features_with_a_learned_normal": len(rejector._moments),
        "features_dangerous_when_low": list(DANGEROUS_WHEN_LOW),
    }


def run_bear_outlier_rejector(
    rejector: BearOutlierRejector, control_socket, read_vectors, publish_flags,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
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
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )

def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Judge first, then learn from the vector: a vector compared against a
    distribution it has already been added to is a vector compared against itself,
    which makes every reading look ordinary. The part's own tick already does them
    in that order; this only has to hand it the vectors.
    """
    from runtime.input_assembly import Batch

    vectors = Batch(read=context.bus.reader("bear-feature-vector"))
    publish_flags = context.bus.publisher_for("bear-feature-out-of-distribution-flag")

    return run_bear_outlier_rejector(
        rejector=BearOutlierRejector(
            deviation_threshold=context.number("bear_outlier_deviation_threshold"),
            minimum_observations=int(context.number("bear_outlier_minimum_observations")),
            half_life_observations=context.number("bear_outlier_half_life_observations"),
            maximum_unjudgeable_fraction=context.number("bear_outlier_maximum_unjudgeable_fraction"),
            dangerous_side_threshold=context.number("bear_outlier_dangerous_side_threshold"),
            squeeze_room_deviation=context.number("bear_squeeze_room_deviation"),
            rising_volatility_deviation=context.number("bear_rising_volatility_deviation"),
        ),
        control_socket=context.control_socket,
        read_vectors=vectors.payloads,
        publish_flags=publish_flags,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
