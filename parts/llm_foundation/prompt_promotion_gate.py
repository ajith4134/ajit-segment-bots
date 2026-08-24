"""prompt-promotion-gate: a version replaces another only on evidence, never on hope.

Promotion is where a measured improvement becomes the thing that actually runs, and
where a measurement error becomes a permanent regression. The gate exists because
every incentive points the other way: a new prompt is written because somebody
believed it was better, and a small favourable difference always looks like proof.

The gate refuses on five grounds, and each corresponds to a way a prompt got worse
while its number went up:

- **A difference smaller than the margin is noise.** With a golden set of forty
  cases, a two-percent edge is one case. The margin is a setting and the difference
  is reported beside it.
- **No dimension may regress.** A version cannot buy agreement with structural
  validity. The four numbers are checked separately, which is why the evaluator
  keeps them apart.
- **Cost is part of the comparison.** A version three percent better and four times
  more expensive is a worse version, and stating that here rather than downstream
  is what stops the cost being invisible.
- **The incumbent must have been scored on the same cases.** Otherwise the
  comparison is between two different examinations.
- **A version with no incumbent is promoted only if it clears an absolute bar.**
  Being the first is not the same as being good enough, and "it is all we have" is
  how an unusable prompt becomes production.

Rolling back is a promotion of an older version, handled by the same gate with the
same evidence -- because a rollback taken in a panic without evidence is how a
system oscillates between two prompts forever.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.llm_types import PromptPromotion
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "prompt-promotion-gate"

PART_DECLARATION = PartDeclaration(
    part_id="prompt-promotion-gate",
    consumes=("prompt-score",),
    produces=("prompt-promotion", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

PROMOTED = "promoted"
INSIDE_THE_MARGIN = "the-difference-is-smaller-than-the-margin"
A_DIMENSION_REGRESSED = "it-is-worse-on-a-dimension-it-may-not-trade-away"
COSTS_TOO_MUCH_MORE = "the-improvement-does-not-justify-the-cost"
DIFFERENT_CASE_SETS = "the-two-versions-faced-different-cases"
BELOW_THE_ABSOLUTE_BAR = "the-first-version-for-this-purpose-is-not-good-enough-to-run"
NOT_SCORED = "one-of-the-versions-has-no-usable-score"

# The dimensions a promotion may not regress on. Cost is compared separately
# because it is allowed to be worse when the gain justifies it.
QUALITY_DIMENSIONS = (
    "schema_valid_fraction",
    "factually_supported_fraction",
    "agreement_with_outcome",
)


@dataclass(frozen=True)
class GateDecision:
    version_id: str
    state: str
    promotion: PromptPromotion | None
    margin: float | None
    regressions: tuple
    cost_ratio: float | None
    reason: str
    decided_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == PROMOTED and self.promotion is not None


@dataclass
class GateStanding:
    decisions: int = 0
    promotions: int = 0
    refused_inside_the_margin: int = 0
    refused_regression: int = 0
    refused_cost: int = 0
    refused_different_cases: int = 0
    refused_below_absolute_bar: int = 0
    first_versions_promoted: int = 0


class PromptPromotionGate:
    """Compares two scores dimension by dimension and refuses on any of five grounds."""

    def __init__(
        self,
        required_margin: float,
        regression_tolerance: float,
        maximum_cost_ratio: float,
        absolute_bar: dict,
        now_ns=time.time_ns,
    ) -> None:
        if required_margin <= 0:
            raise ValueError(
                "a margin of zero promotes on noise: with forty cases a two-percent edge "
                "is one case"
            )
        if regression_tolerance < 0:
            raise ValueError("the regression tolerance is not negative")
        if maximum_cost_ratio < 1.0:
            raise ValueError(
                "the cost ratio is how many times more expensive a winner may be, and "
                "cannot be below parity"
            )
        missing = set(QUALITY_DIMENSIONS) - set(absolute_bar)
        if missing:
            raise ValueError(
                f"the absolute bar must cover every quality dimension; missing "
                f"{sorted(missing)}"
            )
        self._required_margin = required_margin
        self._regression_tolerance = regression_tolerance
        self._maximum_cost_ratio = maximum_cost_ratio
        self._absolute_bar = dict(absolute_bar)
        self._now_ns = now_ns
        self._scores: dict[str, object] = {}
        self._case_sets: dict[str, tuple] = {}
        self.standing = GateStanding()

    def observe_score(self, score, cases=()) -> None:
        self._scores[score.version_id] = score
        if cases:
            self._case_sets[score.version_id] = tuple(cases)

    def decide(self, challenger_id: str, incumbent_id: str | None, template_id: str) -> GateDecision:
        self.standing.decisions += 1
        challenger = self._scores.get(challenger_id)
        if challenger is None or not challenger.is_usable:
            return self._decision(
                challenger_id, NOT_SCORED, None, None, (), None,
                "the challenger has no usable score. An unscored version is unmeasured, "
                "not promising",
            )

        if incumbent_id is None:
            return self._first_version(challenger, template_id)

        incumbent = self._scores.get(incumbent_id)
        if incumbent is None or not incumbent.is_usable:
            return self._decision(
                challenger_id, NOT_SCORED, None, None, (), None,
                f"{incumbent_id} has no usable score to compare against",
            )

        challenger_cases = self._case_sets.get(challenger_id)
        incumbent_cases = self._case_sets.get(incumbent_id)
        if (
            challenger_cases is not None
            and incumbent_cases is not None
            and challenger_cases != incumbent_cases
        ):
            self.standing.refused_different_cases += 1
            return self._decision(
                challenger_id, DIFFERENT_CASE_SETS, None, None, (), None,
                "the two versions faced different cases, so the comparison is between two "
                "different examinations",
            )

        regressions = tuple(
            dimension
            for dimension in QUALITY_DIMENSIONS
            if getattr(challenger, dimension)
            < getattr(incumbent, dimension) - self._regression_tolerance
        )
        if regressions:
            self.standing.refused_regression += 1
            return self._decision(
                challenger_id, A_DIMENSION_REGRESSED, None, None, regressions, None,
                f"worse on {', '.join(regressions)}. A version cannot buy agreement with "
                f"structural validity -- the dimensions are checked separately for exactly "
                f"this",
            )

        margin = self._headline_margin(challenger, incumbent)
        if margin < self._required_margin:
            self.standing.refused_inside_the_margin += 1
            return self._decision(
                challenger_id, INSIDE_THE_MARGIN, None, margin, (), None,
                f"{margin:+.2%} against a {self._required_margin:.2%} margin. A difference "
                f"this size is one case changing its mind",
            )

        cost_ratio = self._cost_ratio(challenger, incumbent)
        if cost_ratio is not None and cost_ratio > self._maximum_cost_ratio:
            self.standing.refused_cost += 1
            return self._decision(
                challenger_id, COSTS_TOO_MUCH_MORE, None, margin, (), cost_ratio,
                f"{margin:+.2%} better and {cost_ratio:.1f}x the cost, against a "
                f"{self._maximum_cost_ratio:.1f}x ceiling. A version that wins by three "
                f"percent and costs four times as much has not won",
            )

        promotion = PromptPromotion(
            version_id=challenger_id,
            template_id=template_id,
            purpose=challenger.purpose,
            replaces=incumbent_id,
            margin=margin,
            cases_run=challenger.cases_run,
            reason=(
                f"{margin:+.2%} on {challenger.cases_run} shared case(s) with no dimension "
                f"regressed"
                + (f" at {cost_ratio:.2f}x cost" if cost_ratio is not None else "")
            ),
            promoted_at_ns=self._now_ns(),
        )
        self.standing.promotions += 1
        return self._decision(
            challenger_id, PROMOTED, promotion, margin, (), cost_ratio, promotion.reason,
        )

    def _first_version(self, challenger, template_id) -> GateDecision:
        below = tuple(
            dimension
            for dimension in QUALITY_DIMENSIONS
            if getattr(challenger, dimension) < self._absolute_bar[dimension]
        )
        if below:
            self.standing.refused_below_absolute_bar += 1
            return self._decision(
                challenger.version_id, BELOW_THE_ABSOLUTE_BAR, None, None, below, None,
                f"the first version for {challenger.purpose} is below the bar on "
                f"{', '.join(below)}. Being the only candidate is not the same as being "
                f"good enough, and 'it is all we have' is how an unusable prompt becomes "
                f"production",
            )

        promotion = PromptPromotion(
            version_id=challenger.version_id,
            template_id=template_id,
            purpose=challenger.purpose,
            replaces=None,
            margin=0.0,
            cases_run=challenger.cases_run,
            reason=(
                f"first version for {challenger.purpose}, clearing the absolute bar on "
                f"every dimension over {challenger.cases_run} case(s)"
            ),
            promoted_at_ns=self._now_ns(),
        )
        self.standing.promotions += 1
        self.standing.first_versions_promoted += 1
        return self._decision(
            challenger.version_id, PROMOTED, promotion, 0.0, (), None, promotion.reason,
        )

    @staticmethod
    def _headline_margin(challenger, incumbent) -> float:
        """Agreement with what happened, which is the only dimension about being right."""
        return challenger.agreement_with_outcome - incumbent.agreement_with_outcome

    @staticmethod
    def _cost_ratio(challenger, incumbent) -> float | None:
        if incumbent.mean_output_tokens <= 0:
            return None
        return challenger.mean_output_tokens / incumbent.mean_output_tokens

    def _decision(
        self, version_id, state, promotion, margin, regressions, cost_ratio, reason,
    ) -> GateDecision:
        return GateDecision(
            version_id=version_id, state=state, promotion=promotion, margin=margin,
            regressions=regressions, cost_ratio=cost_ratio, reason=reason,
            decided_at_ns=self._now_ns(),
        )


def describe_promotion_gate(gate: PromptPromotionGate) -> dict:
    return {
        "part_id": PART_ID,
        "decisions": gate.standing.decisions,
        "promotions": gate.standing.promotions,
        "refused_inside_the_margin": gate.standing.refused_inside_the_margin,
        "refused_for_a_regression": gate.standing.refused_regression,
        "refused_for_cost": gate.standing.refused_cost,
        "refused_different_case_sets": gate.standing.refused_different_cases,
        "refused_below_the_absolute_bar": gate.standing.refused_below_absolute_bar,
        "first_versions_promoted": gate.standing.first_versions_promoted,
        "quality_dimensions": list(QUALITY_DIMENSIONS),
        "promotes_without_a_score": False,
    }


def run_prompt_promotion_gate(
    gate: PromptPromotionGate, control_socket, read_candidates, publish_promotions,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for challenger_id, incumbent_id, template_id in read_candidates(gate):
            decision = gate.decide(challenger_id, incumbent_id, template_id)
            if decision.is_usable:
                publish_promotions(decision.promotion)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_promotion_gate(gate),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Every score is observed; a newly scored version is the challenger, and
    the incumbent is the version last promoted for the same purpose, which
    this gate remembers from its own decisions. The template is the one
    the scored version's id names, in the registry's form template:version.
    """
    from runtime.input_assembly import Batch

    scores = Batch(read=context.bus.reader("prompt-score"))
    publish_promotions = context.bus.publisher_for("prompt-promotion")
    bar = [float(b) for b in context.setting("prompt_absolute_bar").value]
    gate = PromptPromotionGate(
        required_margin=context.number("prompt_required_margin"),
        regression_tolerance=context.number("prompt_regression_tolerance"),
        maximum_cost_ratio=context.number("prompt_maximum_cost_ratio"),
        absolute_bar=dict(zip(QUALITY_DIMENSIONS, bar, strict=True)),
    )
    incumbent_by_purpose: dict[str, str] = {}

    def template_of(version_id: str) -> str:
        return version_id.rsplit(":", 1)[0] if ":" in version_id else version_id

    def read_candidates(_gate):
        candidates = []
        for score in scores.payloads():
            gate.observe_score(score)
            if score.is_fitted:
                candidates.append((score.version_id, incumbent_by_purpose.get(score.purpose), template_of(score.version_id)))
        return tuple(candidates)

    def publish(promotion) -> None:
        if promotion is not None:
            incumbent_by_purpose[promotion.purpose] = promotion.version_id
            publish_promotions((promotion,))

    return run_prompt_promotion_gate(
        gate=gate,
        control_socket=context.control_socket,
        read_candidates=read_candidates,
        publish_promotions=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
