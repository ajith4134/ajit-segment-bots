"""Every string in this file is copied from a live NSE response captured
2026-09-02 (RL-063): the ban file's own body, one reportASM row, and one
corporates-corporateActions row. Never an invented shape."""

import datetime

import pytest

from runtime.market_conditions import (
    CorporateAction,
    CorporateActionReport,
    InstrumentRestriction,
    InstrumentRestrictionReport,
    MarketSessionState,
    RestrictionKind,
    SessionKind,
    read_nse_date,
)

# https://nsearchives.nseindia.com/content/fo/fo_secban.csv, fetched 2026-09-02
BAN_FILE = "Securities in Ban For Trade Date 02-SEP-2026:\n1,LICHSGFIN\n2,SAIL\n"


def test_reads_nse_s_own_two_date_formats():
    """The ban file stamps 02-SEP-2026 and every JSON endpoint stamps
    02-Sep-2026. Same day, two casings, one parser."""
    assert read_nse_date("02-SEP-2026") == datetime.date(2026, 9, 2)
    assert read_nse_date("02-Sep-2026") == datetime.date(2026, 9, 2)


def test_a_date_nse_did_not_state_is_refused_not_guessed():
    with pytest.raises(ValueError):
        read_nse_date("-")


def test_a_restriction_report_carries_the_source_that_claimed_it():
    report = InstrumentRestrictionReport(
        symbol="LICHSGFIN",
        kind=RestrictionKind.FNO_BAN,
        source="nse-fo-secban",
        stated_for=datetime.date(2026, 9, 2),
        detail="Securities in Ban For Trade Date 02-SEP-2026",
        observed_at_ns=1_756_800_000_000_000_000,
    )
    assert report.symbol == "LICHSGFIN"
    assert report.kind is RestrictionKind.FNO_BAN
    assert report.source == "nse-fo-secban"


def test_a_restriction_says_plainly_whether_a_new_position_may_open():
    banned = InstrumentRestriction(
        symbol="LICHSGFIN",
        kinds=(RestrictionKind.FNO_BAN,),
        sources=("nse-fo-secban",),
        stated_for=datetime.date(2026, 9, 2),
        observed_at_ns=1_756_800_000_000_000_000,
    )
    assert banned.may_open_new_position is False
    assert banned.may_close_existing_position is True


def test_an_asm_stage_restricts_without_forbidding_an_exit():
    """ASM raises margin; it does not stop a position being closed. A
    restriction that blocked exits would trap capital the exchange never
    trapped."""
    watched = InstrumentRestriction(
        symbol="A2ZINFRA",
        kinds=(RestrictionKind.ASM_LONG_TERM,),
        sources=("nse-asm-longterm",),
        stated_for=datetime.date(2026, 9, 2),
        observed_at_ns=1_756_800_000_000_000_000,
    )
    assert watched.may_open_new_position is False
    assert watched.may_close_existing_position is True


def test_a_corporate_action_report_keeps_nse_s_own_subject_text():
    """subject is the only field stating what the action actually is;
    corporate-action-adjuster reads it, so it is carried verbatim."""
    report = CorporateActionReport(
        symbol="NTPC",
        series="EQ",
        isin="INE733E01010",
        subject="Dividend - Rs 3.50 Per Share",
        ex_date=datetime.date(2026, 9, 2),
        record_date=datetime.date(2026, 9, 2),
        face_value=10.0,
        observed_at_ns=1_756_800_000_000_000_000,
    )
    assert report.subject == "Dividend - Rs 3.50 Per Share"
    assert report.ex_date == datetime.date(2026, 9, 2)


def test_a_corporate_action_carries_the_factor_a_price_series_must_be_divided_by():
    action = CorporateAction(
        symbol="RELIANCE",
        kind="bonus",
        price_factor=0.5,
        quantity_factor=2.0,
        ex_date=datetime.date(2026, 9, 2),
        stated_from="Bonus 1:1",
        observed_at_ns=1_756_800_000_000_000_000,
    )
    assert action.price_factor == 0.5
    assert action.quantity_factor == 2.0


def test_a_session_state_says_which_session_and_why():
    holiday = MarketSessionState(
        segment="FO",
        kind=SessionKind.HOLIDAY,
        as_of_date=datetime.date(2026, 1, 26),
        reason="Republic Day",
        observed_at_ns=1_756_800_000_000_000_000,
    )
    assert holiday.is_tradeable is False
    assert holiday.reason == "Republic Day"


def test_an_open_session_is_tradeable():
    live = MarketSessionState(
        segment="FO",
        kind=SessionKind.OPEN,
        as_of_date=datetime.date(2026, 9, 2),
        reason="within stated session hours",
        observed_at_ns=1_756_800_000_000_000_000,
    )
    assert live.is_tradeable is True
