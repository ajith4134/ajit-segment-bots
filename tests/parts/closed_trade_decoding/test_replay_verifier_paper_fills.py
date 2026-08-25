"""A paper fill is not a missing venue fill, and nothing verified is not agreement.

The defect these pin fired on the first trade phase 5 put through the block: two
SERIOUS existence mismatches reported on a paper trade, and it would have been two
on every paper trade forever. On paper the paper book *is* the venue -- nothing
will confirm the fill because nothing else made it, so there is no disagreement to
find.

That is the failure the part's own docstring warns about. A verifier that treats
an expected absence like a missing fill trains everyone to ignore it, and then the
real missing fill -- a live position the system does not know exists -- goes unread
with the rest.
"""

from __future__ import annotations

import pytest

from parts.closed_trade_decoding.trade_replay_verifier import (
    AGREES,
    NO_VENUE_RECORD,
    SERIOUS,
    TradeReplayVerifier,
)

EXACT = 1e-9
ONE_SECOND = 1.0


def verifier() -> TradeReplayVerifier:
    return TradeReplayVerifier(
        quantity_tolerance=EXACT, price_tolerance=EXACT,
        fee_tolerance=EXACT, timestamp_tolerance_seconds=ONE_SECOND,
    )


def journal_entry(fill_id, quantity=1.0, price=100.0, fee=0.5, timestamp=1):
    return {
        "fill_id": fill_id, "quantity": quantity,
        "price": price, "fee": fee, "timestamp": timestamp,
    }


def test_a_paper_fill_raises_no_mismatch():
    """The exact shape of the live defect: two paper fills, two false alarms."""
    v = verifier()
    for fill_id in ("f1", "f2"):
        v.observe_journal_entry(fill_id, journal_entry(fill_id))
        v.observe_paper_fill(fill_id)

    outcome = v.verify("trade-1", ("f1", "f2"))

    assert outcome.mismatches == ()
    assert v.standing.serious == 0
    assert v.standing.paper_fills_not_verifiable == 2


def test_a_trade_of_only_paper_fills_is_not_counted_as_agreement():
    """Counting it as agreement is the flattering error.

    A board would read "the record matches the venue" off a trade no venue ever
    saw, and every paper trade would raise the agreement rate.
    """
    v = verifier()
    v.observe_journal_entry("f1", journal_entry("f1"))
    v.observe_paper_fill("f1")

    outcome = v.verify("trade-1", ("f1",))

    assert outcome.state == NO_VENUE_RECORD
    assert outcome.state != AGREES
    assert v.standing.trades_that_agreed == 0
    assert v.standing.trades_not_verifiable == 1
    # Nothing was checked, so nothing is claimed to have been checked.
    assert outcome.fields_checked == ()


def test_a_live_fill_the_venue_never_reported_is_still_serious():
    """The check this part was written for, kept intact by the fix above."""
    v = verifier()
    v.observe_journal_entry("g1", journal_entry("g1"))
    # Not marked paper, and no venue fill observed: the venue never confirmed it.

    outcome = v.verify("trade-2", ("g1",))

    assert len(outcome.mismatches) == 1
    assert outcome.mismatches[0].severity == SERIOUS
    assert "never reported" in outcome.mismatches[0].reason
    assert v.standing.serious == 1


def test_a_venue_confirmed_fill_that_matches_is_agreement():
    v = verifier()
    v.observe_journal_entry("g1", journal_entry("g1"))
    v.observe_venue_fill("g1", journal_entry("g1"))

    outcome = v.verify("trade-3", ("g1",))

    assert outcome.state == AGREES
    assert v.standing.trades_that_agreed == 1
    assert v.standing.trades_not_verifiable == 0


def test_a_venue_confirmed_fill_that_disagrees_is_still_caught():
    """A paper fill beside it must not mask a real disagreement."""
    v = verifier()
    v.observe_journal_entry("p1", journal_entry("p1"))
    v.observe_paper_fill("p1")
    v.observe_journal_entry("g1", journal_entry("g1", price=100.0))
    v.observe_venue_fill("g1", journal_entry("g1", price=95.0))

    outcome = v.verify("trade-4", ("p1", "g1"))

    assert len(outcome.mismatches) == 1
    assert outcome.mismatches[0].field == "price"
    assert v.standing.paper_fills_not_verifiable == 1


def test_a_mixed_trade_that_agrees_reports_agreement_not_unverifiable():
    """One venue-confirmed fill is enough to have verified something."""
    v = verifier()
    v.observe_journal_entry("p1", journal_entry("p1"))
    v.observe_paper_fill("p1")
    v.observe_journal_entry("g1", journal_entry("g1"))
    v.observe_venue_fill("g1", journal_entry("g1"))

    outcome = v.verify("trade-5", ("p1", "g1"))

    assert outcome.state == AGREES
    assert v.standing.trades_that_agreed == 1
    assert v.standing.paper_fills_not_verifiable == 1


def test_a_venue_fill_the_journal_never_saw_is_still_critical():
    """The most serious case of all, untouched: the position is real and unknown."""
    v = verifier()
    v.observe_venue_fill("g1", journal_entry("g1"))

    outcome = v.verify("trade-6", ("g1",))

    assert outcome.mismatches[0].severity == "critical"
    assert v.standing.fills_the_journal_never_saw == 1


@pytest.mark.parametrize("field_name,changed", [
    ("quantity", 2.0), ("price", 95.0), ("fee", 5.0),
])
def test_every_field_check_survives_the_paper_change(field_name, changed):
    v = verifier()
    v.observe_journal_entry("g1", journal_entry("g1"))
    v.observe_venue_fill("g1", journal_entry("g1", **{field_name: changed}))

    outcome = v.verify("trade-7", ("g1",))

    assert [m.field for m in outcome.mismatches] == [field_name]
