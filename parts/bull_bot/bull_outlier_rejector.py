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
from runtime.learned_state import (
    STARTED_COLD_UNREADABLE,
    CheckpointSchedule,
    LearnedStateStore,
)
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
    # Whether this part started with the normals it had learned before, or cold.
    # A cold start is what makes every vector unjudgeable, and it is exactly the
    # fact a restart hides.
    checkpoint_verdict: str | None = None
    checkpoint_detail: str | None = None
    checkpoint_saved_at_ns: int | None = None
    checkpoints_written: int = 0
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


    # -- what survives a restart ----------------------------------------------
    #
    # None of this existed until 2026-08-25, and the cost was measured on the
    # live spine: after each restart this part had a learned normal for 3 of its
    # features, judged every vector unjudgeable for want of the rest, and
    # bull-conviction-model refused every candidate it was handed as out of
    # distribution. The bot could not form an opinion at all until the normals
    # had been relearned -- and every restart put it back to three.

    def learned_settings(self) -> dict:
        """The settings the stored moments were learned under.

        The half-life above all: moments decayed at one rate and read at another
        describe a spread nothing ever observed, so `runtime.learned_state`
        refuses the checkpoint rather than restoring it.
        """
        return {
            "half_life_observations": self._half_life,
            "minimum_observations": self._minimum,
        }

    def state(self) -> dict:
        return {
            "moments": {name: moments.state() for name, moments in self._moments.items()},
            "vectors_judged": self.standing.vectors_judged,
        }

    def restore_state(self, state: dict) -> None:
        for name, stored in state["moments"].items():
            self._moment_for(name).restore_state(stored)
        self.standing.vectors_judged = int(state.get("vectors_judged", 0))

    @property
    def training_observations(self) -> int:
        """How many observations the best-observed feature has.

        The most, not the total: the checkpoint is due when this part has learned
        something new, and a total over features would make one busy feature look
        like progress across all of them.
        """
        return max((moments.count for moments in self._moments.values()), default=0)

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


COMPONENT = "normals"


def restore_or_start_cold(rejector: BullOutlierRejector, store, part_id: str = PART_ID) -> None:
    """Adopt the previous process's normals, or record why this one starts cold.

    Never raises past a part's start: a checkpoint that cannot be adopted is a
    reason to learn again, not a reason to refuse to run -- and the reason goes on
    the standing, because "this bot started cold and will refuse everything for an
    hour" is exactly what a restart otherwise hides.
    """
    restoration = store.restore(part_id, COMPONENT, rejector.learned_settings())
    rejector.standing.checkpoint_saved_at_ns = restoration.saved_at_ns
    if not restoration.was_restored:
        rejector.standing.checkpoint_verdict = restoration.verdict
        rejector.standing.checkpoint_detail = restoration.detail
        return
    try:
        rejector.restore_state(restoration.state)
    except (KeyError, TypeError, ValueError) as refusal:
        rejector.standing.checkpoint_verdict = STARTED_COLD_UNREADABLE
        rejector.standing.checkpoint_detail = (
            f"{restoration.detail}, but it could not be adopted: {refusal}"
        )
        return
    rejector.standing.checkpoint_verdict = restoration.verdict
    rejector.standing.checkpoint_detail = restoration.detail


def describe_rejection(rejector: BullOutlierRejector) -> dict:
    return {
        "part_id": PART_ID,
        "vectors_judged": rejector.standing.vectors_judged,
        "flagged_out_of_distribution": rejector.standing.flagged,
        "unjudgeable_vectors": rejector.standing.unjudgeable_vectors,
        "flagged_by_worst_feature": dict(sorted(rejector.standing.by_worst_feature.items())),
        "largest_deviation_seen": rejector.standing.largest_deviation_seen,
        "features_with_a_learned_normal": len(rejector._moments),
        "checkpoint_verdict": rejector.standing.checkpoint_verdict,
        "checkpoint_detail": rejector.standing.checkpoint_detail,
        "checkpoint_saved_at_ns": rejector.standing.checkpoint_saved_at_ns,
        "checkpoints_written": rejector.standing.checkpoints_written,
    }


def run_bull_outlier_rejector(
    rejector: BullOutlierRejector, control_socket, read_vectors, publish_flags,
    health_interval_seconds: float, emit_health,
    checkpoint=None,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    """`checkpoint` is called with the rejector whenever it may be worth storing.

    On every tick, not only after a vector: whether enough has been learned to be
    worth an fsync is the schedule's decision, and a part that only checkpointed
    after judging would never write the first one on a quiet market.
    """
    def tick() -> None:
        flags = []
        for vector in read_vectors():
            flags.append(rejector.judge(vector))
            rejector.observe_vector(vector)
        publish_flags(tuple(flags))
        if checkpoint is not None:
            checkpoint(rejector)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_rejection(rejector),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Judge first, then learn from the vector: a vector compared against a
    distribution it has already been added to is a vector compared against itself,
    which makes every reading look ordinary. The part's own tick already does them
    in that order; this only has to hand it the vectors.
    """
    import pathlib

    from runtime.input_assembly import Batch

    vectors = Batch(read=context.bus.reader("bull-feature-vector"))
    publish_flags = context.bus.publisher_for("bull-feature-out-of-distribution-flag")
    rejector = BullOutlierRejector(
        deviation_threshold=context.number("bull_outlier_deviation_threshold"),
        minimum_observations=int(context.number("bull_outlier_minimum_observations")),
        half_life_observations=context.number("bull_outlier_half_life_observations"),
        maximum_unjudgeable_fraction=context.number("bull_outlier_maximum_unjudgeable_fraction"),
    )
    # What normal looks like, carried across restarts since 2026-08-25. Without
    # it this part began every process with nothing learned, judged every vector
    # unjudgeable for want of a normal to compare it against, and bull-conviction-
    # model refused every candidate as out of distribution -- so the bot could not
    # form an opinion until the normals had been learned again, and every restart
    # put it back to the beginning.
    store = LearnedStateStore(
        pathlib.Path(str(context.setting("learned_state_root").value)).expanduser()
    )
    store.root.mkdir(parents=True, exist_ok=True)
    restore_or_start_cold(rejector, store)
    schedule = CheckpointSchedule(int(context.number("learned_state_checkpoint_interval")))

    def checkpoint(rejector: BullOutlierRejector) -> None:
        observations = rejector.training_observations
        if not schedule.is_due(observations):
            return
        store.save(PART_ID, COMPONENT, rejector.state(), rejector.learned_settings())
        schedule.record_written(observations)
        rejector.standing.checkpoints_written += 1

    return run_bull_outlier_rejector(
        rejector=rejector,
        checkpoint=checkpoint,
        control_socket=context.control_socket,
        read_vectors=vectors.payloads,
        publish_flags=publish_flags,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
