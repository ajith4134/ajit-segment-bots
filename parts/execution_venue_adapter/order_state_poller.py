"""order-state-poller: follow each open order until it resolves.

An order that was sent and never followed is the worst state this system can be
in: capital committed, a position possibly open, and nothing in the system aware
of either. So every order placed is watched until the venue says it is finished.

The poll interval is **learned per symbol** (RL-060) rather than fixed. Orders on
a liquid symbol resolve in under a second and polling them once a second wastes
almost nothing; orders on a thin symbol rest for minutes and polling those at the
same rate spends the rate budget that the liquid ones need. The part measures how
long its own orders actually take to resolve and polls each symbol at a fraction
of that -- fast enough to see the fill promptly, slow enough not to burn the
budget on an order that will not move.

An order that has been open far longer than anything on its symbol ever takes is
escalated rather than polled forever: something is wrong with it that more
polling will not discover.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, QuantileEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "order-state-poller"

PART_DECLARATION = PartDeclaration(
    part_id="order-state-poller",
    consumes=("order-request",),
    produces=("raw-venue-order-status", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

# The quantile of resolution times the poll interval is derived from, and the
# fraction of it actually waited. Polling at a fraction of the typical resolution
# means a normal order is seen within one interval of finishing.
RESOLUTION_QUANTILE = 0.5
POLL_FRACTION_OF_RESOLUTION = 0.25

DUE = "due"
WAITING = "waiting"
OVERDUE = "overdue"


@dataclass(frozen=True)
class PollDecision:
    """Whether one order should be polled now, and how long it has been open."""

    client_order_id: str
    venue_id: str
    symbol: str
    state: str
    open_seconds: float
    poll_interval_seconds: float
    polls_made: int
    interval_estimate: Estimate
    reason: str
    decided_at_ns: int

    @property
    def should_poll(self) -> bool:
        return self.state in (DUE, OVERDUE)


@dataclass
class _OpenOrder:
    venue_id: str
    symbol: str
    opened_at_monotonic: float
    last_polled_monotonic: float
    polls: int = 0


@dataclass
class PollerStanding:
    orders_open: int = 0
    polls_made: int = 0
    orders_resolved: int = 0
    orders_overdue: int = 0
    resolutions_learned: int = 0
    longest_open_seconds: float = 0.0


class OrderStatePoller:
    """Decides which open orders are due a poll, at a rate learned per symbol."""

    def __init__(
        self,
        prior_resolution_seconds: float,
        minimum_poll_interval_seconds: float,
        maximum_poll_interval_seconds: float,
        overdue_multiple: float,
        minimum_observations: int,
        window: int,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        if minimum_poll_interval_seconds <= 0:
            raise ValueError("a poll interval of zero is an unbounded request loop")
        if maximum_poll_interval_seconds < minimum_poll_interval_seconds:
            raise ValueError("the poll ceiling cannot be below its floor")
        self._prior_resolution = prior_resolution_seconds
        self._minimum_interval = minimum_poll_interval_seconds
        self._maximum_interval = maximum_poll_interval_seconds
        self._overdue_multiple = overdue_multiple
        self._minimum_observations = minimum_observations
        self._window = window
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._open: dict[str, _OpenOrder] = {}
        self._resolution_times: dict[tuple[str, str], QuantileEstimator] = {}
        self.standing = PollerStanding()

    def observe_order_sent(self, client_order_id: str, venue_id: str, symbol: str) -> None:
        now = self._monotonic()
        self._open[client_order_id] = _OpenOrder(venue_id, symbol, now, now - self._maximum_interval)
        self.standing.orders_open = len(self._open)

    def observe_order_resolved(self, client_order_id: str) -> None:
        """The venue reached a terminal state -- and told us how long that took."""
        order = self._open.pop(client_order_id, None)
        if order is None:
            return
        elapsed = self._monotonic() - order.opened_at_monotonic
        self._estimator_for((order.venue_id, order.symbol)).observe(elapsed)
        self.standing.resolutions_learned += 1
        self.standing.orders_resolved += 1
        self.standing.longest_open_seconds = max(self.standing.longest_open_seconds, elapsed)
        self.standing.orders_open = len(self._open)

    def decide(self, client_order_id: str) -> PollDecision | None:
        order = self._open.get(client_order_id)
        if order is None:
            return None

        key = (order.venue_id, order.symbol)
        estimate = self._interval_estimate(key)
        now = self._monotonic()
        open_seconds = now - order.opened_at_monotonic
        since_poll = now - order.last_polled_monotonic

        overdue_past = estimate.value * self._overdue_multiple
        if open_seconds > overdue_past:
            self.standing.orders_overdue += 1
            state, reason = OVERDUE, (
                f"open {open_seconds:.1f}s, past {self._overdue_multiple:g}x the polling interval "
                f"for this symbol; more polling will not explain it"
            )
        elif since_poll >= estimate.value:
            state, reason = DUE, f"last polled {since_poll:.1f}s ago"
        else:
            state, reason = WAITING, f"polled {since_poll:.1f}s ago, interval is {estimate.value:.1f}s"

        if state in (DUE, OVERDUE):
            order.last_polled_monotonic = now
            order.polls += 1
            self.standing.polls_made += 1

        return PollDecision(
            client_order_id=client_order_id,
            venue_id=order.venue_id,
            symbol=order.symbol,
            state=state,
            open_seconds=open_seconds,
            poll_interval_seconds=estimate.value,
            polls_made=order.polls,
            interval_estimate=estimate,
            reason=reason,
            decided_at_ns=self._now_ns(),
        )

    def decide_all(self) -> tuple[PollDecision, ...]:
        return tuple(
            decision
            for client_order_id in list(self._open)
            if (decision := self.decide(client_order_id)) is not None
        )

    def _interval_estimate(self, key) -> Estimate:
        resolution = self._estimator_for(key).estimate(
            RESOLUTION_QUANTILE,
            self._minimum_observations,
            bound_low=self._minimum_interval / POLL_FRACTION_OF_RESOLUTION,
            bound_high=self._maximum_interval / POLL_FRACTION_OF_RESOLUTION,
        )
        interval = resolution.value * POLL_FRACTION_OF_RESOLUTION
        return Estimate(
            value=interval,
            is_fitted=resolution.is_fitted,
            observations=resolution.observations,
            prior=self._prior_resolution * POLL_FRACTION_OF_RESOLUTION,
            was_clamped=resolution.was_clamped,
            bound_low=self._minimum_interval,
            bound_high=self._maximum_interval,
            reason=f"{POLL_FRACTION_OF_RESOLUTION:g} of {resolution.reason}",
        )

    def _estimator_for(self, key) -> QuantileEstimator:
        estimator = self._resolution_times.get(key)
        if estimator is None:
            estimator = QuantileEstimator(window=self._window, prior=self._prior_resolution)
            self._resolution_times[key] = estimator
        return estimator


def describe_polling(poller: OrderStatePoller) -> dict:
    fitted = sum(1 for key in poller._resolution_times if poller._interval_estimate(key).is_fitted)
    return {
        "part_id": PART_ID,
        "orders_open": poller.standing.orders_open,
        "polls_made": poller.standing.polls_made,
        "orders_resolved": poller.standing.orders_resolved,
        "orders_overdue": poller.standing.orders_overdue,
        "resolutions_learned": poller.standing.resolutions_learned,
        "longest_open_seconds": poller.standing.longest_open_seconds,
        "symbols_with_a_fitted_interval": fitted,
    }


def run_order_state_poller(
    poller: OrderStatePoller, control_socket, read_events, publish_polls,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_events(poller)
        publish_polls(tuple(d for d in poller.decide_all() if d.should_poll))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
