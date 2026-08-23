"""retrieval-quality-scorer: which retrieved sources actually helped.

Retrieval systems are almost never measured. Passages are fetched, pasted into a
prompt, and the answer is judged -- but nothing connects the two, so a source that
consistently drags answers down keeps being retrieved forever because its wording
matches well.

This part closes that loop. Every hit is remembered against the answer it went
into, and when that answer is scored the credit or blame lands on the sources that
were actually present.

Four things make the attribution honest rather than flattering:

- **Only sources that were in the prompt are credited.** A source retrieved and then
  dropped by the assembler for budget reasons did not influence the answer, and
  crediting it would reward a passage nobody read.
- **A source present in every answer cannot be measured.** If it is always there,
  its presence never varies, and the difference between answers cannot be attributed
  to it. That is reported as unmeasurable rather than scored highly.
- **The comparison is against answers without it, not against a fixed bar.** A
  source is useful if answers containing it were better than answers that did not
  contain it -- which is the only phrasing that survives a period when every answer
  was good.
- **Usefulness decays.** A source that helped six months ago against a market that
  has changed is not evidence about now.

The scorer removes nothing. It reports, and the index does the demoting -- because a
part that could delete what it cannot measure would optimise its own metric.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import RateEstimator
from runtime.llm_types import RetrievalScore
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "retrieval-quality-scorer"

PART_DECLARATION = PartDeclaration(
    part_id="retrieval-quality-scorer",
    consumes=("retrieval-hit", "prompt-score"),
    produces=("retrieval-score", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

USEFUL = "useful"
NOT_USEFUL = "answers-were-no-better-with-it"
HARMFUL = "answers-were-worse-with-it"
NEVER_RETRIEVED = "never-retrieved"
TOO_FEW_ANSWERS = "too-few-graded-answers-to-say-anything"
ALWAYS_PRESENT = "it-is-in-every-answer-so-its-effect-cannot-be-separated"


@dataclass(frozen=True)
class SourceStanding:
    source_reference: str
    state: str
    score: RetrievalScore
    with_it_rate: float | None
    without_it_rate: float | None
    difference: float | None
    answers_with_it: int
    answers_without_it: int
    reason: str
    scored_at_ns: int

    @property
    def should_be_demoted(self) -> bool:
        return self.state == HARMFUL


@dataclass
class ScorerStanding:
    hits_recorded: int = 0
    answers_graded: int = 0
    sources_scored: int = 0
    useful_sources: int = 0
    harmful_sources: int = 0
    unmeasurable_sources: int = 0
    sources_dropped_before_the_prompt: int = 0
    sources_removed: int = 0


class RetrievalQualityScorer:
    """Attributes answer quality to the sources that were actually in the prompt."""

    def __init__(
        self,
        minimum_answers: int,
        useful_margin: float,
        prior_quality: float,
        prior_weight: float,
        half_life_observations: float,
        now_ns=time.time_ns,
    ) -> None:
        if minimum_answers < 2:
            raise ValueError("a difference over one answer is that answer")
        if useful_margin <= 0:
            raise ValueError(
                "a margin of zero calls every rounding difference a useful source"
            )
        self._minimum_answers = minimum_answers
        self._useful_margin = useful_margin
        self._prior_quality = prior_quality
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._now_ns = now_ns
        # answer_id -> the sources that were actually in the prompt
        self._present: dict[str, set] = {}
        self._graded: dict[str, bool] = {}
        self._with_it: dict[str, RateEstimator] = {}
        self._times_retrieved: dict[str, int] = {}
        self._all_sources: set = set()
        self.standing = ScorerStanding()

    def observe_hit(self, answer_id: str, hit, made_it_into_the_prompt: bool) -> None:
        """A hit dropped by the assembler did not influence the answer."""
        self.standing.hits_recorded += 1
        self._all_sources.add(hit.source_reference)
        self._times_retrieved[hit.source_reference] = (
            self._times_retrieved.get(hit.source_reference, 0) + 1
        )
        if not made_it_into_the_prompt:
            self.standing.sources_dropped_before_the_prompt += 1
            return
        self._present.setdefault(answer_id, set()).add(hit.source_reference)

    def observe_answer(self, answer_id: str, was_good: bool) -> None:
        """A graded answer. Credit lands on whatever was present in its prompt."""
        if answer_id in self._graded:
            return
        self._graded[answer_id] = was_good
        self.standing.answers_graded += 1
        for source_reference in self._present.get(answer_id, set()):
            self._with_it.setdefault(
                source_reference,
                RateEstimator(
                    prior=self._prior_quality,
                    prior_weight=self._prior_weight,
                    half_life_observations=self._half_life,
                ),
            ).observe(was_good)

    def score(self, source_reference: str) -> SourceStanding:
        self.standing.sources_scored += 1
        retrieved = self._times_retrieved.get(source_reference, 0)
        with_it = [
            answer_id
            for answer_id, sources in self._present.items()
            if source_reference in sources and answer_id in self._graded
        ]
        without_it = [
            answer_id
            for answer_id in self._graded
            if source_reference not in self._present.get(answer_id, set())
        ]

        if retrieved == 0:
            return self._standing(
                source_reference, NEVER_RETRIEVED, retrieved, None, None, 0, 0,
                "never retrieved, which is not the same as never useful",
            )

        if len(with_it) < self._minimum_answers:
            return self._standing(
                source_reference, TOO_FEW_ANSWERS, retrieved, None, None,
                len(with_it), len(without_it),
                f"{len(with_it)} graded answer(s) contained it, below the "
                f"{self._minimum_answers} bar",
            )

        if not without_it:
            self.standing.unmeasurable_sources += 1
            return self._standing(
                source_reference, ALWAYS_PRESENT, retrieved, None, None,
                len(with_it), 0,
                "it is in every graded answer, so its presence never varies and the "
                "difference between answers cannot be attributed to it. That is "
                "unmeasurable, which is not the same as excellent",
            )

        with_rate = sum(1 for answer_id in with_it if self._graded[answer_id]) / len(with_it)
        without_rate = sum(
            1 for answer_id in without_it if self._graded[answer_id]
        ) / len(without_it)
        difference = with_rate - without_rate

        if difference >= self._useful_margin:
            self.standing.useful_sources += 1
            state = USEFUL
            reason = (
                f"answers containing it were good {with_rate:.0%} of the time against "
                f"{without_rate:.0%} without it, {difference:+.0%}. The comparison is "
                f"against answers without it, which survives a period when everything "
                f"was good"
            )
        elif difference <= -self._useful_margin:
            self.standing.harmful_sources += 1
            state = HARMFUL
            reason = (
                f"answers containing it were good {with_rate:.0%} against {without_rate:.0%} "
                f"without it, {difference:+.0%}. It is reported, not removed -- a part that "
                f"could delete what it cannot measure would optimise its own metric"
            )
        else:
            state = NOT_USEFUL
            reason = (
                f"{difference:+.0%} difference, inside the {self._useful_margin:.0%} margin. "
                f"It is retrieved because its wording matches, not because it helps"
            )

        return self._standing(
            source_reference, state, retrieved, with_rate, without_rate,
            len(with_it), len(without_it), reason, difference,
        )

    def _standing(
        self, source_reference, state, retrieved, with_rate, without_rate,
        answers_with, answers_without, reason, difference=None,
    ) -> SourceStanding:
        estimator = self._with_it.get(source_reference)
        estimate = (
            estimator.estimate(self._minimum_answers) if estimator else None
        )
        score = RetrievalScore(
            source_reference=source_reference,
            times_retrieved=retrieved,
            times_the_answer_was_good=answers_with,
            usefulness=(
                estimate.value if estimate is not None else self._prior_quality
            ),
            is_fitted=bool(estimate and estimate.is_fitted and state in (USEFUL, NOT_USEFUL, HARMFUL)),
            scored_at_ns=self._now_ns(),
        )
        return SourceStanding(
            source_reference=source_reference, state=state, score=score,
            with_it_rate=with_rate, without_it_rate=without_rate, difference=difference,
            answers_with_it=answers_with, answers_without_it=answers_without,
            reason=reason, scored_at_ns=self._now_ns(),
        )

    def sources_seen(self) -> tuple:
        return tuple(sorted(self._all_sources))


def describe_retrieval_scoring(scorer: RetrievalQualityScorer) -> dict:
    return {
        "part_id": PART_ID,
        "hits_recorded": scorer.standing.hits_recorded,
        "answers_graded": scorer.standing.answers_graded,
        "sources_scored": scorer.standing.sources_scored,
        "useful_sources": scorer.standing.useful_sources,
        "harmful_sources": scorer.standing.harmful_sources,
        "unmeasurable_sources": scorer.standing.unmeasurable_sources,
        "hits_dropped_before_reaching_the_prompt": (
            scorer.standing.sources_dropped_before_the_prompt
        ),
        "removes_anything": False,
        "sources_removed": scorer.standing.sources_removed,
        "credits_sources_that_never_reached_the_prompt": False,
    }


def run_retrieval_quality_scorer(
    scorer: RetrievalQualityScorer, control_socket, read_answers, publish_scores,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for source_reference in read_answers(scorer):
            publish_scores(scorer.score(source_reference).score)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
