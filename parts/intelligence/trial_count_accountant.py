"""trial-count-accountant: how many things were tried before this one looked good.

The most dangerous number in a learning system is the one nobody counts. Test
twenty hypotheses at the 5% level and one comes back significant by construction;
test two hundred and ten do. Every part of this system that searches -- the
hypothesis block, the mutation of instructions, the model versions, the trade
clusters -- generates trials, and the significance of any result depends on how
many were run to find it.

This is the ledger of that count, and it is a ledger rather than a counter for a
reason: **the trials have to be recorded when they are run, not remembered when a
result is judged.** A system that counted trials retrospectively would count the
ones it remembered, which are the ones that worked.

What it produces:

- **The raw count**, by family. Instructions mutated, models versioned,
  hypotheses raised, clusters mined -- each is a separate search and its own
  multiple-comparison problem.
- **The corrected significance threshold.** A result from a family of two hundred
  trials needs a far smaller p-value to mean the same thing, and this part
  computes what it needs rather than leaving everyone to remember.
- **Whether a claimed result clears it.** Not whether it is true -- whether it
  clears the bar its own search implies.

**Trials are counted even when nothing came of them.** That is the whole point: a
family where nineteen attempts were abandoned and the twentieth looked good has
twenty trials, and a ledger that recorded only the survivor would report one.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "trial-count-accountant"

PART_DECLARATION = PartDeclaration(
    part_id="trial-count-accountant",
    consumes=("opportunity-instruction", "mutated-hypothesis", "model-version", "trade-cluster"),
    produces=("trial-ledger", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

INSTRUCTIONS = "opportunity-instructions"
HYPOTHESES = "mutated-hypotheses"
MODEL_VERSIONS = "model-versions"
TRADE_CLUSTERS = "trade-clusters"

CLEARS_THE_BAR = "clears-the-bar-its-own-search-implies"
DOES_NOT_CLEAR = "does-not-clear-the-bar-its-own-search-implies"
NOTHING_RECORDED = "no-trials-recorded-for-this-family"


@dataclass(frozen=True)
class TrialLedger:
    """How many things a family tried, and what a result from it must clear."""

    family: str
    trials: int
    abandoned: int
    survived: int
    nominal_significance: float
    corrected_significance: float
    expected_false_positives: float
    reason: str
    counted_at_ns: int

    def clears(self, p_value: float) -> bool:
        return p_value <= self.corrected_significance


@dataclass(frozen=True)
class SignificanceVerdict:
    """Whether one claimed result clears the bar its own search implies."""

    family: str
    claim: str
    p_value: float
    corrected_significance: float
    trials: int
    verdict: str
    reason: str
    judged_at_ns: int

    @property
    def clears_the_bar(self) -> bool:
        return self.verdict == CLEARS_THE_BAR


@dataclass
class AccountantStanding:
    trials_recorded: int = 0
    abandoned_recorded: int = 0
    verdicts: int = 0
    cleared: int = 0
    did_not_clear: int = 0
    by_family: dict = field(default_factory=dict)
    largest_family: int = 0


class TrialCountAccountant:
    """Counts every trial as it is run, and computes what a result must clear."""

    def __init__(self, nominal_significance: float, now_ns=time.time_ns) -> None:
        if not 0.0 < nominal_significance < 0.5:
            raise ValueError("the nominal significance level is a tail probability")
        self._nominal = nominal_significance
        self._now_ns = now_ns
        self._trials: dict[str, int] = {}
        self._abandoned: dict[str, int] = {}
        self._survived: dict[str, int] = {}
        self.standing = AccountantStanding()

    def record_trial(self, family: str, survived: bool) -> None:
        """One thing tried, counted whether or not anything came of it.

        Counted at the time rather than remembered afterwards: a system counting
        retrospectively counts the ones it remembers, which are the ones that
        worked.
        """
        self._trials[family] = self._trials.get(family, 0) + 1
        if survived:
            self._survived[family] = self._survived.get(family, 0) + 1
        else:
            self._abandoned[family] = self._abandoned.get(family, 0) + 1
            self.standing.abandoned_recorded += 1
        self.standing.trials_recorded += 1
        self.standing.by_family[family] = self._trials[family]
        self.standing.largest_family = max(self.standing.largest_family, self._trials[family])

    def corrected_significance(self, family: str) -> float:
        """The threshold a result from this family must clear.

        Bonferroni: divide the nominal level by the number of trials. Blunt, and
        blunt in the safe direction -- it is conservative, which is the right way
        for a bar to be wrong when the alternative is a system convinced by its
        own search.
        """
        trials = max(1, self._trials.get(family, 0))
        return self._nominal / trials

    def ledger(self, family: str) -> TrialLedger:
        trials = self._trials.get(family, 0)
        corrected = self.corrected_significance(family)
        return TrialLedger(
            family=family,
            trials=trials,
            abandoned=self._abandoned.get(family, 0),
            survived=self._survived.get(family, 0),
            nominal_significance=self._nominal,
            corrected_significance=corrected,
            expected_false_positives=trials * self._nominal,
            reason=(
                f"{trials} trial(s) in {family} ({self._abandoned.get(family, 0)} abandoned, "
                f"{self._survived.get(family, 0)} survived). At the nominal "
                f"{self._nominal:.0%} level that search alone would be expected to produce "
                f"{trials * self._nominal:.1f} result(s) that look significant and are not, "
                f"so a result here must clear {corrected:.5f} to mean what {self._nominal:.0%} "
                f"means for a single test"
                if trials
                else f"no trial has been recorded in {family}"
            ),
            counted_at_ns=self._now_ns(),
        )

    def all_ledgers(self) -> tuple:
        return tuple(self.ledger(family) for family in sorted(self._trials))

    def judge(self, family: str, claim: str, p_value: float) -> SignificanceVerdict:
        """Whether a claimed result clears the bar its own search implies."""
        self.standing.verdicts += 1
        trials = self._trials.get(family, 0)
        corrected = self.corrected_significance(family)

        if trials == 0:
            return SignificanceVerdict(
                family=family, claim=claim, p_value=p_value,
                corrected_significance=corrected, trials=0, verdict=NOTHING_RECORDED,
                reason=(
                    f"no trial has been recorded in {family}, so there is no search to correct "
                    f"for -- which is not the same as the search having been small"
                ),
                judged_at_ns=self._now_ns(),
            )

        clears = p_value <= corrected
        if clears:
            self.standing.cleared += 1
        else:
            self.standing.did_not_clear += 1

        return SignificanceVerdict(
            family=family, claim=claim, p_value=p_value,
            corrected_significance=corrected, trials=trials,
            verdict=CLEARS_THE_BAR if clears else DOES_NOT_CLEAR,
            reason=(
                f"{claim} has p = {p_value:.5f} against a bar of {corrected:.5f}, which is the "
                f"{self._nominal:.0%} level divided by the {trials} trial(s) this family ran"
                + (
                    ". It clears it -- which is a statement about the bar, not about whether "
                    "the mechanism is real"
                    if clears
                    else f". It would clear an uncorrected {self._nominal:.0%} bar, and that "
                    f"is exactly the mistake this part exists to prevent"
                )
            ),
            judged_at_ns=self._now_ns(),
        )


def describe_trial_counting(accountant: TrialCountAccountant) -> dict:
    return {
        "part_id": PART_ID,
        "trials_recorded": accountant.standing.trials_recorded,
        "abandoned_trials_recorded": accountant.standing.abandoned_recorded,
        "families": dict(sorted(accountant.standing.by_family.items())),
        "largest_family": accountant.standing.largest_family,
        "verdicts": accountant.standing.verdicts,
        "results_that_cleared": accountant.standing.cleared,
        "results_that_did_not": accountant.standing.did_not_clear,
        "nominal_significance": accountant._nominal,
        "corrected_thresholds": {
            family: accountant.corrected_significance(family)
            for family in sorted(accountant._trials)
        },
    }


def run_trial_count_accountant(
    accountant: TrialCountAccountant, control_socket, read_trials, publish_ledgers,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_trials(accountant)
        publish_ledgers(accountant.all_ledgers())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
