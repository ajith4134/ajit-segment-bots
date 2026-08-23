"""hypothesis-regime-tagger: which market a hypothesis is actually a claim about.

An untagged hypothesis is a claim about all markets, and almost none of them are.
A mean-reversion formula that tested well over a quarter tested well over the
regime that quarter happened to be, and trading it through the next one is how a
system loses money on something it correctly measured.

So every hypothesis gets a tag, and the tag is derived from **where its evidence
came from**, not from where somebody thinks it should apply:

- **Concentrated in one regime**: tagged for that regime. The honest reading of
  evidence gathered in one market.
- **Spread across regimes and working in all of them**: tagged as
  regime-independent, which is rare and worth knowing when it happens.
- **Spread across regimes and working in only some**: tagged for those, with the
  others recorded as tested and failed -- which is stronger evidence than never
  having been tested there.
- **Too little evidence in any regime to say**: tagged as untagged, and an
  untagged hypothesis is not traded outside the regime it was found in.

**The tag bounds where the instruction may fire.** That is the whole point: a tag
nothing enforces is a note.

**A regime break invalidates a tag.** The regime the hypothesis was tagged for no
longer exists, and the tag has to be re-earned in the new one rather than
inherited.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, RateEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "hypothesis-regime-tagger"

PART_DECLARATION = PartDeclaration(
    part_id="hypothesis-regime-tagger",
    consumes=("candidate-formula", "market-regime"),
    produces=("hypothesis-regime-tag", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

TAGGED_FOR_ONE = "tagged-for-the-regime-its-evidence-came-from"
TAGGED_FOR_SEVERAL = "tagged-for-the-regimes-it-works-in"
REGIME_INDEPENDENT = "works-in-every-regime-it-has-been-tested-in"
UNTAGGED = "too-little-evidence-in-any-regime-to-say"


@dataclass(frozen=True)
class HypothesisRegimeTag:
    """Where a hypothesis's evidence came from, and where it may therefore fire."""

    hypothesis_id: str
    state: str
    regimes_it_may_fire_in: tuple
    regimes_tested_and_failed: tuple
    regimes_untested: tuple
    evidence_by_regime: dict
    reason: str
    tagged_at_ns: int

    @property
    def is_tagged(self) -> bool:
        return self.state != UNTAGGED

    def may_fire_in(self, regime: str) -> bool:
        """The tag bounds where the instruction fires. A tag nothing enforces is a note."""
        if self.state == REGIME_INDEPENDENT:
            return True
        return regime in self.regimes_it_may_fire_in


@dataclass
class TaggerStanding:
    hypotheses_tagged: int = 0
    tagged_for_one: int = 0
    tagged_for_several: int = 0
    regime_independent: int = 0
    untagged: int = 0
    tags_invalidated_by_a_break: int = 0
    by_regime: dict = field(default_factory=dict)


class HypothesisRegimeTagger:
    """Tags a hypothesis with the regimes its own evidence supports, and no others."""

    def __init__(
        self,
        minimum_trades_per_regime: int,
        working_threshold: float,
        prior_hit_rate: float,
        prior_weight: float,
        half_life_observations: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < working_threshold < 1.0:
            raise ValueError("the working threshold is a hit rate and must be inside (0, 1)")
        self._minimum = minimum_trades_per_regime
        self._threshold = working_threshold
        self._prior_hit_rate = prior_hit_rate
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._now_ns = now_ns
        self._evidence: dict[tuple[str, str], RateEstimator] = {}
        self._known_regimes: set[str] = set()
        self.standing = TaggerStanding()

    def observe_outcome(self, hypothesis_id: str, regime: str, was_right: bool) -> None:
        """One resolved trade, attributed to the regime it happened in."""
        self._estimator_for(hypothesis_id, regime).observe(was_right)
        self._known_regimes.add(regime)
        self.standing.by_regime[regime] = self.standing.by_regime.get(regime, 0) + 1

    def observe_regime_break(self, hypothesis_id: str, regime: str) -> None:
        """The regime a tag was earned in no longer exists, so the tag must be re-earned."""
        if (hypothesis_id, regime) in self._evidence:
            del self._evidence[(hypothesis_id, regime)]
            self.standing.tags_invalidated_by_a_break += 1

    def tag(self, hypothesis_id: str) -> HypothesisRegimeTag:
        self.standing.hypotheses_tagged += 1

        evidence = {}
        working = []
        failed = []
        for regime in sorted(self._known_regimes):
            estimator = self._evidence.get((hypothesis_id, regime))
            if estimator is None:
                continue
            estimate = estimator.estimate(self._minimum)
            evidence[regime] = {
                "hit_rate": estimate.value,
                "trades": estimate.observations,
                "is_measured": estimate.is_fitted,
            }
            if not estimate.is_fitted:
                continue
            if estimate.value >= self._threshold:
                working.append(regime)
            else:
                failed.append(regime)

        untested = tuple(
            sorted(regime for regime in self._known_regimes if regime not in evidence)
        )

        if not working and not failed:
            self.standing.untagged += 1
            return self._tag(
                hypothesis_id, UNTAGGED, (), tuple(failed), untested, evidence,
                f"no regime has {self._minimum} measured trade(s) for this hypothesis. An "
                f"untagged hypothesis is not traded outside the regime it was found in, "
                f"because an untagged claim is a claim about every market and almost none are",
            )

        if working and not failed and len(working) > 1:
            self.standing.regime_independent += 1
            return self._tag(
                hypothesis_id, REGIME_INDEPENDENT, tuple(working), (), untested, evidence,
                f"it works in all {len(working)} regime(s) it has been tested in "
                f"({', '.join(working)}), which is rare and worth knowing when it happens"
                + (f"; {len(untested)} regime(s) remain untested" if untested else ""),
            )

        if len(working) == 1:
            self.standing.tagged_for_one += 1
            state = TAGGED_FOR_ONE
        else:
            self.standing.tagged_for_several += 1
            state = TAGGED_FOR_SEVERAL

        return self._tag(
            hypothesis_id, state, tuple(working), tuple(failed), untested, evidence,
            f"tagged for {', '.join(working) or 'no regime'}"
            + (
                f"; tested and failed in {', '.join(failed)}, which is stronger evidence than "
                f"never having been tested there"
                if failed
                else ""
            )
            + (f"; untested in {', '.join(untested)}" if untested else "")
            + ". The tag bounds where the instruction may fire -- a hypothesis that tested well "
            "over a quarter tested well over the regime that quarter happened to be",
        )

    def _estimator_for(self, hypothesis_id: str, regime: str) -> RateEstimator:
        key = (hypothesis_id, regime)
        estimator = self._evidence.get(key)
        if estimator is None:
            estimator = RateEstimator(
                prior=self._prior_hit_rate, prior_weight=self._prior_weight,
                half_life_observations=self._half_life,
            )
            self._evidence[key] = estimator
        return estimator

    def _tag(
        self, hypothesis_id, state, working, failed, untested, evidence, reason
    ) -> HypothesisRegimeTag:
        return HypothesisRegimeTag(
            hypothesis_id=hypothesis_id,
            state=state,
            regimes_it_may_fire_in=working,
            regimes_tested_and_failed=failed,
            regimes_untested=untested,
            evidence_by_regime=evidence,
            reason=reason,
            tagged_at_ns=self._now_ns(),
        )


def describe_regime_tagging(tagger: HypothesisRegimeTagger) -> dict:
    return {
        "part_id": PART_ID,
        "hypotheses_tagged": tagger.standing.hypotheses_tagged,
        "tagged_for_one_regime": tagger.standing.tagged_for_one,
        "tagged_for_several": tagger.standing.tagged_for_several,
        "regime_independent": tagger.standing.regime_independent,
        "untagged": tagger.standing.untagged,
        "tags_invalidated_by_a_break": tagger.standing.tags_invalidated_by_a_break,
        "by_regime": dict(sorted(tagger.standing.by_regime.items())),
        "regimes_known": sorted(tagger._known_regimes),
    }


def run_hypothesis_regime_tagger(
    tagger: HypothesisRegimeTagger, control_socket, read_outcomes, publish_tags,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        hypotheses = read_outcomes(tagger)
        publish_tags(tuple(tagger.tag(hypothesis_id) for hypothesis_id in hypotheses))

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

    A formula's held-out record is read as outcomes in the regime current
    on the symbols it was fitted on; the tag is re-made for every formula
    that received evidence this wake.
    """
    from runtime.input_assembly import Batch, LatestByKey

    formulas = Batch(read=context.bus.reader("candidate-formula"))
    regimes = LatestByKey(read=context.bus.reader("market-regime"), key_of=lambda r: (r.venue_id, r.symbol))
    publish_tags = context.bus.publisher_for("hypothesis-regime-tag")
    tagger = HypothesisRegimeTagger(
        minimum_trades_per_regime=int(context.number("decoding_minimum_trades")),
        working_threshold=context.number("hypothesis_working_threshold"),
        prior_hit_rate=context.number("learning_prior_hit_rate"),
        prior_weight=context.number("learning_prior_weight"),
        half_life_observations=context.number("learning_half_life_observations"),
    )
    counted: dict[str, int] = {}

    def read_outcomes(_tagger):
        current = {r.regime for r in regimes.mapping().values() if r.is_classified} or {"unclassified"}
        touched = set()
        for formula in formulas.payloads():
            seen = counted.get(formula.formula_id, 0)
            new = max(0, formula.held_out_trades - seen)
            if new == 0 or formula.held_out_hit_rate is None:
                continue
            right = int(round(formula.held_out_hit_rate * new))
            for regime in sorted(current):
                for index in range(new):
                    tagger.observe_outcome(formula.formula_id, regime, index < right)
            counted[formula.formula_id] = formula.held_out_trades
            touched.add(formula.formula_id)
        return tuple(sorted(touched))

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_tags(kept)

    return run_hypothesis_regime_tagger(
        tagger=tagger,
        control_socket=context.control_socket,
        read_outcomes=read_outcomes,
        publish_tags=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
