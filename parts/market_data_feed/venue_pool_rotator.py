"""venue-pool-rotator: spread request classes across venues by standing and headroom."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "venue-pool-rotator"

PART_DECLARATION = PartDeclaration(
    part_id="venue-pool-rotator",
    consumes=("symbol-universe", "venue-standing", "venue-rate-budget", "stream-plan"),
    produces=("market-data", "order-book-snapshot", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

SERVING = "serving"
THROTTLED = "throttled"
BANNED = "banned"
UNKNOWN = "unknown"

# How much of a throttled venue's share is kept. It is not cut to zero: a
# throttled venue is still answering, and a symbol only that venue carries would
# otherwise go dark for a condition that is meant to be temporary.
THROTTLED_WEIGHT = 0.25


@dataclass(frozen=True)
class Routing:
    """Which venue one request class should use next, and why that one."""

    request_class: str
    symbol: str
    venue_id: str | None
    reason: str
    weight: float
    decided_at_ns: int


@dataclass
class RotatorStanding:
    routed: int = 0
    unroutable: int = 0
    by_venue: dict[str, int] = field(default_factory=dict)
    venues_banned: int = 0
    # Routings decided that nothing could act on, because no routed REST fetch
    # exists yet (RL-062). Counted so the gap is a number, not a silence.
    routings_with_no_fetch_path: int = 0


class VenuePoolRotator:
    """Chooses a venue per request, weighted by standing and remaining headroom.

    Weighted round-robin rather than "always the best": leaning on one venue is
    how an IP ban is earned, and the venue with the most headroom right now is
    the one whose headroom is about to shrink. A symbol no serving venue carries
    routes nowhere and says so -- an unroutable request must not silently become
    a request to a banned venue.
    """

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._standing: dict[str, str] = {}
        self._headroom: dict[str, float] = {}
        self._carries: dict[str, set[str]] = {}
        self._served: dict[tuple[str, str], dict[str, float]] = {}
        self.standing = RotatorStanding()

    def set_venue_standing(self, venue_id: str, state: str) -> None:
        self._standing[venue_id] = state

    def set_rate_headroom(self, venue_id: str, remaining_fraction: float) -> None:
        self._headroom[venue_id] = max(0.0, min(1.0, remaining_fraction))

    def set_symbol_venues(self, symbol: str, venue_ids: set[str]) -> None:
        self._carries[symbol] = set(venue_ids)

    def weight_of(self, venue_id: str) -> float:
        """A venue's share of traffic: standing first, then how much room is left."""
        state = self._standing.get(venue_id, UNKNOWN)
        if state == BANNED:
            return 0.0
        headroom = self._headroom.get(venue_id, 1.0)
        if state == THROTTLED:
            return THROTTLED_WEIGHT * headroom
        if state == UNKNOWN:
            return 0.0
        return headroom

    def route(self, request_class: str, symbol: str) -> Routing:
        candidates = {
            venue_id: self.weight_of(venue_id)
            for venue_id in self._carries.get(symbol, set())
        }
        usable = {venue: weight for venue, weight in candidates.items() if weight > 0}
        if not usable:
            self.standing.unroutable += 1
            banned = sorted(v for v in candidates if self._standing.get(v) == BANNED)
            self.standing.venues_banned = len(banned)
            return Routing(
                request_class=request_class,
                symbol=symbol,
                venue_id=None,
                reason=(
                    f"no venue carrying {symbol} is serving"
                    + (f"; banned: {', '.join(banned)}" if banned else "")
                ),
                weight=0.0,
                decided_at_ns=self._now_ns(),
            )

        served = self._served.setdefault((request_class, symbol), {})
        # The venue furthest below its share goes next, so the split converges on
        # the weights instead of always naming the same venue.
        total = sum(usable.values())
        chosen = min(
            usable,
            key=lambda venue: (served.get(venue, 0.0) / (usable[venue] / total), venue),
        )
        served[chosen] = served.get(chosen, 0.0) + 1.0
        self.standing.routed += 1
        self.standing.by_venue[chosen] = self.standing.by_venue.get(chosen, 0) + 1
        return Routing(
            request_class=request_class,
            symbol=symbol,
            venue_id=chosen,
            reason=f"standing {self._standing.get(chosen, UNKNOWN)}, weight {usable[chosen]:.2f}",
            weight=usable[chosen],
            decided_at_ns=self._now_ns(),
        )


def describe_rotation(rotator: VenuePoolRotator) -> dict:
    return {
        "part_id": PART_ID,
        "routed": rotator.standing.routed,
        "unroutable": rotator.standing.unroutable,
        "by_venue": dict(rotator.standing.by_venue),
        "venues_banned": rotator.standing.venues_banned,
    }


def run_venue_pool_rotator(
    rotator: VenuePoolRotator, control_socket, read_requests, publish_routing,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for request_class, symbol in read_requests():
            publish_routing(rotator.route(request_class, symbol))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_rotation(rotator),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The rotator keeps, from the bus, which venues carry each symbol, what each
    venue's standing is, and how much of its rate budget is left. The blueprint
    says it produces market-data and order-book-snapshot -- the routed REST
    fetch of a symbol a stream does not carry. No REST market-data fetch exists
    in phase 1, so this part publishes nothing on either type and its standing
    says so (RL-062): `routings_with_no_fetch_path` counts every routing that
    was decided and could not be acted on. The routing itself is real and is
    what the fetch will be built on.
    """
    from runtime.input_assembly import Batch, LatestValue

    universe = Batch(read=context.bus.reader("symbol-universe"))
    standings = Batch(read=context.bus.reader("venue-standing"))
    budgets = Batch(read=context.bus.reader("venue-rate-budget"))
    plans = LatestValue(read=context.bus.reader("stream-plan"))
    # Declared, never called: the fetch these would carry does not exist yet.
    context.bus.publisher_for("market-data")
    context.bus.publisher_for("order-book-snapshot")
    rotator = VenuePoolRotator()

    carries: dict[str, set[str]] = {}

    def read_requests():
        for entry in universe.payloads():
            carries.setdefault(entry.symbol, set()).add(entry.venue_id)
            rotator.set_symbol_venues(entry.symbol, carries[entry.symbol])
        for standing in standings.payloads():
            rotator.set_venue_standing(standing.venue_id, standing.state)
        for budget in budgets.payloads():
            rotator.set_rate_headroom(budget.venue_id, budget.remaining_fraction)
        plans.value()
        return ()  # nothing asks for a routed fetch until a fetch path exists

    def publish_routing(routing) -> None:
        rotator.standing.routings_with_no_fetch_path += 1

    return run_venue_pool_rotator(
        rotator=rotator,
        control_socket=context.control_socket,
        read_requests=read_requests,
        publish_routing=publish_routing,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
