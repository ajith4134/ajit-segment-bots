"""board-snapshot-builder: the whole system's measured state, every tile traced (RL-012).

Rule 8 in one part. It assembles what a person looks at, and the discipline is
absolute: **a tile exists only because a probe produced it.** There is no default
value, no last-known-good silently reused, no tile that is green because nothing
said otherwise.

The three states that are not "fine" are all distinct, because collapsing them is
how a board becomes a lie:

- **NOT BUILT** -- this thing does not exist yet. True and unalarming.
- **NOT MEASURED** -- it exists and nobody could read it. Never green.
- **FAILING** -- it was read and it is broken.

A snapshot also carries **its own age and the age of its oldest input**. A board
assembled from probes that ran an hour ago is a historical document, and one
presented as current is worse than none at all.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "board-snapshot-builder"

PART_DECLARATION = PartDeclaration(
    part_id="board-snapshot-builder",
    consumes=(
        "heartbeat-table", "probe-result", "usdt-pnl-statement", "alert", "switch-record",
        "account-balance", "feed-coverage", "competence-map", "main-account-setting",
        "capital-allotment", "trade-capital-bounds", "leverage-ceiling", "allocation-headroom",
        "capital-settings-verdict", "capital-utilisation", "allocation-proposal",
        "decision-cost", "prompt-score",
    ),
    produces=("board-snapshot", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

OK = "OK"
NOT_BUILT = "NOT BUILT"
NOT_MEASURED = "NOT MEASURED"
FAILING = "FAILING"

TILE_STATES = (OK, NOT_BUILT, NOT_MEASURED, FAILING)


class TileWithoutProof(ValueError):
    """A tile was offered with no evidence behind it."""


@dataclass(frozen=True)
class Tile:
    """One thing on the board, and the probe that produced it."""

    label: str
    state: str
    value: str
    proof: str
    measured_at_ns: int
    age_seconds: float

    @property
    def is_trustworthy(self) -> bool:
        return self.state != NOT_MEASURED


@dataclass(frozen=True)
class BoardSnapshot:
    """Everything the board shows, and how old the oldest part of it is."""

    tiles: tuple[Tile, ...]
    ok: int
    not_built: int
    not_measured: int
    failing: int
    oldest_input_age_seconds: float
    built_at_ns: int
    is_complete: bool
    reason: str


@dataclass
class BuilderStanding:
    snapshots_built: int = 0
    tiles_offered: int = 0
    tiles_refused: int = 0
    incomplete_snapshots: int = 0
    oldest_input_seen_seconds: float = 0.0
    by_state: dict = field(default_factory=dict)


class BoardSnapshotBuilder:
    """Assembles tiles that each carry their own proof, and refuses ones that do not."""

    def __init__(self, stale_input_seconds: float, monotonic=time.monotonic, now_ns=time.time_ns) -> None:
        if stale_input_seconds <= 0:
            raise ValueError("an input must be allowed some age or nothing can ever be shown")
        self._stale_after = stale_input_seconds
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._tiles: dict[str, tuple[str, str, str, float, int]] = {}
        self.standing = BuilderStanding()

    def offer_tile(self, label: str, state: str, value: str, proof: str) -> None:
        """Add or replace one tile. Refused outright if it carries no proof.

        Refused rather than shown with an empty proof: a tile whose provenance
        cannot be named is not a status, and letting one through would make every
        other tile's proof optional in practice.
        """
        self.standing.tiles_offered += 1
        if state not in TILE_STATES:
            self.standing.tiles_refused += 1
            raise TileWithoutProof(f"{label!r} offered state {state!r}, which is not a board state")
        if not proof.strip():
            self.standing.tiles_refused += 1
            raise TileWithoutProof(
                f"{label!r} was offered with no proof; a tile whose provenance cannot be named "
                f"is not a status (Rule 8, RL-012)"
            )
        self._tiles[label] = (state, value, proof, self._monotonic(), self._now_ns())
        self.standing.by_state[state] = self.standing.by_state.get(state, 0) + 1

    def mark_unmeasurable(self, label: str, reason: str, proof: str) -> None:
        """A thing that exists and could not be read. Never green, never absent."""
        self.offer_tile(label, NOT_MEASURED, reason, proof)

    def build(self, expected_labels: tuple[str, ...] = ()) -> BoardSnapshot:
        """The snapshot, with anything expected but never offered shown as unmeasured."""
        now = self._monotonic()
        tiles = []
        counts = {state: 0 for state in TILE_STATES}
        oldest = 0.0

        labels = sorted(set(self._tiles) | set(expected_labels))
        for label in labels:
            held = self._tiles.get(label)
            if held is None:
                counts[NOT_MEASURED] += 1
                tiles.append(
                    Tile(
                        label=label, state=NOT_MEASURED,
                        value="no probe has reported this",
                        proof="expected on the board and never offered",
                        measured_at_ns=self._now_ns(), age_seconds=0.0,
                    )
                )
                continue

            state, value, proof, at, at_ns = held
            age = now - at
            oldest = max(oldest, age)
            if state == OK and age > self._stale_after:
                # A tile whose probe ran long ago is not current, whatever it
                # said. Shown as unmeasured rather than as the good news it was.
                state = NOT_MEASURED
                value = f"last measured {age:.0f}s ago, past {self._stale_after:.0f}s"
            counts[state] += 1
            tiles.append(
                Tile(
                    label=label, state=state, value=value, proof=proof,
                    measured_at_ns=at_ns, age_seconds=age,
                )
            )

        self.standing.snapshots_built += 1
        self.standing.oldest_input_seen_seconds = max(
            self.standing.oldest_input_seen_seconds, oldest
        )
        complete = counts[NOT_MEASURED] == 0
        if not complete:
            self.standing.incomplete_snapshots += 1

        return BoardSnapshot(
            tiles=tuple(tiles),
            ok=counts[OK],
            not_built=counts[NOT_BUILT],
            not_measured=counts[NOT_MEASURED],
            failing=counts[FAILING],
            oldest_input_age_seconds=oldest,
            built_at_ns=self._now_ns(),
            is_complete=complete,
            reason=(
                f"{counts[OK]} measured, {counts[NOT_BUILT]} not built, "
                f"{counts[NOT_MEASURED]} not measured, {counts[FAILING]} failing; "
                f"oldest input {oldest:.0f}s old"
            ),
        )


def describe_board(builder: BoardSnapshotBuilder) -> dict:
    return {
        "part_id": PART_ID,
        "snapshots_built": builder.standing.snapshots_built,
        "tiles_offered": builder.standing.tiles_offered,
        "tiles_refused": builder.standing.tiles_refused,
        "incomplete_snapshots": builder.standing.incomplete_snapshots,
        "oldest_input_seen_seconds": builder.standing.oldest_input_seen_seconds,
        "by_state": dict(builder.standing.by_state),
    }


def run_board_snapshot_builder(
    builder: BoardSnapshotBuilder, control_socket, read_inputs, publish_snapshot,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        expected = read_inputs(builder)
        publish_snapshot(builder.build(expected))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
