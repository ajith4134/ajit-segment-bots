"""subscription-session-caller: calls made against quota that refills.

A subscription call is free at the margin and expensive at the boundary. Nothing is
charged per call, so the temptation is to treat it as unlimited -- and then the
window ends, the quota is gone, and every part that needed a call for the rest of
the window gets nothing. The scarce thing is not money, it is the remaining window.

So this caller spends carefully in ways a metered caller does not have to:

- **Every call is recorded before its response is used.** The record is the only
  evidence the quota was consumed; recording it afterwards means a crash between
  the two loses the accounting and the watcher believes quota that is gone is still
  there.
- **A failed call still spent something.** Most providers count a request that
  errored, and treating failures as free is how a retry loop consumes a window
  invisibly.
- **Retries are bounded and backed off.** Retrying immediately against a rate limit
  is what turns a rate limit into a suspension, and a suspension does not refill on
  the window's clock.
- **A session that has gone stale is re-established rather than retried through.**
  An expired session returns an authentication error on every call, and retrying it
  spends quota to receive the same error repeatedly.

The caller never decides whether a call should be made. That was the router's
decision, and re-deciding it here would put the same policy in two places where they
can disagree.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.llm_types import LlmCallRecord, LlmResponse, SUBSCRIPTION
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "subscription-session-caller"

PART_DECLARATION = PartDeclaration(
    part_id="subscription-session-caller",
    consumes=("subscription-llm-request",),
    produces=("llm-response", "llm-call-record", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

ANSWERED = "answered"
RATE_LIMITED = "rate-limited"
SESSION_EXPIRED = "the-session-is-no-longer-valid"
CALL_FAILED = "the-call-failed"
RETRIES_EXHAUSTED = "retries-exhausted"
NO_SESSION = "no-session-is-installed"


@dataclass(frozen=True)
class CallOutcome:
    routed_id: str
    state: str
    response: LlmResponse | None
    record: LlmCallRecord
    attempt: int
    backoff_seconds: float | None
    reason: str
    called_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == ANSWERED and self.response is not None


@dataclass
class CallerStanding:
    calls_made: int = 0
    answered: int = 0
    rate_limited: int = 0
    failures: int = 0
    session_expirations: int = 0
    sessions_re_established: int = 0
    retries: int = 0
    retries_exhausted: int = 0
    quota_spent_on_failures: float = 0.0


class SubscriptionSessionCaller:
    """Calls the subscription endpoint, recording every attempt as spend."""

    def __init__(
        self,
        maximum_retries: int,
        initial_backoff_seconds: float,
        backoff_multiplier: float,
        quota_cost_per_call: float,
        now_ns=time.time_ns,
    ) -> None:
        if maximum_retries < 0:
            raise ValueError("a negative retry budget is not a budget")
        if initial_backoff_seconds <= 0 or backoff_multiplier < 1.0:
            raise ValueError(
                "retrying immediately against a rate limit is what turns a rate limit "
                "into a suspension, and a suspension does not refill on the window's clock"
            )
        if quota_cost_per_call <= 0:
            raise ValueError(
                "a call that costs no quota is a call the watcher will never see"
            )
        self._maximum_retries = maximum_retries
        self._initial_backoff = initial_backoff_seconds
        self._backoff_multiplier = backoff_multiplier
        self._quota_cost = quota_cost_per_call
        self._now_ns = now_ns
        self._call = None
        self._establish_session = None
        self._session_is_valid = False
        self._attempts: dict[str, int] = {}
        self._sequence = 0
        self.standing = CallerStanding()

    def install_session(self, call, establish_session=None) -> None:
        """`call(routed) -> (text, finish_reason, input_tokens, output_tokens, seconds)`."""
        self._call = call
        self._establish_session = establish_session
        self._session_is_valid = True

    def backoff_for(self, attempt: int) -> float:
        return self._initial_backoff * (self._backoff_multiplier ** max(attempt - 1, 0))

    def call(self, routed) -> CallOutcome:
        if self._call is None:
            return self._outcome(
                routed, NO_SESSION, None, 0, None, 0.0,
                "no session is installed. The transport belongs outside this part",
            )

        attempt = self._attempts.get(routed.routed_id, 0) + 1
        if attempt > self._maximum_retries + 1:
            self.standing.retries_exhausted += 1
            return self._outcome(
                routed, RETRIES_EXHAUSTED, None, attempt - 1, None, 0.0,
                f"{attempt - 1} attempt(s) already made, at the limit. Each one spent "
                f"quota whether it answered or not",
            )
        self._attempts[routed.routed_id] = attempt
        if attempt > 1:
            self.standing.retries += 1

        if not self._session_is_valid:
            if self._establish_session is None:
                return self._outcome(
                    routed, SESSION_EXPIRED, None, attempt, None, 0.0,
                    "the session expired and there is no way to re-establish it. "
                    "Retrying through an expired session spends quota to receive the same "
                    "authentication error repeatedly",
                )
            try:
                self._establish_session()
                self._session_is_valid = True
                self.standing.sessions_re_established += 1
            except Exception as failure:
                return self._outcome(
                    routed, SESSION_EXPIRED, None, attempt, None, 0.0,
                    f"the session could not be re-established ({type(failure).__name__})",
                )

        self.standing.calls_made += 1
        try:
            text, finish_reason, input_tokens, output_tokens, seconds = self._call(routed)
        except PermissionError as failure:
            # An authentication failure is about the session, not the request.
            self._session_is_valid = False
            self.standing.session_expirations += 1
            self.standing.quota_spent_on_failures += self._quota_cost
            return self._outcome(
                routed, SESSION_EXPIRED, None, attempt, self.backoff_for(attempt),
                self._quota_cost,
                f"the session is no longer valid ({failure}). It is re-established rather "
                f"than retried through",
            )
        except TimeoutError:
            self.standing.rate_limited += 1
            self.standing.quota_spent_on_failures += self._quota_cost
            return self._outcome(
                routed, RATE_LIMITED, None, attempt, self.backoff_for(attempt),
                self._quota_cost,
                f"rate limited. Backing off {self.backoff_for(attempt):.1f}s -- and this "
                f"attempt still counted against the window",
            )
        except Exception as failure:
            self.standing.failures += 1
            self.standing.quota_spent_on_failures += self._quota_cost
            return self._outcome(
                routed, CALL_FAILED, None, attempt, self.backoff_for(attempt),
                self._quota_cost,
                f"the call failed ({type(failure).__name__}). A failed call still spent "
                f"quota: treating failures as free is how a retry loop consumes a window "
                f"invisibly",
            )

        self._sequence += 1
        response = LlmResponse(
            response_id=f"resp-{self._sequence}",
            rendered_id=routed.rendered_id,
            version_id=routed.version_id,
            model_id=routed.model_id,
            text=text,
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_seconds=seconds,
            payment_kind=SUBSCRIPTION,
            was_cached=False,
            responded_at_ns=self._now_ns(),
        )
        self.standing.answered += 1
        self._attempts.pop(routed.routed_id, None)

        return CallOutcome(
            routed_id=routed.routed_id,
            state=ANSWERED,
            response=response,
            record=self._record(routed, input_tokens, output_tokens, seconds, True),
            attempt=attempt,
            backoff_seconds=None,
            reason=(
                f"answered in {seconds:.2f}s for {input_tokens + output_tokens} token(s). "
                f"The record is written before the response is used, so a crash between "
                f"them cannot lose the accounting"
            ),
            called_at_ns=self._now_ns(),
        )

    def _record(self, routed, input_tokens, output_tokens, seconds, succeeded) -> LlmCallRecord:
        return LlmCallRecord(
            call_id=f"call-{routed.routed_id}",
            part_id=routed.part_id,
            purpose=routed.purpose,
            version_id=routed.version_id,
            model_id=routed.model_id,
            payment_kind=SUBSCRIPTION,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            money_spent=0.0,
            quota_spent=self._quota_cost,
            latency_seconds=seconds,
            was_cached=False,
            succeeded=succeeded,
            called_at_ns=self._now_ns(),
        )

    def _outcome(
        self, routed, state, response, attempt, backoff, quota_spent, reason,
    ) -> CallOutcome:
        record = LlmCallRecord(
            call_id=f"call-{routed.routed_id}-{attempt}",
            part_id=routed.part_id,
            purpose=routed.purpose,
            version_id=routed.version_id,
            model_id=routed.model_id,
            payment_kind=SUBSCRIPTION,
            input_tokens=0,
            output_tokens=0,
            money_spent=0.0,
            quota_spent=quota_spent,
            latency_seconds=0.0,
            was_cached=False,
            succeeded=False,
            called_at_ns=self._now_ns(),
        )
        return CallOutcome(
            routed_id=routed.routed_id, state=state, response=response, record=record,
            attempt=attempt, backoff_seconds=backoff, reason=reason,
            called_at_ns=self._now_ns(),
        )


def describe_subscription_calling(caller: SubscriptionSessionCaller) -> dict:
    return {
        "part_id": PART_ID,
        "calls_made": caller.standing.calls_made,
        "answered": caller.standing.answered,
        "rate_limited": caller.standing.rate_limited,
        "failures": caller.standing.failures,
        "session_expirations": caller.standing.session_expirations,
        "sessions_re_established": caller.standing.sessions_re_established,
        "retries": caller.standing.retries,
        "retries_exhausted": caller.standing.retries_exhausted,
        "quota_spent_on_calls_that_did_not_answer": (
            caller.standing.quota_spent_on_failures
        ),
        "treats_a_failed_call_as_free": False,
        "decides_whether_a_call_should_be_made": False,
    }


def run_subscription_session_caller(
    caller: SubscriptionSessionCaller, control_socket, read_requests, publish_responses,
    publish_records, health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        for routed in read_requests():
            outcome = caller.call(routed)
            publish_records(outcome.record)
            if outcome.is_usable:
                publish_responses(outcome.response)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
