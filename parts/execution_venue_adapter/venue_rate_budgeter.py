"""venue-rate-budgeter: what the venue still allows this window, per endpoint class.

Per endpoint class, because the venues meter differently by what is being asked.
Binance charges request *weight* -- a depth snapshot at 1000 levels costs twenty
times a symbol list -- against a per-minute IP budget, while its order endpoints
have their own per-second and per-minute counts. Bybit meters a blanket
600-requests-per-5-seconds per IP. A single counter would either starve the cheap
calls or overrun on the expensive ones.

Two sources, and the venue's own is authoritative. Binance reports used weight in
a response header, so the local count is a *prediction* that is corrected the
moment the venue disagrees -- and it must be, because the limit is per IP and
every other process on this box spends from the same budget invisibly.

Bybit publishes no such header on its public endpoints (measured 2026-08-22), so
there the local count is all there is, and that difference is reported rather
than papered over.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "venue-rate-budgeter"

PART_DECLARATION = PartDeclaration(
    part_id="venue-rate-budgeter",
    consumes=("order-request", "venue-standing"),
    produces=("venue-rate-budget", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

# The classes a venue meters separately. A caller asks for budget by class
# rather than by endpoint, so adding an endpoint does not add a budget.
MARKET_DATA = "market-data"
ORDER_PLACEMENT = "order-placement"
ACCOUNT_QUERY = "account-query"
REQUEST_CLASSES = (MARKET_DATA, ORDER_PLACEMENT, ACCOUNT_QUERY)

REPORTED_BY_VENUE = "reported-by-venue"
COUNTED_LOCALLY = "counted-locally"


@dataclass(frozen=True)
class VenueRateBudget:
    """What remains of one class's allowance, and how confident that figure is."""

    venue_id: str
    request_class: str
    limit: float
    spent: float
    remaining: float
    remaining_fraction: float
    window_seconds: float
    seconds_until_window_resets: float
    source: str
    reason: str
    observed_at_ns: int

    @property
    def has_room_for(self) -> float:
        return max(0.0, self.remaining)


@dataclass
class _ClassBudget:
    limit: float
    window_seconds: float
    spends: list[tuple[float, float]] = field(default_factory=list)
    venue_reported_spent: float | None = None
    venue_reported_at: float | None = None


@dataclass
class BudgeterStanding:
    requests_counted: int = 0
    requests_refused: int = 0
    venue_corrections: int = 0
    largest_undercount: float = 0.0
    classes_tracked: int = 0


class VenueRateBudgeter:
    """Counts spend per class in a sliding window, corrected by the venue's own figure."""

    def __init__(self, monotonic=time.monotonic, now_ns=time.time_ns) -> None:
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._budgets: dict[tuple[str, str], _ClassBudget] = {}
        self._standing_by_venue: dict[str, str] = {}
        self.standing = BudgeterStanding()

    def declare_limit(
        self, venue_id: str, request_class: str, limit: float, window_seconds: float
    ) -> None:
        """State a venue's published allowance for one class.

        Supplied by the caller from the adapter's declared venue facts rather
        than known here: this part meters, and what the limits are is venue
        knowledge that lives in exactly one place (spec §3.1).
        """
        if request_class not in REQUEST_CLASSES:
            raise ValueError(f"{request_class!r} is not a metered request class")
        self._budgets[(venue_id, request_class)] = _ClassBudget(limit, window_seconds)
        self.standing.classes_tracked = len(self._budgets)

    def set_venue_standing(self, venue_id: str, state: str) -> None:
        self._standing_by_venue[venue_id] = state

    def spend(self, venue_id: str, request_class: str, weight: float = 1.0) -> bool:
        """Record a request about to be sent. False means it must not be sent.

        Checked *before* the request rather than after, because the cost of
        being wrong is a per-IP ban that outlasts the part, and a counter that
        learns about the overrun afterwards has already caused it.
        """
        budget = self._budgets.get((venue_id, request_class))
        if budget is None:
            # An unmetered class is not a free one. Refusing keeps an unlimited
            # loop from being possible against a venue nobody declared limits for.
            self.standing.requests_refused += 1
            return False
        if self._standing_by_venue.get(venue_id) == "banned":
            self.standing.requests_refused += 1
            return False

        remaining = self.read(venue_id, request_class).remaining
        if weight > remaining:
            self.standing.requests_refused += 1
            return False

        budget.spends.append((self._monotonic(), weight))
        self.standing.requests_counted += 1
        return True

    def observe_venue_reported_spend(self, venue_id: str, request_class: str, spent: float) -> None:
        """The venue's own figure, which overrides the local count.

        Where the venue reports more than we counted, something else on this IP
        spent it. That gap is the number worth watching: it is the part of the
        budget this system cannot see and must leave room for.
        """
        budget = self._budgets.get((venue_id, request_class))
        if budget is None:
            return
        locally = self._spent_in_window(budget)
        undercount = spent - locally
        if undercount > 0:
            self.standing.largest_undercount = max(self.standing.largest_undercount, undercount)
        budget.venue_reported_spent = spent
        budget.venue_reported_at = self._monotonic()
        self.standing.venue_corrections += 1

    def read(self, venue_id: str, request_class: str) -> VenueRateBudget:
        budget = self._budgets.get((venue_id, request_class))
        if budget is None:
            return VenueRateBudget(
                venue_id=venue_id, request_class=request_class, limit=0.0, spent=0.0,
                remaining=0.0, remaining_fraction=0.0, window_seconds=0.0,
                seconds_until_window_resets=0.0, source=COUNTED_LOCALLY,
                reason="no limit has been declared for this class, so nothing may be spent",
                observed_at_ns=self._now_ns(),
            )

        now = self._monotonic()
        locally = self._spent_in_window(budget)
        source, spent = COUNTED_LOCALLY, locally
        reason = "counted locally; this venue reports no usage"

        if budget.venue_reported_at is not None and now - budget.venue_reported_at < budget.window_seconds:
            # The venue's figure, plus whatever has been spent since it spoke.
            since = sum(
                weight for at, weight in budget.spends if at > budget.venue_reported_at
            )
            spent = max(locally, (budget.venue_reported_spent or 0.0) + since)
            source = REPORTED_BY_VENUE
            reason = "the venue's own figure, plus what was spent after it reported"

        remaining = budget.limit - spent
        oldest = min((at for at, _ in budget.spends), default=now)
        return VenueRateBudget(
            venue_id=venue_id,
            request_class=request_class,
            limit=budget.limit,
            spent=spent,
            remaining=remaining,
            remaining_fraction=(remaining / budget.limit) if budget.limit else 0.0,
            window_seconds=budget.window_seconds,
            seconds_until_window_resets=max(0.0, budget.window_seconds - (now - oldest)),
            source=source,
            reason=reason,
            observed_at_ns=self._now_ns(),
        )

    def _spent_in_window(self, budget: _ClassBudget) -> float:
        now = self._monotonic()
        budget.spends = [
            (at, weight) for at, weight in budget.spends if now - at < budget.window_seconds
        ]
        return sum(weight for _, weight in budget.spends)

    def read_all(self) -> tuple[VenueRateBudget, ...]:
        return tuple(
            self.read(venue_id, request_class)
            for venue_id, request_class in sorted(self._budgets)
        )


def describe_budgets(budgeter: VenueRateBudgeter) -> dict:
    return {
        "part_id": PART_ID,
        "requests_counted": budgeter.standing.requests_counted,
        "requests_refused": budgeter.standing.requests_refused,
        "venue_corrections": budgeter.standing.venue_corrections,
        "largest_unseen_spend": budgeter.standing.largest_undercount,
        "classes_tracked": budgeter.standing.classes_tracked,
        "budgets": [
            {
                "venue_id": budget.venue_id,
                "request_class": budget.request_class,
                "remaining": budget.remaining,
                "remaining_fraction": budget.remaining_fraction,
                "source": budget.source,
            }
            for budget in budgeter.read_all()
        ],
    }


def run_venue_rate_budgeter(
    budgeter: VenueRateBudgeter, control_socket, read_events, publish_budgets,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_events(budgeter)
        publish_budgets(budgeter.read_all())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
