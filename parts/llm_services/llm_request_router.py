"""llm-request-router: which pocket pays for this call, or nobody does.

Every rendered request arrives here and leaves as exactly one of three things: a
subscription call, a paid call, or a local call. Nothing leaves as two, and a
request that cannot be afforded leaves as nothing at all.

The routing is a preference order with hard gates rather than a cost optimisation,
because the three pockets are not interchangeable:

- **Subscription quota refills.** Spending it is free in the sense that matters --
  unspent quota at the end of a window is wasted, so it is spent first.
- **Local compute costs electricity and latency, not money.** It is the fallback
  when quota is gone and the purpose tolerates a weaker model.
- **Metered money is spent and gone.** It is last, it is gated on the spend ceiling,
  and it is the only pocket where an error is not recoverable by waiting.

Four gates, applied in order, each closing a way a router quietly overspends:

- **The part's own budget.** A part at its limit is refused here rather than at the
  caller, so one part looping cannot drain a pocket before anybody notices.
- **Backpressure.** The gauge produces one admit fraction for the whole subsystem;
  the router applies it deterministically by fingerprint rather than randomly, so
  the same request is admitted or shed consistently and a retry does not become a
  second chance at the lottery.
- **Quota and spend state.** Exhausted means exhausted; there is no reserve.
- **The purpose's own requirements.** A purpose that must not run on a weak model
  is deferred rather than downgraded, because silently answering with a worse model
  is the failure nobody sees.

A refused request is refused with a reason, never queued. A queue answers scarcity
with latency, and the whole point of a budget is that scarcity gets a decision
(RL-066).
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

from runtime.llm_types import (
    DEFERRED, LOCAL, METERED, REFUSED, RoutedLlmRequest, SUBSCRIPTION,
)
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "llm-request-router"

PART_DECLARATION = PartDeclaration(
    part_id="llm-request-router",
    consumes=(
        "rendered-llm-request", "llm-quota-state", "llm-spend-state", "llm-model-choice",
        "llm-backpressure", "llm-part-budget",
    ),
    produces=(
        "subscription-llm-request", "paid-llm-request", "part-health", "local-llm-request",
    ),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

ROUTED = "routed"
PART_OUT_OF_BUDGET = "the-asking-part-has-spent-its-allowance"
SHED_BY_BACKPRESSURE = "shed-because-the-subsystem-is-being-slowed-down"
NOTHING_CAN_PAY = "no-pocket-can-pay-for-this-call"
WOULD_NEED_A_WEAKER_MODEL = "only-a-model-this-purpose-does-not-accept-is-available"
NO_MODEL_CHOSEN = "no-model-was-chosen-for-this-request"


@dataclass(frozen=True)
class RoutingDecision:
    rendered_id: str
    state: str
    routed: RoutedLlmRequest | None
    payment_kind: str | None
    considered: tuple
    reason: str
    routed_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == ROUTED and self.routed is not None


@dataclass
class RouterStanding:
    requests_seen: int = 0
    routed_to_subscription: int = 0
    routed_to_local: int = 0
    routed_to_paid: int = 0
    refused_part_budget: int = 0
    shed_by_backpressure: int = 0
    refused_nothing_can_pay: int = 0
    deferred_would_need_a_weaker_model: int = 0
    queued: int = 0


class LlmRequestRouter:
    """Assigns each rendered request to exactly one pocket, or refuses it."""

    def __init__(self, preference_order=(SUBSCRIPTION, LOCAL, METERED), now_ns=time.time_ns) -> None:
        if set(preference_order) != {SUBSCRIPTION, LOCAL, METERED}:
            raise ValueError(
                "the preference order must cover every pocket exactly once: an omitted "
                "pocket is money or quota that silently never gets used"
            )
        self._preference_order = tuple(preference_order)
        self._now_ns = now_ns
        self._quota = None
        self._spend = None
        self._backpressure = None
        self._budgets: dict[str, object] = {}
        self._choices: dict[str, object] = {}
        self._local_is_available = False
        self._sequence = 0
        self.standing = RouterStanding()

    def observe_quota(self, quota) -> None:
        self._quota = quota

    def observe_spend(self, spend) -> None:
        self._spend = spend

    def observe_backpressure(self, backpressure) -> None:
        self._backpressure = backpressure

    def observe_budget(self, budget) -> None:
        self._budgets[budget.part_id] = budget

    def observe_model_choice(self, choice) -> None:
        self._choices[choice.request_id] = choice

    def observe_local_availability(self, is_available: bool) -> None:
        self._local_is_available = is_available

    def admits(self, fingerprint: str) -> bool:
        """Deterministic by fingerprint: a retry is not a second chance at a lottery."""
        if self._backpressure is None:
            return True
        if self._backpressure.admit_fraction >= 1.0:
            return True
        if self._backpressure.admit_fraction <= 0.0:
            return False
        digest = int(hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:8], 16)
        return (digest % 10_000) / 10_000.0 < self._backpressure.admit_fraction

    def route(self, rendered, part_id: str, acceptable_payment_kinds=None) -> RoutingDecision:
        self.standing.requests_seen += 1
        acceptable = tuple(
            acceptable_payment_kinds
            if acceptable_payment_kinds is not None
            else (SUBSCRIPTION, LOCAL, METERED)
        )

        budget = self._budgets.get(part_id)
        if budget is None or budget.is_spent:
            self.standing.refused_part_budget += 1
            return self._decision(
                rendered.rendered_id, PART_OUT_OF_BUDGET, None, None, (),
                f"{part_id} has no allowance left. Refusing here rather than at the caller "
                f"is what stops one part looping from draining a pocket before anybody "
                f"notices",
            )

        if not self.admits(rendered.fingerprint):
            self.standing.shed_by_backpressure += 1
            return self._decision(
                rendered.rendered_id, SHED_BY_BACKPRESSURE, None, None, (),
                f"shed at an admit fraction of "
                f"{self._backpressure.admit_fraction:.2f}: {self._backpressure.reason}. "
                f"Shedding is deterministic by fingerprint, so a retry is not a second "
                f"chance",
            )

        choice = self._choices.get(rendered.request_id)
        if choice is None:
            return self._decision(
                rendered.rendered_id, NO_MODEL_CHOSEN, None, None, (),
                "no model was chosen for this request. Routing without a model would pick "
                "one implicitly, which is the choice nobody records",
            )

        considered = []
        for pocket in self._preference_order:
            considered.append(pocket)
            if pocket not in acceptable:
                continue
            if not self._can_pay(pocket):
                continue
            if pocket == LOCAL and not self._local_is_available:
                continue

            self._sequence += 1
            routed = RoutedLlmRequest(
                routed_id=f"routed-{self._sequence}",
                rendered_id=rendered.rendered_id,
                request_id=rendered.request_id,
                version_id=rendered.version_id,
                purpose=rendered.purpose,
                part_id=part_id,
                payment_kind=pocket,
                model_id=choice.model_id,
                text=rendered.text,
                output_schema=dict(rendered.output_schema),
                facts=dict(rendered.facts),
                fingerprint=rendered.fingerprint,
                admitted_fraction=(
                    self._backpressure.admit_fraction if self._backpressure else 1.0
                ),
                reason=f"routed to {pocket}",
                routed_at_ns=self._now_ns(),
            )
            if pocket == SUBSCRIPTION:
                self.standing.routed_to_subscription += 1
            elif pocket == LOCAL:
                self.standing.routed_to_local += 1
            else:
                self.standing.routed_to_paid += 1

            return self._decision(
                rendered.rendered_id, ROUTED, routed, pocket, tuple(considered),
                f"{pocket} pays for this call"
                + (
                    ". Unspent quota at the end of a window is wasted, so it goes first"
                    if pocket == SUBSCRIPTION
                    else ". Money is spent and gone, which is why it is last"
                    if pocket == METERED
                    else ". Local costs electricity and latency rather than money"
                ),
            )

        # Nothing could pay. Distinguish "no money anywhere" from "the only pocket
        # left runs a model this purpose does not accept".
        payable = [pocket for pocket in self._preference_order if self._can_pay(pocket)]
        if payable and not set(payable) & set(acceptable):
            self.standing.deferred_would_need_a_weaker_model += 1
            return self._decision(
                rendered.rendered_id, WOULD_NEED_A_WEAKER_MODEL, None, None,
                tuple(considered),
                f"only {', '.join(payable)} can pay, and this purpose does not accept it. "
                f"The request is deferred rather than downgraded: silently answering with "
                f"a worse model is the failure nobody sees",
            )

        self.standing.refused_nothing_can_pay += 1
        return self._decision(
            rendered.rendered_id, NOTHING_CAN_PAY, None, None, tuple(considered),
            "no pocket can pay for this call. It is refused with a reason rather than "
            "queued -- a queue answers scarcity with latency instead of a decision",
        )

    def _can_pay(self, pocket: str) -> bool:
        if pocket == SUBSCRIPTION:
            return self._quota is not None and not self._quota.is_exhausted
        if pocket == METERED:
            return self._spend is not None and not self._spend.is_exhausted
        return self._local_is_available

    def _decision(
        self, rendered_id, state, routed, payment_kind, considered, reason,
    ) -> RoutingDecision:
        return RoutingDecision(
            rendered_id=rendered_id, state=state, routed=routed,
            payment_kind=payment_kind, considered=considered, reason=reason,
            routed_at_ns=self._now_ns(),
        )


def describe_routing(router: LlmRequestRouter) -> dict:
    return {
        "part_id": PART_ID,
        "requests_seen": router.standing.requests_seen,
        "routed_to_subscription": router.standing.routed_to_subscription,
        "routed_to_local": router.standing.routed_to_local,
        "routed_to_paid": router.standing.routed_to_paid,
        "refused_part_out_of_budget": router.standing.refused_part_budget,
        "shed_by_backpressure": router.standing.shed_by_backpressure,
        "refused_nothing_can_pay": router.standing.refused_nothing_can_pay,
        "deferred_rather_than_downgraded": (
            router.standing.deferred_would_need_a_weaker_model
        ),
        "preference_order": list(router._preference_order),
        "queues_a_refused_request": False,
        "queued": router.standing.queued,
        "sheds_randomly": False,
    }


def run_llm_request_router(
    router: LlmRequestRouter, control_socket, read_rendered, publish_subscription,
    publish_paid, publish_local, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for rendered, part_id, acceptable in read_rendered():
            decision = router.route(rendered, part_id, acceptable)
            if not decision.is_usable:
                continue
            if decision.payment_kind == SUBSCRIPTION:
                publish_subscription(decision.routed)
            elif decision.payment_kind == METERED:
                publish_paid(decision.routed)
            else:
                publish_local(decision.routed)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
