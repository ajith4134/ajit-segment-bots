"""One past session's instruments, held the way the option segments hold them.

Real data only (RL-063): the session is 2026-09-04, NIFTY's contracts come from the
expired-instruments route (they expired 2026-09-08) and the index from Upstox's
ordinary history, both cached after the first run.
"""

from __future__ import annotations

import datetime

import pytest

from operate.historical_prints import NoBrokerToken, upstox_access_token
from operate.past_session_prints import past_session_instruments

DAY = "2026-09-04"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


@pytest.fixture(scope="module")
def instruments():
    try:
        upstox_access_token()
    except NoBrokerToken as missing:
        pytest.skip(str(missing))
    return past_session_instruments(DAY, ("NIFTY",), contracts_per_underlying=8, minimum_prints=60)


def _day_of(at_ns: int) -> str:
    return datetime.datetime.fromtimestamp(at_ns / 1e9, IST).date().isoformat()


def test_the_underlying_and_eight_contracts_are_held(instruments):
    underlyings = [i for i in instruments if i.option_type is None]
    contracts = [i for i in instruments if i.option_type is not None]
    assert [i.trading_symbol for i in underlyings] == ["NIFTY"]
    assert len(contracts) == 8
    assert {i.option_type for i in contracts} == {"CE", "PE"}


def test_the_contracts_are_the_nearest_expiry_that_had_not_passed(instruments):
    contracts = [i for i in instruments if i.option_type is not None]
    assert {i.expiry for i in contracts} == {"2026-09-08"}
    assert all(i.lot_size > 0 for i in contracts)


def test_every_print_is_inside_that_session(instruments):
    for instrument in instruments:
        assert len(instrument.prints) >= 60, instrument.trading_symbol
        assert {_day_of(at) for at, _ in instrument.prints} == {DAY}
        times = [at for at, _ in instrument.prints]
        assert times == sorted(times)
        last = datetime.datetime.fromtimestamp(times[-1] / 1e9, IST)
        assert (last.hour, last.minute) <= (15, 30)


def test_strikes_are_chosen_by_the_open_not_the_close(instruments):
    """Choosing by where the day ended would be picking contracts with hindsight."""
    index = next(i for i in instruments if i.option_type is None)
    opening = index.prints[0][1]
    contracts = [i for i in instruments if i.option_type is not None]
    strikes = sorted({i.strike for i in contracts})
    assert len(strikes) == 4
    widest = max(abs(strike - opening) for strike in strikes)
    step = min(b - a for a, b in zip(strikes, strikes[1:]))
    # The four strikes nearest the open, two either side, sit within two steps of it.
    assert widest <= 2 * step + step / 2
