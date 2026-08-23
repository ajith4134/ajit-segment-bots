"""memory-pressure-forecaster: minutes to memory exhaustion, from each part's growth."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "memory-pressure-forecaster"

PART_DECLARATION = PartDeclaration(
    part_id="memory-pressure-forecaster",
    consumes=("part-resource-usage", "hardware-capacity"),
    produces=("memory-forecast", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

# Samples needed before a growth rate is published. One point is a level, two is
# noise; a forecast from either would have the governor switching parts off over
# a garbage-collection cycle.
MINIMUM_SAMPLES = 4


@dataclass(frozen=True)
class MemoryForecast:
    """When memory runs out at the current rate, and who is filling it."""

    seconds_to_exhaustion: float | None
    growth_bytes_per_second: float
    available_bytes: int
    fastest_growing_part: str | None
    fastest_growth_bytes_per_second: float | None
    samples: int
    reason: str
    observed_at_ns: int


@dataclass
class ForecasterStanding:
    forecasts: int = 0
    exhaustion_predicted: int = 0
    samples_by_part: dict[str, int] = field(default_factory=dict)


class MemoryPressureForecaster:
    """Fits each part's memory against time and extrapolates the total.

    Least-squares over a bounded window rather than last-minus-first: a part that
    spikes and settles would otherwise read as growing forever. Shrinking parts
    count too -- their negative rate is real, and ignoring it would forecast
    exhaustion that the machine is actually moving away from.
    """

    def __init__(self, window_samples: int, monotonic=time.monotonic, now_ns=time.time_ns) -> None:
        self._window = window_samples
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._history: dict[str, list[tuple[float, int]]] = {}
        self.standing = ForecasterStanding()

    def observe(self, usages) -> None:
        now = self._monotonic()
        for usage in usages:
            if usage.memory_current_bytes is None:
                continue
            samples = self._history.setdefault(usage.part_id, [])
            samples.append((now, usage.memory_current_bytes))
            del samples[: max(0, len(samples) - self._window)]
            self.standing.samples_by_part[usage.part_id] = len(samples)

    def forecast(self, capacity) -> MemoryForecast:
        rates = {
            part_id: rate
            for part_id, samples in self._history.items()
            if (rate := self._growth_rate(samples)) is not None
        }
        available = capacity.facts.available_ram_bytes
        total_rate = sum(rates.values())
        fastest = max(rates.items(), key=lambda item: item[1], default=(None, None))
        self.standing.forecasts += 1

        if not rates:
            return self._forecast(None, 0.0, available, fastest, "not enough samples to fit a rate")
        if total_rate <= 0:
            return self._forecast(None, total_rate, available, fastest, "memory is not growing")
        seconds = available / total_rate
        self.standing.exhaustion_predicted += 1
        return self._forecast(
            seconds, total_rate, available, fastest, f"{total_rate / 1e6:.1f} MB/s across {len(rates)} part(s)"
        )

    def _forecast(self, seconds, rate, available, fastest, reason) -> MemoryForecast:
        return MemoryForecast(
            seconds_to_exhaustion=seconds,
            growth_bytes_per_second=rate,
            available_bytes=available,
            fastest_growing_part=fastest[0],
            fastest_growth_bytes_per_second=fastest[1],
            samples=sum(len(s) for s in self._history.values()),
            reason=reason,
            observed_at_ns=self._now_ns(),
        )

    def _growth_rate(self, samples) -> float | None:
        if len(samples) < MINIMUM_SAMPLES:
            return None
        times = [t for t, _ in samples]
        sizes = [float(s) for _, s in samples]
        mean_time = sum(times) / len(times)
        mean_size = sum(sizes) / len(sizes)
        variance = sum((t - mean_time) ** 2 for t in times)
        if variance == 0:
            return None
        covariance = sum((t - mean_time) * (s - mean_size) for t, s in zip(times, sizes))
        return covariance / variance


def describe_forecast(forecaster: MemoryPressureForecaster) -> dict:
    return {
        "part_id": PART_ID,
        "forecasts": forecaster.standing.forecasts,
        "exhaustion_predicted": forecaster.standing.exhaustion_predicted,
        "samples_by_part": dict(forecaster.standing.samples_by_part),
    }


def run_memory_pressure_forecaster(
    forecaster: MemoryPressureForecaster, control_socket, read_usage_and_capacity, publish_forecast,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        usages, capacity = read_usage_and_capacity()
        forecaster.observe(usages)
        publish_forecast(forecaster.forecast(capacity))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
