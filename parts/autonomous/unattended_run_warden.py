"""unattended-run-warden: restart what is broken, and stop when restarting is not it.

The warden exists because this system runs with nobody watching, and the failure it
prevents is not a crashed part -- it is a crashed part being restarted five hundred
times an hour while the disk fills with the same traceback.

So every restart is bounded by three things:

- **A backoff that grows.** The second restart is a fix; the fifth is a loop. Delay
  grows with each attempt so a genuinely transient fault recovers quickly and a
  permanent one stops consuming the machine.
- **A ceiling per part.** After it, the part stays down and a replacement is asked
  for instead. A part that will not stay up is a part that needs changing, not
  restarting, and the warden's job is to notice the difference rather than to keep
  trying.
- **A ceiling across the system.** Many parts restarting at once is not many
  independent faults, it is one cause -- a venue outage, a full disk, a bad settings
  file -- and restarting into it makes the cause harder to see. Above the ceiling
  the warden stops restarting anything and says why.

Two things it will not do. **It never restarts a part that is holding capital-bearing
state**, because a restart mid-position risks losing track of a live position -- that
part is stopped and escalated instead. And **it never restarts to clear a fault it
does not understand**: an unrecognised fault is escalated, since restarting is a
treatment for a specific class of failure and not a general-purpose remedy.

**An escalation is an event; the fault it comes from is a level** (2026-09-02).
`part-fault` is republished by failing-part-detector on a refresh interval -- that is
what makes a standing fault knowable rather than a thing you had to be listening for
at the right moment -- and this part read every restatement as a new event. Measured
on the live spine: ~300 escalations per part per five minutes, ~180 journal lines a
second, every part in the system, almost all of them SUSPICIOUSLY_PERFECT, which is
simply true of a working part. A repeated escalation is worse than a quiet one: it
buries the escalation that means something, and it inflates the journal that
`build_trade_board.py` reads end to end, which is much of why that generator went
from ten minutes to over an hour.

So an escalation goes out when the claim *changes*, and a standing claim is restated
on its own interval rather than on the producer's. Suppressed is not silenced: the
count is in the standing, and a fault still standing after the repeat interval is
said again -- an unattended run cannot afford quiet that hides a fault any more than
it can afford noise that buries one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.autonomy_types import RestartRequest
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "unattended-run-warden"

PART_DECLARATION = PartDeclaration(
    part_id="unattended-run-warden",
    consumes=("part-health", "part-fault"),
    produces=("restart-request", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

RESTART = "restart"
BACKING_OFF = "waiting-out-the-backoff"
GIVE_UP_AND_REPLACE = "it-will-not-stay-up-so-it-needs-changing-not-restarting"
SYSTEM_WIDE_CAUSE = "too-many-parts-are-failing-at-once-for-this-to-be-independent"
HOLDS_CAPITAL_STATE = "restarting-it-could-lose-track-of-a-live-position"
UNRECOGNISED_FAULT = "restarting-is-not-a-treatment-for-this-fault"

# The faults a restart is actually a treatment for.
RESTARTABLE_FAULTS = ("crashed", "alive-but-producing-nothing", "taking-longer-every-tick")


@dataclass(frozen=True)
class WardenDecision:
    part_id: str
    state: str
    request: RestartRequest | None
    escalate: bool
    reason: str
    decided_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == RESTART and self.request is not None


@dataclass
class WardenStanding:
    faults_seen: int = 0
    restarts_requested: int = 0
    backoffs: int = 0
    given_up_on: int = 0
    refused_system_wide: int = 0
    refused_capital_state: int = 0
    refused_unrecognised: int = 0
    escalations: int = 0
    # Restatements of a claim already escalated. Counted rather than dropped
    # silently: this number is how loudly the detector is restating a standing
    # fault, and a board that could not see it is how the storm went unnoticed.
    escalations_suppressed: int = 0


class UnattendedRunWarden:
    """Restarts what a restart fixes, and escalates everything else."""

    def __init__(
        self,
        maximum_restarts: int,
        within_seconds: float,
        initial_backoff_seconds: float,
        backoff_multiplier: float,
        system_wide_ceiling: int,
        escalation_repeat_seconds: float,
        escalation_forget_seconds: float,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        if maximum_restarts < 1:
            raise ValueError("a warden that never restarts anything is not a warden")
        if within_seconds <= 0:
            raise ValueError("the restart count is per window, not since start")
        if initial_backoff_seconds <= 0 or backoff_multiplier < 1.0:
            raise ValueError(
                "the second restart is a fix and the fifth is a loop, so the delay grows"
            )
        if system_wide_ceiling < 2:
            raise ValueError(
                "many parts restarting at once is one cause, and a ceiling below two "
                "cannot express that"
            )
        if escalation_repeat_seconds <= 0 or escalation_forget_seconds <= 0:
            raise ValueError(
                "a standing fault is restated on an interval and forgotten after one; "
                "neither can be zero, or the warden either never repeats itself or "
                "never stops"
            )
        if escalation_forget_seconds > escalation_repeat_seconds:
            raise ValueError(
                "forgetting a fault before it would have been restated makes every "
                "restatement look like a new fault, which is the storm this exists to "
                f"stop; got forget={escalation_forget_seconds}s, "
                f"repeat={escalation_repeat_seconds}s"
            )
        self._maximum_restarts = maximum_restarts
        self._within_seconds = within_seconds
        self._initial_backoff = initial_backoff_seconds
        self._backoff_multiplier = backoff_multiplier
        self._system_wide_ceiling = system_wide_ceiling
        self._escalation_repeat_seconds = escalation_repeat_seconds
        self._escalation_forget_seconds = escalation_forget_seconds
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._restarts: dict[str, list] = {}
        self._last_restart: dict[str, float] = {}
        self._holds_capital_state: set = set()
        # Per part: what was last escalated about it, when that was said, and when
        # the claim was last seen at all. The last of the three is what lets a
        # fault that stopped being restated be forgotten -- the detector drops the
        # key rather than publishing an all-clear, so absence is the only signal
        # a cleared fault has.
        self._escalated_claim: dict[str, tuple[str, str]] = {}
        self._escalated_at: dict[str, float] = {}
        self._claim_last_seen_at: dict[str, float] = {}
        self.standing = WardenStanding()

    def declare_holds_capital_state(self, part_id: str) -> None:
        """A restart mid-position risks losing track of a live position."""
        self._holds_capital_state.add(part_id)

    def restarts_in_window(self, part_id: str) -> int:
        now = self._monotonic()
        recent = [
            stamp for stamp in self._restarts.get(part_id, [])
            if now - stamp < self._within_seconds
        ]
        self._restarts[part_id] = recent
        return len(recent)

    def parts_restarting_now(self) -> int:
        return sum(
            1 for part_id in list(self._restarts) if self.restarts_in_window(part_id) > 0
        )

    def backoff_for(self, attempts: int) -> float:
        return self._initial_backoff * (self._backoff_multiplier ** max(attempts - 1, 0))

    def _is_worth_saying_again(self, part_id: str, claim: tuple[str, str]) -> bool:
        """Whether this claim about this part is news, or the same thing restated.

        News is: a claim nothing has been said about, a claim different from the
        last one, a claim whose last saying is older than the repeat interval, or
        a claim that stopped being restated for long enough to be forgotten and
        has now returned. Everything else is the producer restating a level, and
        the warden is not obliged to restate it to a human at the same rate.
        """
        now = self._monotonic()
        last_seen = self._claim_last_seen_at.get(part_id)
        went_quiet = (
            last_seen is not None and now - last_seen > self._escalation_forget_seconds
        )
        if went_quiet:
            self._escalated_claim.pop(part_id, None)
            self._escalated_at.pop(part_id, None)
        self._claim_last_seen_at[part_id] = now

        if self._escalated_claim.get(part_id) != claim:
            return True
        said_at = self._escalated_at.get(part_id)
        return said_at is None or now - said_at >= self._escalation_repeat_seconds

    def _escalation(self, part_id, state, reason, kind) -> WardenDecision:
        """One escalation decision, said to a human only when it is news."""
        if self._is_worth_saying_again(part_id, (state, kind)):
            self._escalated_claim[part_id] = (state, kind)
            self._escalated_at[part_id] = self._monotonic()
            self.standing.escalations += 1
            return self._decision(part_id, state, None, True, reason)
        self.standing.escalations_suppressed += 1
        return self._decision(part_id, state, None, False, reason)

    def decide(self, fault) -> WardenDecision:
        self.standing.faults_seen += 1
        part_id = fault.part_id

        if fault.kind not in RESTARTABLE_FAULTS:
            self.standing.refused_unrecognised += 1
            return self._escalation(
                part_id, UNRECOGNISED_FAULT, kind=fault.kind, reason=
                f"{fault.kind} is not something a restart treats. Restarting is a "
                f"treatment for a specific class of failure, not a general-purpose "
                f"remedy, so this is escalated instead",
            )

        if part_id in self._holds_capital_state:
            self.standing.refused_capital_state += 1
            return self._escalation(
                part_id, HOLDS_CAPITAL_STATE, kind=fault.kind, reason=
                f"{part_id} holds capital-bearing state. Restarting it mid-position could "
                f"lose track of a live position, so it is stopped and escalated rather "
                f"than restarted",
            )

        if self.parts_restarting_now() >= self._system_wide_ceiling:
            self.standing.refused_system_wide += 1
            return self._escalation(
                part_id, SYSTEM_WIDE_CAUSE, kind=fault.kind, reason=
                f"{self.parts_restarting_now()} part(s) are already restarting. That is "
                f"one cause rather than many independent faults, and restarting into it "
                f"makes the cause harder to see",
            )

        attempts = self.restarts_in_window(part_id)
        if attempts >= self._maximum_restarts:
            self.standing.given_up_on += 1
            return self._escalation(
                part_id, GIVE_UP_AND_REPLACE, kind=fault.kind, reason=
                f"{attempts} restart(s) in {self._within_seconds:.0f}s and it is still "
                f"failing. A part that will not stay up needs changing, not restarting",
            )

        backoff = self.backoff_for(attempts)
        last = self._last_restart.get(part_id)
        if last is not None and self._monotonic() - last < backoff:
            self.standing.backoffs += 1
            return self._decision(
                part_id, BACKING_OFF,
                RestartRequest(
                    part_id=part_id, reason=fault.detail, restarts_already=attempts,
                    within_seconds=self._within_seconds, is_backing_off=True,
                    backoff_seconds=backoff, requested_at_ns=self._now_ns(),
                ),
                False,
                f"waiting {backoff:.1f}s before restart {attempts + 1}. A transient fault "
                f"recovers quickly and a permanent one stops consuming the machine",
            )

        self._restarts.setdefault(part_id, []).append(self._monotonic())
        self._last_restart[part_id] = self._monotonic()
        self.standing.restarts_requested += 1

        return self._decision(
            part_id, RESTART,
            RestartRequest(
                part_id=part_id, reason=fault.detail, restarts_already=attempts,
                within_seconds=self._within_seconds, is_backing_off=False,
                backoff_seconds=backoff, requested_at_ns=self._now_ns(),
            ),
            False,
            f"restart {attempts + 1} of at most {self._maximum_restarts} in "
            f"{self._within_seconds:.0f}s, for {fault.kind}",
        )

    def _decision(self, part_id, state, request, escalate, reason) -> WardenDecision:
        return WardenDecision(
            part_id=part_id, state=state, request=request, escalate=escalate,
            reason=reason, decided_at_ns=self._now_ns(),
        )


def describe_warden(warden: UnattendedRunWarden) -> dict:
    return {
        "part_id": PART_ID,
        "faults_seen": warden.standing.faults_seen,
        "restarts_requested": warden.standing.restarts_requested,
        "backoffs": warden.standing.backoffs,
        "parts_given_up_on": warden.standing.given_up_on,
        "refused_system_wide_cause": warden.standing.refused_system_wide,
        "refused_holds_capital_state": warden.standing.refused_capital_state,
        "refused_unrecognised_fault": warden.standing.refused_unrecognised,
        "escalations": warden.standing.escalations,
        "escalations_suppressed": warden.standing.escalations_suppressed,
        "restartable_faults": list(RESTARTABLE_FAULTS),
        "restarts_without_a_ceiling": False,
        "restarts_a_part_holding_capital_state": False,
    }


def run_unattended_run_warden(
    warden: UnattendedRunWarden, control_socket, read_faults, publish_requests,
    escalate, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for fault in read_faults():
            decision = warden.decide(fault)
            if decision.escalate:
                escalate(decision)
            elif decision.is_usable:
                publish_requests(decision.request)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_warden(warden),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Which parts hold capital-bearing state is the operator's own list, the
    same one the replacement planner reads. An escalation carries no restart
    request by construction -- it is the decision that restarting is wrong --
    and this part produces only restart requests, so an escalation goes out
    on stderr, which the spine journals; a decision that must reach a human
    is written where the operator already looks. Part-health is consumed as
    the wake signal for the faults that follow it.
    """
    import json as _json
    import sys as _sys

    from runtime.input_assembly import Batch

    health = Batch(read=context.bus.reader("part-health"))
    faults = Batch(read=context.bus.reader("part-fault"))
    publish_requests = context.bus.publisher_for("restart-request")

    warden = UnattendedRunWarden(
        maximum_restarts=int(context.number("warden_maximum_restarts")),
        within_seconds=context.number("warden_restart_window_seconds"),
        initial_backoff_seconds=context.number("warden_initial_backoff_seconds"),
        backoff_multiplier=context.number("warden_backoff_multiplier"),
        system_wide_ceiling=int(context.number("warden_system_wide_ceiling")),
        escalation_repeat_seconds=context.number("warden_escalation_repeat_seconds"),
        escalation_forget_seconds=context.number("warden_escalation_forget_seconds"),
    )
    for part in context.setting("capital_state_parts").value:
        warden.declare_holds_capital_state(str(part))

    def read_faults():
        health.payloads()
        return faults.payloads()

    def escalate(decision):
        print(
            _json.dumps(
                {
                    "part_id": PART_ID,
                    "event": "escalation",
                    "faulty_part": decision.part_id,
                    "state": decision.state,
                    "reason": decision.reason,
                }
            ),
            file=_sys.stderr,
            flush=True,
        )

    return run_unattended_run_warden(
        warden=warden,
        control_socket=context.control_socket,
        read_faults=read_faults,
        publish_requests=lambda request: publish_requests((request,)),
        escalate=escalate,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
