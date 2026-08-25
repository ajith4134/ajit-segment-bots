"""No part calls its own object with arguments that object cannot take.

The second class of defect that only appears on the day a producer starts. All
four of these were written months before they could fail, and all four failed
within one minute of `symbol-profile-store` first running on 2026-08-25:

    BullExitPlanProposer.observe_symbol_profile() missing 2 required arguments
    BearExitPlanProposer.observe_symbol_profile() missing 2 required arguments
    BullFeatureBuilder.observe_symbol_profile() missing 2 required arguments
    BearFeatureBuilder has no attribute 'observe_symbol_profile'

Two more were found by the same probe and had never run at all: the bear weight
learner's instruction scorecard, and the tailgater's whole checkpoint -- guarded
by a `hasattr` around four methods the model did not have, so that bot's learned
state has never been saved or restored.
"""

from __future__ import annotations

from dashboard.check_part_calls import check_every_part


def test_every_followed_call_is_one_its_object_can_answer():
    bad, checked, modules = check_every_part()
    assert not bad, "\n".join(
        f"{call.source_file}:{call.line}: {call.class_name}.{call.method} {call.problem}"
        for call in sorted(bad, key=lambda c: (c.source_file, c.line))
    )
    assert modules > 300


def test_the_checker_still_follows_the_calls_it_used_to():
    """A parser that stopped following anything would report zero defects."""
    _, checked, _ = check_every_part()
    assert checked > 400
