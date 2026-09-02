"""A blueprint part with no source file must not stop the planner starting.

Real crash, 2026-09-02, the first day the blueprint declared a part the code
had not built: `part-replacement-planner` walks every feature and resolved a
module for each, and `resolve_part_module` raises for a part that has none. It
crash-looped through five restarts and unattended-run-warden escalated -- on
a condition its own error message already calls normal ("a part with no module
is DECLARED, not startable").

The blueprint and the code had been 1:1 until that day, which is the only
reason this had never fired.
"""

import pytest

from runtime.part_launcher import PartHasNoModule, resolve_part_module
from parts.autonomous.part_replacement_planner import (
    PartReplacementPlanner,
    describe_replacement_planning,
)


def test_resolve_part_module_still_raises_for_a_part_with_no_file():
    """The behaviour being handled, asserted rather than assumed -- if this
    ever stops raising, the handling below is dead code that reads as live."""
    with pytest.raises(PartHasNoModule):
        resolve_part_module("exchange-filing-reader")


def test_a_part_with_no_module_is_skipped_and_counted():
    """What start_part now does, in the shape start_part does it."""
    planner = PartReplacementPlanner()
    declared = 0
    for part_id in ("part-replacement-planner", "exchange-filing-reader"):
        try:
            source = resolve_part_module(part_id)
        except PartHasNoModule:
            planner.standing.parts_without_a_module += 1
            continue
        planner.declare_part(
            part_id, skipped_tick_effect="delays", holds_capital_state=False,
            can_hand_over_state=False, current_source=source,
        )
        declared += 1

    assert declared == 1
    assert planner.standing.parts_without_a_module == 1


def test_the_count_of_unbuilt_parts_is_on_the_standing():
    """It is how many parts the design names and the code has not built. That
    belongs on a board, not swallowed by a continue."""
    planner = PartReplacementPlanner()
    planner.standing.parts_without_a_module = 24
    assert describe_replacement_planning(planner)["parts_without_a_module"] == 24
