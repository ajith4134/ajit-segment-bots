"""heartbeat-collector: every part's latest health, with its age.

The age is the whole point. A health report says "I was fine"; only its age says
whether that is still true. A collector that stored the latest report per part
and showed it without an age would present a part that died an hour ago exactly
as it presents one that reported a second ago -- and the dead one is the reason
anybody is looking.

So a part that has stopped reporting is a **state**, not an absence. Rule 8: it
renders as SILENT, visibly distinct, never as its last good news.

It also knows which parts are *expected*. Built from what should be running
rather than from what has reported, because a report-driven table can never
contain the part that never started -- which is exactly the failure worth seeing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "heartbeat-collector"

PART_DECLARATION = PartDeclaration(
    part_id="heartbeat-collector",
    consumes=("part-health",),
    produces=("heartbeat-table", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

REPORTING = "reporting"
LATE = "late"
SILENT = "silent"
NEVER_REPORTED = "never-reported"


@dataclass(frozen=True)
class Heartbeat:
    """One part's latest word about itself, and how long ago it said it."""

    part_id: str
    state: str
    age_seconds: float | None
    reported_state: str | None
    rate_ratio: float | None
    staleness_seconds: float | None
    refused_control_frame: str | None
    reason: str
    # What the part's inputs lost since it started, per data type. Carried here
    # because this table is the first place anything reads it: PartHealth has
    # carried input_loss since the substrate was built and, measured on
    # 2026-08-23, nothing consumed it while a symbol's price sat frozen for 56
    # minutes in a part whose health read fine.
    input_loss: tuple[tuple[str, int], ...] = ()

    @property
    def is_healthy(self) -> bool:
        return self.state == REPORTING


@dataclass(frozen=True)
class HeartbeatTable:
    """Every expected part, whether or not it has ever spoken."""

    heartbeats: tuple[Heartbeat, ...]
    reporting: int
    late: int
    silent: int
    never_reported: int
    collected_at_ns: int

    @property
    def all_reporting(self) -> bool:
        return self.reporting == len(self.heartbeats)


@dataclass
class CollectorStanding:
    reports_received: int = 0
    parts_expected: int = 0
    tables_built: int = 0
    parts_gone_silent: int = 0
    longest_silence_seconds: float = 0.0


class HeartbeatCollector:
    """Holds the latest health per part and reports how old each one is."""

    def __init__(
        self,
        late_after_seconds: float,
        silent_after_seconds: float,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        if silent_after_seconds <= late_after_seconds:
            raise ValueError("a part must be late before it is silent")
        self._late_after = late_after_seconds
        self._silent_after = silent_after_seconds
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._expected: set[str] = set()
        self._latest: dict[str, tuple[object, float]] = {}
        self._was_silent: set[str] = set()
        self.standing = CollectorStanding()

    def expect_part(self, part_id: str) -> None:
        """Register a part that should be reporting.

        Built from what should run rather than from what has reported: a table
        assembled only from arriving reports can never contain the part that
        never started.
        """
        self._expected.add(part_id)
        self.standing.parts_expected = len(self._expected)

    def forget_part(self, part_id: str) -> None:
        """A part deliberately switched off is no longer expected to report."""
        self._expected.discard(part_id)
        self._latest.pop(part_id, None)
        self._was_silent.discard(part_id)
        self.standing.parts_expected = len(self._expected)

    def observe_health(self, health) -> None:
        self.standing.reports_received += 1
        self._expected.add(health.part_id)
        self._latest[health.part_id] = (health, self._monotonic())
        self._was_silent.discard(health.part_id)
        self.standing.parts_expected = len(self._expected)

    def read_table(self) -> HeartbeatTable:
        now = self._monotonic()
        heartbeats = []
        counts = {REPORTING: 0, LATE: 0, SILENT: 0, NEVER_REPORTED: 0}

        for part_id in sorted(self._expected):
            held = self._latest.get(part_id)
            if held is None:
                counts[NEVER_REPORTED] += 1
                heartbeats.append(
                    Heartbeat(
                        part_id=part_id, state=NEVER_REPORTED, age_seconds=None,
                        reported_state=None, rate_ratio=None, staleness_seconds=None,
                        refused_control_frame=None, input_loss=(),
                        reason="this part is expected but has never reported; it may never have started",
                    )
                )
                continue

            health, at = held
            age = now - at
            if age >= self._silent_after:
                state = SILENT
                if part_id not in self._was_silent:
                    self.standing.parts_gone_silent += 1
                    self._was_silent.add(part_id)
                self.standing.longest_silence_seconds = max(
                    self.standing.longest_silence_seconds, age
                )
                reason = (
                    f"last reported {age:.1f}s ago, past {self._silent_after:.0f}s; its last word "
                    f"was good news and that is exactly why it must not be shown as current"
                )
            elif age >= self._late_after:
                state = LATE
                reason = f"last reported {age:.1f}s ago, past {self._late_after:.0f}s"
            else:
                state = REPORTING
                reason = f"reported {age:.1f}s ago"

            counts[state] += 1
            heartbeats.append(
                Heartbeat(
                    part_id=part_id,
                    state=state,
                    age_seconds=age,
                    reported_state=getattr(health, "state", None),
                    rate_ratio=getattr(health, "rate_ratio", None),
                    staleness_seconds=getattr(health, "staleness_seconds", None),
                    refused_control_frame=getattr(health, "refused_control_frame", None),
                    input_loss=tuple(
                        (str(kind), int(count))
                        for kind, count in getattr(health, "input_loss", ()) or ()
                    ),
                    reason=reason,
                )
            )

        self.standing.tables_built += 1
        return HeartbeatTable(
            heartbeats=tuple(heartbeats),
            reporting=counts[REPORTING],
            late=counts[LATE],
            silent=counts[SILENT],
            never_reported=counts[NEVER_REPORTED],
            collected_at_ns=self._now_ns(),
        )

    def heartbeat_of(self, part_id: str) -> Heartbeat | None:
        return next(
            (beat for beat in self.read_table().heartbeats if beat.part_id == part_id), None
        )


def describe_heartbeats(collector: HeartbeatCollector) -> dict:
    table = collector.read_table()
    return {
        "part_id": PART_ID,
        "parts_expected": collector.standing.parts_expected,
        "reports_received": collector.standing.reports_received,
        "tables_built": collector.standing.tables_built,
        "reporting": table.reporting,
        "late": table.late,
        "silent": table.silent,
        "never_reported": table.never_reported,
        "parts_gone_silent": collector.standing.parts_gone_silent,
        "longest_silence_seconds": collector.standing.longest_silence_seconds,
    }


def run_heartbeat_collector(
    collector: HeartbeatCollector, control_socket, read_health, publish_table,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for health in read_health():
            collector.observe_health(health)
        publish_table(collector.read_table())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )


# ---- the table as a file, so a board built in another process can read it ------

# Written beside the learned state rather than on the bus alone: every board this
# project renders is built by a process that is not on the bus, and the part
# monitor's RUNNING rung was unreachable for exactly that reason -- the rung was
# declared, nothing wrote the evidence anywhere a board could read it, and every
# live part rendered as if it had never run.
TABLE_SCHEMA_VERSION = 1


def heartbeat_table_as_document(table: HeartbeatTable, standing: CollectorStanding) -> dict:
    return {
        "schema_version": TABLE_SCHEMA_VERSION,
        "part_id": PART_ID,
        "collected_at_ns": table.collected_at_ns,
        "reporting": table.reporting,
        "late": table.late,
        "silent": table.silent,
        "never_reported": table.never_reported,
        "parts_expected": standing.parts_expected,
        "reports_received": standing.reports_received,
        "heartbeats": [
            {
                "part_id": beat.part_id,
                "state": beat.state,
                "age_seconds": beat.age_seconds,
                "reported_state": beat.reported_state,
                "rate_ratio": beat.rate_ratio,
                "staleness_seconds": beat.staleness_seconds,
                "refused_control_frame": beat.refused_control_frame,
                "input_loss": list(beat.input_loss),
                "reason": beat.reason,
            }
            for beat in table.heartbeats
        ],
    }


def write_heartbeat_table(path, table: HeartbeatTable, standing: CollectorStanding) -> None:
    """Replace the table file atomically.

    A reader must never see half a table: a board that read a truncated file would
    either fail to build or, worse, build from the parts that happened to be
    written first. Temp file beside the destination, then `os.replace`. No fsync:
    this is a live observation, and after a reboot a missing or old table is the
    correct reading -- nothing was running.
    """
    import json
    import os
    import pathlib
    import tempfile

    destination = pathlib.Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=destination.parent,
        prefix=f".{destination.name}.", suffix=".partial", delete=False,
    )
    try:
        with handle:
            json.dump(heartbeat_table_as_document(table, standing), handle, sort_keys=True)
        os.replace(handle.name, destination)
    except BaseException:
        pathlib.Path(handle.name).unlink(missing_ok=True)
        raise


def read_heartbeat_table_file(path) -> dict | None:
    """The last table written, or None when there is none or it cannot be read.

    None is a state the caller must render as its own thing (NOT MEASURED), never
    as 'all quiet': a collector that never ran and a system with nothing running
    look identical from here, and only the former is a gap in the evidence.
    """
    import json
    import pathlib

    try:
        document = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("schema_version") != TABLE_SCHEMA_VERSION:
        return None
    return document


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Expected parts are the ones that have reported at least once: a part knows
    nothing about the circuit (T-4), so this one cannot be handed the list of what
    should be running. NEVER_REPORTED is therefore reachable only through
    `expect_part`, which is the governor's to call once it places parts. Until then
    a part that never started is invisible here and visible in the launcher's own
    record -- stated rather than papered over.
    """
    import pathlib

    from runtime.input_assembly import Batch

    health_reports = Batch(read=context.bus.reader("part-health"))
    publish_table = context.bus.publisher_for("heartbeat-table")
    table_path = pathlib.Path(str(context.setting("heartbeat_table_path").value)).expanduser()

    collector = HeartbeatCollector(
        late_after_seconds=context.number("heartbeat_late_after_seconds"),
        silent_after_seconds=context.number("heartbeat_silent_after_seconds"),
    )

    def publish_and_write(table: HeartbeatTable) -> None:
        publish_table((table,))
        write_heartbeat_table(table_path, table, collector.standing)

    def emit_and_observe_own_health(health) -> None:
        # A part never receives its own message (wiring rule 1), so the collector
        # would be the one part missing from its own table -- and the table is
        # where the boards read what is alive. It observes its own report as it
        # sends it.
        context.emit_health(health)
        collector.observe_health(health)

    return run_heartbeat_collector(
        collector=collector,
        control_socket=context.control_socket,
        read_health=health_reports.payloads,
        publish_table=publish_and_write,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=emit_and_observe_own_health,
    )
