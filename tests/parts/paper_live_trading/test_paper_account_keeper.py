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


def test_a_holding_no_order_could_sell_is_not_an_open_position():
    """Real residues, read off the live paper book on 2026-09-16.

    16 of the 52 open stock-options positions were rounding residue: the
    smallest `TRENT 2700 PE 29 SEP 26` at 6.66e-15 units against a 225 lot, and
    four larger than the global 0.001 step but smaller than their own contract's
    lot -- `BAJAJ-AUTO 11300 CE` at 27.346 against 75, `SHREECEM 22500 CE` at
    8.062 against 25. `quantity == 0` is what a residue never reaches, so each
    one stayed open for good, and an open position marks its symbol as held --
    so the segment could never open on that symbol again.

    Restored here through the same checkpoint shape the live book is written in.
    """
    keeper = PaperAccountKeeper(
        segment="stock-options", currency="INR", default_quantity_increment=0.001,
    )
    keeper.restore_from_checkpoint({
        "starting": 7_500_000.0, "cash": 624_951.0,
        "realised_total": -323_404.0, "fees_total": 0.0, "fills_applied": 5_353,
        "positions": {
            # Below the global fallback step, and with no lot recorded.
            "upstox|TRENT 2700 PE 29 SEP 26": {
                "quantity": 6.661338147750939e-15, "average_price": 44.0,
                "margin_posted": 0.0,
            },
            # Above the global step, below its own contract's lot.
            "upstox|BAJAJ-AUTO 11300 CE 29 SEP 26": {
                "quantity": 27.346000000000004, "average_price": 212.0,
                "margin_posted": 1_200.0, "quantity_increment": 75.0,
            },
            # A real position: one whole lot and more.
            "upstox|ABCAPITAL 380 CE 29 SEP 26": {
                "quantity": 18_600.0, "average_price": 9.35,
                "margin_posted": 173_910.0, "quantity_increment": 3_100.0,
            },
        },
        "seen_fills": [],
    })

    assert keeper.read_balance().open_positions == 1
    assert keeper.standing.residues_closed == 2
    # The residue's margin is the account's own money and comes back to cash,
    # rather than being written off with the position.
    assert keeper.standing.residue_margin_returned == 1_200.0
    assert keeper.read_balance().cash == 624_951.0 + 1_200.0


def test_a_fill_that_leaves_less_than_one_lot_closes_the_position():
    """The other half: a residue is never created in the first place.

    `quantity += signed` in floating point is what produced the 16 above -- a
    sell of the whole position leaves 1e-15 rather than 0.
    """
    keeper = PaperAccountKeeper(
        segment="stock-options", currency="INR", default_quantity_increment=0.001,
    )
    keeper.set_allotment(1_000_000.0)

    def a_fill(fill_id, side, quantity):
        return Fill(
            fill_id=fill_id, venue_id="upstox", symbol="TRENT 2700 PE 29 SEP 26",
            side=side, price=44.0, quantity=quantity, fee=1.0,
            filled_at_ns=1, order_id="o", is_paper=True, leverage=1.0,
            segment="stock-options", quantity_increment=225.0,
        )

    keeper.apply_fill(a_fill("f1", BUY, 900.0))
    assert keeper.read_balance().open_positions == 1
    # Sold back all but a thousandth of a unit -- less than one lot, so nothing
    # that could be sent to the exchange could ever close it.
    keeper.apply_fill(a_fill("f2", "sell", 899.999))
    assert keeper.read_balance().open_positions == 0
    assert keeper.standing.residues_closed == 1
