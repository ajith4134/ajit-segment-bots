"""golden-case-keeper: real past decisions whose right answer is now known.

Prompts cannot be evaluated against opinion. A human reading two answers and
preferring one measures the reader, and doing that at scale produces prompts tuned
to sound good. The only ground truth available here is what actually happened, so a
golden case is built from a closed trade: the facts as they were at decision time,
and the outcome that arrived afterwards.

The hard constraint is temporal. **A case must contain nothing that was not known
when the decision was made.** It is trivially easy to build a golden set that
includes the outcome in the facts, and a prompt evaluated on it scores perfectly
while being useless live. So the keeper stamps decision time on every case and
refuses any fact measured after it.

Three further rules, each preventing a specific way a golden set rots:

- **The set is balanced against outcomes.** A set of ninety winners and ten losers
  rewards a prompt that always says yes. Cases are admitted with the balance
  tracked, and a set that has tipped is reported rather than silently used.
- **A case is retired when the world it describes is gone.** A delisted symbol or a
  changed funding interval means the case tests a market that no longer exists.
- **Cases are never edited to make a prompt look better.** There is no edit method,
  and a case whose expected answer turned out wrong is retired with its reason, not
  adjusted.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.llm_types import GoldenCase
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "golden-case-keeper"

PART_DECLARATION = PartDeclaration(
    part_id="golden-case-keeper",
    consumes=("validated-llm-output", "closed-trade"),
    produces=("golden-case", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

KEPT = "kept"
CONTAINS_THE_FUTURE = "a-fact-was-measured-after-the-decision"
OUTCOME_NOT_KNOWN = "the-trade-has-not-closed-so-there-is-no-right-answer"
ALREADY_KEPT = "already-in-the-set"
RETIRED = "retired"
SET_IS_UNBALANCED = "the-set-has-tipped-towards-one-outcome"

# Why a case stops being usable. Never because a prompt scored badly on it.
WORLD_HAS_CHANGED = "the-instrument-or-its-rules-no-longer-exist"
EXPECTATION_WAS_WRONG = "the-expected-answer-turned-out-to-be-wrong"


@dataclass(frozen=True)
class CaseOutcome:
    case_id: str
    state: str
    case: GoldenCase | None
    offending_facts: tuple
    reason: str
    at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == KEPT and self.case is not None


@dataclass
class KeeperStanding:
    cases_offered: int = 0
    cases_kept: int = 0
    rejected_contains_the_future: int = 0
    rejected_outcome_unknown: int = 0
    duplicates: int = 0
    retired_world_changed: int = 0
    retired_expectation_wrong: int = 0
    facts_dropped_for_being_later: int = 0


class GoldenCaseKeeper:
    """Builds cases from closed trades, refusing anything known only afterwards."""

    def __init__(
        self,
        balance_tolerance: float,
        minimum_cases_before_balance_matters: int,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < balance_tolerance <= 0.5:
            raise ValueError(
                "the tolerance is how far from an even split the set may drift, and it "
                "cannot exceed a half"
            )
        if minimum_cases_before_balance_matters < 2:
            raise ValueError("a balance over fewer than two cases is not a balance")
        self._balance_tolerance = balance_tolerance
        self._minimum_for_balance = minimum_cases_before_balance_matters
        self._now_ns = now_ns
        self._cases: dict[str, GoldenCase] = {}
        self._outcome_of: dict[str, bool] = {}
        self._retired: dict[str, str] = {}
        self.standing = KeeperStanding()

    def keep(
        self, case_id: str, purpose: str, facts: dict, fact_times_ns: dict,
        context_sections, expected_value: dict, decided_at_ns: int,
        outcome_known_at_ns: int | None, was_profitable: bool | None,
        source_reference: str,
    ) -> CaseOutcome:
        self.standing.cases_offered += 1

        if case_id in self._cases:
            self.standing.duplicates += 1
            return self._outcome(
                case_id, ALREADY_KEPT, self._cases[case_id], (),
                "already in the set. The same decision twice weights it twice",
            )

        if outcome_known_at_ns is None or was_profitable is None:
            self.standing.rejected_outcome_unknown += 1
            return self._outcome(
                case_id, OUTCOME_NOT_KNOWN, None, (),
                "the trade has not closed, so there is no right answer to evaluate "
                "against. A case built on an open position measures a preference",
            )

        # The failure that makes a golden set worthless: a fact the decision could
        # not have had. It scores perfectly and predicts nothing.
        from_the_future = tuple(
            sorted(
                name
                for name, measured_at in fact_times_ns.items()
                if measured_at > decided_at_ns
            )
        )
        if from_the_future:
            self.standing.rejected_contains_the_future += 1
            self.standing.facts_dropped_for_being_later += len(from_the_future)
            return self._outcome(
                case_id, CONTAINS_THE_FUTURE, None, from_the_future,
                f"{', '.join(from_the_future)} was measured after the decision. A prompt "
                f"evaluated on this scores perfectly and is useless live",
            )

        case = GoldenCase(
            case_id=case_id,
            purpose=purpose,
            facts=dict(facts),
            context_sections=tuple(context_sections or ()),
            expected_value=dict(expected_value),
            outcome_was_known_at_ns=outcome_known_at_ns,
            source_reference=source_reference,
            added_at_ns=self._now_ns(),
        )
        self._cases[case_id] = case
        self._outcome_of[case_id] = was_profitable
        self.standing.cases_kept += 1

        balance = self.balance_for(purpose)
        note = ""
        if balance is not None and abs(balance - 0.5) > self._balance_tolerance:
            note = (
                f". The set for {purpose} is now {balance:.0%} profitable outcomes, which "
                f"rewards a prompt that always says the majority answer"
            )

        return self._outcome(
            case_id, KEPT, case, (),
            f"built from a closed trade, {len(facts)} fact(s) all measured at or before "
            f"the decision{note}",
        )

    def balance_for(self, purpose: str) -> float | None:
        cases = [
            case_id
            for case_id, case in self._cases.items()
            if case.purpose == purpose and case_id not in self._retired
        ]
        if len(cases) < self._minimum_for_balance:
            return None
        profitable = sum(1 for case_id in cases if self._outcome_of.get(case_id))
        return profitable / len(cases)

    def is_balanced(self, purpose: str) -> bool:
        balance = self.balance_for(purpose)
        return balance is None or abs(balance - 0.5) <= self._balance_tolerance

    def retire(self, case_id: str, why: str) -> CaseOutcome:
        """A case leaves the set for a reason about the world, never about a score."""
        if why not in (WORLD_HAS_CHANGED, EXPECTATION_WAS_WRONG):
            raise ValueError(
                f"{why!r} is not a reason to retire a case. A case is never retired "
                f"because a prompt scored badly on it -- that is the case working"
            )
        case = self._cases.get(case_id)
        if case is None:
            return self._outcome(case_id, ALREADY_KEPT, None, (), "no such case")
        self._retired[case_id] = why
        if why == WORLD_HAS_CHANGED:
            self.standing.retired_world_changed += 1
        else:
            self.standing.retired_expectation_wrong += 1
        return self._outcome(
            case_id, RETIRED, case, (),
            f"retired: {why}. It is retired rather than edited -- adjusting a case to "
            f"make a prompt look better is tuning the ruler",
        )

    def cases_for(self, purpose: str) -> tuple:
        return tuple(
            case
            for case_id, case in sorted(self._cases.items())
            if case.purpose == purpose and case_id not in self._retired
        )

    def expected_outcome(self, case_id: str) -> bool | None:
        return self._outcome_of.get(case_id)

    def _outcome(self, case_id, state, case, offending, reason) -> CaseOutcome:
        return CaseOutcome(
            case_id=case_id, state=state, case=case, offending_facts=offending,
            reason=reason, at_ns=self._now_ns(),
        )


def describe_golden_cases(keeper: GoldenCaseKeeper) -> dict:
    return {
        "part_id": PART_ID,
        "cases_offered": keeper.standing.cases_offered,
        "cases_kept": keeper.standing.cases_kept,
        "rejected_for_containing_the_future": (
            keeper.standing.rejected_contains_the_future
        ),
        "rejected_outcome_not_known": keeper.standing.rejected_outcome_unknown,
        "duplicates": keeper.standing.duplicates,
        "retired_because_the_world_changed": keeper.standing.retired_world_changed,
        "retired_because_the_expectation_was_wrong": (
            keeper.standing.retired_expectation_wrong
        ),
        "can_edit_a_case": False,
        "retires_a_case_for_scoring_badly": False,
    }


def run_golden_case_keeper(
    keeper: GoldenCaseKeeper, control_socket, read_closed_trades, publish_cases,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        for job in read_closed_trades():
            outcome = keeper.keep(**job)
            if outcome.is_usable:
                publish_cases(outcome.case)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
