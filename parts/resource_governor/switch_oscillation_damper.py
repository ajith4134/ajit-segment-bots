"""switch-oscillation-damper: a part switched on and off repeatedly in a short window."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "switch-oscillation-damper"

PART_DECLARATION = PartDeclaration(
    part_id="switch-oscillation-damper",
    consumes=("switch-record",),
    produces=("flap-report", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

ON = "on"
OFF = "off"
# What gate-actuator writes on a record for a switch the launcher made. The
# string rather than the import: a part names data, never another part (T-4).
FLIPPED_OUTCOME = "flipped"


@dataclass(frozen=True)
class FlapReport:
    """A part whose switch is being flipped faster than its work can finish."""

    part_id: str
    transitions_in_window: int
    window_seconds: float
    shortest_on_seconds: float | None
    hold_for_seconds: float
    observed_at_ns: int


@dataclass
class DamperStanding:
    switches_seen: int = 0
    flaps_reported: int = 0
    parts_held: dict = field(default_factory=dict)


class SwitchOscillationDamper:
    """Counts transitions per part and asks for a hold when they come too fast.

    Flapping is expensive in a way neither side sees: each switch pays a start
    cost and throws away whatever the part had in memory, so a part switched
    every few seconds does nothing but start. The hold grows with the count, so
    a part that keeps flapping is held longer rather than at a fixed penalty.
    """

    def __init__(
        self,
        transitions_before_flap: int,
        window_seconds: float,
        base_hold_seconds: float,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        self._threshold = transitions_before_flap
        self._window = window_seconds
        self._base_hold = base_hold_seconds
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._transitions: dict[str, list[tuple[float, str]]] = {}
        self.standing = DamperStanding()

    def observe_switch(self, part_id: str, state: str) -> FlapReport | None:
        now = self._monotonic()
        self.standing.switches_seen += 1
        history = [entry for entry in self._transitions.get(part_id, []) if now - entry[0] < self._window]
        if history and history[-1][1] == state:
            self._transitions[part_id] = history
            return None
        history.append((now, state))
        self._transitions[part_id] = history

        if len(history) < self._threshold:
            return None

        hold = self._base_hold * (len(history) - self._threshold + 1)
        self.standing.flaps_reported += 1
        self.standing.parts_held[part_id] = hold
        return FlapReport(
            part_id=part_id,
            transitions_in_window=len(history),
            window_seconds=self._window,
            shortest_on_seconds=self._shortest_on(history),
            hold_for_seconds=hold,
            observed_at_ns=self._now_ns(),
        )

    def _shortest_on(self, history) -> float | None:
        spans = [
            later[0] - earlier[0]
            for earlier, later in zip(history, history[1:])
            if earlier[1] == ON and later[1] == OFF
        ]
        return min(spans) if spans else None


def describe_flapping(damper: SwitchOscillationDamper) -> dict:
    return {
        "part_id": PART_ID,
        "switches_seen": damper.standing.switches_seen,
        "flaps_reported": damper.standing.flaps_reported,
        "parts_held": dict(damper.standing.parts_held),
    }


def run_switch_oscillation_damper(
    damper: SwitchOscillationDamper, control_socket, read_switch_records, publish_flap,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for part_id, state in read_switch_records():
            report = damper.observe_switch(part_id, state)
            if report is not None:
                publish_flap(report)

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

    Only a switch the launcher actually made is a transition; a refused or
    failed one changed nothing and must not count towards a flap.
    """
    from runtime.input_assembly import Batch

    records = Batch(read=context.bus.reader("switch-record"))
    publish_flaps = context.bus.publisher_for("flap-report")
    damper = SwitchOscillationDamper(
        transitions_before_flap=int(context.number("switch_transitions_before_flap")),
        window_seconds=context.number("switch_flap_window"),
        base_hold_seconds=context.number("switch_flap_base_hold"),
    )

    def read_switch_records():
        return tuple(
            (record.part_id, ON if record.action == ON else OFF)
            for record in records.payloads()
            if record.outcome == FLIPPED_OUTCOME
        )

    return run_switch_oscillation_damper(
        damper=damper,
        control_socket=context.control_socket,
        read_switch_records=read_switch_records,
        publish_flap=lambda report: publish_flaps((report,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
