"""Two free sources of real Indian history, so no replay depends on one quota.

Upstox is this project's broker and its history endpoint has a **daily** quota:
after a day of fetching it answers HTTP 429 through four backoffs totalling 200
seconds (measured 2026-09-06). The operator asked for sources that do not depend
on one broker, and then for one that carries **options**, because both options
segments are Phase A and Yahoo has no NSE option chain.

    yahoo_finance_prints   equities and indices, 1m for ~a month, 1d for 10 years
    nse_fo_bhavcopy        every option and future, daily OHLC, ~2 years

Both verified against real data on 2026-09-06, and both no-account and no-key.
The Yahoo check is the sharp one: against this project's own captured Upstox
tape, minute by minute, MARUTI/NATIONALUM/BAJAJ-AUTO agreed to a **median
difference of 0.0000%** over 133/133/82 shared minutes.

Nothing here touches the network. The fixtures are the real shapes both sources
actually returned that day (RL-063).
"""

from __future__ import annotations

import pytest

from operate import nse_fo_bhavcopy as bhavcopy
from operate import yahoo_finance_prints as yahoo

# One real Yahoo chart response, trimmed. Stamps are epoch seconds and land on
# 2026-09-04; 03:45 UTC is 09:15 IST, the NSE open.
A_REAL_CHART = {
    "chart": {
        "error": None,
        "result": [{
            "meta": {"currency": "INR", "symbol": "RELIANCE.NS"},
            "timestamp": [1788492900, 1788492960, 1788493020],
            "indicators": {"quote": [{"close": [1349.4, None, 1350.8]}]},
        }],
    }
}

# Real rows from NSE's UDiFF derivatives file for 2026-09-04.
A_REAL_BHAVCOPY = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
    "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,"
    "TtlTradgVol,OpnIntrst\n"
    "2026-09-04,2026-09-04,FO,NSE,IDO,1,,NIFTY,,2026-09-08,2026-09-08,23900.00,CE,"
    "NIFTY,145.00,180.50,112.00,123.80,3733592,1268995\n"
    "2026-09-04,2026-09-04,FO,NSE,IDO,2,,NIFTY,,2026-09-08,2026-09-08,23900.00,PE,"
    "NIFTY,78.00,87.40,47.40,59.85,7170241,900000\n"
    "2026-09-04,2026-09-04,FO,NSE,IDO,3,,NIFTY,,2026-09-08,2026-09-08,22850.00,CE,"
    "NIFTY,0.00,0.00,0.00,1930.45,0,0\n"
    "2026-09-04,2026-09-04,FO,NSE,STO,4,,RELIANCE,,2026-09-29,2026-09-29,1320.00,CE,"
    "RELIANCE,30.00,34.00,28.00,31.05,120000,50000\n"
    "2026-09-04,2026-09-04,FO,NSE,IDF,5,,NIFTY,,2026-09-30,2026-09-30,0.00,,"
    "NIFTY,23900.00,24000.00,23800.00,23950.00,10000,1000\n"
)


# ---- Yahoo: equities and indices ---------------------------------------------

def test_an_equity_and_an_index_have_tickers_and_an_option_has_none():
    """None for an option rather than a guessed ticker: Yahoo has no NSE option
    chain, and a guess would produce a confident 404 that reads like a quiet
    contract."""
    assert yahoo.ticker_for("RELIANCE", "EQ") == "RELIANCE.NS"
    assert yahoo.ticker_for("NIFTY", "INDEX") == "^NSEI"
    assert yahoo.ticker_for("BANKNIFTY", "INDEX") == "^NSEBANK"
    assert yahoo.ticker_for("NIFTY 23900 CE 08 SEP 26", "CE") is None
    assert yahoo.ticker_for("NIFTY 23900 PE 08 SEP 26", "PE") is None


def test_an_index_this_project_does_not_name_has_no_guessed_ticker():
    """An index is not a listed security and there is no rule to derive its
    ticker from, so an unknown one is unknown rather than invented."""
    assert yahoo.ticker_for("SOMETHING NOBODY LISTED", "INDEX") is None


def test_bars_become_prints_oldest_first():
    prints = yahoo.prints_from_chart(A_REAL_CHART)
    assert [price for _at, price in prints] == [1349.4, 1350.8]
    assert [at for at, _price in prints] == sorted(at for at, _ in prints)


def test_a_bar_with_no_close_is_dropped_not_carried_forward():
    """Yahoo leaves a gap where the exchange did not trade. Filling it would
    invent a price for a minute that had none."""
    assert len(yahoo.prints_from_chart(A_REAL_CHART)) == 2


def test_a_chart_carrying_an_error_yields_nothing():
    assert yahoo.prints_from_chart({"chart": {"error": "Not Found", "result": None}}) == []


def test_only_the_day_asked_for_is_returned():
    """A 1m request spans five days; a replay of one session must not be handed
    the other four."""
    assert yahoo.prints_from_chart(A_REAL_CHART, only_day="1999-01-01") == []
    assert len(yahoo.prints_from_chart(A_REAL_CHART, only_day="2026-09-04")) == 2


# ---- NSE bhavcopy: every option ----------------------------------------------

def test_every_option_is_read_and_futures_are_not():
    """Futures are dropped here rather than by the caller: a caller that forgot
    would size an option position against a future's price."""
    contracts = bhavcopy.contracts_in(A_REAL_BHAVCOPY)
    assert len(contracts) == 4
    assert {contract.option_type for contract in contracts} == {"CE", "PE"}
    assert all(contract.underlying in ("NIFTY", "RELIANCE") for contract in contracts)


def test_both_options_segments_are_carried():
    """Index options and stock options -- the two Phase A segments -- come from
    the same free file."""
    contracts = bhavcopy.contracts_in(A_REAL_BHAVCOPY)
    assert any(contract.underlying == "NIFTY" for contract in contracts)
    assert any(contract.underlying == "RELIANCE" for contract in contracts)


def test_a_contract_nobody_traded_still_carries_a_close_and_says_it_did_not_trade():
    """NSE settles every listed strike, so a close exists whether or not anybody
    traded. Replaying that close as a price would be replaying a market that did
    not exist."""
    contracts = bhavcopy.contracts_in(A_REAL_BHAVCOPY)
    untraded = [c for c in contracts if c.strike == 22850.0]
    assert len(untraded) == 1
    assert untraded[0].close == 1930.45
    assert untraded[0].volume == 0
    assert untraded[0].traded is False


def test_a_chain_holds_only_what_traded_unless_asked_otherwise():
    contracts = bhavcopy.contracts_in(A_REAL_BHAVCOPY)
    assert len(bhavcopy.chain_of(contracts, "NIFTY")) == 2
    assert len(bhavcopy.chain_of(contracts, "NIFTY", traded_only=False)) == 3


def test_the_nearest_strikes_come_first_at_the_nearest_expiry():
    contracts = bhavcopy.contracts_in(A_REAL_BHAVCOPY)
    near = bhavcopy.nearest_the_money(bhavcopy.chain_of(contracts, "NIFTY"), spot=23905.0)
    assert near
    assert all(contract.expiry == "2026-09-08" for contract in near)
    assert near[0].strike == 23900.0


def test_a_row_this_reader_cannot_parse_is_skipped_not_guessed_at():
    """NSE has changed this file's shape before, and a fabricated strike is
    worse than a contract nobody replayed."""
    broken = A_REAL_BHAVCOPY + (
        "2026-09-04,2026-09-04,FO,NSE,IDO,9,,NIFTY,,2026-09-08,2026-09-08,"
        "not-a-number,CE,NIFTY,1,1,1,1,1,1\n"
    )
    assert len(bhavcopy.contracts_in(broken)) == 4


def test_the_url_is_nses_own_udiff_name():
    """The legacy content/historical/DERIVATIVES names are gone and answer 404;
    these are what NSE serves now."""
    url = bhavcopy.bhavcopy_url("20260904")
    assert url.endswith("BhavCopy_NSE_FO_0_0_0_20260904_F_0000.csv.zip")
    assert "nsearchives.nseindia.com/content/fo" in url


def test_a_missing_file_raises_rather_than_reading_as_a_quiet_day(tmp_path, monkeypatch):
    """An error page parsed as a bhavcopy is an empty bhavcopy, and an empty one
    reads as 'nothing traded that day' -- the failure that looks exactly like the
    good case."""
    monkeypatch.setattr(bhavcopy, "BHAVCOPY_CACHE", tmp_path)

    class Refusing:
        status_code = 404
        content = b""

    class Session:
        def get(self, *_args, **_kwargs):
            return Refusing()

    with pytest.raises(RuntimeError, match="no derivatives file"):
        bhavcopy.fetch_bhavcopy("20230904", session=Session())


def test_a_cached_day_is_read_back_without_fetching(tmp_path, monkeypatch):
    """A settled session never changes, so refetching a megabyte proves nothing."""
    monkeypatch.setattr(bhavcopy, "BHAVCOPY_CACHE", tmp_path)
    path = bhavcopy.cache_path_for("20260904")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(A_REAL_BHAVCOPY, encoding="utf-8")

    class Exploding:
        def get(self, *_args, **_kwargs):
            raise AssertionError("a cached day must not be fetched again")

    assert bhavcopy.fetch_bhavcopy("20260904", session=Exploding()) == A_REAL_BHAVCOPY


# ---- NSE intraday options: the source that closed the last gap ---------------

from operate import nse_intraday_option_prices as nse_intraday  # noqa: E402

# One real response shape. NSE writes IST wall-clock as though it were an epoch:
# 1788513300000 reads as 09:15 UTC and is really 09:15 IST, the NSE open.
A_REAL_OPTION_CHART = {
    "closePrice": 120.95,
    "grapthData": [[1788513300000, 135.2], [1788513360000, 0], [1788513420000, 137.8]],
}


def test_an_index_option_and_a_stock_option_have_different_prefixes():
    """NSE's own two names. There is no rule to derive the split from -- an index
    is not a listed security -- so the caller says which it is."""
    import datetime

    index = nse_intraday.identifier_for(
        "NIFTY", datetime.date(2026, 9, 8), "CE", 23900.0, underlying_is_an_index=True)
    stock = nse_intraday.identifier_for(
        "RELIANCE", datetime.date(2026, 9, 29), "CE", 1320.0, underlying_is_an_index=False)
    assert index == "OPTIDXNIFTY08-09-2026CE23900.00"
    assert stock == "OPTSTKRELIANCE29-09-2026CE1320.00"


def test_the_strike_carries_two_decimals():
    """Not cosmetic: NSE answers 200 with an empty series for a name it does not
    recognise, so a strike written 23900 reads as a contract nobody traded rather
    than as a request nobody understood."""
    import datetime

    assert nse_intraday.identifier_for(
        "NIFTY", datetime.date(2026, 9, 8), "PE", 23900, underlying_is_an_index=True
    ).endswith("PE23900.00")


def test_stamps_are_shifted_out_of_ist_into_the_instant_they_really_were():
    """Measured against this project's own tape for the same contract: read as
    UTC, 52 shared minutes at a 3.88% median difference; shifted back 5:30, 69
    shared minutes at 0.30%."""
    import datetime

    prints = nse_intraday.prints_from_chart(A_REAL_OPTION_CHART)
    first = datetime.datetime.fromtimestamp(prints[0][0] / 1e9, datetime.UTC)
    assert first.strftime("%H:%M") == "03:45"       # 09:15 IST, the NSE open
    assert first.strftime("%Y-%m-%d") == "2026-09-04"


def test_a_point_with_no_price_is_dropped():
    assert [price for _at, price in nse_intraday.prints_from_chart(A_REAL_OPTION_CHART)] == [
        135.2, 137.8,
    ]


def test_the_session_served_is_read_back_rather_than_assumed():
    """The endpoint takes no date. A caller that believed it had asked for one
    would replay whatever the last session was and call it the day it wanted."""
    assert nse_intraday.the_session_this_serves(A_REAL_OPTION_CHART) == "2026-09-04"
    assert nse_intraday.the_session_this_serves({"grapthData": []}) is None


def test_prints_come_back_oldest_first():
    prints = nse_intraday.prints_from_chart(A_REAL_OPTION_CHART)
    assert [at for at, _price in prints] == sorted(at for at, _ in prints)


# ---- the chooser -------------------------------------------------------------

def test_an_option_for_a_session_nse_is_not_serving_falls_through(monkeypatch):
    """NSE has a session but not the one asked for. Saying so beats replaying
    the wrong day."""
    from operate import historical_prints

    monkeypatch.setattr(
        historical_prints, "option_prints_from_nse",
        lambda row, day, session=None: ([], "nse-intraday"),
    )
    row = {
        "instrument_key": "NSE_FO|1", "instrument_type": "CE",
        "trading_symbol": "NIFTY 23900 CE 08 SEP 26", "underlying_symbol": "NIFTY",
        "strike_price": 23900.0, "expiry": 1788513300000, "segment": "NSE_FO",
    }
    captured = {}

    def fake_upstox(key, frm, to, token):
        captured["asked"] = key
        return [(1, 2.0)]

    monkeypatch.setattr(historical_prints, "historical_prints", fake_upstox)
    monkeypatch.setattr(historical_prints, "upstox_access_token", lambda *a, **k: "token")
    prints, source = historical_prints.prints_for_instrument(row, "2025-01-01")
    assert source == "upstox"
    assert captured["asked"] == "NSE_FO|1"


def test_the_index_underlyings_nse_names_with_optidx_are_written_down():
    from operate.historical_prints import nse_index_underlyings

    named = nse_index_underlyings()
    assert "NIFTY" in named and "BANKNIFTY" in named
    assert "RELIANCE" not in named
