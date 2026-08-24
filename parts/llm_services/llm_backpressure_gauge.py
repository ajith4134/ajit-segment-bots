"""llm-backpressure-gauge: one number saying how hard to slow down, and why.

Without this part every caller inspects quota, spend and survival tier for itself,
and they disagree -- one part decides the system is out of budget while another
carries on, and the disagreement is invisible because each is individually correct.
So the gauge produces a single admit fraction with a stated reason, and everything
downstream obeys it.

The shape of the curve is the whole design. Three properties:

- **It is not a cliff.** Admitting everything until the budget is gone and then
  nothing means the last part to ask gets nothing, which is a race rather than a
  policy. Admission falls smoothly as the pool drains.
- **It accounts for how much of the window is left.** Sixty percent used ten minutes
  into an hour is on pace; the same figure fifty minutes in is not. Pressure is
  measured against elapsed time, so a fast burn is throttled before it empties the
  window rather than after.
- **Survival tier overrides everything.** When the trading system itself is in a
  degraded tier, reasoning is not what should be consuming resources, and the gauge
  says so with the tier named rather than blending it into an average.

The tightest constraint wins. Quota, money and survival are not averaged -- averaging
lets a healthy quota mask an exhausted budget, which is the exact case where money
matters most.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.llm_types import LlmBackpressure
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "llm-backpressure-gauge"

PART_DECLARATION = PartDeclaration(
    part_id="llm-backpressure-gauge",
    consumes=("llm-quota-state", "llm-spend-state", "survival-tier", "llm-part-budget"),
    produces=("llm-backpressure", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

OPEN = "open"
THROTTLED_BY_QUOTA = "throttled-because-the-window-is-draining"
THROTTLED_BY_SPEND = "throttled-because-the-money-is-draining"
THROTTLED_BY_PACE = "throttled-because-the-burn-is-ahead-of-the-clock"
STOPPED_BY_SURVIVAL = "stopped-because-the-trading-system-is-in-a-degraded-tier"
STOPPED_EXHAUSTED = "stopped-because-nothing-is-left"
NOT_MEASURED = "nothing-has-been-measured-yet"

# Survival tiers in which reasoning is not what should be consuming resources.
TIERS_THAT_STOP_REASONING = ("critical", "shutdown")


@dataclass(frozen=True)
class GaugeReading:
    state: str
    backpressure: LlmBackpressure
    binding_constraint: str
    quota_fraction: float | None
    spend_fraction: float | None
    pace_ratio: float | None
    reason: str
    measured_at_ns: int

    @property
    def admits_everything(self) -> bool:
        return self.backpressure.admit_fraction >= 1.0


@dataclass
class GaugeStanding:
    readings: int = 0
    times_open: int = 0
    times_throttled: int = 0
    times_stopped: int = 0
    times_survival_bound: int = 0
    times_quota_bound: int = 0
    times_spend_bound: int = 0
    times_pace_bound: int = 0


class LlmBackpressureGauge:
    """Turns quota, spend, pace and survival into one admit fraction with a reason."""

    def __init__(
        self,
        throttle_begins_at: float,
        pace_tolerance: float,
        minimum_admit_fraction: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < throttle_begins_at < 1.0:
            raise ValueError(
                "throttling must begin before the pool is empty: admitting everything "
                "until it is gone makes the last part to ask lose a race rather than "
                "follow a policy"
            )
        if pace_tolerance < 1.0:
            raise ValueError(
                "the pace tolerance is how far ahead of the clock the burn may run, and "
                "being exactly on pace is not an overrun"
            )
        if not 0.0 <= minimum_admit_fraction < 1.0:
            raise ValueError(
                "the floor is what still gets through while throttled; at 1.0 nothing is "
                "ever throttled"
            )
        self._throttle_begins_at = throttle_begins_at
        self._pace_tolerance = pace_tolerance
        self._minimum_admit_fraction = minimum_admit_fraction
        self._now_ns = now_ns
        self._quota = None
        self._spend = None
        self._survival_tier: str | None = None
        self.standing = GaugeStanding()

    def observe_quota(self, quota) -> None:
        self._quota = quota

    def observe_spend(self, spend) -> None:
        self._spend = spend

    def observe_survival_tier(self, tier: str) -> None:
        self._survival_tier = tier

    def admit_fraction_for(self, fraction_used: float) -> float:
        """Falls smoothly from the throttle point to the floor, never a cliff."""
        if fraction_used <= self._throttle_begins_at:
            return 1.0
        if fraction_used >= 1.0:
            return 0.0
        span = 1.0 - self._throttle_begins_at
        travelled = (fraction_used - self._throttle_begins_at) / span
        return max(1.0 - travelled, self._minimum_admit_fraction)

    def pace_ratio(self) -> float | None:
        """Used-so-far against elapsed-so-far. Above one is burning ahead of the clock."""
        if self._quota is None or self._quota.window_seconds <= 0:
            return None
        remaining = max(self._quota.resets_at_ns - self._now_ns(), 0) / 1e9
        elapsed_fraction = 1.0 - remaining / self._quota.window_seconds
        if elapsed_fraction <= 0:
            return None
        return self._quota.fraction_used / elapsed_fraction

    def measure(self) -> GaugeReading:
        self.standing.readings += 1
        now = self._now_ns()

        if self._quota is None and self._spend is None:
            return self._reading(
                NOT_MEASURED, 0.0, "nothing-measured", None, None, None,
                "no quota and no spend state has been measured. Admitting freely on no "
                "measurement is exactly the assumption Rule 8 forbids, so nothing is "
                "admitted until something is known",
            )

        # Survival overrides everything and is never blended into an average.
        if self._survival_tier in TIERS_THAT_STOP_REASONING:
            self.standing.times_stopped += 1
            self.standing.times_survival_bound += 1
            return self._reading(
                STOPPED_BY_SURVIVAL, 0.0, "survival-tier",
                self._quota.fraction_used if self._quota else None,
                self._spend.fraction_used if self._spend else None,
                self.pace_ratio(),
                f"the trading system is in the {self._survival_tier} tier. Reasoning is "
                f"not what should be consuming resources, and the tier is named rather "
                f"than averaged into a number",
            )

        candidates = []
        if self._quota is not None:
            candidates.append(
                (self._quota.fraction_used, THROTTLED_BY_QUOTA, "quota")
            )
        if self._spend is not None:
            candidates.append(
                (self._spend.fraction_used, THROTTLED_BY_SPEND, "spend")
            )

        pace = self.pace_ratio()
        if pace is not None and pace > self._pace_tolerance:
            # Convert the overrun into an equivalent used-fraction so one comparison
            # decides everything. The overrun is scaled across the throttling span
            # rather than added to it: adding it raw would turn a burn 40% above pace
            # into a full stop, which is a cliff wearing the appearance of a curve.
            overrun = (pace - self._pace_tolerance) / self._pace_tolerance
            candidates.append(
                (
                    min(
                        self._throttle_begins_at
                        + min(overrun, 1.0) * (1.0 - self._throttle_begins_at),
                        1.0,
                    ),
                    THROTTLED_BY_PACE,
                    "pace",
                )
            )

        # The tightest constraint wins. Averaging lets a healthy quota mask an
        # exhausted budget, which is the case where money matters most.
        fraction_used, state, binding = max(candidates, key=lambda entry: entry[0])
        admit = self.admit_fraction_for(fraction_used)

        if admit <= 0.0:
            self.standing.times_stopped += 1
            state = STOPPED_EXHAUSTED
        elif admit >= 1.0:
            self.standing.times_open += 1
            state = OPEN
        else:
            self.standing.times_throttled += 1

        if binding == "quota":
            self.standing.times_quota_bound += 1
        elif binding == "spend":
            self.standing.times_spend_bound += 1
        elif binding == "pace":
            self.standing.times_pace_bound += 1

        return self._reading(
            state, admit, binding,
            self._quota.fraction_used if self._quota else None,
            self._spend.fraction_used if self._spend else None,
            pace,
            f"{binding} is the tightest constraint at {fraction_used:.0%} used, admitting "
            f"{admit:.0%}"
            + (
                f". The burn is {pace:.2f}x the clock, so it is throttled before the "
                f"window empties rather than after"
                if binding == "pace"
                else ""
            ),
        )

    def _reading(
        self, state, admit, binding, quota_fraction, spend_fraction, pace, reason,
    ) -> GaugeReading:
        return GaugeReading(
            state=state,
            backpressure=LlmBackpressure(
                admit_fraction=admit,
                reason=reason,
                quota_fraction_used=quota_fraction if quota_fraction is not None else 0.0,
                spend_fraction_used=spend_fraction if spend_fraction is not None else 0.0,
                survival_tier=self._survival_tier,
                measured_at_ns=self._now_ns(),
            ),
            binding_constraint=binding, quota_fraction=quota_fraction,
            spend_fraction=spend_fraction, pace_ratio=pace, reason=reason,
            measured_at_ns=self._now_ns(),
        )


def describe_backpressure(gauge: LlmBackpressureGauge) -> dict:
    return {
        "part_id": PART_ID,
        "readings": gauge.standing.readings,
        "times_open": gauge.standing.times_open,
        "times_throttled": gauge.standing.times_throttled,
        "times_stopped": gauge.standing.times_stopped,
        "times_bound_by_quota": gauge.standing.times_quota_bound,
        "times_bound_by_spend": gauge.standing.times_spend_bound,
        "times_bound_by_pace": gauge.standing.times_pace_bound,
        "times_bound_by_survival_tier": gauge.standing.times_survival_bound,
        "averages_its_constraints": False,
        "admits_freely_when_nothing_is_measured": False,
    }


def run_llm_backpressure_gauge(
    gauge: LlmBackpressureGauge, control_socket, read_state, publish_backpressure,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_state(gauge)
        publish_backpressure(gauge.measure().backpressure)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_backpressure(gauge),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The latest quota, spend and survival tier are what the gauge measures
    from. Part budgets are read and drained: the gauge answers for the
    subsystem, and the router answers per part.
    """
    from runtime.input_assembly import Batch

    quotas = Batch(read=context.bus.reader("llm-quota-state"))
    spends = Batch(read=context.bus.reader("llm-spend-state"))
    tiers = Batch(read=context.bus.reader("survival-tier"))
    budgets = Batch(read=context.bus.reader("llm-part-budget"))
    publish_backpressure = context.bus.publisher_for("llm-backpressure")
    gauge = LlmBackpressureGauge(
        throttle_begins_at=context.number("llm_throttle_begins_at"),
        pace_tolerance=context.number("llm_pace_tolerance"),
        minimum_admit_fraction=context.number("llm_minimum_admit_fraction"),
    )

    def read_state(_gauge) -> None:
        budgets.payloads()
        for quota in quotas.payloads():
            gauge.observe_quota(quota)
        for spend in spends.payloads():
            gauge.observe_spend(spend)
        for reading in tiers.payloads():
            tier = getattr(reading, "tier", reading)
            gauge.observe_survival_tier(str(getattr(tier, "tier", tier)))

    return run_llm_backpressure_gauge(
        gauge=gauge,
        control_socket=context.control_socket,
        read_state=read_state,
        publish_backpressure=lambda backpressure: publish_backpressure((backpressure,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
