"""order-resubmitter: resend an order whose rejection was transient, within budget.

The dangerous part of the execution block, and it is dangerous in one specific
way: a resubmission that is actually a *second* order doubles a position, and the
system finds out when the venue reports twice the exposure it expected.

Three things stop that:

- **The client order id is the original's.** A resubmission is the same order,
  not a new one, so a venue that already accepted it rejects the duplicate.
- **An attempt budget per order**, so a rejection that keeps recurring stops
  being retried rather than being retried forever at a widening interval.
- **The venue's rate budget is checked first**, because a burst of retries
  against a rate limit is how a transient rejection becomes a ban.

Whether a rejection is worth retrying comes from `order-reject-classifier`, which
learns it. What this part adds is the backoff between attempts, which is learned
too: how long a venue takes to clear a transient condition is a property of that
venue, and waiting the wrong amount either wastes budget or wastes the fill.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, QuantileEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "order-resubmitter"

PART_DECLARATION = PartDeclaration(
    part_id="order-resubmitter",
    consumes=("order-reject-reason", "order-request", "venue-rate-budget"),
    produces=("order-request", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

RESUBMIT = "resubmit"
WAIT = "wait"
GIVE_UP_ATTEMPTS = "give-up-attempts-spent"
GIVE_UP_NOT_TRANSIENT = "give-up-not-transient"
GIVE_UP_NO_BUDGET = "give-up-no-rate-budget"

# The quantile of observed clear-times used as the wait. Above the median because
# retrying before the condition has cleared spends budget on a rejection that was
# always going to happen.
CLEAR_TIME_QUANTILE = 0.75


@dataclass(frozen=True)
class ResubmitDecision:
    """Whether to send this order again, when, and on whose evidence."""

    client_order_id: str
    venue_id: str
    symbol: str
    action: str
    attempt: int
    attempts_allowed: int
    wait_seconds: float
    reject_reason: str
    retry_rate: float
    wait_estimate: Estimate
    reason: str
    decided_at_ns: int

    @property
    def should_resubmit(self) -> bool:
        return self.action == RESUBMIT


@dataclass
class _Attempt:
    venue_id: str
    symbol: str
    attempts: int = 0
    last_attempt_monotonic: float = 0.0
    last_reason: str = ""


@dataclass
class ResubmitterStanding:
    decisions: int = 0
    resubmitted: int = 0
    waiting: int = 0
    gave_up_attempts: int = 0
    gave_up_not_transient: int = 0
    gave_up_no_budget: int = 0
    clear_times_learned: int = 0
    orders_tracked: int = 0


class OrderResubmitter:
    """Resends a transiently rejected order, at a learned interval, within limits."""

    def __init__(
        self,
        attempts_allowed: int,
        retry_rate_threshold: float,
        prior_clear_seconds: float,
        maximum_wait_seconds: float,
        minimum_observations: int,
        window: int,
        has_rate_budget=None,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        if attempts_allowed < 1:
            raise ValueError("an order that may not be retried at all does not need this part")
        self._attempts_allowed = attempts_allowed
        self._retry_threshold = retry_rate_threshold
        self._prior_clear = prior_clear_seconds
        self._maximum_wait = maximum_wait_seconds
        self._minimum_observations = minimum_observations
        self._window = window
        self._has_rate_budget = has_rate_budget or (lambda venue_id: True)
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._attempts: dict[str, _Attempt] = {}
        self._clear_times: dict[tuple[str, str], QuantileEstimator] = {}
        self.standing = ResubmitterStanding()

    def decide(self, rejection) -> ResubmitDecision:
        """Whether to resend the order this rejection belongs to.

        `rejection` is an `order-reject-reason`: it already carries the learned
        probability that this kind of rejection clears, which is why this part
        does not re-derive it.
        """
        self.standing.decisions += 1
        order_id = rejection.order_id
        attempt = self._attempts.setdefault(
            order_id, _Attempt(rejection.venue_id, rejection.symbol)
        )
        self.standing.orders_tracked = len(self._attempts)
        key = (rejection.venue_id, rejection.reason)
        wait = self._wait_estimate(key)
        retry_rate = rejection.retry_estimate.value

        if retry_rate < self._retry_threshold:
            self.standing.gave_up_not_transient += 1
            self._attempts.pop(order_id, None)
            return self._decision(
                rejection, GIVE_UP_NOT_TRANSIENT, attempt, wait, retry_rate,
                f"{rejection.reason} clears on retry {retry_rate:.0%} of the time, below the "
                f"{self._retry_threshold:.0%} worth spending a request on",
            )

        if attempt.attempts >= self._attempts_allowed:
            self.standing.gave_up_attempts += 1
            self._attempts.pop(order_id, None)
            return self._decision(
                rejection, GIVE_UP_ATTEMPTS, attempt, wait, retry_rate,
                f"{attempt.attempts} attempts spent of {self._attempts_allowed} allowed",
            )

        if not self._has_rate_budget(rejection.venue_id):
            self.standing.gave_up_no_budget += 1
            return self._decision(
                rejection, GIVE_UP_NO_BUDGET, attempt, wait, retry_rate,
                "no rate budget remains; retrying into a rate limit is how a ban is earned",
            )

        now = self._monotonic()
        if attempt.attempts > 0 and now - attempt.last_attempt_monotonic < wait.value:
            self.standing.waiting += 1
            return self._decision(
                rejection, WAIT, attempt, wait, retry_rate,
                f"waiting {wait.value:.1f}s for this condition to clear "
                f"({'learned' if wait.is_fitted else 'the operator prior'})",
            )

        attempt.attempts += 1
        attempt.last_attempt_monotonic = now
        attempt.last_reason = rejection.reason
        self.standing.resubmitted += 1
        return self._decision(
            rejection, RESUBMIT, attempt, wait, retry_rate,
            f"attempt {attempt.attempts} of {self._attempts_allowed}, same client order id",
        )

    def observe_resubmission_cleared(self, venue_id: str, reason: str, seconds_waited: float) -> None:
        """How long this condition actually took to clear -- the learned signal."""
        self._estimator_for((venue_id, reason)).observe(seconds_waited)
        self.standing.clear_times_learned += 1

    def observe_order_finished(self, client_order_id: str) -> None:
        self._attempts.pop(client_order_id, None)
        self.standing.orders_tracked = len(self._attempts)

    def _wait_estimate(self, key) -> Estimate:
        return self._estimator_for(key).estimate(
            CLEAR_TIME_QUANTILE,
            self._minimum_observations,
            bound_low=0.0,
            bound_high=self._maximum_wait,
        )

    def _estimator_for(self, key) -> QuantileEstimator:
        estimator = self._clear_times.get(key)
        if estimator is None:
            estimator = QuantileEstimator(window=self._window, prior=self._prior_clear)
            self._clear_times[key] = estimator
        return estimator

    def _decision(self, rejection, action, attempt, wait, retry_rate, reason) -> ResubmitDecision:
        return ResubmitDecision(
            client_order_id=rejection.order_id,
            venue_id=rejection.venue_id,
            symbol=rejection.symbol,
            action=action,
            attempt=attempt.attempts,
            attempts_allowed=self._attempts_allowed,
            wait_seconds=wait.value,
            reject_reason=rejection.reason,
            retry_rate=retry_rate,
            wait_estimate=wait,
            reason=reason,
            decided_at_ns=self._now_ns(),
        )


def describe_resubmission(resubmitter: OrderResubmitter) -> dict:
    return {
        "part_id": PART_ID,
        "decisions": resubmitter.standing.decisions,
        "resubmitted": resubmitter.standing.resubmitted,
        "waiting": resubmitter.standing.waiting,
        "gave_up_attempts_spent": resubmitter.standing.gave_up_attempts,
        "gave_up_not_transient": resubmitter.standing.gave_up_not_transient,
        "gave_up_no_rate_budget": resubmitter.standing.gave_up_no_budget,
        "clear_times_learned": resubmitter.standing.clear_times_learned,
        "orders_tracked": resubmitter.standing.orders_tracked,
    }


def run_order_resubmitter(
    resubmitter: OrderResubmitter, control_socket, read_rejections, publish_requests,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        rejections, cleared, finished = read_rejections()
        for venue_id, reason, seconds in cleared:
            resubmitter.observe_resubmission_cleared(venue_id, reason, seconds)
        for client_order_id in finished:
            resubmitter.observe_order_finished(client_order_id)
        decisions = [resubmitter.decide(rejection) for rejection in rejections]
        publish_requests(tuple(d for d in decisions if d.should_resubmit))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A classified rejection is decided on; the order it belongs to is looked
    up from the requests seen, and a resubmission is published as a new order
    request for the same order. A rejection cleared is observed when the
    order is later seen filled or sent.
    """
    from dataclasses import replace

    from runtime.input_assembly import Batch, LatestByKey

    rejections = Batch(read=context.bus.reader("order-reject-reason"))
    orders = Batch(read=context.bus.reader("order-request"))
    budgets = LatestByKey(read=context.bus.reader("venue-rate-budget"), key_of=lambda b: (b.venue_id, b.request_class))
    publish_requests = context.bus.publisher_for("order-request")
    resubmitter = OrderResubmitter(
        attempts_allowed=int(context.number("order_resubmit_attempts_allowed")),
        retry_rate_threshold=context.number("order_reject_retry_threshold"),
        prior_clear_seconds=context.number("order_resubmit_prior_clear_seconds"),
        maximum_wait_seconds=context.number("order_resubmit_maximum_wait"),
        minimum_observations=int(context.number("execution_minimum_observations")),
        window=int(context.number("execution_window")),
        has_rate_budget=lambda venue_id: (
            (b := budgets.mapping().get((venue_id, "order"))) is None or b.remaining > 0
        ),
    )
    seen_orders: dict[str, object] = {}

    def read_rejections():
        for order in orders.payloads():
            seen_orders[order.client_order_id] = order
        return tuple(rejections.payloads()), (), ()

    def publish(decisions) -> None:
        requests = []
        for decision in decisions:
            order = seen_orders.get(decision.client_order_id)
            if order is None:
                continue
            requests.append(replace(order, reason=f"resubmission {decision.attempt} of {decision.attempts_allowed}: {decision.reject_reason}", routed_at_ns=decision.decided_at_ns if hasattr(decision, "decided_at_ns") else order.routed_at_ns))
        if requests:
            publish_requests(tuple(requests))

    return run_order_resubmitter(
        resubmitter=resubmitter,
        control_socket=context.control_socket,
        read_rejections=read_rejections,
        publish_requests=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
