"""decision-quality-critic: how good the decision was, which is not how it turned out.

The one measurement this system needs most and that profit and loss cannot
provide. Over any run short enough to matter, the correlation between decision
quality and outcome is weak -- a good decision loses often and a bad one wins
often enough to be reinforced. A system that learns from outcomes alone learns
noise, and it learns it confidently because the money is real.

So this part scores the **process**, from evidence that existed before the
outcome did:

- **Was the evidence there?** A decision taken on complete features with a
  measured conviction is better than the same decision taken on guesses,
  whatever either returned.
- **Was the case against heard?** A trade whose devil's advocate raised nothing
  is different from one where an objection was overruled -- and the second is a
  worse decision even when it wins.
- **Was the failure anticipated?** A loss that its own premortem described is a
  decision that was made well. A loss nothing anticipated is where the lesson is.
- **Would the alternative have been better?** The counterfactual replayer says
  what standing aside, or the other bot's opinion, would have produced. A
  decision that beat its alternatives was good even when it lost money.

**Outcome is recorded and deliberately excluded from the score.** It is reported
alongside so the gap between them is visible -- a system where good decisions
consistently lose has a problem the score alone would hide.

**A near miss counts.** A trade that was almost taken and would have worked is
evidence about the process; ignoring it makes abstention look free.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.claim_verification import make_request, verify_against_facts
from runtime.learned_estimator import Estimate
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "decision-quality-critic"

PART_DECLARATION = PartDeclaration(
    part_id="decision-quality-critic",
    consumes=(
        "directional-opinion", "trade-episode", "validated-llm-output", "decision-rationale",
        "verified-snapshot", "counterfactual-outcome", "premortem-note", "counter-argument",
        "outcome-significance", "trade-narrative", "near-miss-episode",
    ),
    produces=("decision-quality-score", "part-health", "llm-request"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

PURPOSE = "judge-the-quality-of-a-decision-separately-from-its-outcome"

INSTRUCTION = (
    "Judge whether this was a good decision given what was known when it was made. Do not "
    "consider whether it made money. Use only the facts given; every number you write must "
    "be one of them."
)

# What the score is made of. Named and weighted here rather than buried in a
# formula, so a change to what this system considers a good decision is a change
# to a table somebody can read.
EVIDENCE_WAS_COMPLETE = "the-evidence-was-complete"
CONVICTION_WAS_MEASURED = "the-conviction-was-a-measured-frequency"
THE_CASE_AGAINST_WAS_HEARD = "the-case-against-was-heard-and-not-overruled"
THE_FAILURE_WAS_ANTICIPATED = "the-way-it-failed-was-anticipated"
IT_BEAT_ITS_ALTERNATIVES = "it-beat-what-the-alternatives-would-have-produced"


@dataclass(frozen=True)
class DecisionQualityScore:
    """How well a decision was made, and separately how it turned out."""

    venue_id: str
    symbol: str
    score: float
    components: dict
    outcome_was_good: bool | None
    outcome_is_excluded_from_the_score: bool
    was_a_near_miss: bool
    narrative: str
    unsupported_claims: tuple
    reason: str
    scored_at_ns: int

    @property
    def is_a_well_made_loss(self) -> bool:
        return self.outcome_was_good is False and self.score >= 0.6

    @property
    def is_a_badly_made_win(self) -> bool:
        """The case that a profit-and-loss-driven system reinforces and should not."""
        return self.outcome_was_good is True and self.score < 0.4


@dataclass
class CriticStanding:
    decisions_scored: int = 0
    near_misses_scored: int = 0
    well_made_losses: int = 0
    badly_made_wins: int = 0
    model_sentences_removed: int = 0
    mean_score: float = 0.0
    by_component: dict = field(default_factory=dict)


class DecisionQualityCritic:
    """Scores the process from what was known before the outcome existed."""

    def __init__(
        self,
        component_weights: dict,
        relative_tolerance: float,
        maximum_sentences: int,
        now_ns=time.time_ns,
    ) -> None:
        expected = {
            EVIDENCE_WAS_COMPLETE,
            CONVICTION_WAS_MEASURED,
            THE_CASE_AGAINST_WAS_HEARD,
            THE_FAILURE_WAS_ANTICIPATED,
            IT_BEAT_ITS_ALTERNATIVES,
        }
        if set(component_weights) != expected:
            raise ValueError(
                f"the score is made of exactly {sorted(expected)}; a weight table that names "
                f"something else is scoring a different thing than this part documents"
            )
        if abs(sum(component_weights.values()) - 1.0) > 1e-9:
            raise ValueError("the component weights must sum to one, or the score has no scale")
        self._weights = dict(component_weights)
        self._tolerance = relative_tolerance
        self._maximum_sentences = maximum_sentences
        self._now_ns = now_ns
        self._score_total = 0.0
        self.standing = CriticStanding()

    def facts_for(self, episode, rationale, premortem, argument, counterfactual) -> dict:
        facts = {
            "entry_conviction": round(episode.entry_conviction, 4),
            "realised_fraction": round(episode.realised_fraction, 6),
        }
        if counterfactual is not None:
            facts["alternative_realised_fraction"] = round(counterfactual, 6)
        if rationale is not None:
            facts["unsupported_claims_in_the_rationale"] = len(rationale.unsupported_claims)
        if premortem is not None:
            facts["failure_modes_anticipated"] = len(premortem.failure_modes)
        if argument is not None:
            facts["objections_raised"] = len(argument.objections)
        return facts

    def request(self, episode, rationale, premortem, argument, counterfactual):
        return make_request(
            purpose=PURPOSE,
            venue_id=episode.venue_id,
            symbol=episode.symbol,
            instruction=INSTRUCTION,
            facts=self.facts_for(episode, rationale, premortem, argument, counterfactual),
            maximum_sentences=self._maximum_sentences,
            now_ns=self._now_ns,
        )

    def components_for(
        self, episode, rationale, premortem, argument, counterfactual
    ) -> dict:
        """Each component, scored from evidence that existed before the outcome."""
        components = {}

        components[EVIDENCE_WAS_COMPLETE] = (
            1.0 if rationale is not None and rationale.is_fully_supported else 0.0
        )
        components[CONVICTION_WAS_MEASURED] = 1.0 if episode.conviction_was_measured else 0.0

        if argument is None:
            components[THE_CASE_AGAINST_WAS_HEARD] = 0.0
        elif argument.would_reverse_the_decision:
            # Overruled. A worse decision than one nothing objected to, even
            # when it wins.
            components[THE_CASE_AGAINST_WAS_HEARD] = 0.0
        else:
            components[THE_CASE_AGAINST_WAS_HEARD] = 1.0

        if premortem is None or episode.was_profitable:
            components[THE_FAILURE_WAS_ANTICIPATED] = 1.0 if episode.was_profitable else 0.0
        else:
            anticipated = any(
                word in mode.lower()
                for mode in premortem.failure_modes
                for word in episode.how_it_ended.lower().split("-")
            )
            components[THE_FAILURE_WAS_ANTICIPATED] = 1.0 if anticipated else 0.0

        if counterfactual is None:
            components[IT_BEAT_ITS_ALTERNATIVES] = 0.5
        else:
            components[IT_BEAT_ITS_ALTERNATIVES] = (
                1.0 if episode.realised_fraction >= counterfactual else 0.0
            )

        return components

    def score(
        self, episode, rationale=None, premortem=None, argument=None,
        counterfactual=None, model_output=None, is_a_near_miss=False,
    ) -> DecisionQualityScore:
        self.standing.decisions_scored += 1
        if is_a_near_miss:
            self.standing.near_misses_scored += 1

        components = self.components_for(episode, rationale, premortem, argument, counterfactual)
        for name, value in components.items():
            self.standing.by_component[name] = self.standing.by_component.get(name, 0.0) + value

        # Outcome is deliberately absent from this sum. It is recorded beside it
        # so the gap between decision quality and result stays visible.
        total = sum(components[name] * weight for name, weight in self._weights.items())

        facts = self.facts_for(episode, rationale, premortem, argument, counterfactual)
        narrative = ""
        unsupported = ()
        if model_output is not None:
            verified = verify_against_facts(
                model_output, facts, self._tolerance, require_a_citation=False
            )
            narrative = verified.text
            unsupported = verified.unsupported_claims
            self.standing.model_sentences_removed += len(verified.removed_sentences)

        self._score_total += total
        self.standing.mean_score = self._score_total / self.standing.decisions_scored

        result = DecisionQualityScore(
            venue_id=episode.venue_id,
            symbol=episode.symbol,
            score=total,
            components=components,
            outcome_was_good=episode.was_profitable,
            outcome_is_excluded_from_the_score=True,
            was_a_near_miss=is_a_near_miss,
            narrative=narrative,
            unsupported_claims=unsupported,
            reason=(
                f"decision quality {total:.2f} from "
                + ", ".join(
                    f"{name.replace('-', ' ')} {value:.0f}"
                    for name, value in sorted(components.items())
                )
                + f". The outcome was {'good' if episode.was_profitable else 'bad'} and is "
                f"excluded from the score: over any run short enough to matter, a good "
                f"decision loses often and a bad one wins often enough to be reinforced"
            ),
            scored_at_ns=self._now_ns(),
        )

        if result.is_a_well_made_loss:
            self.standing.well_made_losses += 1
        if result.is_a_badly_made_win:
            self.standing.badly_made_wins += 1
        return result


def describe_decision_quality(critic: DecisionQualityCritic) -> dict:
    scored = max(1, critic.standing.decisions_scored)
    return {
        "part_id": PART_ID,
        "decisions_scored": critic.standing.decisions_scored,
        "near_misses_scored": critic.standing.near_misses_scored,
        "well_made_losses": critic.standing.well_made_losses,
        "badly_made_wins": critic.standing.badly_made_wins,
        "mean_score": critic.standing.mean_score,
        "component_averages": {
            name: total / scored for name, total in sorted(critic.standing.by_component.items())
        },
        "model_sentences_removed": critic.standing.model_sentences_removed,
        "outcome_is_part_of_the_score": False,
    }


def run_decision_quality_critic(
    critic: DecisionQualityCritic, control_socket, read_episodes, publish_scores,
    publish_requests, health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        scores = []
        requests = []
        for episode, rationale, premortem, argument, counterfactual, output, near_miss in read_episodes(critic):
            if output is None:
                requests.append(critic.request(episode, rationale, premortem, argument, counterfactual))
            scores.append(
                critic.score(episode, rationale, premortem, argument, counterfactual, output, near_miss)
            )
        publish_scores(tuple(scores))
        publish_requests(tuple(requests))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
