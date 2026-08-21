#!/usr/bin/env python3
"""RL-070's single definition of "built and complete" -- the rule behind every dot.

Every board this project renders (dashboard/build_part_monitor.py's per-part
cells, dashboard/build_status_board.py's per-block tiles, and
dashboard/part_health_api.py's /api/board payload, which feeds the React
board) has to agree on what a green dot means. Rule 8 makes drift here
specifically dangerous: this predicate decides what the user reads as "done",
so it is written once, here, and every board imports it rather than
re-deriving it. Two copies of this rule would drift silently, and the drift
would be invisible until someone trusted a dot that was never checked the
same way as its neighbour.

Holds the rung vocabulary alongside the predicate rather than splitting them
across files, because part_is_measured_complete() is stated directly in terms
of these names (`state.rung in (TESTED, RUNNING)`) -- keeping the two apart
would let one drift out of step with the other without either side noticing.
"""

from __future__ import annotations

from dataclasses import dataclass

# Rungs, lowest first. The order is the ladder.
DECLARED = "DECLARED"
IMPLEMENTED = "IMPLEMENTED"
TESTED = "TESTED"
RUNNING = "RUNNING"
FAILING = "FAILING"
UNMEASURED = "NOT MEASURED"

LADDER = (DECLARED, IMPLEMENTED, TESTED, RUNNING)

RUNG_MEANING = {
    DECLARED: "in the blueprint, contract intact, no code",
    IMPLEMENTED: "a source file named for this part exists",
    TESTED: "a test file naming this part exists",
    RUNNING: "the part reports a live heartbeat",
    FAILING: "a probe ran and the part is broken",
    UNMEASURED: "no probe could run for this part",
}


@dataclass
class PartState:
    part_id: str
    name: str
    role: str
    category: str
    rung: str
    proof: str


def part_is_measured_complete(state: PartState) -> bool:
    """RL-070: a part's dot is green only when it has climbed to TESTED (a source
    file AND a test file naming it -- the same probes that drive the IMPLEMENTED
    and TESTED rungs) with nothing the contract or wiring checks found wrong with
    it. FAILING never reaches TESTED (the checks that produce FAILING run before
    a rung is assigned), so this is never checked against a part that is both
    TESTED and broken.

    RUNNING also counts: the dot answers "is this built?", not "is this running
    right now?" -- a fully built part that is currently stopped must not read as
    unfinished, which is why RUNNING stays a separate rung the dot does not report.
    """
    return state.rung in (TESTED, RUNNING)


def block_completion(owned: list[PartState]) -> tuple[bool, str]:
    """RL-070: a block's dot is green only when every part in it is green.

    Never green by inference -- a block with no parts declared yet is red, not
    vacuously complete, because "no parts exist" is not the same fact as
    "every part is finished".
    """
    if not owned:
        return False, "no parts declared for this block yet"
    complete = [s for s in owned if part_is_measured_complete(s)]
    if len(complete) == len(owned):
        return True, f"{len(complete)} of {len(owned)} parts are TESTED (source file + test file)"
    unfinished = [s.name for s in owned if not part_is_measured_complete(s)]
    return False, f"{len(complete)} of {len(owned)} parts are TESTED; not yet: {', '.join(unfinished)}"
