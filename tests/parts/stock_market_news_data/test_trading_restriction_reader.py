"""Both fixtures are live NSE responses captured 2026-09-02 (RL-063):
nsearchives.nseindia.com/content/fo/fo_secban.csv in full, and one row from
each list of www.nseindia.com/api/reportASM."""

import datetime

from runtime.market_conditions import RestrictionKind
from parts.stock_market_news_data.trading_restriction_reader import (
    TradingRestrictionReader,
)

BAN_FILE = "Securities in Ban For Trade Date 02-SEP-2026:\n1,LICHSGFIN\n2,SAIL\n"

ASM_DOCUMENT = {
    "longterm": {"data": [{
        "asmSurvIndicator": "Stage I", "asmTime": "02-Sep-2026",
        "companyName": "A2Z Infra Engineering Limited", "isin": "INE619I01012",
        "series": None, "survCode": "LTASM - I (13)",
        "survDesc": "Long Term Additional Surveillance Measure (LTASM) - Stage I",
        "symbol": "A2ZINFRA", "srno": 1,
    }]},
    "shortterm": {"data": [{
        "asmSurvIndicator": "Stage I", "asmTime": "02-Sep-2026",
        "companyName": "Aastha Spintex Limited", "isin": "INE2FMX01012",
        "series": None, "survCode": "STASM - I (11)",
        "survDesc": "Short Term Additional Surveillance Measure (STASM) - Stage I",
        "symbol": "AASTHA", "srno": 1,
    }]},
}

OBSERVED_AT_NS = 1_756_800_000_000_000_000


def test_reads_every_banned_symbol_from_nse_s_own_ban_file():
    reader = TradingRestrictionReader()
    reports = reader.reports_from_ban_file(BAN_FILE, OBSERVED_AT_NS)
    assert [report.symbol for report in reports] == ["LICHSGFIN", "SAIL"]
    assert all(report.kind is RestrictionKind.FNO_BAN for report in reports)
    assert all(report.source == "nse-fo-secban" for report in reports)


def test_the_ban_file_s_header_is_the_date_it_is_stated_for_not_a_symbol():
    """Line one is 'Securities in Ban For Trade Date 02-SEP-2026:' -- read as a
    symbol it becomes a banned instrument that does not exist, and skipped
    without reading it loses the only date the file states."""
    reader = TradingRestrictionReader()
    reports = reader.reports_from_ban_file(BAN_FILE, OBSERVED_AT_NS)
    assert all(report.stated_for == datetime.date(2026, 9, 2) for report in reports)


def test_an_empty_ban_list_is_a_real_answer_not_a_failure():
    """Most days ban nothing. Zero reports must mean zero, and the part must
    not confuse it with 'the fetch failed' -- the fetch failing raises."""
    reader = TradingRestrictionReader()
    empty = "Securities in Ban For Trade Date 03-SEP-2026:\n"
    assert reader.reports_from_ban_file(empty, OBSERVED_AT_NS) == ()


def test_a_ban_file_stating_no_date_is_refused_rather_than_dated_by_wall_clock():
    reader = TradingRestrictionReader()
    try:
        reader.reports_from_ban_file("some other header entirely\n1,SAIL\n", OBSERVED_AT_NS)
    except ValueError as refused:
        assert "trade date" in str(refused)
    else:
        raise AssertionError("a ban file with no stated date must be refused")


def test_reads_both_asm_lists_and_keeps_them_apart():
    reader = TradingRestrictionReader()
    reports = reader.reports_from_asm(ASM_DOCUMENT, OBSERVED_AT_NS)
    by_symbol = {report.symbol: report for report in reports}
    assert by_symbol["A2ZINFRA"].kind is RestrictionKind.ASM_LONG_TERM
    assert by_symbol["AASTHA"].kind is RestrictionKind.ASM_SHORT_TERM


def test_an_asm_row_keeps_nse_s_own_stage_description_as_the_detail():
    reader = TradingRestrictionReader()
    reports = reader.reports_from_asm(ASM_DOCUMENT, OBSERVED_AT_NS)
    long_term = next(r for r in reports if r.symbol == "A2ZINFRA")
    assert long_term.detail == "Long Term Additional Surveillance Measure (LTASM) - Stage I"


def test_an_asm_row_with_no_symbol_is_skipped_not_reported_under_none():
    """NSE has published rows with a null series; a null symbol has not been
    seen, and if it ever is, a restriction keyed None would match nothing and
    silently protect nothing."""
    reader = TradingRestrictionReader()
    document = {"longterm": {"data": [{"symbol": None, "survDesc": "x", "asmTime": "02-Sep-2026"}]},
                "shortterm": {"data": []}}
    assert reader.reports_from_asm(document, OBSERVED_AT_NS) == ()


def test_an_asm_document_missing_a_list_entirely_is_read_not_crashed_on():
    """NSE has served both keys every time it was read, but a missing list is a
    thinner answer rather than a broken one -- and crashing here would take the
    ban list down with it, which is the restriction that actually blocks orders."""
    reader = TradingRestrictionReader()
    document = {"longterm": {"data": [{
        "symbol": "A2ZINFRA", "survDesc": "LTASM", "asmTime": "02-Sep-2026",
    }]}}
    reports = reader.reports_from_asm(document, OBSERVED_AT_NS)
    assert [report.symbol for report in reports] == ["A2ZINFRA"]
