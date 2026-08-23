"""capital-utilisation-meter: how much of a segment's allocation is working.

Three states the money can be in, and conflating any two of them hides a real
problem:

- **In positions** -- committed to open trades.
- **Locked** -- reserved against orders that have been sent but not filled.
- **Free** -- available for the next trade.

The one that is easy to lose is locked. Capital held against an order that will
never fill is invisible in both of the other two: the position does not exist and
the money is not free. A segment can be unable to trade while its position report
shows it flat, and only this part can say why.

**Utilisation above one is reported, never clamped.** It means locks and
positions together exceed the allocation, which is a real fault -- a fill that
was double-counted, a lock that was never released -- and clamping it to 100%
would erase the only evidence.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "capital-utilisation-meter"

PART_DECLARATION = PartDeclaration(
    part_id="capital-utilisation-meter",
    consumes=("account-balance", "locked-allocation", "capital-allotment"),
    produces=("capital-utilisation", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

MEASURED = "measured"
OVER_COMMITTED = "over-committed"
NO_ALLOTMENT = "no-allotment"


@dataclass(frozen=True)
class CapitalUtilisation:
    """Where a segment's allocation currently sits."""

    segment: str
    allotted: float
    in_positions: float
    locked: float
    free: float
    utilisation: float
    state: str
    positions_counted: int
    locks_counted: int
    reason: str
    measured_at_ns: int

    @property
    def is_over_committed(self) -> bool:
        return self.state == OVER_COMMITTED


@dataclass
class UtilisationStanding:
    measurements: int = 0
    over_commitments: int = 0
    highest_utilisation: float = 0.0
    longest_held_lock_seconds: float = 0.0
    segments_measured: int = 0


class CapitalUtilisationMeter:
    """Splits a segment's allocation into positions, locks and free capital."""

    def __init__(self, monotonic=time.monotonic, now_ns=time.time_ns) -> None:
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._allotments: dict[str, float] = {}
        self._positions: dict[str, dict[str, float]] = {}
        self._locks: dict[str, dict[str, tuple[float, float]]] = {}
        self.standing = UtilisationStanding()

    def set_allotment(self, segment: str, allotted: float) -> None:
        self._allotments[segment] = allotted
        self.standing.segments_measured = len(self._allotments)

    def observe_position_capital(self, segment: str, symbol: str, capital: float) -> None:
        """Capital committed to one open position; zero removes it."""
        positions = self._positions.setdefault(segment, {})
        if capital <= 0:
            positions.pop(symbol, None)
        else:
            positions[symbol] = capital

    def observe_lock(self, segment: str, order_id: str, amount: float) -> None:
        """Capital held against a sent order, with when the hold began."""
        self._locks.setdefault(segment, {})[order_id] = (amount, self._monotonic())

    def release_lock(self, segment: str, order_id: str) -> None:
        self._locks.get(segment, {}).pop(order_id, None)

    def measure(self, segment: str) -> CapitalUtilisation:
        self.standing.measurements += 1
        allotted = self._allotments.get(segment)
        positions = self._positions.get(segment, {})
        locks = self._locks.get(segment, {})

        in_positions = sum(positions.values())
        locked = sum(amount for amount, _ in locks.values())
        if locks:
            now = self._monotonic()
            self.standing.longest_held_lock_seconds = max(
                self.standing.longest_held_lock_seconds,
                max(now - held_at for _, held_at in locks.values()),
            )

        if allotted is None:
            return self._utilisation(
                segment, 0.0, in_positions, locked, 0.0, 0.0, NO_ALLOTMENT,
                len(positions), len(locks),
                "no allocation has been read for this segment; utilisation has no denominator",
            )

        committed = in_positions + locked
        free = allotted - committed
        utilisation = committed / allotted if allotted > 0 else 0.0
        self.standing.highest_utilisation = max(self.standing.highest_utilisation, utilisation)

        if utilisation > 1.0:
            self.standing.over_commitments += 1
            return self._utilisation(
                segment, allotted, in_positions, locked, free, utilisation, OVER_COMMITTED,
                len(positions), len(locks),
                f"{committed:,.2f} committed against an allocation of {allotted:,.2f} "
                f"({utilisation:.0%}); a fill was double-counted or a lock was never released",
            )

        return self._utilisation(
            segment, allotted, in_positions, locked, free, utilisation, MEASURED,
            len(positions), len(locks),
            f"{in_positions:,.2f} in {len(positions)} position(s), {locked:,.2f} locked against "
            f"{len(locks)} order(s), {free:,.2f} free",
        )

    def measure_all(self) -> tuple[CapitalUtilisation, ...]:
        return tuple(self.measure(segment) for segment in sorted(self._allotments))

    def _utilisation(
        self, segment, allotted, in_positions, locked, free, utilisation, state,
        position_count, lock_count, reason
    ) -> CapitalUtilisation:
        return CapitalUtilisation(
            segment=segment, allotted=allotted, in_positions=in_positions, locked=locked,
            free=free, utilisation=utilisation, state=state,
            positions_counted=position_count, locks_counted=lock_count,
            reason=reason, measured_at_ns=self._now_ns(),
        )


def describe_utilisation(meter: CapitalUtilisationMeter) -> dict:
    return {
        "part_id": PART_ID,
        "measurements": meter.standing.measurements,
        "segments_measured": meter.standing.segments_measured,
        "over_commitments": meter.standing.over_commitments,
        "highest_utilisation": meter.standing.highest_utilisation,
        "longest_held_lock_seconds": meter.standing.longest_held_lock_seconds,
    }


def run_capital_utilisation_meter(
    meter: CapitalUtilisationMeter, control_socket, read_capital, publish_utilisation,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_capital(meter)
        publish_utilisation(meter.measure_all())

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

    The allotment from the capital desk, locks from the fund-lock ledger, and
    the capital in positions from the segment's balance -- its equity less
    its cash is what is in positions. Measured once per health interval.
    """
    import time as _time

    from runtime.input_assembly import Batch, LatestByKey

    balances = LatestByKey(read=context.bus.reader("account-balance"), key_of=lambda b: b.segment)
    locks = Batch(read=context.bus.reader("locked-allocation"))
    allotments = Batch(read=context.bus.reader("capital-allotment"))
    publish_utilisation = context.bus.publisher_for("capital-utilisation")
    meter = CapitalUtilisationMeter()
    segment = str(context.setting("segment_id").value)
    last_measure = [float("-inf")]

    def read_capital(_meter) -> None:
        for allotment in allotments.payloads():
            meter.set_allotment(allotment.segment, allotment.allotted)
        for lock in locks.payloads():
            if lock.state == "locked":
                meter.observe_lock(segment, lock.order_id, lock.amount)
            elif lock.state == "released":
                meter.release_lock(segment, lock.order_id)
        balance = balances.mapping().get(segment)
        if balance is not None:
            meter.observe_position_capital(segment, "all", max(0.0, balance.equity - balance.cash))

    def tick() -> None:
        read_capital(meter)
        now = _time.monotonic()
        if now - last_measure[0] < context.health_interval_seconds:
            return
        measured = meter.measure_all()
        if measured:
            publish_utilisation(measured)
        last_measure[0] = now

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
    )
