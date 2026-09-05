"""One paper account per segment.

Three segment bots run on one spine since 2026-09-05
(docs/proposals/three-segments-on-one-spine.md). The keeper's own behaviour is
covered by the block test beside this file; what is covered here is the fan-out:
which account a fill is charged to, and whether each was funded at all.
"""

from parts.paper_live_trading.paper_account_keeper import (
    PaperAccountKeeper, SegmentPaperAccounts, describe_segment_paper_accounts,
    fund_each_account_from_its_allotment,
)
from runtime.trading_types import BUY, Fill


class _Allotment:
    def __init__(self, segment, allotted):
        self.segment = segment
        self.allotted = allotted


def three_accounts():
    return SegmentPaperAccounts(
        keepers={
            segment: PaperAccountKeeper(segment=segment, currency="INR")
            for segment in ("index-options", "stock-options", "cash-equity-intraday")
        }
    )


def test_each_segment_is_funded_from_its_own_allotment():
    accounts = three_accounts()
    funded_at = {}

    funded = fund_each_account_from_its_allotment(
        accounts,
        {
            "index-options": _Allotment("index-options", 500_000.0),
            "stock-options": _Allotment("stock-options", 300_000.0),
            "cash-equity-intraday": _Allotment("cash-equity-intraday", 250_000.0),
        },
        funded_at,
    )

    assert sorted(funded) == [
        "cash-equity-intraday", "index-options", "stock-options",
    ]
    assert describe_segment_paper_accounts(accounts)["starting_balance_by_segment"] == {
        "cash-equity-intraday": 250_000.0,
        "index-options": 500_000.0,
        "stock-options": 300_000.0,
    }


def test_an_allotment_that_has_not_changed_does_not_refund_the_account():
    """Funding again would add the allocation on top of what the account has
    already spent, which is how a paper account grows every tick."""
    accounts = three_accounts()
    funded_at = {}
    allotments = {"index-options": _Allotment("index-options", 500_000.0)}

    fund_each_account_from_its_allotment(accounts, allotments, funded_at)
    again = fund_each_account_from_its_allotment(accounts, allotments, funded_at)

    assert again == ()
    balance = accounts.keepers["index-options"].read_balance()
    assert balance.starting_balance == 500_000.0


def test_an_allotment_for_a_segment_this_spine_does_not_trade_is_counted():
    """Not an error: the reader publishes what the operator listed and this part
    keeps accounts for what the spine runs. Counted so it is not invisible."""
    accounts = three_accounts()

    fund_each_account_from_its_allotment(
        accounts, {"futures": _Allotment("futures", 1_000.0)}, {},
    )

    assert describe_segment_paper_accounts(accounts)[
        "allotments_for_a_segment_not_traded"
    ] == {"futures": 1}


def test_a_fill_is_charged_to_the_account_of_its_own_segment():
    accounts = three_accounts()
    fund_each_account_from_its_allotment(
        accounts,
        {
            "index-options": _Allotment("index-options", 500_000.0),
            "stock-options": _Allotment("stock-options", 500_000.0),
        },
        {},
    )

    charged = accounts.keeper_for(
        Fill(
            fill_id="f1", venue_id="upstox", symbol="RELIANCE", side=BUY,
            price=1400.0, quantity=10.0, fee=20.0, filled_at_ns=1,
            segment="stock-options",
        )
    )

    assert charged is accounts.keepers["stock-options"]


def test_a_fill_naming_no_segment_is_charged_to_nothing_and_counted():
    """Charging it to whichever account came first would hide a defect upstream
    and leave one segment's equity behind the positions it is holding."""
    accounts = three_accounts()

    charged = accounts.keeper_for(
        Fill(
            fill_id="f2", venue_id="upstox", symbol="RELIANCE", side=BUY,
            price=1400.0, quantity=10.0, fee=20.0, filled_at_ns=1,
        )
    )

    assert charged is None
    assert describe_segment_paper_accounts(accounts)["fills_without_a_segment"] == 1
