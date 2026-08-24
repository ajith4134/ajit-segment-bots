"""folded-circuit-view: the whole system at a size a decision can use.

321 parts is not a picture anybody reasons over, and every part that needs to know
"where is this system weak" would otherwise walk the full graph and reach its own
conclusion. So this part folds the graph into blocks once and reports where the
health actually is.

Folding is lossy on purpose, and what it keeps is chosen for what the consumers ask:

- **A dark block is louder than a dark part.** One part not running in a block of
  fifteen is normal; a block where nothing runs is a capability that does not exist,
  and that is the thing a gap finder is looking for.
- **Never-started and faulted are different states.** A part that has never run is
  unbuilt or unconfigured; a part that ran and broke is a fault. Collapsing them into
  "not healthy" hides which one a system has.
- **Edges between blocks are counted, not drawn.** How many data types flow from one
  block to another is a number a decision can use; a node-link picture of 1,363 wires
  is not.

Rule 8 governs this part completely: **a part with no measurement renders as
unmeasured, never as healthy.** The correct map of a mostly-unbuilt system is a mostly
dark one, and a map that defaults to green is worse than no map because it reassures
precisely when attention is needed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.autonomy_types import FoldedCircuitMap
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "folded-circuit-view"

PART_DECLARATION = PartDeclaration(
    part_id="folded-circuit-view",
    consumes=("part-health",),
    produces=("folded-circuit-map", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FOLDED = "folded"
NOTHING_DECLARED = "no-part-has-been-declared"

# What is known about a part. Never-measured is its own state (Rule 8).
RUNNING = "running"
FAULTED = "faulted"
NEVER_STARTED = "never-started"
NOT_MEASURED = "not-measured"

PART_STATES = (RUNNING, FAULTED, NEVER_STARTED, NOT_MEASURED)


@dataclass(frozen=True)
class BlockStanding:
    block: str
    parts_total: int
    running: int
    faulted: int
    never_started: int
    not_measured: int

    @property
    def is_entirely_dark(self) -> bool:
        """A capability that does not exist, rather than a part that is down."""
        return self.running == 0 and self.parts_total > 0

    @property
    def running_fraction(self) -> float:
        return self.running / self.parts_total if self.parts_total else 0.0


@dataclass(frozen=True)
class MapOutcome:
    state: str
    circuit_map: FoldedCircuitMap | None
    blocks: tuple
    edges_between_blocks: dict
    reason: str
    measured_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.circuit_map is not None


@dataclass
class ViewStanding:
    folds: int = 0
    parts_declared: int = 0
    blocks_declared: int = 0
    dark_blocks_reported: int = 0
    parts_defaulted_to_healthy: int = 0


class FoldedCircuitView:
    """Folds parts into blocks and reports where the health is, never assuming it."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._block_of: dict[str, str] = {}
        self._state_of: dict[str, str] = {}
        self._consumes: dict[str, tuple] = {}
        self._produces: dict[str, tuple] = {}
        self.standing = ViewStanding()

    def declare_part(
        self, part_id: str, block: str, consumes=(), produces=(),
    ) -> None:
        if part_id not in self._block_of:
            self.standing.parts_declared += 1
        if block not in set(self._block_of.values()):
            self.standing.blocks_declared += 1
        self._block_of[part_id] = block
        self._consumes[part_id] = tuple(consumes)
        self._produces[part_id] = tuple(produces)
        # Declared is not measured: it stays unmeasured until something says otherwise.
        self._state_of.setdefault(part_id, NOT_MEASURED)

    def observe_state(self, part_id: str, state: str) -> None:
        if state not in PART_STATES:
            raise ValueError(
                f"{state!r} is not a state this view can render. A state it cannot render "
                f"would have to be shown as something else, which is how a map lies"
            )
        self._state_of[part_id] = state

    def edges_between_blocks(self) -> dict:
        """Counted, not drawn: 1,363 wires is a hairball, a count is a number."""
        producers: dict[str, set] = {}
        for part_id, produces in self._produces.items():
            block = self._block_of.get(part_id)
            for data in produces:
                producers.setdefault(data, set()).add(block)

        edges: dict = {}
        for part_id, consumes in self._consumes.items():
            block = self._block_of.get(part_id)
            for data in consumes:
                for source in producers.get(data, set()):
                    if source == block:
                        continue
                    edges[(source, block)] = edges.get((source, block), set()) | {data}
        return {pair: len(kinds) for pair, kinds in sorted(edges.items())}

    def fold(self) -> MapOutcome:
        self.standing.folds += 1
        if not self._block_of:
            return self._outcome(
                NOTHING_DECLARED, None, (), {},
                "no part has been declared, so there is nothing to fold",
            )

        by_block: dict[str, list] = {}
        for part_id, block in self._block_of.items():
            by_block.setdefault(block, []).append(part_id)

        blocks = []
        for block, part_ids in sorted(by_block.items()):
            states = [self._state_of.get(part_id, NOT_MEASURED) for part_id in part_ids]
            blocks.append(
                BlockStanding(
                    block=block,
                    parts_total=len(part_ids),
                    running=states.count(RUNNING),
                    faulted=states.count(FAULTED),
                    never_started=states.count(NEVER_STARTED),
                    not_measured=states.count(NOT_MEASURED),
                )
            )

        dark = tuple(
            standing.block for standing in blocks if standing.is_entirely_dark
        )
        self.standing.dark_blocks_reported += len(dark)

        circuit_map = FoldedCircuitMap(
            blocks={
                standing.block: {
                    "parts": standing.parts_total,
                    "running": standing.running,
                    "faulted": standing.faulted,
                    "never_started": standing.never_started,
                    "not_measured": standing.not_measured,
                }
                for standing in blocks
            },
            parts_total=len(self._block_of),
            parts_running=sum(standing.running for standing in blocks),
            parts_faulted=sum(standing.faulted for standing in blocks),
            parts_never_started=sum(standing.never_started for standing in blocks),
            blocks_entirely_dark=dark,
            measured_at_ns=self._now_ns(),
        )

        return self._outcome(
            FOLDED, circuit_map, tuple(blocks), self.edges_between_blocks(),
            f"{len(blocks)} block(s) over {circuit_map.parts_total} part(s): "
            f"{circuit_map.parts_running} running, {circuit_map.parts_faulted} faulted, "
            f"{circuit_map.parts_never_started} never started, "
            f"{sum(standing.not_measured for standing in blocks)} not measured"
            + (
                f". {len(dark)} block(s) are entirely dark, which is a capability that "
                f"does not exist rather than a part that is down"
                if dark
                else ""
            ),
        )

    def _outcome(self, state, circuit_map, blocks, edges, reason) -> MapOutcome:
        return MapOutcome(
            state=state, circuit_map=circuit_map, blocks=blocks,
            edges_between_blocks=edges, reason=reason, measured_at_ns=self._now_ns(),
        )


def describe_folding(view: FoldedCircuitView) -> dict:
    return {
        "part_id": PART_ID,
        "folds": view.standing.folds,
        "parts_declared": view.standing.parts_declared,
        "blocks_declared": view.standing.blocks_declared,
        "dark_blocks_reported": view.standing.dark_blocks_reported,
        "part_states": list(PART_STATES),
        "defaults_an_unmeasured_part_to_healthy": False,
        "parts_defaulted_to_healthy": view.standing.parts_defaulted_to_healthy,
        "draws_part_level_edges": False,
    }


def run_folded_circuit_view(
    view: FoldedCircuitView, control_socket, read_states, publish_map,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for part_id, state in read_states():
            view.observe_state(part_id, state)
        outcome = view.fold()
        if outcome.is_usable:
            publish_map(outcome.circuit_map)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_folding(view),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Every part in the blueprint is declared, so a part nobody has started
    renders as its own state rather than vanishing from the map (Rule 8). What
    can be measured from here is the health stream: a part that reported is
    running, and a part that reported once and has now been silent for longer
    than fold_silent_after_seconds is faulted. Never-started cannot be told
    apart from not-yet-heard-from on this input, so a part that has never
    reported stays not-measured rather than being guessed either way.
    """
    import time as _time

    from runtime.input_assembly import Batch
    from runtime.wiring_plan import load_blueprint

    health = Batch(read=context.bus.reader("part-health"))
    publish_map = context.bus.publisher_for("folded-circuit-map")

    view = FoldedCircuitView()
    for feature in load_blueprint()["features"]:
        view.declare_part(
            feature["id"],
            feature["category"],
            consumes=tuple(feature.get("consumes", ())),
            produces=tuple(feature.get("produces", ())),
        )

    silent_after = context.number("fold_silent_after_seconds")
    last_heard: dict[str, float] = {}

    def read_states():
        now = _time.monotonic()
        states = []
        for report in health.payloads():
            last_heard[report.part_id] = now
            states.append((report.part_id, RUNNING))
        for part_id, heard_at in last_heard.items():
            if now - heard_at > silent_after:
                states.append((part_id, FAULTED))
        return tuple(states)

    return run_folded_circuit_view(
        view=view,
        control_socket=context.control_socket,
        read_states=read_states,
        publish_map=lambda circuit_map: publish_map((circuit_map,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
