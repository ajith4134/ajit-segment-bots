"""The broker-adapter block: what an Indian broker says, and nothing invented.

Every other block has had a test that compares each of its parts' real
`PART_DECLARATION` against the blueprint. This block did not, and it is the one
block whose parts were all written after the Indian-market cutover -- so the
check that catches the "update all three together" defect (the blueprint edit,
the part's own declaration, and `start_part`'s readers) was missing for exactly
the newest code. It was found on 2026-09-05 while adding a ninth part here.

RL-067: what is built matches the diagrams.
"""

import importlib

import pytest

from runtime.part_declaration import load_declaration_from_blueprint

BLOCK_PARTS = {
    "broker-account-funds-reader":
        "parts.broker_adapter.broker_account_funds_reader",
    "broker-history-reader":
        "parts.broker_adapter.broker_history_reader",
    "broker-instrument-catalogue-reader":
        "parts.broker_adapter.broker_instrument_catalogue_reader",
    "broker-market-feed-reader":
        "parts.broker_adapter.broker_market_feed_reader",
    "broker-market-tape-writer":
        "parts.broker_adapter.broker_market_tape_writer",
    "broker-price-level-sampler":
        "parts.broker_adapter.broker_price_level_sampler",
    "broker-token-refresh-scheduler":
        "parts.broker_adapter.broker_token_refresh_scheduler",
    "subscribed-instrument-listing-filter":
        "parts.broker_adapter.subscribed_instrument_listing_filter",
}


def test_every_part_in_this_block_is_named_here():
    """A part added to the block without a line here would be checked by nothing."""
    import json
    import pathlib

    blueprint = json.loads(
        (pathlib.Path(__file__).resolve().parents[3] / "docs/features.json").read_text()
    )
    declared = {
        feature["id"]
        for feature in blueprint["features"]
        if feature["category"] == "broker-adapter"
    }
    missing = declared - set(BLOCK_PARTS)
    assert not missing, f"broker-adapter parts with no entry in this test: {sorted(missing)}"


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_every_built_declaration_equals_the_blueprint(part_id):
    module = importlib.import_module(BLOCK_PARTS[part_id])
    assert module.PART_DECLARATION == load_declaration_from_blueprint(part_id)


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_no_part_in_this_block_imports_another_part(part_id):
    """T-4: a part names data, never another part."""
    source = importlib.import_module(BLOCK_PARTS[part_id]).__file__
    with open(source, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith(("from parts.", "import parts.")):
                raise AssertionError(f"{part_id} imports another part: {line.strip()}")
