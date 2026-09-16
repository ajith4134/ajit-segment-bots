"""Past option sessions, read from Upstox's expired-instruments route.

The ordinary history endpoint serves a listed option only since it listed -- at
most 33 sessions, measured 2026-09-16 -- and refuses an expired one with HTTP 400.
The expired-instruments route answered 200 on this account for NIFTY back to
2024-10-03 (measurements/2026-09-16-how-deep-option-history-goes/).

Real data only (RL-063): these ask Upstox, and every answer is cached, so a
second run spends no quota.
"""

from __future__ import annotations

import datetime

import pytest

from operate.historical_prints import (
    NoBrokerToken,
    expired_candles,
    expired_expiries,
    expired_option_contracts,
    upstox_access_token,
)

NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


@pytest.fixture(scope="module")
def token():
    try:
        return upstox_access_token()
    except NoBrokerToken as missing:
        pytest.skip(str(missing))


def test_nifty_expiries_reach_back_past_this_month(token):
    expiries = expired_expiries(NIFTY_INDEX_KEY, token)
    assert "2026-09-08" in expiries
    assert list(expiries) == sorted(expiries)
    assert expiries[0] <= "2024-10-03"


def test_an_expired_expiry_lists_its_contracts_with_their_lot(token):
    contracts = expired_option_contracts(NIFTY_INDEX_KEY, "2026-09-08", token)
    assert len(contracts) > 50
    row = contracts[0]
    assert row["underlying_symbol"] == "NIFTY"
    assert row["instrument_type"] in ("CE", "PE")
    assert row["lot_size"] > 0 and row["strike_price"] > 0
    assert row["instrument_key"].endswith("|08-09-2026")


def test_an_expired_contract_serves_its_one_minute_bars_oldest_first(token):
    candles = expired_candles("NSE_FO|42650|08-09-2026", "2026-08-09", "2026-09-08", token)
    assert len(candles) > 5_000
    times = [candle.bar_time_ms for candle in candles]
    assert times == sorted(times)
    first_day = datetime.datetime.fromtimestamp(times[0] / 1000, IST).date()
    assert first_day <= datetime.date(2026, 8, 14)
