"""model-registry: every model that has existed, and what justified each one.

The durable record behind every promotion. Without it a system can only say which
model is live; with it, it can say which models have existed, what each was
trained on, what evidence promoted it, and what happened next -- which is the only
way to answer "has our model selection actually been adding anything".

What it records that a version number does not:

- **The evidence that justified the version.** A refutation verdict and a trial
  count, so a promotion made on a result that never cleared its own bar is
  visible in the record rather than only in its consequences.
- **The lineage.** Which version a model was retrained from, so a chain of
  successive small overfits is legible as a chain rather than as a sequence of
  independent improvements.
- **What happened after promotion**, once the scorer has seen it live. That is
  the only way to tell selection that works from selection that keeps picking
  whichever model was luckiest on the validation tail.

**A version is immutable.** Recording an outcome adds a new record rather than
editing the old one; a registry that could be edited is a registry that will be.

**A model without a refutation verdict can be registered but never marked as
promoted on evidence**, and the difference is stated in the record rather than
inferred.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learning_types import ModelVersion
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "model-registry"

PART_DECLARATION = PartDeclaration(
    part_id="model-registry",
    consumes=("retrain-request", "refutation-verdict", "trial-ledger"),
    produces=("model-version", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

REGISTERED = "registered"
REJECTED_DUPLICATE = "a-version-with-this-name-is-already-registered"

CHAMPION = "champion"
CHALLENGER = "challenger"
RETIRED = "retired"


@dataclass(frozen=True)
class VersionOutcome:
    """What happened after a version went live. Appended, never edited."""

    model_name: str
    version: str
    trades: int
    hit_rate: float
    realised: float
    recorded_at_ns: int


@dataclass
class RegistryStanding:
    versions_registered: int = 0
    duplicates_rejected: int = 0
    promoted_on_evidence: int = 0
    promoted_without_a_verdict: int = 0
    outcomes_recorded: int = 0
    longest_lineage: int = 0
    by_role: dict = field(default_factory=dict)


class ModelRegistry:
    """Records every model version, its lineage, and what justified promoting it."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._versions: dict[tuple[str, str], ModelVersion] = {}
        self._parents: dict[tuple[str, str], str | None] = {}
        self._outcomes: dict[tuple[str, str], list] = {}
        self._verdicts: dict[str, str] = {}
        self._trial_bars: dict[str, tuple] = {}
        self.standing = RegistryStanding()

    def observe_refutation_verdict(self, model_name: str, verdict: str) -> None:
        self._verdicts[model_name] = verdict

    def observe_trial_ledger(self, family: str, trials: int, corrected_significance: float) -> None:
        self._trial_bars[family] = (trials, corrected_significance)

    def register(
        self,
        model_name: str,
        version: str,
        role: str,
        trained_on_windows: int,
        validation_score: float,
        family: str,
        parent_version: str | None = None,
        promoted: bool = False,
    ) -> tuple[ModelVersion | None, str]:
        """One version, with the evidence that stands behind it."""
        key = (model_name, version)
        if key in self._versions:
            # Immutable: a registry that can be edited is one that will be.
            self.standing.duplicates_rejected += 1
            return None, REJECTED_DUPLICATE

        verdict = self._verdicts.get(model_name)
        trials, corrected = self._trial_bars.get(family, (0, None))

        record = ModelVersion(
            model_name=model_name,
            version=version,
            role=role,
            trained_on_windows=trained_on_windows,
            validation_score=validation_score,
            refutation_verdict=verdict,
            trials_in_family=trials,
            corrected_significance=corrected,
            promoted=promoted,
            reason=(
                f"{model_name} {version} as {role}, trained on {trained_on_windows} window(s), "
                f"validation {validation_score:.4f}"
                + (
                    f"; refutation verdict: {verdict}"
                    if verdict is not None
                    else "; no refutation verdict, so this cannot be recorded as promoted on "
                    "evidence however good the validation looks"
                )
                + (
                    f"; {trials} trial(s) in {family}, needing p <= {corrected:.5f}"
                    if corrected is not None
                    else f"; no trial count for {family}, so the bar its search implies is "
                    f"unknown"
                )
                + (
                    f"; retrained from {parent_version}"
                    if parent_version
                    else "; no parent, so this is a fresh model rather than a refinement"
                )
            ),
            registered_at_ns=self._now_ns(),
        )

        self._versions[key] = record
        self._parents[key] = parent_version
        self.standing.versions_registered += 1
        self.standing.by_role[role] = self.standing.by_role.get(role, 0) + 1
        if promoted:
            if record.was_promoted_on_evidence:
                self.standing.promoted_on_evidence += 1
            else:
                self.standing.promoted_without_a_verdict += 1
        self.standing.longest_lineage = max(
            self.standing.longest_lineage, len(self.lineage(model_name, version))
        )
        return record, REGISTERED

    def record_outcome(
        self, model_name: str, version: str, trades: int, hit_rate: float, realised: float
    ) -> VersionOutcome:
        """What happened after this version went live. Appended, never edited.

        The only way to tell selection that works from selection that keeps
        picking whichever model was luckiest on the validation tail.
        """
        outcome = VersionOutcome(
            model_name=model_name, version=version, trades=trades, hit_rate=hit_rate,
            realised=realised, recorded_at_ns=self._now_ns(),
        )
        self._outcomes.setdefault((model_name, version), []).append(outcome)
        self.standing.outcomes_recorded += 1
        return outcome

    def lineage(self, model_name: str, version: str) -> tuple:
        """Every version this one descends from.

        A chain of successive small overfits is legible as a chain rather than
        as a sequence of independent improvements.
        """
        chain = [version]
        seen = {version}
        current = version
        while True:
            parent = self._parents.get((model_name, current))
            if parent is None or parent in seen:
                break
            chain.append(parent)
            seen.add(parent)
            current = parent
        return tuple(reversed(chain))

    def outcomes_for(self, model_name: str, version: str) -> tuple:
        return tuple(self._outcomes.get((model_name, version), ()))

    def version(self, model_name: str, version: str) -> ModelVersion | None:
        return self._versions.get((model_name, version))

    def versions_of(self, model_name: str) -> tuple:
        return tuple(
            record
            for (name, _), record in sorted(self._versions.items())
            if name == model_name
        )

    def selection_has_added_something(self, model_name: str) -> tuple[bool | None, str]:
        """Whether promoting successive versions has actually improved anything.

        The question a version number cannot answer, and the one that says
        whether the whole champion-challenger machinery is earning its keep.
        """
        promoted = [
            record
            for record in self.versions_of(model_name)
            if record.promoted and self.outcomes_for(model_name, record.version)
        ]
        if len(promoted) < 2:
            return None, (
                f"{len(promoted)} promoted version(s) with a live outcome; two are needed "
                f"before selection can be said to have added anything"
            )
        realised = [
            sum(outcome.realised for outcome in self.outcomes_for(model_name, record.version))
            for record in promoted
        ]
        improving = realised[-1] > realised[0]
        return improving, (
            f"the latest promoted version has produced {realised[-1]:+.4f} against "
            f"{realised[0]:+.4f} for the first, over {len(promoted)} promoted version(s). "
            + (
                "Selection has been adding something"
                if improving
                else "Selection has not been adding anything, which is what keeps picking "
                "whichever model was luckiest on the validation tail looks like"
            )
        )


def describe_registry(registry: ModelRegistry) -> dict:
    return {
        "part_id": PART_ID,
        "versions_registered": registry.standing.versions_registered,
        "duplicates_rejected": registry.standing.duplicates_rejected,
        "promoted_on_evidence": registry.standing.promoted_on_evidence,
        "promoted_without_a_refutation_verdict": registry.standing.promoted_without_a_verdict,
        "outcomes_recorded": registry.standing.outcomes_recorded,
        "longest_lineage": registry.standing.longest_lineage,
        "by_role": dict(sorted(registry.standing.by_role.items())),
        "versions_are_immutable": True,
    }


def run_model_registry(
    registry: ModelRegistry, control_socket, read_requests, publish_versions,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        registrations = read_requests(registry)
        versions = []
        for registration in registrations:
            record, _ = registry.register(**registration)
            if record is not None:
                versions.append(record)
        publish_versions(tuple(versions))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
