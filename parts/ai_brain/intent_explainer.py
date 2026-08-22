"""intent-explainer: why this trade exists, in terms a person can check.

The rationale is what someone reads months later when they ask why the system
took a position. That makes it the most dangerous prose in the system: it will be
believed, and it is written at the one moment when the reasoning is freshest and
least examined.

So a model may **phrase** the explanation and may never **establish** anything in
it. The part assembles the measurements first -- convictions, weights, the regime,
the stop, what each bot said -- and the model is asked to write those into two or
three sentences. Every number in what comes back must trace to one of them, and a
sentence whose numbers do not is removed from the text and reported by name.

**A rationale with one invented number is worse than no rationale**, because it
reads exactly like the true ones. That is why removal is the response rather than
a warning: a warning that appears next to text most people will read anyway is
not a control.

**It works with no model at all.** If nothing answers, the facts themselves are
the rationale. A part whose explanation depended on a model being reachable would
go silent exactly when the model is down, and a decision with no recorded reason
is worse than one recorded bluntly.

**Feature attribution is included when it exists** and its absence is stated
rather than papered over: "the model weighted book imbalance most" is a different
claim from "we do not know what the model weighted".
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.claim_verification import make_request, verify_against_facts, written_without_a_model
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trade_intent import DecisionRationale

PART_ID = "intent-explainer"

PART_DECLARATION = PartDeclaration(
    part_id="intent-explainer",
    consumes=(
        "trade-intent", "directional-opinion", "validated-llm-output",
        "verified-snapshot", "feature-attribution",
    ),
    produces=("decision-rationale", "llm-request", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

PURPOSE = "explain-why-this-trade-exists"

INSTRUCTION = (
    "Write why this position is being taken, using only the facts given. Every number "
    "you write must be one of them. Do not add context, do not estimate, and do not "
    "explain what the facts mean in general -- only what they mean here."
)


@dataclass
class ExplainerStanding:
    rationales_written: int = 0
    written_by_a_model: int = 0
    written_without_a_model: int = 0
    sentences_removed: int = 0
    fully_supported: int = 0
    attribution_missing: int = 0


class IntentExplainer:
    """Assembles the facts, asks a model to phrase them, and keeps only what checks out."""

    def __init__(
        self,
        relative_tolerance: float,
        maximum_sentences: int,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < relative_tolerance < 1.0:
            raise ValueError(
                "the tolerance is how far a written number may sit from the measured one, as "
                "a fraction of it, and must be inside (0, 1)"
            )
        self._tolerance = relative_tolerance
        self._maximum_sentences = maximum_sentences
        self._now_ns = now_ns
        self._attribution: dict[tuple[str, str], dict] = {}
        self._snapshots: dict[tuple[str, str], dict] = {}
        self.standing = ExplainerStanding()

    def observe_feature_attribution(self, venue_id: str, symbol: str, attribution: dict) -> None:
        """Which features moved the conviction, when a model could say."""
        self._attribution[(venue_id, symbol)] = dict(attribution)

    def observe_verified_snapshot(self, venue_id: str, symbol: str, snapshot: dict) -> None:
        """Measurements someone else has already verified, safe to cite."""
        self._snapshots[(venue_id, symbol)] = dict(snapshot)

    def facts_for(self, intent, opinions) -> dict:
        """The closed set of things the rationale may contain."""
        key = (intent.venue_id, intent.symbol)
        facts = {
            "weighted_conviction": round(intent.conviction.value, 4),
            "contributing_bots": len(intent.contributing_bots),
            "dissenting_bots": len(intent.dissenting_bots),
        }
        if intent.stop_price is not None:
            facts["stop_price"] = intent.stop_price
        if intent.horizon_seconds:
            facts["horizon_seconds"] = intent.horizon_seconds

        for opinion in opinions:
            facts[f"{opinion.bot}_conviction"] = round(opinion.conviction.value, 4)
            weight = intent.opinion_weights.get(opinion.bot)
            if weight is not None:
                facts[f"{opinion.bot}_weight"] = round(weight, 4)

        attribution = self._attribution.get(key)
        if attribution:
            for name, contribution in attribution.items():
                facts[f"attribution_{name}"] = round(contribution, 4)
        else:
            self.standing.attribution_missing += 1

        facts.update(self._snapshots.get(key, {}))
        return facts

    def request(self, intent, opinions):
        """What to ask a model. The facts travel with the question."""
        return make_request(
            purpose=PURPOSE,
            venue_id=intent.venue_id,
            symbol=intent.symbol,
            instruction=INSTRUCTION,
            facts=self.facts_for(intent, opinions),
            maximum_sentences=self._maximum_sentences,
            now_ns=self._now_ns,
        )

    def explain(self, intent, opinions, model_output: str | None) -> DecisionRationale:
        """One rationale, containing nothing that cannot be traced to a measurement."""
        self.standing.rationales_written += 1
        facts = self.facts_for(intent, opinions)

        if model_output is None:
            self.standing.written_without_a_model += 1
            verified = written_without_a_model(intent.reason, facts)
        else:
            self.standing.written_by_a_model += 1
            verified = verify_against_facts(
                model_output, facts, self._tolerance, require_a_citation=True
            )
            self.standing.sentences_removed += len(verified.removed_sentences)

        if verified.is_fully_supported:
            self.standing.fully_supported += 1

        headline = (
            f"{intent.side} {intent.symbol}: {intent.action} at "
            f"{intent.conviction.value:.1%} conviction, {intent.agreement}"
        )
        body = verified.text or intent.reason

        attribution = self._attribution.get((intent.venue_id, intent.symbol))
        if not attribution:
            # Stated rather than omitted: "we do not know what the model
            # weighted" is a different claim from "the model weighted nothing".
            body += (
                " No feature attribution was available for this decision, so what the model "
                "weighted most is not known."
            )

        return DecisionRationale(
            venue_id=intent.venue_id,
            symbol=intent.symbol,
            action=intent.action,
            headline=headline,
            body=body,
            citations=dict(verified.citations),
            unsupported_claims=verified.unsupported_claims,
            was_written_by_a_model=verified.was_written_by_a_model,
            reason=(
                f"{len(verified.kept_sentences)} sentence(s) kept, "
                f"{len(verified.removed_sentences)} removed for citing numbers nothing "
                f"measured, over {verified.numbers_checked} number(s) checked against "
                f"{len(facts)} fact(s)"
            ),
            written_at_ns=self._now_ns(),
        )


def describe_explaining(explainer: IntentExplainer) -> dict:
    return {
        "part_id": PART_ID,
        "rationales_written": explainer.standing.rationales_written,
        "written_by_a_model": explainer.standing.written_by_a_model,
        "written_without_a_model": explainer.standing.written_without_a_model,
        "fully_supported": explainer.standing.fully_supported,
        "sentences_removed_as_unsupported": explainer.standing.sentences_removed,
        "decisions_with_no_feature_attribution": explainer.standing.attribution_missing,
    }


def run_intent_explainer(
    explainer: IntentExplainer, control_socket, read_intents_and_output,
    publish_rationales, publish_requests, health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        rationales = []
        requests = []
        for intent, opinions, model_output in read_intents_and_output(explainer):
            if model_output is None:
                requests.append(explainer.request(intent, opinions))
            rationales.append(explainer.explain(intent, opinions, model_output))
        publish_rationales(tuple(rationales))
        publish_requests(tuple(requests))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
