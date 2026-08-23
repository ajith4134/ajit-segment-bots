"""order-not-found-debouncer: an order is lost only after the venue says so repeatedly.

A venue answering "no such order" is usually lying by accident. An order placed
milliseconds ago has not propagated to the node that was asked; a venue under
load answers from a stale replica; a read after a cancel races the cancel itself.
Every one of those clears on the next poll.

Treating the first "not found" as truth is how a live position becomes invisible:
the order is written off, a replacement is sent, and both fill. So this counts
consecutive denials and declares the order lost only past a threshold -- and the
threshold is **learned**, not fixed (RL-060), because how many spurious denials a
venue produces is a property of that venue on that day and nothing else can know
it.

The learning is honest about its own limits: until enough resolved cases have
been seen, it uses the operator's prior and reports that it is unfitted.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, QuantileEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "order-not-found-debouncer"

PART_DECLARATION = PartDeclaration(
    part_id="order-not-found-debouncer",
    consumes=("raw-venue-order-status",),
    produces=("order-reject-reason", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

ORDER_LOST = "order-not-found"

# The quantile of observed spurious-denial runs the threshold is set at. High,
# because the two errors are not symmetric: waiting one extra poll costs a
# fraction of a second, and writing off a live order costs a duplicate position.
SPURIOUS_RUN_QUANTILE = 0.99


@dataclass(frozen=True)
class LostOrderVerdict:
    """Whether this order should now be treated as gone."""

    order_id: str
    venue_id: str
    consecutive_denials: int
    threshold: int
    is_lost: bool
    threshold_estimate: Estimate
    reason: str
    decided_at_ns: int


@dataclass
class DebouncerStanding:
    denials_seen: int = 0
    orders_declared_lost: int = 0
    spurious_runs_observed: int = 0
    longest_spurious_run: int = 0
    orders_watched: int = 0
    threshold_in_use: int = 0


class OrderNotFoundDebouncer:
    """Counts consecutive denials per order against a learned tolerance."""

    def __init__(
        self,
        prior_threshold: int,
        maximum_threshold: int,
        minimum_observations: int,
        window: int,
        now_ns=time.time_ns,
    ) -> None:
        if prior_threshold < 1:
            raise ValueError("an order cannot be declared lost before it has been denied once")
        if maximum_threshold < prior_threshold:
            raise ValueError("the operator's ceiling cannot be below the prior it bounds")
        self._prior_threshold = prior_threshold
        self._maximum_threshold = maximum_threshold
        self._minimum_observations = minimum_observations
        self._now_ns = now_ns
        # One estimator per venue: a venue whose reads are eventually consistent
        # produces long spurious runs, and one whose reads are not produces none.
        self._runs: dict[str, QuantileEstimator] = {}
        self._window = window
        self._denials: dict[str, int] = {}
        # Orders already declared lost, still counted. Forgetting an order at the
        # moment it is declared lost was a real bug: if it later turned up, the
        # run this venue had actually produced was truncated to whatever had
        # accumulated since, and the estimator learned a shorter tolerance than
        # the truth -- biasing the threshold down, which is the direction that
        # writes off live orders.
        self._declared_lost: set[str] = set()
        self.standing = DebouncerStanding()

    def observe_denial(self, order_id: str, venue_id: str) -> LostOrderVerdict:
        """One "no such order" reply. Returns whether that is now believed."""
        self.standing.denials_seen += 1
        self._denials[order_id] = self._denials.get(order_id, 0) + 1
        self.standing.orders_watched = len(self._denials)

        estimate = self._threshold_estimate(venue_id)
        threshold = max(1, int(round(estimate.value)))
        self.standing.threshold_in_use = threshold
        consecutive = self._denials[order_id]
        is_lost = consecutive >= threshold

        if is_lost and order_id not in self._declared_lost:
            self.standing.orders_declared_lost += 1
            self._declared_lost.add(order_id)

        return LostOrderVerdict(
            order_id=order_id,
            venue_id=venue_id,
            consecutive_denials=consecutive,
            threshold=threshold,
            is_lost=is_lost,
            threshold_estimate=estimate,
            reason=(
                f"{consecutive} consecutive denials against a threshold of {threshold} "
                f"({'learned' if estimate.is_fitted else 'the operator prior'})"
            ),
            decided_at_ns=self._now_ns(),
        )

    def observe_order_appeared(self, order_id: str, venue_id: str) -> None:
        """The order turned up after all -- so every denial before it was spurious.

        This is the learned signal. A run that resolved is direct evidence of how
        many false denials this venue produces, and it is the only way to know.
        """
        run_length = self._denials.pop(order_id, 0)
        self._declared_lost.discard(order_id)
        if run_length > 0:
            self._estimator_for(venue_id).observe(float(run_length))
            self.standing.spurious_runs_observed += 1
            self.standing.longest_spurious_run = max(self.standing.longest_spurious_run, run_length)
        self.standing.orders_watched = len(self._denials)

    def observe_order_resolved(self, order_id: str) -> None:
        """The order finished normally; stop watching it without learning anything."""
        self._denials.pop(order_id, None)
        self._declared_lost.discard(order_id)
        self.standing.orders_watched = len(self._denials)

    def _threshold_estimate(self, venue_id: str) -> Estimate:
        # One above the longest spurious run seen, so a denial run that has
        # exceeded everything this venue has ever produced spuriously is the
        # first one believed.
        estimate = self._estimator_for(venue_id).estimate(
            quantile=SPURIOUS_RUN_QUANTILE,
            minimum_observations=self._minimum_observations,
            bound_low=1.0,
            bound_high=float(self._maximum_threshold),
        )
        if not estimate.is_fitted:
            return estimate
        raised = min(estimate.value + 1.0, float(self._maximum_threshold))
        return Estimate(
            value=raised,
            is_fitted=True,
            observations=estimate.observations,
            prior=estimate.prior,
            was_clamped=estimate.was_clamped or raised != estimate.value + 1.0,
            bound_low=estimate.bound_low,
            bound_high=estimate.bound_high,
            reason=f"one past {estimate.reason}",
        )

    def _estimator_for(self, venue_id: str) -> QuantileEstimator:
        estimator = self._runs.get(venue_id)
        if estimator is None:
            estimator = QuantileEstimator(window=self._window, prior=float(self._prior_threshold))
            self._runs[venue_id] = estimator
        return estimator

    def learned_thresholds(self) -> dict[str, Estimate]:
        return {venue_id: self._threshold_estimate(venue_id) for venue_id in sorted(self._runs)}


def describe_debouncing(debouncer: OrderNotFoundDebouncer) -> dict:
    learned = debouncer.learned_thresholds()
    return {
        "part_id": PART_ID,
        "denials_seen": debouncer.standing.denials_seen,
        "orders_declared_lost": debouncer.standing.orders_declared_lost,
        "spurious_runs_observed": debouncer.standing.spurious_runs_observed,
        "longest_spurious_run": debouncer.standing.longest_spurious_run,
        "orders_watched": debouncer.standing.orders_watched,
        "learned_thresholds": {
            venue_id: {
                "threshold": estimate.value,
                "is_fitted": estimate.is_fitted,
                "observations": estimate.observations,
                "was_clamped": estimate.was_clamped,
            }
            for venue_id, estimate in learned.items()
        },
    }


def run_order_not_found_debouncer(
    debouncer: OrderNotFoundDebouncer, control_socket, read_statuses, publish_verdicts,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        denials, appearances, resolutions = read_statuses()
        for order_id, venue_id in appearances:
            debouncer.observe_order_appeared(order_id, venue_id)
        for order_id in resolutions:
            debouncer.observe_order_resolved(order_id)
        publish_verdicts(
            tuple(debouncer.observe_denial(order_id, venue_id) for order_id, venue_id in denials)
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
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch

    statuses = Batch(read=context.bus.reader("raw-venue-order-status"))
    publish_verdicts = context.bus.publisher_for("order-reject-reason")
    debouncer = OrderNotFoundDebouncer(
        prior_threshold=int(context.number("order_not_found_prior_threshold")),
        maximum_threshold=int(context.number("order_not_found_maximum_threshold")),
        minimum_observations=int(context.number("execution_minimum_observations")),
        window=int(context.number("execution_window")),
    )

    def read_statuses():
        denials, appearances, resolutions = [], [], []
        for status in statuses.payloads():
            text = f"{status.outcome} {status.reason}".lower()
            if "not found" in text or "unknown order" in text or "does not exist" in text:
                denials.append((status.client_order_id, status.venue_id))
            elif status.outcome in ("filled", "cancelled", "rejected"):
                resolutions.append(status.client_order_id)
            elif status.outcome == "sent" or status.venue_response:
                appearances.append((status.client_order_id, status.venue_id))
        return tuple(denials), tuple(appearances), tuple(resolutions)

    def publish(verdicts) -> None:
        kept = tuple(item for item in verdicts if item is not None)
        if kept:
            publish_verdicts(kept)

    return run_order_not_found_debouncer(
        debouncer=debouncer,
        control_socket=context.control_socket,
        read_statuses=read_statuses,
        publish_verdicts=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
