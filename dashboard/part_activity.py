#!/usr/bin/env python3
"""What each part is doing right now, read from the heartbeat table it writes.

The part monitor answers *how far built* a part is. This answers a different
question -- *what it is doing* -- and it is a different fact with a different
proof. A part can be RUNNING and doing nothing; a counter that has not moved
between two observed tables says so, and this module is what lets a board say it.

Every number here comes from `heartbeat-collector`'s table: the same file the
part monitor reads for its RUNNING rung and the trade board reads for *Parts
alive*. No second count is kept for a board's benefit, because a second count is
free to disagree with the one the bot acts on.

A rate is a *measured* delta between two tables this process actually observed.
Until a second table has been read there is no rate, and the reading is
`NOT MEASURED` rather than zero -- Rule 8: absence of evidence renders as its own
state, and a rate of zero is a different fact from a rate never taken.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

NOT_MEASURED = "NOT MEASURED"

# A part is doing something when a counter it publishes moved between two tables.
# Which counters exist is the part's own business (T-4): this module names none of
# them, so a part that adds a counter appears here with no dashboard work at all.


@dataclass(frozen=True)
class CounterRate:
    """One standing counter, its value, and how fast it is moving."""

    name: str
    value: float
    per_second: float | None  # None until two tables have been observed
    delta: float | None
    over_seconds: float | None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "value": self.value,
            "per_second": self.per_second,
            "delta": self.delta,
            "over_seconds": self.over_seconds,
            "is_measured": self.per_second is not None,
        }


@dataclass(frozen=True)
class PartActivity:
    """One part's live behaviour as its own heartbeat reports it."""

    part_id: str
    state: str
    age_seconds: float | None
    staleness_seconds: float | None
    rate_ratio: float | None
    input_loss: list
    refused_control_frame: object
    reason: str
    counters: list[CounterRate]
    is_working: bool | None  # None until a rate has been measured

    def as_dict(self) -> dict:
        return {
            "part_id": self.part_id,
            "state": self.state,
            "age_seconds": self.age_seconds,
            "staleness_seconds": self.staleness_seconds,
            "rate_ratio": self.rate_ratio,
            "input_loss": self.input_loss,
            "refused_control_frame": self.refused_control_frame,
            "reason": self.reason,
            "counters": [c.as_dict() for c in self.counters],
            "is_working": self.is_working,
        }


@dataclass
class ActivityReader:
    """Reads the heartbeat table and remembers the last one, so a rate can exist.

    Held as an object rather than a function because a rate needs two observations
    and the second one has to be compared against something. The memory is this
    process's own, and it is stated in the payload how far apart the two tables
    were -- a rate over an unknown interval is not a rate.
    """

    _previous_counters: dict[str, dict[str, float]] = field(default_factory=dict)
    _previous_collected_at_ns: int | None = None

    def read_activity(self, now_ns: int | None = None) -> tuple[list[PartActivity], dict]:
        """Every part the collector heard from, with what its counters are doing."""
        from build_part_monitor import read_heartbeat_table_path
        from parts.observability.heartbeat_collector import read_heartbeat_table_file

        try:
            table_path = read_heartbeat_table_path()
        except Exception as refusal:
            return [], {
                "ok": False,
                "proof": f"no heartbeat table: settings refused ({refusal})",
                "table_path": None,
                "table_age_seconds": None,
                "has_a_rate": False,
            }

        document = read_heartbeat_table_file(table_path)
        if document is None:
            return [], {
                "ok": False,
                "proof": (
                    f"no heartbeat table at {table_path}: "
                    "heartbeat-collector has not written one"
                ),
                "table_path": str(table_path),
                "table_age_seconds": None,
                "has_a_rate": False,
            }

        if now_ns is None:
            now_ns = time.time_ns()
        collected_at_ns = int(document.get("collected_at_ns", 0))
        table_age = (now_ns - collected_at_ns) / 1e9
        silent_after = read_silence_threshold_seconds()
        if silent_after is not None and table_age >= silent_after:
            # The collector itself has stopped. Its last word is not current, and
            # showing it as current is exactly the failure Rule 8 exists to stop.
            return [], {
                "ok": False,
                "proof": (
                    f"heartbeat table at {table_path} is {table_age:.0f}s old, past the "
                    f"{silent_after:.0f}s silence threshold: the collector has stopped"
                ),
                "table_path": str(table_path),
                "table_age_seconds": table_age,
                "has_a_rate": False,
            }

        elapsed = None
        if self._previous_collected_at_ns is not None:
            elapsed = (collected_at_ns - self._previous_collected_at_ns) / 1e9

        activities: list[PartActivity] = []
        current_counters: dict[str, dict[str, float]] = {}
        for beat in document.get("heartbeats", ()):
            part_id = beat.get("part_id", "")
            standing = beat.get("standing") or {}
            numeric = {k: float(v) for k, v in standing.items() if isinstance(v, (int, float))}
            current_counters[part_id] = numeric
            previous = self._previous_counters.get(part_id)
            counters = build_counter_rates(numeric, previous, elapsed)
            activities.append(
                PartActivity(
                    part_id=part_id,
                    state=beat.get("state", NOT_MEASURED),
                    age_seconds=beat.get("age_seconds"),
                    staleness_seconds=beat.get("staleness_seconds"),
                    rate_ratio=beat.get("rate_ratio"),
                    input_loss=list(beat.get("input_loss") or []),
                    refused_control_frame=beat.get("refused_control_frame"),
                    reason=beat.get("reason", ""),
                    counters=counters,
                    is_working=judge_is_working(counters),
                )
            )

        self._previous_counters = current_counters
        self._previous_collected_at_ns = collected_at_ns

        return activities, {
            "ok": True,
            "proof": (
                f"heartbeat table at {table_path}, written {table_age:.1f}s ago, "
                f"{len(activities)} part(s) reporting"
            ),
            "table_path": str(table_path),
            "table_age_seconds": table_age,
            "has_a_rate": elapsed is not None and elapsed > 0,
            "rate_over_seconds": elapsed,
            "late": document.get("late"),
            "never_reported": document.get("never_reported"),
        }


def build_counter_rates(
    current: dict[str, float],
    previous: dict[str, float] | None,
    elapsed_seconds: float | None,
) -> list[CounterRate]:
    """Pair each counter with its movement, or with nothing when none was measured."""
    rates: list[CounterRate] = []
    can_rate = previous is not None and elapsed_seconds is not None and elapsed_seconds > 0
    for name in sorted(current):
        value = current[name]
        if can_rate and name in previous:
            delta = value - previous[name]
            rates.append(
                CounterRate(
                    name=name,
                    value=value,
                    per_second=delta / elapsed_seconds,
                    delta=delta,
                    over_seconds=elapsed_seconds,
                )
            )
        else:
            rates.append(
                CounterRate(name=name, value=value, per_second=None, delta=None, over_seconds=None)
            )
    return rates


def judge_is_working(counters: list[CounterRate]) -> bool | None:
    """True when some counter actually moved, None when no rate was measured.

    Deliberately three-valued. A part whose counters all held still is *idle*, and
    that is a finding; a part whose rates have never been taken is *unknown*, and
    calling that idle would be asserting a measurement nobody made.
    """
    measured = [c for c in counters if c.per_second is not None]
    if not measured:
        return None
    return any(c.delta for c in measured)


def read_silence_threshold_seconds() -> float | None:
    """How long a table may go unwritten before it proves nothing (an operator setting)."""
    try:
        from runtime.settings_reader import load_settings_document, settings_directory

        document = load_settings_document(settings_directory() / "runtime.toml", "runtime")
        return float(document.read_value("heartbeat_silent_after_seconds"))
    except Exception:
        return None


def summarise_block_activity(activities: list[PartActivity]) -> dict:
    """What a whole block is doing, from the parts of it that are reporting."""
    if not activities:
        return {
            "reporting": 0,
            "working": 0,
            "idle": 0,
            "unknown": 0,
            "busiest": None,
            "state": NOT_MEASURED,
        }
    working = [a for a in activities if a.is_working is True]
    idle = [a for a in activities if a.is_working is False]
    unknown = [a for a in activities if a.is_working is None]

    busiest = None
    best_rate = 0.0
    for activity in activities:
        for counter in activity.counters:
            if counter.per_second and counter.per_second > best_rate:
                best_rate = counter.per_second
                busiest = {
                    "part_id": activity.part_id,
                    "counter": counter.name,
                    "per_second": counter.per_second,
                }

    if unknown and not working and not idle:
        state = NOT_MEASURED
    elif working:
        state = "WORKING"
    elif idle:
        state = "IDLE"
    else:
        state = NOT_MEASURED

    return {
        "reporting": len(activities),
        "working": len(working),
        "idle": len(idle),
        "unknown": len(unknown),
        "busiest": busiest,
        "state": state,
    }


if __name__ == "__main__":
    reader = ActivityReader()
    reader.read_activity()
    time.sleep(2)
    activities, provenance = reader.read_activity()
    print(provenance["proof"])
    print(f"rate measured over {provenance.get('rate_over_seconds')}s")
    for activity in sorted(activities, key=lambda a: a.part_id):
        moving = [c for c in activity.counters if c.per_second]
        mark = {True: "WORKING", False: "IDLE", None: NOT_MEASURED}[activity.is_working]
        top = max(moving, key=lambda c: abs(c.per_second), default=None)
        detail = f"{top.name} {top.per_second:+.1f}/s" if top else "no counter moved"
        print(f"  {activity.part_id:<38} {mark:<12} {detail}")
