"""part-priority-reader: the user's order of which parts matter most under contention."""

from __future__ import annotations

import time
import tomllib
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.settings_reader import settings_directory

PART_ID = "part-priority-reader"

PART_DECLARATION = PartDeclaration(
    part_id="part-priority-reader",
    consumes=(),
    produces=("part-priority", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

PRIORITY_FILENAME = "part-priority.toml"

# Where a part with no stated priority sits. Deliberately not the top and not the
# bottom: an unlisted part must not outrank one the operator ranked, and must not
# be starved for never having been mentioned.
UNSTATED_PRIORITY = 50


@dataclass(frozen=True)
class PartPriority:
    part_id: str
    priority: int
    is_stated: bool
    reason: str


@dataclass
class PriorityStanding:
    reads: int = 0
    stated: int = 0
    unstated: int = 0
    last_failure: str | None = None
    source_path: str | None = None


class PartPriorityReader:
    """Reads the operator's ranking; anything unlisted takes the middle.

    Lower numbers win. A parse failure keeps the last ranking that worked, for
    the same reason settings do: an operator mid-edit must not be able to change
    which parts survive contention by saving a broken file.
    """

    def __init__(self, priority_path=None, now_ns=time.time_ns) -> None:
        self._path = priority_path or (settings_directory() / PRIORITY_FILENAME)
        self._now_ns = now_ns
        self._priorities: dict[str, int] = {}
        self.standing = PriorityStanding(source_path=str(self._path))

    def read(self) -> dict[str, int]:
        self.standing.reads += 1
        try:
            parsed = tomllib.loads(self._path.read_text())
        except (OSError, tomllib.TOMLDecodeError) as failure:
            self.standing.last_failure = f"{type(failure).__name__}: {failure}"
            return dict(self._priorities)
        stated = {
            str(part_id): int(value)
            for part_id, value in parsed.get("priority", {}).items()
            if isinstance(value, int)
        }
        self._priorities = stated
        self.standing.stated = len(stated)
        self.standing.last_failure = None
        return dict(stated)

    def priority_of(self, part_id: str) -> PartPriority:
        if part_id in self._priorities:
            return PartPriority(part_id, self._priorities[part_id], True, "stated by the operator")
        self.standing.unstated += 1
        return PartPriority(
            part_id, UNSTATED_PRIORITY, False, "not listed; takes the middle rank"
        )

    def rank(self, part_ids) -> tuple[PartPriority, ...]:
        priorities = [self.priority_of(part_id) for part_id in part_ids]
        return tuple(sorted(priorities, key=lambda p: (p.priority, p.part_id)))


def describe_priorities(reader: PartPriorityReader) -> dict:
    return {
        "part_id": PART_ID,
        "reads": reader.standing.reads,
        "stated": reader.standing.stated,
        "unstated_lookups": reader.standing.unstated,
        "source_path": reader.standing.source_path,
        "last_failure": reader.standing.last_failure,
    }


def run_part_priority_reader(
    reader: PartPriorityReader, control_socket, publish_priorities,
    health_interval_seconds: float, emit_health,
) -> int:
    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=lambda: publish_priorities(reader.read()),
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The reader hands back a mapping of part id to rank; what goes on the bus is one
    typed message per part, carrying whether the operator stated that rank or the
    part took the middle. A bare integer would have lost the difference between a
    rank someone chose and a rank nobody did -- and the planner weighs both, so the
    consumer has to be able to tell them apart.
    """
    publish_priorities = context.bus.publisher_for("part-priority")
    reader = PartPriorityReader()

    def publish_ranking(ranking: dict) -> None:
        publish_priorities(reader.rank(ranking.keys()))

    return run_part_priority_reader(
        reader=reader,
        control_socket=context.control_socket,
        publish_priorities=publish_ranking,
        health_interval_seconds=context.health_interval_seconds,
        emit_health=context.emit_health,
    )
