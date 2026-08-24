"""survival-tier-monitor: how much runway is left, in whatever runs out first.

This is a budget measurement, not a health one. Everything can be working perfectly
while the system is four hours from having no money to think with, and nothing else in
the block is looking at that.

The tier is set by the **binding** resource -- whichever is closest to exhaustion --
rather than by an average, because averaging a healthy quota with an exhausted budget
reports "fine" in exactly the situation where the exhausted one is about to stop
everything.

Runway is measured in time, not in fraction remaining. Forty percent left is
comfortable at the current burn and critical at ten times it, and the difference is
the only thing a conservation decision can act on. So the monitor projects from the
observed burn rate and reports seconds to exhaustion.

Three properties that stop the tier being noise:

- **It is hysteretic.** A tier that flips between comfortable and frugal every reading
  makes every consumer thrash. Improving out of a tier needs more headroom than
  falling into it needed.
- **Falling is immediate; rising is slow.** Contracting needs no evidence and
  expanding needs sustained evidence, which is the same asymmetry the autonomy
  envelope uses and for the same reason.
- **Unmeasured is not comfortable.** With nothing measured the tier is the most
  restrictive one, because assuming runway that has not been measured is how a system
  discovers it is out of money by stopping.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.autonomy_types import (
    COMFORTABLE, CRITICAL, FRUGAL, SHUTDOWN, SURVIVAL_TIERS, SurvivalTier,
)
from runtime.rolling_statistics import RollingWindow
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "survival-tier-monitor"

PART_DECLARATION = PartDeclaration(
    part_id="survival-tier-monitor",
    consumes=("llm-quota-state", "llm-spend-state", "part-health"),
    produces=("survival-tier", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

MEASURED = "measured"
NOT_MEASURED = "nothing-has-been-measured-so-the-tier-is-the-most-restrictive"

QUOTA = "subscription-quota"
MONEY = "metered-spend"


@dataclass(frozen=True)
class TierReading:
    state: str
    tier: SurvivalTier
    previous_tier: str | None
    held_by_hysteresis: bool
    reason: str
    measured_at_ns: int

    @property
    def worsened(self) -> bool:
        if self.previous_tier is None:
            return False
        return SURVIVAL_TIERS.index(self.tier.tier) > SURVIVAL_TIERS.index(
            self.previous_tier
        )


@dataclass
class MonitorStanding:
    readings: int = 0
    tier_changes: int = 0
    held_by_hysteresis: int = 0
    times_bound_by_quota: int = 0
    times_bound_by_money: int = 0
    unmeasured_readings: int = 0
    shortest_runway_seconds: float | None = None


class SurvivalTierMonitor:
    """Sets the tier from whichever resource runs out first, with hysteresis."""

    def __init__(
        self,
        thresholds,
        improvement_margin: float,
        readings_before_improving: int,
        burn_window: int,
        now_ns=time.time_ns,
    ) -> None:
        """`thresholds` maps tier -> the fraction-remaining at or below which it applies."""
        missing = set(SURVIVAL_TIERS[1:]) - set(thresholds)
        if missing:
            raise ValueError(
                f"every tier below comfortable needs a threshold; missing {sorted(missing)}"
            )
        if improvement_margin <= 0:
            raise ValueError(
                "a tier that flips every reading makes every consumer thrash, so "
                "improving out of one needs more headroom than falling into it"
            )
        if readings_before_improving < 2:
            raise ValueError("falling is immediate; rising takes sustained evidence")
        if burn_window < 2:
            raise ValueError("a burn rate needs more than one observation")
        self._thresholds = dict(thresholds)
        self._improvement_margin = improvement_margin
        self._readings_before_improving = readings_before_improving
        self._now_ns = now_ns
        self._burn: dict[str, RollingWindow] = {
            QUOTA: RollingWindow(burn_window), MONEY: RollingWindow(burn_window),
        }
        self._last_fraction: dict[str, float] = {}
        self._tier: str | None = None
        self._improving_for = 0
        self.standing = MonitorStanding()

    def observe(self, resource: str, fraction_remaining: float) -> None:
        if resource not in self._burn:
            raise ValueError(f"{resource!r} is not a resource this monitor tracks")
        previous = self._last_fraction.get(resource)
        if previous is not None:
            self._burn[resource].observe(max(previous - fraction_remaining, 0.0))
        self._last_fraction[resource] = fraction_remaining

    def runway_seconds(self, resource: str, seconds_per_reading: float) -> float | None:
        """Projected from the observed burn. Forty percent left means nothing alone."""
        window = self._burn.get(resource)
        remaining = self._last_fraction.get(resource)
        if window is None or remaining is None:
            return None
        rate = window.mean(2)
        if rate is None or rate <= 0:
            return None
        return (remaining / rate) * seconds_per_reading

    def tier_for(self, fraction: float) -> str:
        for tier in (SHUTDOWN, CRITICAL, FRUGAL):
            if fraction <= self._thresholds[tier]:
                return tier
        return COMFORTABLE

    def measure(self, seconds_per_reading: float = 1.0) -> TierReading:
        self.standing.readings += 1
        previous = self._tier

        if not self._last_fraction:
            self.standing.unmeasured_readings += 1
            self._tier = SHUTDOWN
            return self._reading(
                NOT_MEASURED, SHUTDOWN, "nothing", 0.0, None, previous, False,
                "nothing has been measured. The tier is the most restrictive one: "
                "assuming runway that has not been measured is how a system discovers it "
                "is out of money by stopping",
            )

        # The binding resource, never an average: averaging a healthy quota with an
        # exhausted budget reports fine exactly when the exhausted one stops everything.
        binding = min(self._last_fraction, key=lambda name: self._last_fraction[name])
        fraction = self._last_fraction[binding]
        if binding == QUOTA:
            self.standing.times_bound_by_quota += 1
        else:
            self.standing.times_bound_by_money += 1

        runway = self.runway_seconds(binding, seconds_per_reading)
        if runway is not None and (
            self.standing.shortest_runway_seconds is None
            or runway < self.standing.shortest_runway_seconds
        ):
            self.standing.shortest_runway_seconds = runway

        proposed = self.tier_for(fraction)
        held = False

        if previous is not None and SURVIVAL_TIERS.index(proposed) < SURVIVAL_TIERS.index(previous):
            # Improving. Needs margin and sustained evidence.
            improved_threshold = self._thresholds.get(previous, 0.0) + self._improvement_margin
            self._improving_for += 1
            if (
                fraction < improved_threshold
                or self._improving_for < self._readings_before_improving
            ):
                proposed = previous
                held = True
                self.standing.held_by_hysteresis += 1
        else:
            self._improving_for = 0

        if proposed != previous:
            self.standing.tier_changes += 1
        self._tier = proposed

        return self._reading(
            MEASURED, proposed, binding, fraction, runway, previous, held,
            f"{fraction:.0%} of {binding} remains"
            + (
                f", about {runway / 3600.0:.1f}h at the observed burn"
                if runway is not None
                else ", with no burn rate measured yet"
            )
            + (
                f". Held at {previous} by hysteresis: improving out of a tier needs more "
                f"headroom than falling into it"
                if held
                else ""
            ),
        )

    def _reading(
        self, state, tier, binding, fraction, runway, previous, held, reason,
    ) -> TierReading:
        return TierReading(
            state=state,
            tier=SurvivalTier(
                tier=tier, binding_resource=binding, fraction_remaining=fraction,
                seconds_to_exhaustion=runway, reason=reason,
                measured_at_ns=self._now_ns(),
            ),
            previous_tier=previous, held_by_hysteresis=held, reason=reason,
            measured_at_ns=self._now_ns(),
        )


def describe_survival(monitor: SurvivalTierMonitor) -> dict:
    return {
        "part_id": PART_ID,
        "readings": monitor.standing.readings,
        "tier_changes": monitor.standing.tier_changes,
        "held_by_hysteresis": monitor.standing.held_by_hysteresis,
        "times_bound_by_quota": monitor.standing.times_bound_by_quota,
        "times_bound_by_money": monitor.standing.times_bound_by_money,
        "unmeasured_readings": monitor.standing.unmeasured_readings,
        "shortest_runway_seconds": monitor.standing.shortest_runway_seconds,
        "tiers": list(SURVIVAL_TIERS),
        "averages_its_resources": False,
        "treats_unmeasured_as_comfortable": False,
    }


def run_survival_tier_monitor(
    monitor: SurvivalTierMonitor, control_socket, read_resources, publish_tier,
    seconds_per_reading: float, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for resource, fraction in read_resources():
            monitor.observe(resource, fraction)
        publish_tier(monitor.measure(seconds_per_reading).tier)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_survival(monitor),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Quota is the subscription's own fraction used; money is metered spend
    against its ceiling. Both arrive as levels and are read as levels, and a
    resource that has never reported leaves the monitor at its most
    restrictive answer, which is the honest one. Part-health is consumed as
    the wake signal, and the seconds between readings -- which turn a burn
    rate into a runway -- are the part's own health interval, the cadence
    this loop actually runs at.
    """
    from runtime.input_assembly import Batch, LatestValue

    quotas = LatestValue(read=context.bus.reader("llm-quota-state"))
    spends = LatestValue(read=context.bus.reader("llm-spend-state"))
    health = Batch(read=context.bus.reader("part-health"))
    publish_tier = context.bus.publisher_for("survival-tier")

    from runtime.autonomy_types import CRITICAL, FRUGAL, SHUTDOWN

    monitor = SurvivalTierMonitor(
        thresholds={
            FRUGAL: context.number("survival_frugal_at_fraction"),
            CRITICAL: context.number("survival_critical_at_fraction"),
            SHUTDOWN: context.number("survival_shutdown_at_fraction"),
        },
        improvement_margin=context.number("survival_improvement_margin"),
        readings_before_improving=int(
            context.number("survival_readings_before_improving")
        ),
        burn_window=int(context.number("survival_burn_window")),
    )

    def read_resources():
        health.payloads()
        readings = []
        quota = quotas.value()
        if quota is not None:
            readings.append((QUOTA, max(0.0, 1.0 - quota.fraction_used)))
        spend = spends.value()
        if spend is not None:
            readings.append((MONEY, max(0.0, 1.0 - spend.fraction_used)))
        return tuple(readings)

    return run_survival_tier_monitor(
        monitor=monitor,
        control_socket=context.control_socket,
        read_resources=read_resources,
        publish_tier=lambda tier: publish_tier((tier,)),
        seconds_per_reading=context.health_interval_seconds,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
