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
    )
