"""duty-cycle-planner: the hours each heavy part may run, so retraining lands quiet."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "duty-cycle-planner"

PART_DECLARATION = PartDeclaration(
    part_id="duty-cycle-planner",
    consumes=("part-resource-usage", "market-data"),
    produces=("duty-cycle", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

HOURS_IN_DAY = 24


@dataclass(frozen=True)
class DutyCycle:
    """The hours of the UTC day a heavy part is allowed to run."""

    part_id: str
    allowed_hours: tuple[int, ...]
    quietest_hour: int | None
    busiest_hour: int | None
    samples: int
    reason: str
    planned_at_ns: int


@dataclass
class PlannerStanding:
    hours_observed: int = 0
    plans_made: int = 0
    activity_by_hour: dict = field(default_factory=dict)


class DutyCyclePlanner:
    """Learns which UTC hours the market is quiet, and books heavy work into them.

    Market activity is measured rather than assumed: crypto has no session, so
    the quiet hours are a property of this symbol set and this venue pair, and
    they drift. Until enough hours have been seen, every hour is allowed and the
    reason says so -- refusing to run heavy work on no evidence would delay
    retraining forever.
    """

    def __init__(self, quiet_hours_wanted: int, minimum_days_observed: int, now_ns=time.time_ns) -> None:
        self._hours_wanted = quiet_hours_wanted
        self._minimum_days = minimum_days_observed
        self._now_ns = now_ns
        self._messages_by_hour: dict[int, int] = {}
        self._days_by_hour: dict[int, set[int]] = {}
        self.standing = PlannerStanding()

    def observe_market_activity(self, hour_of_day: int, day_index: int, messages: int) -> None:
        hour = hour_of_day % HOURS_IN_DAY
        self._messages_by_hour[hour] = self._messages_by_hour.get(hour, 0) + messages
        self._days_by_hour.setdefault(hour, set()).add(day_index)
        self.standing.hours_observed = len(self._messages_by_hour)
        self.standing.activity_by_hour = dict(sorted(self._messages_by_hour.items()))

    @property
    def days_observed(self) -> int:
        return min((len(days) for days in self._days_by_hour.values()), default=0)

    def plan(self, part_id: str) -> DutyCycle:
        self.standing.plans_made += 1
        samples = sum(self._messages_by_hour.values())
        if self.days_observed < self._minimum_days or len(self._messages_by_hour) < HOURS_IN_DAY:
            return DutyCycle(
                part_id=part_id,
                allowed_hours=tuple(range(HOURS_IN_DAY)),
                quietest_hour=None,
                busiest_hour=None,
                samples=samples,
                reason=(
                    f"only {self.days_observed} full day(s) of {len(self._messages_by_hour)} hours "
                    f"observed; every hour allowed until there is evidence"
                ),
                planned_at_ns=self._now_ns(),
            )

        ranked = sorted(self._messages_by_hour.items(), key=lambda item: (item[1], item[0]))
        quiet = tuple(sorted(hour for hour, _ in ranked[: self._hours_wanted]))
        return DutyCycle(
            part_id=part_id,
            allowed_hours=quiet,
            quietest_hour=ranked[0][0],
            busiest_hour=ranked[-1][0],
            samples=samples,
            reason=f"{self._hours_wanted} quietest UTC hours over {self.days_observed} day(s)",
            planned_at_ns=self._now_ns(),
        )


def describe_duty_cycles(planner: DutyCyclePlanner) -> dict:
    return {
        "part_id": PART_ID,
        "plans_made": planner.standing.plans_made,
        "hours_observed": planner.standing.hours_observed,
        "days_observed": planner.days_observed,
        "activity_by_hour": dict(planner.standing.activity_by_hour),
    }


def run_duty_cycle_planner(
    planner: DutyCyclePlanner, control_socket, read_activity, read_heavy_parts, publish_duty_cycles,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for hour, day, messages in read_activity():
            planner.observe_market_activity(hour, day, messages)
        publish_duty_cycles(tuple(planner.plan(part_id) for part_id in read_heavy_parts()))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_duty_cycles(planner),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Market activity is counted from market-data per UTC hour and published
    as a plan per heavy part once per health interval. A heavy part is one
    part-appetite-meter has measured above duty_cycle_heavy_cpu_fraction of a
    core; which parts are heavy is measured, never listed (T-4).
    """
    import datetime
    import time as _time

    # `market-data` carries trades AND candles: venue-trade-stream-reader
    # publishes the first, ccxt-venue-reader the second, and both have always
    # declared it. This part wants trades and now says so, rather than assuming
    # the wire holds only what it happens to want -- a part that dies on an
    # unexpected shape is a part the wiring can kill.
    from runtime.market_data_stream import trades_in
    from runtime.input_assembly import Batch, LatestByKey

    market_data = Batch(read=context.bus.reader("market-data"))
    usages = LatestByKey(read=context.bus.reader("part-resource-usage"), key_of=lambda u: u.part_id)
    publish_duty_cycles = context.bus.publisher_for("duty-cycle")
    planner = DutyCyclePlanner(
        quiet_hours_wanted=int(context.number("duty_cycle_quiet_hours_wanted")),
        minimum_days_observed=int(context.number("duty_cycle_minimum_days_observed")),
    )
    heavy_fraction = context.number("duty_cycle_heavy_cpu_fraction")
    counts: dict[tuple[int, int], int] = {}
    first_day = [None]
    last_plan = [float("-inf")]

    def read_activity():
        # Counted per (hour, day) as messages arrive; handed over as totals so
        # far, which the planner keeps as the latest figure for that hour.
        for item in trades_in(market_data.payloads()):
            when = datetime.datetime.fromtimestamp(item.venue_time_ns / 1e9, datetime.UTC)
            if first_day[0] is None:
                first_day[0] = when.date()
            day_index = (when.date() - first_day[0]).days
            counts[(when.hour, day_index)] = counts.get((when.hour, day_index), 0) + 1
        return tuple((hour, day, messages) for (hour, day), messages in counts.items())

    def read_heavy_parts():
        now = _time.monotonic()
        if now - last_plan[0] < context.health_interval_seconds:
            return ()
        last_plan[0] = now
        return tuple(
            part_id for part_id, usage in sorted(usages.mapping().items())
            if usage.cpu_seconds_per_second is not None and usage.cpu_seconds_per_second >= heavy_fraction
        )

    def publish(cycles) -> None:
        if cycles:
            publish_duty_cycles(cycles)

    return run_duty_cycle_planner(
        planner=planner,
        control_socket=context.control_socket,
        read_activity=read_activity,
        read_heavy_parts=read_heavy_parts,
        publish_duty_cycles=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
