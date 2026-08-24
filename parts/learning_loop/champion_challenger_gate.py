"""champion-challenger-gate: whether the new model actually replaces the old one.

The decision that makes every other learning part matter or not. A gate that
promotes too readily turns the whole system into a random walk over model
versions; one that never promotes makes retraining ceremonial.

A challenger must beat the champion on **all** of these, and the list is short
because each covers a distinct way a promotion goes wrong:

- **Out-of-sample performance, on the same data.** Comparing a challenger's
  validation score to a champion's from a different period compares two markets,
  not two models.
- **A refutation verdict.** A challenger nothing tried to break is a challenger
  nobody tested.
- **The bar its own search implies.** Twenty challengers were trained; the best of
  twenty beats the champion by chance regularly, and the trial ledger is what
  turns that from a promotion into a coin flip nobody counted.
- **Recall.** A challenger that scores better and has forgotten an era is worse,
  and only the forgetting report can see it -- the era it forgot is by
  construction one the recent data does not test.

**Promotion is atomic and reversible.** The old champion is retained as the
challenger, so a promotion that turns out badly is undone by promoting back
rather than by retraining from nothing.

**A tie keeps the champion.** The incumbent has a live record and the challenger
has a validation score, and those are not the same evidence.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "champion-challenger-gate"

PART_DECLARATION = PartDeclaration(
    part_id="champion-challenger-gate",
    consumes=("model-version", "refutation-verdict", "trial-ledger", "forgetting-report"),
    produces=("champion-choice", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

PROMOTE = "promote-the-challenger"
KEEP = "keep-the-champion"
NOTHING_TO_COMPARE = "there-is-no-challenger-to-compare"

DID_NOT_BEAT_IT = "it-did-not-beat-the-champion-on-the-same-data"
NOT_REFUTATION_TESTED = "nothing-tried-to-break-it"
WAS_REFUTED = "the-refutation-battery-broke-it"
DOES_NOT_CLEAR_ITS_TRIALS = "the-best-of-many-challengers-beats-the-champion-by-chance"
HAS_FORGOTTEN = "it-scores-better-and-has-forgotten-an-era"
A_TIE = "a-tie-keeps-the-champion"


@dataclass(frozen=True)
class ChampionChoice:
    """Which model is live, and what did or did not justify changing it."""

    model_name: str
    decision: str
    champion_version: str | None
    challenger_version: str | None
    champion_score: float | None
    challenger_score: float | None
    conditions_met: dict
    failing_conditions: tuple
    is_reversible: bool
    reason: str
    decided_at_ns: int

    @property
    def promotes(self) -> bool:
        return self.decision == PROMOTE


@dataclass
class GateStanding:
    decisions: int = 0
    promotions: int = 0
    kept: int = 0
    reversals: int = 0
    by_failing_condition: dict = field(default_factory=dict)
    largest_improvement_promoted: float | None = None


class ChampionChallengerGate:
    """Promotes a challenger only when it clears every condition, and keeps the old one."""

    def __init__(
        self,
        minimum_improvement: float,
        minimum_recall: float,
        now_ns=time.time_ns,
    ) -> None:
        if minimum_improvement <= 0:
            raise ValueError(
                "a tie keeps the champion: the incumbent has a live record and the challenger "
                "has a validation score, and those are not the same evidence"
            )
        if not 0.0 < minimum_recall <= 1.0:
            raise ValueError("recall is a fraction and its bar must be inside (0, 1]")
        self._minimum_improvement = minimum_improvement
        self._minimum_recall = minimum_recall
        self._now_ns = now_ns
        self._champions: dict[str, tuple] = {}
        self._challengers: dict[str, tuple] = {}
        self._verdicts: dict[str, str] = {}
        self._trial_clears: dict[str, bool] = {}
        self._recall: dict[str, float] = {}
        self._previous_champions: dict[str, tuple] = {}
        self.standing = GateStanding()

    def observe_champion(self, model_name: str, version: str, score_on_shared_data: float) -> None:
        """The champion's score on the same held-out data the challenger was scored on.

        The same data, because comparing scores from different periods compares
        two markets rather than two models.
        """
        self._champions[model_name] = (version, score_on_shared_data)

    def observe_challenger(self, model_name: str, version: str, score_on_shared_data: float) -> None:
        self._challengers[model_name] = (version, score_on_shared_data)

    def observe_refutation_verdict(self, model_name: str, verdict: str) -> None:
        self._verdicts[model_name] = verdict

    def observe_trial_verdict(self, model_name: str, clears_its_bar: bool) -> None:
        self._trial_clears[model_name] = clears_its_bar

    def observe_forgetting_report(self, model_name: str, recall: float) -> None:
        self._recall[model_name] = recall

    def decide(self, model_name: str) -> ChampionChoice:
        self.standing.decisions += 1
        champion = self._champions.get(model_name)
        challenger = self._challengers.get(model_name)

        if challenger is None:
            return self._choice(
                model_name, NOTHING_TO_COMPARE, champion, None, {}, (),
                "no challenger has been trained for this model",
            )

        champion_version, champion_score = champion if champion else (None, None)
        challenger_version, challenger_score = challenger

        improvement = (
            None if champion_score is None else challenger_score - champion_score
        )

        conditions = {
            DID_NOT_BEAT_IT: (
                improvement is None or improvement >= self._minimum_improvement
            ),
            NOT_REFUTATION_TESTED: model_name in self._verdicts,
            WAS_REFUTED: self._verdicts.get(model_name) != "refuted",
            DOES_NOT_CLEAR_ITS_TRIALS: self._trial_clears.get(model_name, False),
            HAS_FORGOTTEN: self._recall.get(model_name, 1.0) >= self._minimum_recall,
        }

        failing = tuple(name for name, met in conditions.items() if not met)
        for name in failing:
            self.standing.by_failing_condition[name] = (
                self.standing.by_failing_condition.get(name, 0) + 1
            )

        if failing:
            self.standing.kept += 1
            return self._choice(
                model_name, KEEP, champion, challenger, conditions, failing,
                f"the challenger fails {len(failing)} condition(s): "
                + "; ".join(self._explain(name, improvement, model_name) for name in failing),
            )

        # Promotion keeps the old champion as the challenger, so a bad promotion
        # is undone by promoting back rather than by retraining from nothing.
        if champion is not None:
            self._previous_champions[model_name] = champion
            self._challengers[model_name] = champion
        self._champions[model_name] = challenger

        self.standing.promotions += 1
        if improvement is not None and (
            self.standing.largest_improvement_promoted is None
            or improvement > self.standing.largest_improvement_promoted
        ):
            self.standing.largest_improvement_promoted = improvement

        return self._choice(
            model_name, PROMOTE, challenger, champion, conditions, (),
            f"{challenger_version} scores {challenger_score:.4f} against "
            + (
                f"{champion_version}'s {champion_score:.4f} on the same data, "
                f"a {improvement:+.4f} improvement"
                if champion_score is not None
                else "no incumbent"
            )
            + f"; refutation did not break it, it clears the bar its own search implies, and "
            f"it recalls {self._recall.get(model_name, 1.0):.0%} of what it was trained on. "
            f"The old champion is kept as the challenger, so this is undone by promoting back "
            f"rather than by retraining from nothing",
        )

    def revert(self, model_name: str) -> ChampionChoice:
        """Undo a promotion by promoting the previous champion back."""
        previous = self._previous_champions.pop(model_name, None)
        if previous is None:
            return self._choice(
                model_name, KEEP, self._champions.get(model_name), None, {}, (),
                "there is no previous champion to revert to",
            )
        current = self._champions.get(model_name)
        self._champions[model_name] = previous
        if current is not None:
            self._challengers[model_name] = current
        self.standing.reversals += 1
        return self._choice(
            model_name, PROMOTE, previous, current, {}, (),
            f"reverted to {previous[0]}: a promotion that turned out badly is undone by "
            f"promoting back, which is why the old champion was kept",
        )

    def live_version(self, model_name: str) -> str | None:
        champion = self._champions.get(model_name)
        return None if champion is None else champion[0]

    def _explain(self, condition: str, improvement, model_name: str) -> str:
        if condition == DID_NOT_BEAT_IT:
            return (
                f"it improves on the champion by {improvement:+.4f}, below the "
                f"{self._minimum_improvement:.4f} needed -- a tie keeps the champion, because "
                f"the incumbent has a live record and the challenger has a validation score"
            )
        if condition == NOT_REFUTATION_TESTED:
            return "nothing tried to break it, and an untested challenger is untested"
        if condition == WAS_REFUTED:
            return "the refutation battery broke it"
        if condition == DOES_NOT_CLEAR_ITS_TRIALS:
            return (
                "it does not clear the bar its own search implies; the best of twenty "
                "challengers beats the champion by chance regularly"
            )
        if condition == HAS_FORGOTTEN:
            return (
                f"it recalls {self._recall.get(model_name, 1.0):.0%} of what it was trained "
                f"on, below {self._minimum_recall:.0%} -- and the era it forgot is by "
                f"construction one the recent data does not test"
            )
        return condition

    def _choice(
        self, model_name, decision, live, other, conditions, failing, reason
    ) -> ChampionChoice:
        return ChampionChoice(
            model_name=model_name,
            decision=decision,
            champion_version=live[0] if live else None,
            challenger_version=other[0] if other else None,
            champion_score=live[1] if live else None,
            challenger_score=other[1] if other else None,
            conditions_met=dict(conditions),
            failing_conditions=failing,
            is_reversible=model_name in self._previous_champions,
            reason=reason,
            decided_at_ns=self._now_ns(),
        )


def describe_champion_choice(gate: ChampionChallengerGate) -> dict:
    return {
        "part_id": PART_ID,
        "decisions": gate.standing.decisions,
        "promotions": gate.standing.promotions,
        "kept_the_champion": gate.standing.kept,
        "reversals": gate.standing.reversals,
        "by_failing_condition": dict(sorted(gate.standing.by_failing_condition.items())),
        "largest_improvement_promoted": gate.standing.largest_improvement_promoted,
        "live_versions": {
            model: gate.live_version(model) for model in sorted(gate._champions)
        },
        "a_tie_keeps_the_champion": True,
    }


def run_champion_challenger_gate(
    gate: ChampionChallengerGate, control_socket, read_versions, publish_choices,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        models = read_versions(gate)
        publish_choices(tuple(gate.decide(model) for model in models))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_champion_choice(gate),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch

    versions = Batch(read=context.bus.reader("model-version"))
    refutations = Batch(read=context.bus.reader("refutation-verdict"))
    ledgers = Batch(read=context.bus.reader("trial-ledger"))
    forgetting = Batch(read=context.bus.reader("forgetting-report"))
    publish_choices = context.bus.publisher_for("champion-choice")
    gate = ChampionChallengerGate(
        minimum_improvement=context.number("champion_minimum_improvement"),
        minimum_recall=context.number("champion_minimum_recall"),
    )
    # The ledger is a threshold the family's search implies; whether a version
    # clears it is that version's own corrected significance against it.
    ledger_by_family: dict[str, object] = {}
    significance_by_model: dict[str, float] = {}

    def judge_trials(model_name: str) -> None:
        ledger = ledger_by_family.get(model_name)
        p_value = significance_by_model.get(model_name)
        if ledger is not None and p_value is not None:
            gate.observe_trial_verdict(model_name, ledger.clears(p_value))

    def read_versions(_gate):
        touched = set()
        for version in versions.payloads():
            if version.corrected_significance is not None:
                significance_by_model[version.model_name] = version.corrected_significance
            if version.role == "champion" or version.promoted:
                gate.observe_champion(version.model_name, version.version, version.validation_score)
            else:
                gate.observe_challenger(version.model_name, version.version, version.validation_score)
            if version.refutation_verdict:
                gate.observe_refutation_verdict(version.model_name, version.refutation_verdict)
            touched.add(version.model_name)
        for verdict in refutations.payloads():
            gate.observe_refutation_verdict(verdict.instruction_id, verdict.verdict)
            touched.add(verdict.instruction_id)
        for ledger in ledgers.payloads():
            ledger_by_family[ledger.family] = ledger
            touched.add(ledger.family)
        for model_name in touched:
            judge_trials(model_name)
        for report in forgetting.payloads():
            if report.overall_recall is not None:
                gate.observe_forgetting_report(report.model_name, report.overall_recall)
                touched.add(report.model_name)
        return tuple(sorted(touched))

    def publish(choices) -> None:
        if choices:
            publish_choices(choices)

    return run_champion_challenger_gate(
        gate=gate,
        control_socket=context.control_socket,
        read_versions=read_versions,
        publish_choices=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
