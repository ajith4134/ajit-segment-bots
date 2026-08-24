"""io-pressure-meter: disk and network saturation, apart from CPU and RAM.

A part can be starved by a disk that is busy while every core is idle, and a
governor watching only CPU and memory would see a healthy machine and a part
that will not finish.
"""

from __future__ import annotations

import pathlib
import time
from dataclasses import dataclass

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "io-pressure-meter"

PART_DECLARATION = PartDeclaration(
    part_id="io-pressure-meter",
    consumes=(),
    produces=("io-pressure", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

PRESSURE_PATH = pathlib.Path("/proc/pressure/io")
NET_DEV_PATH = pathlib.Path("/proc/net/dev")


@dataclass(frozen=True)
class IoPressure:
    """Stall fractions and byte rates, or None where the kernel publishes neither."""

    some_stalled_10s: float | None
    full_stalled_10s: float | None
    network_receive_bytes_per_second: float | None
    network_transmit_bytes_per_second: float | None
    measured_at_ns: int
    unmeasurable_reason: str | None = None

    @property
    def is_measured(self) -> bool:
        return self.unmeasurable_reason is None


@dataclass
class _NetSample:
    received: int
    transmitted: int
    at_monotonic: float


class IoPressureMeter:
    """Reads PSI for stalls and /proc/net/dev for throughput.

    PSI is the honest measure of io starvation: it says what fraction of the last
    ten seconds tasks spent waiting on io, which utilisation cannot. Where the
    kernel does not publish it the reading is None and the tile stays unmeasured
    rather than reading as an idle disk.
    """

    def __init__(self, pressure_path=PRESSURE_PATH, net_dev_path=NET_DEV_PATH, monotonic=time.monotonic, now_ns=time.time_ns) -> None:
        self._pressure_path = pathlib.Path(pressure_path)
        self._net_dev_path = pathlib.Path(net_dev_path)
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._previous: _NetSample | None = None
        self.readings = 0

    def measure(self) -> IoPressure:
        self.readings += 1
        some, full, reason = self._read_pressure()
        received, transmitted = self._read_network_rates()
        return IoPressure(
            some_stalled_10s=some,
            full_stalled_10s=full,
            network_receive_bytes_per_second=received,
            network_transmit_bytes_per_second=transmitted,
            measured_at_ns=self._now_ns(),
            unmeasurable_reason=reason,
        )

    def _read_pressure(self) -> tuple[float | None, float | None, str | None]:
        try:
            text = self._pressure_path.read_text()
        except OSError as failure:
            return None, None, f"{self._pressure_path}: {type(failure).__name__}: {failure}"
        values = {}
        for line in text.splitlines():
            kind, _, rest = line.partition(" ")
            for field in rest.split():
                name, _, value = field.partition("=")
                if name == "avg10":
                    values[kind] = float(value) / 100.0
        return values.get("some"), values.get("full"), None

    def _read_network_rates(self) -> tuple[float | None, float | None]:
        try:
            lines = self._net_dev_path.read_text().splitlines()[2:]
        except OSError:
            return None, None
        received = transmitted = 0
        for line in lines:
            interface, _, counters = line.partition(":")
            if interface.strip() == "lo":
                continue
            fields = counters.split()
            if len(fields) < 9:
                continue
            received += int(fields[0])
            transmitted += int(fields[8])

        now = self._monotonic()
        previous = self._previous
        self._previous = _NetSample(received, transmitted, now)
        if previous is None or now <= previous.at_monotonic:
            return None, None
        elapsed = now - previous.at_monotonic
        return (
            (received - previous.received) / elapsed,
            (transmitted - previous.transmitted) / elapsed,
        )


def describe_io_pressure(meter: IoPressureMeter, pressure: IoPressure | None) -> dict:
    return {
        "part_id": PART_ID,
        "readings": meter.readings,
        "pressure": pressure.__dict__ if pressure else None,
    }


def run_io_pressure_meter(
    meter: IoPressureMeter, control_socket, publish_pressure,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    last_pressure: list = [None]

    def measure_and_publish() -> None:
        last_pressure[0] = meter.measure()
        publish_pressure(last_pressure[0])

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=measure_and_publish,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_io_pressure(meter, last_pressure[0]),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    No inputs: it reads the kernel's own pressure file and the network counters
    on every tick, which with nothing to wake it is once per health interval.
    """
    publish_pressure = context.bus.publisher_for("io-pressure")
    meter = IoPressureMeter()

    return run_io_pressure_meter(
        meter=meter,
        control_socket=context.control_socket,
        publish_pressure=lambda pressure: publish_pressure((pressure,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
