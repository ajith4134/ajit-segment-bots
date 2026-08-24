"""probe-runner: run each measurement, keep its output with the command that ran it.

RL-012 and Rule 8: a tile traces to a probe that ran. This is the part that runs
them, and its whole discipline is that a result is worthless without the command
that produced it -- an operator who cannot re-run a measurement cannot check it,
and a number nobody can check is decoration.

A probe that fails is a result, not a gap. A probe that hangs is worse than one
that fails, because it stops every probe behind it, so each is bounded by a
deadline and a timeout is itself a recorded outcome.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "probe-runner"

PART_DECLARATION = PartDeclaration(
    part_id="probe-runner",
    consumes=("heartbeat-table", "journal-gap"),
    produces=("probe-result", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

MEASURED = "measured"
FAILED = "failed"
TIMED_OUT = "timed-out"
NOT_RUN = "not-run"


@dataclass(frozen=True)
class ProbeResult:
    """One measurement, and exactly how to reproduce it."""

    name: str
    outcome: str
    value: str | None
    command: str
    duration_seconds: float
    failure: str | None
    measured_at_ns: int

    @property
    def is_measured(self) -> bool:
        return self.outcome == MEASURED


@dataclass
class RunnerStanding:
    runs: int = 0
    measured: int = 0
    failed: int = 0
    timed_out: int = 0
    probes_registered: int = 0
    slowest_seconds: float = 0.0
    last_failure: str | None = None


class ProbeRunner:
    """Runs registered probes, bounding each one and keeping its command with its output."""

    def __init__(self, timeout_seconds: float, monotonic=time.monotonic, now_ns=time.time_ns) -> None:
        if timeout_seconds <= 0:
            raise ValueError("a probe with no deadline can stop every probe behind it")
        self._timeout = timeout_seconds
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._probes: dict[str, tuple[object, str]] = {}
        self.standing = RunnerStanding()

    def register(self, name: str, probe, command: str) -> None:
        """A probe and the command a person would type to reproduce it.

        The command is required rather than optional: a result nobody can re-run
        is one they must take on trust, and Rule 8 exists because trust is
        exactly what a status display should not require.
        """
        if not command.strip():
            raise ValueError(f"probe {name!r} was registered with no command to reproduce it")
        self._probes[name] = (probe, command)
        self.standing.probes_registered = len(self._probes)

    def run(self, name: str) -> ProbeResult:
        held = self._probes.get(name)
        if held is None:
            return ProbeResult(
                name=name, outcome=NOT_RUN, value=None, command="",
                duration_seconds=0.0, failure=f"no probe named {name!r} is registered",
                measured_at_ns=self._now_ns(),
            )

        probe, command = held
        started = self._monotonic()
        self.standing.runs += 1
        try:
            value = probe()
        except TimeoutError as timeout:
            duration = self._monotonic() - started
            self.standing.timed_out += 1
            self.standing.last_failure = f"{name}: {timeout}"
            return ProbeResult(
                name=name, outcome=TIMED_OUT, value=None, command=command,
                duration_seconds=duration, failure=str(timeout), measured_at_ns=self._now_ns(),
            )
        except Exception as failure:
            duration = self._monotonic() - started
            self.standing.failed += 1
            self.standing.last_failure = f"{name}: {type(failure).__name__}: {failure}"
            return ProbeResult(
                name=name, outcome=FAILED, value=None, command=command,
                duration_seconds=duration,
                failure=f"{type(failure).__name__}: {failure}", measured_at_ns=self._now_ns(),
            )

        duration = self._monotonic() - started
        self.standing.slowest_seconds = max(self.standing.slowest_seconds, duration)

        if duration > self._timeout:
            # Recorded rather than discarded: the value arrived, but a probe this
            # slow will eventually block the ones behind it and that is worth
            # seeing before it does.
            self.standing.timed_out += 1
            return ProbeResult(
                name=name, outcome=TIMED_OUT, value=str(value), command=command,
                duration_seconds=duration,
                failure=f"took {duration:.2f}s, past the {self._timeout:.2f}s deadline",
                measured_at_ns=self._now_ns(),
            )

        self.standing.measured += 1
        return ProbeResult(
            name=name, outcome=MEASURED, value=str(value), command=command,
            duration_seconds=duration, failure=None, measured_at_ns=self._now_ns(),
        )

    def run_all(self) -> tuple[ProbeResult, ...]:
        return tuple(self.run(name) for name in sorted(self._probes))


def describe_probes(runner: ProbeRunner) -> dict:
    return {
        "part_id": PART_ID,
        "probes_registered": runner.standing.probes_registered,
        "runs": runner.standing.runs,
        "measured": runner.standing.measured,
        "failed": runner.standing.failed,
        "timed_out": runner.standing.timed_out,
        "slowest_seconds": runner.standing.slowest_seconds,
        "last_failure": runner.standing.last_failure,
    }


def run_probe_runner(
    runner: ProbeRunner, control_socket, publish_results,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=lambda: publish_results(runner.run_all()),
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_probes(runner),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The probes are the same ones the status board runs -- the capture,
    substrate and trading probes in runtime/probes -- each registered with
    the command a person types to re-run it, plus two read off the bus: how
    many parts the heartbeat table says are reporting, and the journal gaps
    seen. Run once per health interval; the bus inputs wake the part between.
    """
    import time as _time

    from runtime.input_assembly import Batch, LatestValue
    from runtime.probes import capture_probes, substrate_probes, trading_probes

    tables = LatestValue(read=context.bus.reader("heartbeat-table"))
    gaps = Batch(read=context.bus.reader("journal-gap"))
    publish_results = context.bus.publisher_for("probe-result")
    runner = ProbeRunner(timeout_seconds=context.number("probe_timeout"))
    gaps_seen = [0]

    for module, prefix in ((capture_probes, "capture"), (substrate_probes, "substrate"), (trading_probes, "trading")):
        for name in dir(module):
            if name.startswith("probe_"):
                probe = getattr(module, name)
                runner.register(
                    f"{prefix}:{name[6:]}", probe,
                    command=f".venv/bin/python -c 'from runtime.probes.{module.__name__.rsplit('.', 1)[-1]} import {name}; print({name}())'",
                )

    def parts_reporting():
        table = tables.value()
        return None if table is None else f"{table.reporting} of {len(table.heartbeats)} reporting"

    def journal_gaps():
        return f"{gaps_seen[0]} gap(s) seen since start"

    runner.register("bus:parts-reporting", parts_reporting, command="read the latest heartbeat-table on the bus")
    runner.register("bus:journal-gaps", journal_gaps, command="count journal-gap messages on the bus")
    last_run = [float("-inf")]

    def tick() -> None:
        gaps_seen[0] += len(gaps.payloads())
        tables.value()
        now = _time.monotonic()
        if now - last_run[0] < context.health_interval_seconds:
            return
        results = runner.run_all()
        if results:
            publish_results(tuple(results))
        last_run[0] = now

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
    )
