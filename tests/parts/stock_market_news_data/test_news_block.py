"""What the block declares in code must equal what the blueprint declares
(RL-067), the same check every other block already carries.

This is the test that catches the failure a unit test cannot see: a part whose
`consumes` in code is thinner than its `consumes` in the blueprint binds fewer
inboxes than the wiring plan built for it, so a wire that the diagram says
exists carries nothing, and no unit test of either end notices.
"""

import importlib

import pytest

from runtime.part_declaration import load_declaration_from_blueprint

BLOCK_PARTS = {
    "trading-restriction-reader":
        "parts.stock_market_news_data.trading_restriction_reader",
    "instrument-restriction-state":
        "parts.stock_market_news_data.instrument_restriction_state",
    "corporate-action-reader":
        "parts.stock_market_news_data.corporate_action_reader",
    "corporate-action-adjuster":
        "parts.stock_market_news_data.corporate_action_adjuster",
    "market-session-calendar":
        "parts.stock_market_news_data.market_session_calendar",
}


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_every_built_declaration_equals_the_blueprint(part_id):
    module = importlib.import_module(BLOCK_PARTS[part_id])
    assert module.PART_DECLARATION == load_declaration_from_blueprint(part_id)


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_every_part_carries_the_one_entry_point(part_id):
    """T-1: one shape, no privileged parts. A part with no start_part is a part
    the governor cannot switch on -- 277 of them were in exactly that state on
    2026-08-23 while the monitor showed them as TESTED."""
    module = importlib.import_module(BLOCK_PARTS[part_id])
    assert callable(module.start_part)
