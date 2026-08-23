"""human-override-reader: the one input this system cannot produce for itself.

Everything else in the autonomous block is the system reasoning about itself. This
part is the door through which somebody outside can say stop, and its entire design
is about making that door impossible to close from the inside.

- **It consumes nothing.** Its blueprint entry has no inputs, which is not an
  oversight: a part that read the system's own state could be talked out of an
  override by that state. It reads a file the system does not write.
- **No part may clear an override.** Clearing is done from outside, by removing it
  from the source. There is no method here that revokes one, and a part that could
  revoke an override is a part that can be persuaded to.
- **An unreadable source is treated as an active stop, not as no override.** If the
  door cannot be checked, the system does not get to assume nobody is at it. This is
  the one place where failing closed costs opportunity and failing open costs
  everything.
- **An override outranks every other signal.** It is not weighed against survival
  tier, market conditions or anything else, and nothing downstream is permitted to
  average it into a score.

Expiry is honoured because a standing override nobody remembers is how a system stays
halted for a week after the reason passed -- but expiry is stated by the person at the
time, never inferred here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.autonomy_types import HumanOverride
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "human-override-reader"

PART_DECLARATION = PartDeclaration(
    part_id="human-override-reader",
    consumes=(),
    produces=("human-override", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

READ = "read"
NONE_PRESENT = "no-override-is-present"
UNREADABLE = "the-override-source-could-not-be-read"
EXPIRED = "the-override-has-expired"
MALFORMED = "the-override-could-not-be-understood"

# What an override may say. A closed set, because an instruction this part cannot
# parse must not be silently ignored.
STOP_EVERYTHING = "stop-everything"
STOP_TRADING = "stop-trading"
STOP_SELF_MODIFICATION = "stop-self-modification"
CLOSE_POSITIONS = "close-positions"
RESUME = "resume"

INSTRUCTIONS = (
    STOP_EVERYTHING, STOP_TRADING, STOP_SELF_MODIFICATION, CLOSE_POSITIONS, RESUME,
)


@dataclass(frozen=True)
class OverrideReading:
    state: str
    override: HumanOverride | None
    reason: str
    read_at_ns: int

    @property
    def stops_something(self) -> bool:
        return (
            self.override is not None
            and self.override.is_active
            and self.override.instruction != RESUME
        )


@dataclass
class ReaderStanding:
    reads: int = 0
    overrides_seen: int = 0
    unreadable_reads: int = 0
    malformed: int = 0
    expired: int = 0
    revocations_attempted: int = 0
    longest_active_seconds: float = 0.0


class HumanOverrideReader:
    """Reads an instruction from outside and refuses to let anything in here clear it."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._read_source = None
        self._sequence = 0
        self.standing = ReaderStanding()

    def install_source(self, read_source) -> None:
        """`read_source() -> None | {'instruction', 'scope', ...}`, from outside.

        The source is somewhere this system does not write. A part that could write
        its own override could revoke it.
        """
        self._read_source = read_source

    def read(self) -> OverrideReading:
        self.standing.reads += 1
        if self._read_source is None:
            self.standing.unreadable_reads += 1
            return self._reading(
                UNREADABLE, self._stop_everything("no override source is installed"),
                "no override source is installed. That is treated as an active stop: if "
                "the door cannot be checked, the system does not get to assume nobody is "
                "at it",
            )

        try:
            raw = self._read_source()
        except Exception as failure:
            self.standing.unreadable_reads += 1
            return self._reading(
                UNREADABLE,
                self._stop_everything(f"the source could not be read ({type(failure).__name__})"),
                f"the override source could not be read ({type(failure).__name__}). This "
                f"is the one place where failing closed costs opportunity and failing "
                f"open costs everything",
            )

        if raw is None:
            return self._reading(
                NONE_PRESENT, None,
                "no override is present. Nothing is inferred from that beyond its own "
                "absence",
            )

        instruction = raw.get("instruction")
        if instruction not in INSTRUCTIONS:
            self.standing.malformed += 1
            return self._reading(
                MALFORMED,
                self._stop_everything(f"an override said {instruction!r}, which is not understood"),
                f"{instruction!r} is not an instruction this part understands. An "
                f"instruction that cannot be parsed must not be silently ignored, so it "
                f"is treated as a stop",
            )

        self._sequence += 1
        issued_at = int(raw.get("issued_at_ns", self._now_ns()))
        expires_at = raw.get("expires_at_ns")
        now = self._now_ns()
        is_expired = expires_at is not None and now > expires_at

        override = HumanOverride(
            override_id=str(raw.get("override_id", f"override-{self._sequence}")),
            instruction=instruction,
            scope=raw.get("scope", "everything"),
            is_active=not is_expired,
            issued_at_ns=issued_at,
            expires_at_ns=expires_at,
            source_reference=raw.get("source_reference", "outside"),
        )
        self.standing.overrides_seen += 1
        self.standing.longest_active_seconds = max(
            self.standing.longest_active_seconds, (now - issued_at) / 1e9
        )

        if is_expired:
            self.standing.expired += 1
            return self._reading(
                EXPIRED, override,
                f"the override expired {(now - expires_at) / 1e9:.0f}s ago. Expiry was "
                f"stated by the person at the time and is never inferred here",
            )

        return self._reading(
            READ, override,
            f"{instruction} over {override.scope}, issued "
            f"{(now - issued_at) / 1e9:.0f}s ago. It outranks every other signal, and "
            f"nothing in this system can clear it",
        )

    def _stop_everything(self, why: str) -> HumanOverride:
        self._sequence += 1
        return HumanOverride(
            override_id=f"assumed-{self._sequence}",
            instruction=STOP_EVERYTHING,
            scope="everything",
            is_active=True,
            issued_at_ns=self._now_ns(),
            expires_at_ns=None,
            source_reference=f"assumed: {why}",
        )

    def _reading(self, state, override, reason) -> OverrideReading:
        return OverrideReading(
            state=state, override=override, reason=reason, read_at_ns=self._now_ns(),
        )


def describe_override_reading(reader: HumanOverrideReader) -> dict:
    return {
        "part_id": PART_ID,
        "reads": reader.standing.reads,
        "overrides_seen": reader.standing.overrides_seen,
        "unreadable_reads_treated_as_a_stop": reader.standing.unreadable_reads,
        "malformed_instructions": reader.standing.malformed,
        "expired_overrides": reader.standing.expired,
        "longest_active_seconds": reader.standing.longest_active_seconds,
        "understood_instructions": list(INSTRUCTIONS),
        "can_revoke_an_override": False,
        "revocations_attempted": reader.standing.revocations_attempted,
        "reads_system_state": False,
    }


def run_human_override_reader(
    reader: HumanOverrideReader, control_socket, publish_overrides,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        reading = reader.read()
        if reading.override is not None:
            publish_overrides(reading.override)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
