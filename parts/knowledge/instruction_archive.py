"""instruction-archive: every instruction this system has ever written.

Without it the system reproposes what it already tried, counts each as new, and
never notices that it has been round this loop before. The archive is what makes
"we have tried this" answerable, and that answer is worth more than most of the
knowledge it sits beside.

- **Nothing is ever removed.** A retired instruction is marked retired; a refuted
  one is marked refuted. Removing either would make the same idea look novel the
  next time it is proposed, and the trial count would restart at one.
- **The whole life is recorded**, not just the ending: written, traded, scored,
  retired, mutated, resurrected. An archive holding only the final state cannot
  say how long something worked before it stopped, which is the number that says
  whether the idea was ever real.
- **Trial counts are kept per family and never reset.** A family that has been
  mined a hundred times keeps that count even when every one of the hundred has
  been retired -- the next member still needs the corrected bar.
- **The archive is what the deduplicator remembers with.** It is the long memory,
  and everything else in the block is allowed to forget.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "instruction-archive"

PART_DECLARATION = PartDeclaration(
    part_id="instruction-archive",
    consumes=("opportunity-instruction", "retired-instruction"),
    produces=("instruction-history", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

WRITTEN = "written"
TRADED = "traded"
SCORED = "scored"
RETIRED = "retired"
MUTATED = "mutated"
RESURRECTED = "resurrected"


@dataclass(frozen=True)
class ArchiveEvent:
    """One thing that happened to an instruction, appended and never edited."""

    instruction_id: str
    event: str
    detail: dict
    at_ns: int


@dataclass(frozen=True)
class InstructionHistory:
    """An instruction's whole life, not just how it ended."""

    instruction_id: str
    family: str
    measurement: str
    comparison: str
    threshold: float
    regime_tag: str | None
    events: tuple
    written_at_ns: int
    retired_at_ns: int | None
    trades: int
    realised: float
    trials_in_family_when_written: int
    reason: str

    @property
    def is_retired(self) -> bool:
        return self.retired_at_ns is not None

    @property
    def seconds_alive(self) -> float | None:
        """How long it worked before it stopped -- the number that says if it was real."""
        if self.retired_at_ns is None:
            return None
        return (self.retired_at_ns - self.written_at_ns) / 1e9

    def events_of(self, kind: str) -> tuple:
        return tuple(event for event in self.events if event.event == kind)


@dataclass
class ArchiveStanding:
    instructions_archived: int = 0
    events_recorded: int = 0
    retired: int = 0
    resurrected: int = 0
    mutated: int = 0
    families: int = 0
    longest_life_seconds: float | None = None
    by_event: dict = field(default_factory=dict)


class InstructionArchive:
    """The long memory. Nothing is ever removed from it."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._instructions: dict[str, dict] = {}
        self._events: dict[str, list] = {}
        self._family_trials: dict[str, int] = {}
        self.standing = ArchiveStanding()

    def archive(self, instruction) -> InstructionHistory:
        """One newly written instruction, and its family's running trial count."""
        instruction_id = instruction.instruction_id
        family = getattr(instruction, "family", None) or instruction.hypothesis_id.split(":")[0]

        # Never reset: a family mined a hundred times keeps that count even when
        # every one of the hundred has been retired, because the next member
        # still needs the corrected bar.
        self._family_trials[family] = max(
            self._family_trials.get(family, 0), instruction.trials_in_family
        )

        self._instructions[instruction_id] = {
            "family": family,
            "measurement": instruction.measurement,
            "comparison": instruction.comparison,
            "threshold": instruction.threshold,
            "regime_tag": instruction.regime_tag,
            "written_at_ns": instruction.written_at_ns,
            "retired_at_ns": None,
            "trades": 0,
            "realised": 0.0,
            "trials_in_family_when_written": instruction.trials_in_family,
        }
        self.standing.instructions_archived += 1
        self.standing.families = len(self._family_trials)
        self._record(instruction_id, WRITTEN, {"reason": instruction.reason})
        return self.history_of(instruction_id)

    def record_trade(self, instruction_id: str, realised: float) -> None:
        record = self._instructions.get(instruction_id)
        if record is None:
            return
        record["trades"] += 1
        record["realised"] += realised
        self._record(instruction_id, TRADED, {"realised": realised})

    def record_score(self, instruction_id: str, hit_rate: float, expectancy: float | None) -> None:
        self._record(instruction_id, SCORED, {"hit_rate": hit_rate, "expectancy": expectancy})

    def record_retirement(self, instruction_id: str, because: str) -> None:
        """Marked retired, never removed: removing makes the idea look novel next month."""
        record = self._instructions.get(instruction_id)
        if record is None:
            return
        record["retired_at_ns"] = self._now_ns()
        self.standing.retired += 1
        life = (record["retired_at_ns"] - record["written_at_ns"]) / 1e9
        if (
            self.standing.longest_life_seconds is None
            or life > self.standing.longest_life_seconds
        ):
            self.standing.longest_life_seconds = life
        self._record(instruction_id, RETIRED, {"because": because, "seconds_alive": life})

    def record_mutation(self, instruction_id: str, mutation: str, child_id: str) -> None:
        self.standing.mutated += 1
        self._record(instruction_id, MUTATED, {"mutation": mutation, "child": child_id})

    def record_resurrection(self, instruction_id: str) -> None:
        record = self._instructions.get(instruction_id)
        if record is None:
            return
        record["retired_at_ns"] = None
        self.standing.resurrected += 1
        self._record(instruction_id, RESURRECTED, {})

    def has_been_tried(self, measurement: str, comparison: str, threshold: float, tolerance: float) -> tuple:
        """Whether this system has been round this loop before, and which instruction it was."""
        for instruction_id, record in sorted(self._instructions.items()):
            if record["measurement"] != measurement or record["comparison"] != comparison:
                continue
            if abs(record["threshold"] - threshold) <= tolerance:
                return True, instruction_id
        return False, None

    def trials_in_family(self, family: str) -> int:
        return self._family_trials.get(family, 0)

    def history_of(self, instruction_id: str) -> InstructionHistory | None:
        record = self._instructions.get(instruction_id)
        if record is None:
            return None
        events = tuple(self._events.get(instruction_id, ()))
        life = (
            None
            if record["retired_at_ns"] is None
            else (record["retired_at_ns"] - record["written_at_ns"]) / 1e9
        )
        return InstructionHistory(
            instruction_id=instruction_id,
            family=record["family"],
            measurement=record["measurement"],
            comparison=record["comparison"],
            threshold=record["threshold"],
            regime_tag=record["regime_tag"],
            events=events,
            written_at_ns=record["written_at_ns"],
            retired_at_ns=record["retired_at_ns"],
            trades=record["trades"],
            realised=record["realised"],
            trials_in_family_when_written=record["trials_in_family_when_written"],
            reason=(
                f"{len(events)} event(s) over its life"
                + (
                    f", which lasted {life / 86400:.1f} day(s) -- how long it worked before it "
                    f"stopped is what says whether the idea was ever real"
                    if life is not None
                    else ", still live"
                )
                + f". Trial {record['trials_in_family_when_written']} in {record['family']}, "
                f"a family now at {self.trials_in_family(record['family'])}"
            ),
        )

    def all_histories(self) -> tuple:
        return tuple(
            history
            for history in (
                self.history_of(instruction_id) for instruction_id in sorted(self._instructions)
            )
            if history is not None
        )

    def _record(self, instruction_id: str, event: str, detail: dict) -> None:
        self._events.setdefault(instruction_id, []).append(
            ArchiveEvent(
                instruction_id=instruction_id, event=event, detail=dict(detail),
                at_ns=self._now_ns(),
            )
        )
        self.standing.events_recorded += 1
        self.standing.by_event[event] = self.standing.by_event.get(event, 0) + 1


def describe_archive(archive: InstructionArchive) -> dict:
    return {
        "part_id": PART_ID,
        "instructions_archived": archive.standing.instructions_archived,
        "events_recorded": archive.standing.events_recorded,
        "retired": archive.standing.retired,
        "resurrected": archive.standing.resurrected,
        "mutated": archive.standing.mutated,
        "families": archive.standing.families,
        "longest_life_seconds": archive.standing.longest_life_seconds,
        "by_event": dict(sorted(archive.standing.by_event.items())),
        "trials_by_family": dict(sorted(archive._family_trials.items())),
        "anything_is_ever_removed": False,
    }


def run_instruction_archive(
    archive: InstructionArchive, control_socket, read_instructions, publish_history,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_instructions(archive)
        publish_history(archive.all_histories())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Every written instruction is archived; a retirement or a resurrection
    is recorded against it. Histories go out once per health interval.
    """
    import time as _time

    from runtime.input_assembly import Batch

    instructions = Batch(read=context.bus.reader("opportunity-instruction"))
    retirements = Batch(read=context.bus.reader("retired-instruction"))
    publish_history = context.bus.publisher_for("instruction-history")
    archive = InstructionArchive()
    last_publish = [float("-inf")]

    def read_instructions(_archive) -> None:
        for instruction in instructions.payloads():
            if archive.history_of(instruction.instruction_id) is None:
                archive.archive(instruction)
        for record in retirements.payloads():
            if archive.history_of(record.instruction_id) is None:
                continue
            if record.state == "retired" and record.because:
                archive.record_retirement(record.instruction_id, str(record.because))
            elif record.state == "resurrected":
                archive.record_resurrection(record.instruction_id)

    def publish(items) -> None:
        now = _time.monotonic()
        if now - last_publish[0] < context.health_interval_seconds:
            return
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_history(kept)
            last_publish[0] = now

    return run_instruction_archive(
        archive=archive,
        control_socket=context.control_socket,
        read_instructions=read_instructions,
        publish_history=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
