"""hypothesis-deduplicator: whether this idea is actually new.

A generator that produces the same idea in different words produces trials
without producing information. Worse, it produces the *appearance* of independent
confirmation: three restatements of one hypothesis that all test well look like
three findings, and the trial ledger cannot tell them apart because it counts by
name.

So novelty is measured on what a hypothesis **does**, not on how it is worded:

- **The same measurement, comparison and direction** is the same hypothesis
  whatever the sentence around it. Two candidate formulas that fire on the same
  ticks are one candidate.
- **A threshold near an existing one** is the same hypothesis with a tuned knob.
  That is the commonest way a search produces a hundred variations of one idea,
  and counting them separately is how the trial correction gets defeated from
  inside.
- **The same instruction in a different regime is genuinely new**, because a
  claim conditioned on a regime is a different claim.

**Novelty is a score, not a verdict.** A near-duplicate can be worth testing when
the original has retired or its regime has broken, and the ranker needs the
number rather than a refusal.

**Everything ever proposed is remembered**, including what was rejected. A
deduplicator with a short memory reproposes last month's ideas and counts each as
new.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "hypothesis-deduplicator"

PART_DECLARATION = PartDeclaration(
    part_id="hypothesis-deduplicator",
    consumes=("candidate-formula", "mutated-hypothesis", "instruction-history"),
    produces=("novelty-score", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

NOVEL = "novel"
A_TUNED_DUPLICATE = "the-same-hypothesis-with-a-tuned-threshold"
AN_EXACT_DUPLICATE = "the-same-hypothesis-in-different-words"
A_REGIME_VARIANT = "the-same-hypothesis-conditioned-on-a-different-regime"


@dataclass(frozen=True)
class HypothesisShape:
    """What a hypothesis does, stripped of how it is worded."""

    measurement: str
    comparison: str
    threshold: float
    direction: str
    regime: str | None

    @property
    def key(self) -> tuple:
        return (self.measurement, self.comparison, self.direction, self.regime)


@dataclass(frozen=True)
class NoveltyScore:
    """How new a hypothesis is, and what it duplicates if anything."""

    hypothesis_id: str
    state: str
    novelty: float
    nearest_existing: str | None
    threshold_distance: float | None
    fires_on_the_same_ticks_as: str | None
    reason: str
    scored_at_ns: int

    @property
    def is_novel(self) -> bool:
        return self.state == NOVEL


@dataclass
class DeduplicatorStanding:
    hypotheses_scored: int = 0
    novel: int = 0
    tuned_duplicates: int = 0
    exact_duplicates: int = 0
    regime_variants: int = 0
    remembered: int = 0
    by_measurement: dict = field(default_factory=dict)


class HypothesisDeduplicator:
    """Scores novelty on what a hypothesis does, and remembers everything ever proposed."""

    def __init__(
        self,
        threshold_tolerance: float,
        overlap_tolerance: float,
        now_ns=time.time_ns,
    ) -> None:
        if threshold_tolerance < 0:
            raise ValueError("the tolerance is a distance and cannot be negative")
        if not 0.0 <= overlap_tolerance <= 1.0:
            raise ValueError("overlap is a fraction of the ticks two hypotheses share")
        self._threshold_tolerance = threshold_tolerance
        self._overlap_tolerance = overlap_tolerance
        self._now_ns = now_ns
        self._shapes: dict[str, HypothesisShape] = {}
        self._firings: dict[str, set] = {}
        self.standing = DeduplicatorStanding()

    def remember(self, hypothesis_id: str, shape: HypothesisShape, fires_on=()) -> None:
        """Everything ever proposed, including what was rejected.

        A short memory reproposes last month's ideas and counts each as new.
        """
        self._shapes[hypothesis_id] = shape
        if fires_on:
            self._firings[hypothesis_id] = set(fires_on)
        self.standing.remembered = len(self._shapes)

    def overlap_with(self, fires_on, existing_id: str) -> float | None:
        """What fraction of ticks two hypotheses fire on together.

        Two formulas that fire on the same ticks are one candidate whatever the
        sentences around them say.
        """
        existing = self._firings.get(existing_id)
        if not existing or not fires_on:
            return None
        fires_on = set(fires_on)
        union = fires_on | existing
        if not union:
            return None
        return len(fires_on & existing) / len(union)

    def score(self, hypothesis_id: str, shape: HypothesisShape, fires_on=()) -> NoveltyScore:
        self.standing.hypotheses_scored += 1
        self.standing.by_measurement[shape.measurement] = (
            self.standing.by_measurement.get(shape.measurement, 0) + 1
        )

        same_shape = [
            (existing_id, existing)
            for existing_id, existing in self._shapes.items()
            if existing.key == shape.key and existing_id != hypothesis_id
        ]

        # Ticks first: two formulas firing together are one hypothesis whatever
        # their thresholds or wording.
        for existing_id in self._firings:
            overlap = self.overlap_with(fires_on, existing_id)
            if overlap is not None and overlap >= self._overlap_tolerance:
                self.standing.exact_duplicates += 1
                return self._score(
                    hypothesis_id, AN_EXACT_DUPLICATE, 1.0 - overlap, existing_id, None,
                    existing_id,
                    f"it fires on {overlap:.0%} of the same ticks as {existing_id}. Two "
                    f"formulas that fire together are one candidate, and counting them "
                    f"separately produces the appearance of independent confirmation",
                )

        if same_shape:
            nearest_id, nearest = min(
                same_shape, key=lambda entry: abs(entry[1].threshold - shape.threshold)
            )
            distance = abs(nearest.threshold - shape.threshold)
            if distance <= self._threshold_tolerance:
                # The commonest way a search produces a hundred variations of one
                # idea, and how the trial correction gets defeated from inside.
                self.standing.tuned_duplicates += 1
                novelty = distance / self._threshold_tolerance if self._threshold_tolerance else 0.0
                return self._score(
                    hypothesis_id, A_TUNED_DUPLICATE, novelty, nearest_id, distance, None,
                    f"its threshold is {distance:.4g} from {nearest_id}'s, inside the "
                    f"{self._threshold_tolerance:.4g} that makes it the same hypothesis with a "
                    f"tuned knob. Novelty is a score rather than a refusal: this is worth "
                    f"testing if the original has retired or its regime has broken",
                )

        regime_variants = [
            existing_id
            for existing_id, existing in self._shapes.items()
            if (existing.measurement, existing.comparison, existing.direction)
            == (shape.measurement, shape.comparison, shape.direction)
            and existing.regime != shape.regime
        ]
        if regime_variants:
            # A claim conditioned on a regime is a different claim.
            self.standing.regime_variants += 1
            return self._score(
                hypothesis_id, A_REGIME_VARIANT, 0.7, regime_variants[0], None, None,
                f"the same measurement and direction as {regime_variants[0]} but conditioned "
                f"on {shape.regime} rather than its regime. A claim conditioned on a regime is "
                f"a different claim, so this is genuinely new",
            )

        self.standing.novel += 1
        return self._score(
            hypothesis_id, NOVEL, 1.0, None, None, None,
            f"nothing proposed so far uses {shape.measurement} {shape.comparison} in the "
            f"{shape.direction} direction"
            + (f" conditioned on {shape.regime}" if shape.regime else "")
            + f", over {len(self._shapes)} remembered hypothesis/hypotheses",
        )

    def _score(
        self, hypothesis_id, state, novelty, nearest, distance, same_ticks, reason
    ) -> NoveltyScore:
        return NoveltyScore(
            hypothesis_id=hypothesis_id,
            state=state,
            novelty=max(0.0, min(1.0, novelty)),
            nearest_existing=nearest,
            threshold_distance=distance,
            fires_on_the_same_ticks_as=same_ticks,
            reason=reason,
            scored_at_ns=self._now_ns(),
        )


def describe_deduplication(deduplicator: HypothesisDeduplicator) -> dict:
    return {
        "part_id": PART_ID,
        "hypotheses_scored": deduplicator.standing.hypotheses_scored,
        "novel": deduplicator.standing.novel,
        "tuned_duplicates": deduplicator.standing.tuned_duplicates,
        "exact_duplicates": deduplicator.standing.exact_duplicates,
        "regime_variants": deduplicator.standing.regime_variants,
        "remembered": deduplicator.standing.remembered,
        "by_measurement": dict(sorted(deduplicator.standing.by_measurement.items())),
        "novelty_is_a_verdict": False,
    }


def run_hypothesis_deduplicator(
    deduplicator: HypothesisDeduplicator, control_socket, read_candidates, publish_scores,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_scores(
            tuple(
                deduplicator.score(hypothesis_id, shape, fires_on)
                for hypothesis_id, shape, fires_on in read_candidates(deduplicator)
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
        read_standing=lambda: describe_deduplication(deduplicator),
    )

def _shape_of(item):
    """A hypothesis's comparable shape, from whichever type carries it.

    Three shapes reach this part and they carry their claim differently. A
    mutation and an instruction state `measurement`/`comparison`/`threshold`
    directly or through a `context` mapping. **A mined formula states neither: it
    carries `terms`**, each one a measurement, a comparison and a threshold, and
    it was the only producer this part actually had.

    Until 2026-08-26 this function looked only for `measurement` and a `context`,
    so every `candidate-formula` returned None and was silently skipped -- 228
    received, `hypotheses_scored` 0, and not one `novelty-score` ever published.
    `hypothesis-ranker` reads novelty from here, `instruction-writer` reads it
    from the ranker, and a hypothesis with no novelty score fails the writer's
    novelty bar at 0.0, so this one unreadable field refused every hypothesis the
    system ever mined. The reads went through `getattr(..., None)` defaults, which
    is exactly the shape `check_payload_reads.py` cannot see -- a default turns a
    field nobody carries into a value everybody accepts.

    A formula's shape is its **first** term. A conjunction has no single
    measurement, and calling it by its first term would make two formulas sharing
    an opening term look like duplicates; so a multi-term formula is refused here
    rather than mis-shaped, which is the same answer `instruction-writer` gives it
    for the same reason -- the scanner watches one comparison, not a conjunction.
    """
    terms = getattr(item, "terms", None)
    if terms:
        if len(terms) != 1:
            return None
        term = terms[0]
        return HypothesisShape(
            measurement=str(term.measurement),
            comparison=str(term.comparison),
            threshold=float(term.threshold),
            direction="",
            regime=getattr(item, "regime_tag", None),
        )

    context = getattr(item, "context", None) or {}
    if not isinstance(context, dict):
        context = {}
    measurement = getattr(item, "measurement", None) or context.get("measurement")
    if measurement is None:
        return None
    return HypothesisShape(
        measurement=str(measurement),
        comparison=str(getattr(item, "comparison", None) or context.get("comparison", "")),
        threshold=float(getattr(item, "threshold", None) or context.get("threshold", 0.0) or 0.0),
        direction=str(getattr(item, "direction", None) or context.get("direction", "")),
        regime=getattr(item, "regime_tag", None) or context.get("regime"),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Every instruction the archive holds is remembered as a shape; every new
    candidate or mutation is scored for novelty against them.
    """
    from runtime.input_assembly import Batch

    formulas = Batch(read=context.bus.reader("candidate-formula"))
    mutations = Batch(read=context.bus.reader("mutated-hypothesis"))
    histories = Batch(read=context.bus.reader("instruction-history"))
    publish_scores = context.bus.publisher_for("novelty-score")
    deduplicator = HypothesisDeduplicator(
        threshold_tolerance=context.number("hypothesis_threshold_tolerance"),
        overlap_tolerance=context.number("hypothesis_overlap_tolerance"),
    )

    def read_candidates(_deduplicator):
        for history in histories.payloads():
            shape = _shape_of(history)
            if shape is not None:
                deduplicator.remember(history.instruction_id, shape, ())
        jobs = []
        for source in (formulas, mutations):
            for item in source.payloads():
                shape = _shape_of(item)
                hypothesis_id = getattr(item, "hypothesis_id", None) or getattr(item, "formula_id", None)
                if shape is not None and hypothesis_id:
                    jobs.append((hypothesis_id, shape, ()))
        return tuple(jobs)

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_scores(kept)

    return run_hypothesis_deduplicator(
        deduplicator=deduplicator,
        control_socket=context.control_socket,
        read_candidates=read_candidates,
        publish_scores=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
