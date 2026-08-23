"""order-reject-classifier: why the venue refused an order, and whether to try again.

The judgement is not "what did the venue say" -- that is a lookup -- but "will this
work if I send it again". Those are different questions, and only the second one
matters to `order-resubmitter`.

So this part learns (RL-060). Every classification it makes is checked against
what happened next: a rejection it called transient, whose resubmission then
filled, is evidence it was right; one that was rejected again the same way is
evidence it was wrong. The retry decision comes from that measured rate, not from
a list of codes somebody once decided were retryable.

The venue's own code is still the strongest signal and is used first. What is
learned is the part no documentation can tell us: on this venue, right now, how
often does a rejection of *this kind* actually clear on a retry.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, OutcomeCounter, RateEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "order-reject-classifier"

PART_DECLARATION = PartDeclaration(
    part_id="order-reject-classifier",
    consumes=("raw-venue-order-status",),
    produces=("order-reject-reason", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

# The closed set of reasons an order can be refused. Every venue's vocabulary
# collapses into one of these, because downstream parts must reason about
# rejections without knowing which venue produced them (T-4).
INSUFFICIENT_MARGIN = "insufficient-margin"
PRICE_OUT_OF_BOUNDS = "price-out-of-bounds"
QUANTITY_OUT_OF_BOUNDS = "quantity-out-of-bounds"
RATE_LIMITED = "rate-limited"
REDUCE_ONLY_VIOLATED = "reduce-only-violated"
SYMBOL_NOT_TRADING = "symbol-not-trading"
DUPLICATE_ORDER = "duplicate-order"
ORDER_NOT_FOUND = "order-not-found"
VENUE_UNAVAILABLE = "venue-unavailable"
UNCLASSIFIED = "unclassified"

REJECT_REASONS = (
    INSUFFICIENT_MARGIN, PRICE_OUT_OF_BOUNDS, QUANTITY_OUT_OF_BOUNDS, RATE_LIMITED,
    REDUCE_ONLY_VIOLATED, SYMBOL_NOT_TRADING, DUPLICATE_ORDER, ORDER_NOT_FOUND,
    VENUE_UNAVAILABLE, UNCLASSIFIED,
)

# What each reason is believed to be worth retrying, before any evidence. These
# are starting beliefs the estimator moves away from, not thresholds: a rejection
# for insufficient margin will not clear by resending, and one for a rate limit
# usually will. Where the venue proves otherwise, the estimate follows the venue.
RETRY_PRIORS = {
    INSUFFICIENT_MARGIN: 0.02,
    PRICE_OUT_OF_BOUNDS: 0.15,
    QUANTITY_OUT_OF_BOUNDS: 0.02,
    RATE_LIMITED: 0.90,
    REDUCE_ONLY_VIOLATED: 0.05,
    SYMBOL_NOT_TRADING: 0.01,
    DUPLICATE_ORDER: 0.01,
    ORDER_NOT_FOUND: 0.30,
    VENUE_UNAVAILABLE: 0.85,
    UNCLASSIFIED: 0.20,
}

# Phrases that identify a reason in a venue's own message, checked after its
# numeric code. Wire vocabulary rather than decision numbers: what these mean is
# fixed by the venue, and what to *do* about them is what this part learns.
REJECT_PHRASES = {
    INSUFFICIENT_MARGIN: ("insufficient", "margin is insufficient", "not enough", "balance"),
    PRICE_OUT_OF_BOUNDS: ("price", "percent price", "limit price", "tick size", "price filter"),
    QUANTITY_OUT_OF_BOUNDS: ("quantity", "lot size", "min notional", "qty"),
    RATE_LIMITED: ("too many", "rate limit", "too frequent", "request weight"),
    REDUCE_ONLY_VIOLATED: ("reduce only", "reduceonly", "reduce-only"),
    SYMBOL_NOT_TRADING: ("not trading", "closed", "delisted", "suspended", "settling"),
    DUPLICATE_ORDER: ("duplicate", "already exists", "clientorderid"),
    ORDER_NOT_FOUND: ("does not exist", "unknown order", "not found", "order not exist"),
    VENUE_UNAVAILABLE: ("unavailable", "internal error", "system busy", "maintenance", "timeout"),
}


@dataclass(frozen=True)
class OrderRejectReason:
    """One classified rejection, with the learned chance a retry would work."""

    order_id: str
    venue_id: str
    symbol: str
    reason: str
    venue_code: str | None
    venue_message: str
    retry_estimate: Estimate
    should_retry: bool
    classified_at_ns: int


@dataclass
class ClassifierStanding:
    classified: int = 0
    by_reason: dict = field(default_factory=dict)
    unclassified: int = 0
    retries_advised: int = 0
    outcomes_observed: int = 0
    fitted_reasons: int = 0


class OrderRejectClassifier:
    """Names why an order was refused, and learns whether that kind clears on retry."""

    def __init__(
        self,
        retry_threshold: float,
        minimum_observations: int,
        prior_weight: float,
        half_life_observations: float,
        venue_code_reasons: dict[tuple[str, str], str] | None = None,
        now_ns=time.time_ns,
    ) -> None:
        self._retry_threshold = retry_threshold
        self._minimum_observations = minimum_observations
        self._now_ns = now_ns
        # One estimator per (venue, reason): the same rejection means different
        # things on different venues, and pooling them would let a venue that
        # never recovers drag down one that always does.
        self._estimators: dict[tuple[str, str], RateEstimator] = {}
        self._estimator_settings = (prior_weight, half_life_observations)
        self._venue_code_reasons = dict(venue_code_reasons or {})
        self._observed_codes = OutcomeCounter(half_life_observations=half_life_observations)
        self.standing = ClassifierStanding()

    def learn_venue_code(self, venue_id: str, code: str, reason: str) -> None:
        """Record that this venue's code means this reason.

        Populated from what the venue documents and from what is observed. It is
        a mapping of vocabulary, not of policy -- what to do about a reason stays
        with the estimator.
        """
        if reason not in REJECT_REASONS:
            raise ValueError(f"{reason!r} is not one of this system's rejection reasons")
        self._venue_code_reasons[(venue_id, str(code))] = reason

    def classify(
        self, order_id: str, venue_id: str, symbol: str, venue_code: str | None, venue_message: str
    ) -> OrderRejectReason:
        reason = self._name_reason(venue_id, venue_code, venue_message)
        self.standing.classified += 1
        self.standing.by_reason[reason] = self.standing.by_reason.get(reason, 0) + 1
        if reason == UNCLASSIFIED:
            self.standing.unclassified += 1
        if venue_code is not None:
            self._observed_codes.observe(f"{venue_id}:{venue_code}")

        estimate = self._estimator_for(venue_id, reason).estimate(self._minimum_observations)
        should_retry = estimate.value >= self._retry_threshold
        if should_retry:
            self.standing.retries_advised += 1

        return OrderRejectReason(
            order_id=order_id,
            venue_id=venue_id,
            symbol=symbol,
            reason=reason,
            venue_code=venue_code,
            venue_message=venue_message,
            retry_estimate=estimate,
            should_retry=should_retry,
            classified_at_ns=self._now_ns(),
        )

    def observe_retry_outcome(self, venue_id: str, reason: str, succeeded: bool) -> None:
        """What actually happened when a rejection of this kind was resent.

        This is the whole learned component. Without it the retry decision would
        be a list of codes somebody once decided were retryable, and it would
        never find out it was wrong.
        """
        estimator = self._estimator_for(venue_id, reason)
        was_fitted = estimator.estimate(self._minimum_observations).is_fitted
        estimator.observe(succeeded)
        self.standing.outcomes_observed += 1
        if not was_fitted and estimator.estimate(self._minimum_observations).is_fitted:
            self.standing.fitted_reasons += 1

    def _estimator_for(self, venue_id: str, reason: str) -> RateEstimator:
        key = (venue_id, reason)
        estimator = self._estimators.get(key)
        if estimator is None:
            prior_weight, half_life = self._estimator_settings
            estimator = RateEstimator(
                prior=RETRY_PRIORS.get(reason, RETRY_PRIORS[UNCLASSIFIED]),
                prior_weight=prior_weight,
                half_life_observations=half_life,
            )
            self._estimators[key] = estimator
        return estimator

    def _name_reason(self, venue_id: str, venue_code: str | None, venue_message: str) -> str:
        """The venue's code first, its words second, and unclassified rather than a guess."""
        if venue_code is not None:
            known = self._venue_code_reasons.get((venue_id, str(venue_code)))
            if known is not None:
                return known

        lowered = venue_message.lower()
        for reason, phrases in REJECT_PHRASES.items():
            if any(phrase in lowered for phrase in phrases):
                return reason
        return UNCLASSIFIED

    def learned_retry_rates(self) -> dict[str, Estimate]:
        """What has been learned so far, so it can be inspected and journalled."""
        return {
            f"{venue_id}:{reason}": estimator.estimate(self._minimum_observations)
            for (venue_id, reason), estimator in sorted(self._estimators.items())
        }


def describe_classification(classifier: OrderRejectClassifier) -> dict:
    learned = classifier.learned_retry_rates()
    return {
        "part_id": PART_ID,
        "classified": classifier.standing.classified,
        "unclassified": classifier.standing.unclassified,
        "by_reason": dict(classifier.standing.by_reason),
        "retries_advised": classifier.standing.retries_advised,
        "outcomes_observed": classifier.standing.outcomes_observed,
        "fitted_reasons": sum(1 for estimate in learned.values() if estimate.is_fitted),
        "learned_retry_rates": {
            name: {"value": estimate.value, "is_fitted": estimate.is_fitted, "observations": estimate.observations}
            for name, estimate in learned.items()
        },
    }


def run_order_reject_classifier(
    classifier: OrderRejectClassifier, control_socket, read_statuses, publish_reasons,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        rejections, outcomes = read_statuses()
        for venue_id, reason, succeeded in outcomes:
            classifier.observe_retry_outcome(venue_id, reason, succeeded)
        publish_reasons(
            tuple(classifier.classify(*rejection) for rejection in rejections)
        )

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

    A rejected status is classified by the venue's own code and message; a
    later status for the same order says whether a retry succeeded.
    """
    from runtime.input_assembly import Batch

    statuses = Batch(read=context.bus.reader("raw-venue-order-status"))
    publish_reasons = context.bus.publisher_for("order-reject-reason")
    classifier = OrderRejectClassifier(
        retry_threshold=context.number("order_reject_retry_threshold"),
        minimum_observations=int(context.number("execution_minimum_observations")),
        prior_weight=context.number("learning_prior_weight"),
        half_life_observations=context.number("learning_half_life_observations"),
    )
    rejected: dict[str, tuple[str, str]] = {}

    def read_statuses():
        rejections, outcomes = [], []
        for status in statuses.payloads():
            response = status.venue_response if isinstance(status.venue_response, dict) else {}
            if status.outcome == "rejected":
                code = response.get("code")
                rejections.append((status.client_order_id, status.venue_id, status.symbol, None if code is None else str(code), status.reason))
                rejected[status.client_order_id] = (status.venue_id, status.reason)
            elif status.client_order_id in rejected and status.outcome in ("sent", "filled"):
                venue_id, reason = rejected.pop(status.client_order_id)
                outcomes.append((venue_id, reason, True))
        return tuple(rejections), tuple(outcomes)

    def publish(reasons) -> None:
        if reasons:
            publish_reasons(reasons)

    return run_order_reject_classifier(
        classifier=classifier,
        control_socket=context.control_socket,
        read_statuses=read_statuses,
        publish_reasons=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
