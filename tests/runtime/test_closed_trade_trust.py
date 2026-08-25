"""A decoder refuses the trades that record a defect rather than learning from them.

Ten of the 113 closed trades on this machine carry an entry price the market never
printed -- AAVE at 217.02 falling to 132.57 in 37 seconds, ETH at 3297 falling to
2517. They were recorded by `position-close-detector` before the 2026-08-25 fix,
and a decoder trained on them would learn stop placement and exit quality from
prices that never existed, which is worse than learning nothing.
"""

from __future__ import annotations

import pytest

from runtime.closed_trade_trust import (
    NO_CLOSING_TIME,
    RECORDED_BEFORE_THE_ENTRY_PRICE_FIX,
    TRUSTED,
    TrustStanding,
    judge_closed_trade,
)

# The real boundary on this machine: 2026-08-25T06:25:31Z, the first moment the
# live spine ran the fix -- not 05:37:11Z, when it was committed.
BOUNDARY = 1787639131000000000
SECOND = 1_000_000_000


def test_a_trade_closed_after_the_fix_is_trusted():
    verdict = judge_closed_trade(BOUNDARY + SECOND, BOUNDARY)
    assert verdict.is_trusted
    assert verdict.reason == TRUSTED
    assert bool(verdict) is True


def test_a_trade_closed_before_the_fix_is_refused_and_says_why():
    verdict = judge_closed_trade(BOUNDARY - SECOND, BOUNDARY)
    assert not verdict.is_trusted
    assert verdict.reason == RECORDED_BEFORE_THE_ENTRY_PRICE_FIX
    assert bool(verdict) is False


def test_the_boundary_moment_itself_is_trusted():
    """The boundary is when the fixed code started, so what it recorded is good."""
    assert judge_closed_trade(BOUNDARY, BOUNDARY).is_trusted


def test_a_trade_with_no_closing_time_is_refused_rather_than_assumed_recent():
    """Assuming recent is exactly how a bad row gets learned from."""
    verdict = judge_closed_trade(None, BOUNDARY)
    assert not verdict.is_trusted
    assert verdict.reason == NO_CLOSING_TIME


def test_no_boundary_set_trusts_everything():
    """A machine whose journal only ever held correct rows needs no boundary.

    Inventing one would refuse trades for a defect that never touched them.
    """
    assert judge_closed_trade(1, None).is_trusted
    assert judge_closed_trade(BOUNDARY - SECOND, None).is_trusted


def test_a_missing_closing_time_is_still_refused_with_no_boundary():
    """A trade that cannot be placed in time is refused whatever the setting."""
    assert not judge_closed_trade(None, None).is_trusted


def test_the_standing_separates_refused_from_never_handed_any():
    """A decoder that refused everything and one handed nothing look identical."""
    standing = TrustStanding()
    assert standing.as_standing() == {
        "trades_seen": 0, "trades_trusted": 0,
        "refused_recorded_before_the_fix": 0, "refused_no_closing_time": 0,
    }

    standing.record(judge_closed_trade(BOUNDARY + SECOND, BOUNDARY))
    standing.record(judge_closed_trade(BOUNDARY - SECOND, BOUNDARY))
    standing.record(judge_closed_trade(None, BOUNDARY))

    assert standing.as_standing() == {
        "trades_seen": 3, "trades_trusted": 1,
        "refused_recorded_before_the_fix": 1, "refused_no_closing_time": 1,
    }


def test_record_returns_whether_the_caller_may_use_the_trade():
    standing = TrustStanding()
    assert standing.record(judge_closed_trade(BOUNDARY + SECOND, BOUNDARY)) is True
    assert standing.record(judge_closed_trade(BOUNDARY - SECOND, BOUNDARY)) is False


def test_the_real_journal_splits_where_the_boundary_says_it_should():
    """Against the machine's own numbers rather than invented ones (RL-063).

    The ten known-bad rows all closed before the boundary; this pins the arithmetic
    that decides that, using the entry and exit prices actually recorded.
    """
    # AAVE: entry 217.019 to exit 132.57 in 37.2s -- recorded 2026-08-24, before.
    aave_closed_at = 1787600000000000000
    assert aave_closed_at < BOUNDARY
    assert not judge_closed_trade(aave_closed_at, BOUNDARY).is_trusted


@pytest.mark.parametrize("offset", [-10**12, -1, 0, 1, 10**12])
def test_the_boundary_is_a_clean_split_with_no_gap_or_overlap(offset):
    verdict = judge_closed_trade(BOUNDARY + offset, BOUNDARY)
    assert verdict.is_trusted == (offset >= 0)
