"""part-admission-gate: nothing the system wrote runs until it passes here.

This is the boundary between code the system proposed and code the system runs, and
it is the highest-consequence gate in the project: a bad trade loses money once, an
admitted bad part loses money continuously and looks like part of the design while
doing it.

Every check must pass. They are not weighed against each other, because a part that
is beautifully tested and violates the contract is not a better part than one that is
badly tested and does not.

- **The contract must hold.** What the part declares it consumes and produces has to
  match what the blueprint says, and adding it must not break any other part's edges
  (R-01). This is checked by the same checker that guards every commit.
- **Its tests must run and pass**, on real recorded data (RL-063). Tests written by
  whatever wrote the part are weak evidence, which is exactly why they are the floor
  and not the ceiling.
- **It must not duplicate an existing part.** Two parts producing the same data type
  from the same inputs is not redundancy, it is an ambiguity about which one is right.
- **The envelope must permit self-modification.** Admission is the single most
  privileged action in the system, and it requires the widest envelope by definition.
- **A human override blocks it outright**, regardless of everything above.

Admission is reversible by construction: the journal records what changed and the
replacement planner knows how to put it back. **A gate whose decisions cannot be
undone is not a gate, it is a commitment.**
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.autonomy_types import AdmittedPart, MODIFY_ITSELF
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "part-admission-gate"

PART_DECLARATION = PartDeclaration(
    part_id="part-admission-gate",
    consumes=("proposed-part", "upstream-change", "policy-decision"),
    produces=("admitted-part", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

ADMITTED = "admitted"
CONTRACT_BROKEN = "the-contract-does-not-hold"
TESTS_FAILED = "its-tests-do-not-pass"
NO_TESTS_RAN = "no-test-actually-ran"
DUPLICATES_AN_EXISTING_PART = "another-part-already-produces-this-from-the-same-inputs"
ENVELOPE_FORBIDS_IT = "the-envelope-does-not-permit-self-modification"
A_HUMAN_SAID_NO = "a-human-override-blocks-it"
NOT_ON_REAL_DATA = "its-tests-do-not-run-on-recorded-data"

CHECKS = (
    "the-contract-holds", "its-tests-pass", "the-tests-ran-on-recorded-data",
    "it-duplicates-nothing", "the-envelope-permits-it", "no-human-override",
)


@dataclass(frozen=True)
class AdmissionOutcome:
    proposal_id: str
    state: str
    admitted: AdmittedPart | None
    checks_passed: tuple
    reason: str
    decided_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == ADMITTED and self.admitted is not None


@dataclass
class GateStanding:
    proposals_seen: int = 0
    admitted: int = 0
    refused_contract: int = 0
    refused_tests: int = 0
    refused_no_tests_ran: int = 0
    refused_synthetic_data: int = 0
    refused_duplicate: int = 0
    refused_envelope: int = 0
    refused_override: int = 0
    admissions_that_were_reverted: int = 0


class PartAdmissionGate:
    """Every check must pass; none of them can be traded against another."""

    def __init__(self, minimum_tests: int, now_ns=time.time_ns) -> None:
        if minimum_tests < 1:
            raise ValueError(
                "tests written by whatever wrote the part are weak evidence, which is "
                "why they are the floor rather than the ceiling"
            )
        self._minimum_tests = minimum_tests
        self._now_ns = now_ns
        self._check_contract = None
        self._run_tests = None
        self._existing: dict[tuple, str] = {}
        self._envelope_level: str | None = None
        self._override_active = False
        self.standing = GateStanding()

    def install_contract_checker(self, check_contract) -> None:
        """The same checker that guards every commit: `check(proposal) -> (bool, str)`."""
        self._check_contract = check_contract

    def install_test_runner(self, run_tests) -> None:
        """`run_tests(proposal) -> (passed, failed, used_recorded_data)`."""
        self._run_tests = run_tests

    def observe_existing_part(self, part_id: str, consumes, produces) -> None:
        for data in produces:
            self._existing[(data, tuple(sorted(consumes)))] = part_id

    def observe_envelope(self, level: str) -> None:
        self._envelope_level = level

    def observe_override(self, is_active: bool) -> None:
        self._override_active = is_active

    def duplicate_of(self, proposal) -> str | None:
        key_inputs = tuple(sorted(proposal.consumes))
        for data in proposal.produces:
            existing = self._existing.get((data, key_inputs))
            if existing is not None and existing != proposal.part_id:
                return existing
        return None

    def admit(self, proposal) -> AdmissionOutcome:
        self.standing.proposals_seen += 1
        passed: list = []

        if self._override_active:
            self.standing.refused_override += 1
            return self._outcome(
                proposal.proposal_id, A_HUMAN_SAID_NO, None, (),
                "a human override blocks admission, regardless of every other check",
            )

        if self._envelope_level != MODIFY_ITSELF:
            self.standing.refused_envelope += 1
            return self._outcome(
                proposal.proposal_id, ENVELOPE_FORBIDS_IT, None, (),
                f"the envelope is at {self._envelope_level}. Admission is the most "
                f"privileged action in the system and requires the widest envelope by "
                f"definition",
            )
        passed.append("the-envelope-permits-it")
        passed.append("no-human-override")

        duplicate = self.duplicate_of(proposal)
        if duplicate is not None:
            self.standing.refused_duplicate += 1
            return self._outcome(
                proposal.proposal_id, DUPLICATES_AN_EXISTING_PART, None, tuple(passed),
                f"{duplicate} already produces the same data from the same inputs. That "
                f"is not redundancy, it is an ambiguity about which one is right",
            )
        passed.append("it-duplicates-nothing")

        if self._check_contract is None:
            return self._outcome(
                proposal.proposal_id, CONTRACT_BROKEN, None, tuple(passed),
                "no contract checker is installed, so the contract cannot be shown to hold",
            )
        holds, detail = self._check_contract(proposal)
        if not holds:
            self.standing.refused_contract += 1
            return self._outcome(
                proposal.proposal_id, CONTRACT_BROKEN, None, tuple(passed),
                f"the contract does not hold: {detail}. Adding this part would break "
                f"another part's edges",
            )
        passed.append("the-contract-holds")

        if self._run_tests is None:
            self.standing.refused_no_tests_ran += 1
            return self._outcome(
                proposal.proposal_id, NO_TESTS_RAN, None, tuple(passed),
                "no test runner is installed, so no test actually ran. A part admitted "
                "on untested code is a part nobody has evidence about",
            )
        tests_passed, tests_failed, used_recorded_data = self._run_tests(proposal)

        if tests_passed + tests_failed < self._minimum_tests:
            self.standing.refused_no_tests_ran += 1
            return self._outcome(
                proposal.proposal_id, NO_TESTS_RAN, None, tuple(passed),
                f"{tests_passed + tests_failed} test(s) ran, below the "
                f"{self._minimum_tests} required",
            )
        if tests_failed:
            self.standing.refused_tests += 1
            return self._outcome(
                proposal.proposal_id, TESTS_FAILED, None, tuple(passed),
                f"{tests_failed} test(s) failed",
            )
        passed.append("its-tests-pass")

        if not used_recorded_data:
            self.standing.refused_synthetic_data += 1
            return self._outcome(
                proposal.proposal_id, NOT_ON_REAL_DATA, None, tuple(passed),
                "the tests do not run on recorded data. Invented fixtures test the "
                "author's idea of the market rather than the market",
            )
        passed.append("the-tests-ran-on-recorded-data")

        self.standing.admitted += 1
        return self._outcome(
            proposal.proposal_id, ADMITTED,
            AdmittedPart(
                proposal_id=proposal.proposal_id,
                part_id=proposal.part_id,
                checks_passed=tuple(passed),
                contract_holds=True,
                tests_passed=tests_passed,
                reviewed_by=PART_ID,
                admitted_at_ns=self._now_ns(),
            ),
            tuple(passed),
            f"{proposal.part_id} passed all {len(CHECKS)} check(s) with {tests_passed} "
            f"test(s) on recorded data. Admission is reversible: the journal records what "
            f"changed, and a gate whose decisions cannot be undone is a commitment rather "
            f"than a gate",
        )

    def _outcome(self, proposal_id, state, admitted, passed, reason) -> AdmissionOutcome:
        return AdmissionOutcome(
            proposal_id=proposal_id, state=state, admitted=admitted,
            checks_passed=passed, reason=reason, decided_at_ns=self._now_ns(),
        )


def describe_admission(gate: PartAdmissionGate) -> dict:
    return {
        "part_id": PART_ID,
        "proposals_seen": gate.standing.proposals_seen,
        "admitted": gate.standing.admitted,
        "refused_contract_broken": gate.standing.refused_contract,
        "refused_tests_failed": gate.standing.refused_tests,
        "refused_no_tests_ran": gate.standing.refused_no_tests_ran,
        "refused_tests_not_on_recorded_data": gate.standing.refused_synthetic_data,
        "refused_duplicate": gate.standing.refused_duplicate,
        "refused_envelope": gate.standing.refused_envelope,
        "refused_human_override": gate.standing.refused_override,
        "checks": list(CHECKS),
        "trades_one_check_against_another": False,
        "admissions_that_were_reverted": gate.standing.admissions_that_were_reverted,
    }


def run_part_admission_gate(
    gate: PartAdmissionGate, control_socket, read_proposals, publish_admitted,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        for proposal in read_proposals():
            outcome = gate.admit(proposal)
            if outcome.is_usable:
                publish_admitted(outcome.admitted)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
