"""The row is a live response from
www.nseindia.com/api/corporates-corporateActions?index=equities, captured
2026-09-02 (RL-063). NSE writes "-" where it has no date, which is why the
reader must never parse a date it was not given."""

import datetime

from parts.stock_market_news_data.corporate_action_reader import CorporateActionReader

NTPC_DIVIDEND = {
    "bcEndDate": "-", "bcStartDate": "-", "caBroadcastDate": None,
    "comp": "NTPC Limited", "exDate": "02-Sep-2026", "faceVal": "10",
    "ind": "-", "isin": "INE733E01010", "ndEndDate": "-", "ndStartDate": "-",
    "recDate": "02-Sep-2026", "series": "EQ",
    "subject": "Dividend - Rs 3.50 Per Share", "symbol": "NTPC",
}

OBSERVED_AT_NS = 1_756_800_000_000_000_000


def test_reads_a_real_nse_corporate_action_row():
    reader = CorporateActionReader()
    reports = reader.reports_from([NTPC_DIVIDEND], OBSERVED_AT_NS)
    assert len(reports) == 1
    report = reports[0]
    assert report.symbol == "NTPC"
    assert report.series == "EQ"
    assert report.isin == "INE733E01010"
    assert report.subject == "Dividend - Rs 3.50 Per Share"
    assert report.ex_date == datetime.date(2026, 9, 2)
    assert report.record_date == datetime.date(2026, 9, 2)
    assert report.face_value == 10.0


def test_nse_s_own_dash_for_a_missing_date_becomes_none_not_today():
    """A record date of "-" dated to today would make the action look effective
    now. None is the honest reading, and every consumer already handles it."""
    row = dict(NTPC_DIVIDEND, recDate="-")
    reader = CorporateActionReader()
    assert reader.reports_from([row], OBSERVED_AT_NS)[0].record_date is None


def test_a_row_with_no_ex_date_is_skipped_entirely():
    """ex_date is what a price adjustment is applied from. Without it there is
    nothing to apply and nothing to guess."""
    row = dict(NTPC_DIVIDEND, exDate="-")
    reader = CorporateActionReader()
    assert reader.reports_from([row], OBSERVED_AT_NS) == ()


def test_a_skipped_row_is_counted_so_the_gap_is_visible():
    """A row silently dropped is a corporate action nobody knows was missed."""
    reader = CorporateActionReader()
    reader.reports_from([dict(NTPC_DIVIDEND, exDate="-")], OBSERVED_AT_NS)
    assert reader.rows_seen == 1
    assert reader.rows_without_an_ex_date == 1


def test_a_face_value_nse_did_not_state_is_none_not_zero():
    row = dict(NTPC_DIVIDEND, faceVal="-")
    reader = CorporateActionReader()
    assert reader.reports_from([row], OBSERVED_AT_NS)[0].face_value is None


def test_a_null_rather_than_a_dash_is_also_read_as_absent():
    """caBroadcastDate arrives as JSON null in the live response, so null and
    "-" are both ways NSE says 'no value' in the same document."""
    row = dict(NTPC_DIVIDEND, recDate=None, faceVal=None)
    reader = CorporateActionReader()
    report = reader.reports_from([row], OBSERVED_AT_NS)[0]
    assert report.record_date is None
    assert report.face_value is None
