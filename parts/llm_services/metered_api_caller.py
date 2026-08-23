"""metered-api-caller: calls that cost money, priced before they are made.

This is the only part in the system that can spend real money on its own initiative,
so it is built around one rule: **the price is computed before the call, not read off
the invoice afterwards.** A caller that discovers the cost after the fact cannot
refuse anything, and by the time a runaway is visible in a bill the money is gone.

What that requires, and what each piece prevents:

- **A price per model, per token, in both directions.** Input and output tokens are
  charged differently, usually by a factor of three to five, so one blended rate
  systematically misprices exactly the calls that produce long answers.
- **An estimate before sending, and a refusal when the estimate exceeds what is
  left.** Estimating from the rendered text's length is approximate, so the estimate
  is deliberately conservative: over-estimating declines a call that would have
  fitted, under-estimating overspends.
- **A hard ceiling per call.** A single request that would cost more than the
  per-call ceiling is refused whatever the remaining budget, because that shape is
  almost always a prompt that grew rather than a decision worth paying for.
- **The actual cost recorded from the response's own token counts.** The estimate
  is for the decision; the record is for the ledger, and using the estimate as the
  record makes the ledger drift from the invoice.

A failed metered call may still be charged. Providers differ, so the caller records
what the provider reported and, when nothing was reported, records zero with a flag
rather than assuming either way.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.llm_types import LlmCallRecord, LlmResponse, METERED
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "metered-api-caller"

PART_DECLARATION = PartDeclaration(
    part_id="metered-api-caller",
    consumes=("paid-llm-request",),
    produces=("llm-response", "llm-call-record", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

ANSWERED = "answered"
NO_PRICE = "this-model-has-no-price-so-its-cost-cannot-be-known-before-the-call"
ABOVE_THE_PER_CALL_CEILING = "one-call-would-cost-more-than-a-call-may-cost"
WOULD_EXCEED_THE_BUDGET = "the-estimated-cost-exceeds-what-is-left"
CALL_FAILED = "the-call-failed"
RATE_LIMITED = "rate-limited"
NO_ENDPOINT = "no-endpoint-is-installed"


@dataclass(frozen=True)
class MeteredOutcome:
    routed_id: str
    state: str
    response: LlmResponse | None
    record: LlmCallRecord | None
    estimated_cost: float
    actual_cost: float
    cost_was_reported: bool
    reason: str
    called_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == ANSWERED and self.response is not None


@dataclass
class MeteredStanding:
    calls_made: int = 0
    answered: int = 0
    refused_no_price: int = 0
    refused_per_call_ceiling: int = 0
    refused_budget: int = 0
    failures: int = 0
    rate_limited: int = 0
    money_spent: float = 0.0
    money_spent_on_failures: float = 0.0
    calls_with_no_reported_cost: int = 0
    total_estimate_error: float = 0.0


class MeteredApiCaller:
    """Prices a call before making it, refuses what cannot be afforded, records what was."""

    def __init__(
        self,
        per_call_ceiling: float,
        characters_per_token: float,
        estimated_output_tokens: int,
        estimate_safety_multiplier: float,
        now_ns=time.time_ns,
    ) -> None:
        if per_call_ceiling <= 0:
            raise ValueError(
                "without a per-call ceiling one grown prompt can spend a whole period"
            )
        if characters_per_token <= 0:
            raise ValueError("the character-to-token ratio is a positive measured number")
        if estimated_output_tokens < 1:
            raise ValueError(
                "an output estimate of zero prices every call as input-only, which "
                "under-prices exactly the calls that produce long answers"
            )
        if estimate_safety_multiplier < 1.0:
            raise ValueError(
                "the estimate is deliberately conservative: over-estimating declines a "
                "call that would have fitted, under-estimating overspends"
            )
        self._per_call_ceiling = per_call_ceiling
        self._characters_per_token = characters_per_token
        self._estimated_output_tokens = estimated_output_tokens
        self._safety = estimate_safety_multiplier
        self._now_ns = now_ns
        self._prices: dict[str, tuple] = {}
        self._call = None
        self._sequence = 0
        self.standing = MeteredStanding()

    def install_endpoint(self, call) -> None:
        """`call(routed) -> (text, finish_reason, in_tokens, out_tokens, seconds, cost)`."""
        self._call = call

    def observe_price(
        self, model_id: str, input_price_per_token: float, output_price_per_token: float,
    ) -> None:
        """Both directions: they differ by a factor of three to five."""
        if input_price_per_token < 0 or output_price_per_token < 0:
            raise ValueError("a negative price is not a price")
        self._prices[model_id] = (input_price_per_token, output_price_per_token)

    def estimate_cost(self, routed) -> float | None:
        price = self._prices.get(routed.model_id)
        if price is None:
            return None
        input_price, output_price = price
        input_tokens = len(routed.text) / self._characters_per_token
        return self._safety * (
            input_tokens * input_price + self._estimated_output_tokens * output_price
        )

    def call(self, routed, money_left: float) -> MeteredOutcome:
        if self._call is None:
            return self._refused(
                routed, NO_ENDPOINT, 0.0,
                "no endpoint is installed. The transport belongs outside this part",
            )

        estimate = self.estimate_cost(routed)
        if estimate is None:
            self.standing.refused_no_price += 1
            return self._refused(
                routed, NO_PRICE, 0.0,
                f"{routed.model_id} has no price, so this call's cost cannot be known "
                f"before making it. A caller that discovers the cost afterwards cannot "
                f"refuse anything",
            )

        if estimate > self._per_call_ceiling:
            self.standing.refused_per_call_ceiling += 1
            return self._refused(
                routed, ABOVE_THE_PER_CALL_CEILING, estimate,
                f"estimated {estimate:.4f} against a {self._per_call_ceiling:.4f} per-call "
                f"ceiling. A single call this expensive is almost always a prompt that "
                f"grew rather than a decision worth paying for",
            )

        if estimate > money_left:
            self.standing.refused_budget += 1
            return self._refused(
                routed, WOULD_EXCEED_THE_BUDGET, estimate,
                f"estimated {estimate:.4f} against {money_left:.4f} remaining",
            )

        self.standing.calls_made += 1
        try:
            text, finish_reason, input_tokens, output_tokens, seconds, reported_cost = (
                self._call(routed)
            )
        except TimeoutError:
            self.standing.rate_limited += 1
            return self._refused(
                routed, RATE_LIMITED, estimate,
                "rate limited. Nothing is assumed about whether it was charged",
            )
        except Exception as failure:
            self.standing.failures += 1
            return self._refused(
                routed, CALL_FAILED, estimate,
                f"the call failed ({type(failure).__name__}). Providers differ on whether "
                f"a failed call is charged, so nothing is assumed either way",
            )

        cost_was_reported = reported_cost is not None
        if cost_was_reported:
            actual = float(reported_cost)
        else:
            # Compute from the response's own token counts rather than reusing the
            # estimate: the estimate is for the decision, the record is for the ledger.
            input_price, output_price = self._prices[routed.model_id]
            actual = input_tokens * input_price + output_tokens * output_price
            self.standing.calls_with_no_reported_cost += 1

        self.standing.money_spent += actual
        self.standing.total_estimate_error += abs(actual - estimate)
        self.standing.answered += 1

        self._sequence += 1
        response = LlmResponse(
            response_id=f"resp-metered-{self._sequence}",
            rendered_id=routed.rendered_id,
            version_id=routed.version_id,
            model_id=routed.model_id,
            text=text,
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_seconds=seconds,
            payment_kind=METERED,
            was_cached=False,
            responded_at_ns=self._now_ns(),
        )
        record = LlmCallRecord(
            call_id=f"call-{routed.routed_id}",
            part_id=routed.part_id,
            purpose=routed.purpose,
            version_id=routed.version_id,
            model_id=routed.model_id,
            payment_kind=METERED,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            money_spent=actual,
            quota_spent=0.0,
            latency_seconds=seconds,
            was_cached=False,
            succeeded=True,
            called_at_ns=self._now_ns(),
        )

        return MeteredOutcome(
            routed_id=routed.routed_id, state=ANSWERED, response=response, record=record,
            estimated_cost=estimate, actual_cost=actual, cost_was_reported=cost_was_reported,
            reason=(
                f"estimated {estimate:.4f}, actually {actual:.4f}"
                + (
                    " as reported by the provider"
                    if cost_was_reported
                    else " computed from the response's own token counts, since the "
                         "provider reported none"
                )
            ),
            called_at_ns=self._now_ns(),
        )

    def _refused(self, routed, state, estimate, reason) -> MeteredOutcome:
        record = LlmCallRecord(
            call_id=f"call-{routed.routed_id}",
            part_id=routed.part_id,
            purpose=routed.purpose,
            version_id=routed.version_id,
            model_id=routed.model_id,
            payment_kind=METERED,
            input_tokens=0,
            output_tokens=0,
            money_spent=0.0,
            quota_spent=0.0,
            latency_seconds=0.0,
            was_cached=False,
            succeeded=False,
            called_at_ns=self._now_ns(),
        )
        return MeteredOutcome(
            routed_id=routed.routed_id, state=state, response=None, record=record,
            estimated_cost=estimate, actual_cost=0.0, cost_was_reported=False,
            reason=reason, called_at_ns=self._now_ns(),
        )


def describe_metered_calling(caller: MeteredApiCaller) -> dict:
    return {
        "part_id": PART_ID,
        "calls_made": caller.standing.calls_made,
        "answered": caller.standing.answered,
        "refused_no_price": caller.standing.refused_no_price,
        "refused_above_the_per_call_ceiling": caller.standing.refused_per_call_ceiling,
        "refused_would_exceed_the_budget": caller.standing.refused_budget,
        "failures": caller.standing.failures,
        "rate_limited": caller.standing.rate_limited,
        "money_spent": caller.standing.money_spent,
        "calls_with_no_reported_cost": caller.standing.calls_with_no_reported_cost,
        "total_absolute_estimate_error": caller.standing.total_estimate_error,
        "prices_a_call_after_making_it": False,
        "uses_one_blended_token_rate": False,
    }


def run_metered_api_caller(
    caller: MeteredApiCaller, control_socket, read_requests, publish_responses,
    publish_records, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for routed, money_left in read_requests():
            outcome = caller.call(routed, money_left)
            if outcome.record is not None:
                publish_records(outcome.record)
            if outcome.is_usable:
                publish_responses(outcome.response)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
