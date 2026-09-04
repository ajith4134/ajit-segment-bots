"""expiry-day-zero-to-hero-detector, tested against constructed instrument/
feed data -- RL-063 permits constructing a series to test that an estimator
does arithmetic; no live Indian option-chain data has been captured yet, so
this is the honest boundary until it has (spec section 4)."""

import collections
import datetime
import pathlib

import pytest

from parts.opportunity_scanner.expiry_day_zero_to_hero_detector import (
    NOT_AN_OPTION, NOT_EXPIRY_DAY, NOT_FAR_ENOUGH_OTM, NO_DELTA, NO_LISTING,
    NO_PREMIUM, PREMIUM_TOO_HIGH, ZeroToHeroDetector,
)
from runtime.brokers.broker_adapter import (
    BrokerOptionGreeks, InstrumentListing, LtpUpdate,
)
from runtime.market_signal import LONG, SHORT, SignalCalibrator

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
TODAY = datetime.datetime(2026, 9, 1, 10, 0, tzinfo=IST)
TODAY_NS = int(TODAY.timestamp() * 1e9)
TOMORROW_EXPIRY_MS = int(datetime.datetime(2026, 9, 2, 15, 30, tzinfo=IST).timestamp() * 1000)
TODAY_EXPIRY_MS = int(datetime.datetime(2026, 9, 1, 15, 30, tzinfo=IST).timestamp() * 1000)

CALL_KEY = "NSE_FO|CALL1"
PUT_KEY = "NSE_FO|PUT1"


def a_listing(instrument_key, expiry_ms, instrument_type="CE", strike=25000.0):
    return InstrumentListing(
        instrument_key=instrument_key, exchange="NSE", segment="NSE_FO",
        instrument_type=instrument_type, trading_symbol=instrument_key,
        lot_size=50, tick_size=0.05, freeze_quantity=None,
        expiry_ms=expiry_ms, strike_price=strike, underlying_key="NSE_INDEX|Nifty 50",
        intraday_margin_percent=None, intraday_leverage=None,
    )


def calibrator():
    return SignalCalibrator(
        prior_hit_rate=0.1, prior_weight=10.0, half_life_observations=50.0,
        minimum_observations=5,
    )


def a_detector(maximum_premium=5.0, maximum_abs_delta=0.10, now_ns=lambda: TODAY_NS):
    return ZeroToHeroDetector(
        maximum_premium=maximum_premium, maximum_abs_delta=maximum_abs_delta,
        horizon_seconds=3600.0, calibrator=calibrator(), now_ns=now_ns,
    )


def test_the_real_nse_master_is_swept_by_expiry_day_only():
    """The same scoping, against the instrument master the broker actually sent.

    RL-063: the shape here is the real one -- 196 rows, two of them INDEX
    listings carrying no expiry, 194 option contracts across two expiry dates --
    read by the venue's own adapter rather than by a parser written for this test.
    On the live spine the same file arrives at 102,940 rows.
    """
    import json as _json

    from runtime.brokers.upstox import UpstoxAdapter

    captured = (
        pathlib.Path(__file__).resolve().parents[3]
        / "tests/captured/upstox/2026-09-04-nse-instrument-master-nifty-slice.json"
    )
    adapter = UpstoxAdapter.__new__(UpstoxAdapter)
    listings = tuple(
        UpstoxAdapter.read_instrument_listings(adapter, _json.loads(captured.read_text()))
    )

    subject = a_detector()
    for listing in listings:
        subject.observe_listing(listing)

    by_day = collections.Counter(
        datetime.datetime.fromtimestamp(listing.expiry_ms / 1000, tz=IST).date()
        for listing in listings
        if listing.expiry_ms is not None
    )
    assert len(by_day) > 1, "the captured master holds only one expiry, so scoping is untested"

    for day, expected in by_day.items():
        noon = int(
            datetime.datetime(day.year, day.month, day.day, 12, 0, tzinfo=IST).timestamp() * 1e9
        )
        swept = subject.expiring_on(noon)
        assert len(swept) == expected
        # Never the whole master, which is what the sweep used to walk.
        assert len(swept) < len(listings)

    # The index listings carry no expiry and can never be candidates.
    assert subject.standing.instruments_that_are_not_options == sum(
        1 for listing in listings if listing.expiry_ms is None
    )


def test_only_the_contracts_expiring_today_are_swept():
    """0.957 of a core, 28% of the spine, measured 2026-09-04.

    The tick judged every instrument the catalogue carries -- 102,940 of them --
    on every wake, and `_is_expiry_today` converted two timestamps to dates for
    each one: about 200,000 date conversions a tick to reach the answer it had
    already reached the tick before. An expiry date is a property of the contract
    and cannot change between ticks, so it is decided once, when the listing
    arrives.
    """
    subject = a_detector()
    subject.observe_listing(a_listing(CALL_KEY, TODAY_EXPIRY_MS, "CE"))
    subject.observe_listing(a_listing(PUT_KEY, TODAY_EXPIRY_MS, "PE"))
    for index in range(500):
        subject.observe_listing(a_listing(f"NSE_FO|LATER{index}", TOMORROW_EXPIRY_MS, "CE"))
    # An index carries no expiry: not an option, and never a candidate.
    subject.observe_listing(a_listing("NSE_INDEX|Nifty 50", None, "INDEX"))

    assert subject.expiring_on(TODAY_NS) == (CALL_KEY, PUT_KEY)
    assert subject.standing.instruments_known == 503
    assert subject.standing.instruments_expiring_today == 2
    assert subject.standing.instruments_that_are_not_options == 1


def test_a_listing_restated_by_the_catalogue_is_not_filed_twice():
    """`broker-instrument-catalogue-reader` restates all 102,940 listings, repeatedly."""
    subject = a_detector()
    for _ in range(50):
        subject.observe_listing(a_listing(CALL_KEY, TODAY_EXPIRY_MS, "CE"))

    assert subject.expiring_on(TODAY_NS) == (CALL_KEY,)
    assert subject.standing.instruments_expiring_today == 1


def test_a_contract_whose_expiry_is_corrected_moves_to_the_right_day():
    """Filing at observe time must not strand a key under a date it no longer has."""
    subject = a_detector()
    subject.observe_listing(a_listing(CALL_KEY, TOMORROW_EXPIRY_MS, "CE"))
    assert subject.expiring_on(TODAY_NS) == ()

    subject.observe_listing(a_listing(CALL_KEY, TODAY_EXPIRY_MS, "CE"))
    assert subject.expiring_on(TODAY_NS) == (CALL_KEY,)

    subject.observe_listing(a_listing(CALL_KEY, TOMORROW_EXPIRY_MS, "CE"))
    assert subject.expiring_on(TODAY_NS) == ()


def test_expiry_dates_that_have_passed_are_dropped_rather_than_kept_forever():
    """One bucket per expiry ever seen is the unbounded shape this project keeps paying for."""
    subject = a_detector()
    subject.observe_listing(a_listing(CALL_KEY, TODAY_EXPIRY_MS, "CE"))
    assert subject.expiring_on(TODAY_NS) == (CALL_KEY,)

    the_day_after = int(
        datetime.datetime(2026, 9, 3, 10, 0, tzinfo=IST).timestamp() * 1e9
    )
    assert subject.expiring_on(the_day_after) == ()
    assert subject._keys_by_expiry_date == {}


def test_a_cheap_far_otm_call_expiring_today_fires_long():
    subject = a_detector()
    subject.observe_listing(a_listing(CALL_KEY, TODAY_EXPIRY_MS, "CE"))
    subject.observe_ltp(LtpUpdate(
        instrument_key=CALL_KEY, last_traded_price=2.5, last_traded_quantity=50.0,
        last_traded_time_ms=1, close_price=None, broker_time_ns=TODAY_NS,
    ))
    subject.observe_greeks(BrokerOptionGreeks(
        instrument_key=CALL_KEY, delta=0.04, theta=-1.0, gamma=0.001,
        vega=0.5, rho=0.1, implied_volatility=0.3, broker_time_ns=TODAY_NS,
    ))
    candidate, reason = subject.detect(CALL_KEY)
    assert candidate is not None
    assert candidate.direction == LONG
    assert candidate.symbol == CALL_KEY


def test_a_cheap_far_otm_put_expiring_today_fires_short():
    subject = a_detector()
    subject.observe_listing(a_listing(PUT_KEY, TODAY_EXPIRY_MS, "PE"))
    subject.observe_ltp(LtpUpdate(
        instrument_key=PUT_KEY, last_traded_price=1.0, last_traded_quantity=50.0,
        last_traded_time_ms=1, close_price=None, broker_time_ns=TODAY_NS,
    ))
    subject.observe_greeks(BrokerOptionGreeks(
        instrument_key=PUT_KEY, delta=-0.03, theta=-1.0, gamma=0.001,
        vega=0.5, rho=0.1, implied_volatility=0.3, broker_time_ns=TODAY_NS,
    ))
    candidate, reason = subject.detect(PUT_KEY)
    assert candidate is not None
    assert candidate.direction == SHORT


def test_not_expiring_today_refuses():
    subject = a_detector()
    subject.observe_listing(a_listing(CALL_KEY, TOMORROW_EXPIRY_MS))
    subject.observe_ltp(LtpUpdate(
        instrument_key=CALL_KEY, last_traded_price=2.5, last_traded_quantity=50.0,
        last_traded_time_ms=1, close_price=None, broker_time_ns=TODAY_NS,
    ))
    subject.observe_greeks(BrokerOptionGreeks(
        instrument_key=CALL_KEY, delta=0.04, theta=-1.0, gamma=0.001,
        vega=0.5, rho=0.1, implied_volatility=0.3, broker_time_ns=TODAY_NS,
    ))
    candidate, outcome = subject.detect(CALL_KEY)
    assert candidate is None
    assert outcome == NOT_EXPIRY_DAY


def test_an_underlying_index_listing_refuses_by_name_not_a_crash():
    """The live spine crashed on this 2026-09-02: broker-instrument-listing
    carries every tracked instrument, underlyings included, and an INDEX
    listing's expiry_ms is None (it is not an option contract at all) --
    _is_expiry_today crashed trying to divide None by 1000 the moment a real
    NIFTY 50 index listing reached this detector."""
    subject = a_detector()
    subject.observe_listing(
        InstrumentListing(
            instrument_key="NSE_INDEX|Nifty 50", exchange="NSE", segment="NSE_INDEX",
            instrument_type="INDEX", trading_symbol="NIFTY", lot_size=None, tick_size=None,
            freeze_quantity=None, expiry_ms=None, strike_price=None, underlying_key=None,
            intraday_margin_percent=None, intraday_leverage=None,
        )
    )
    candidate, outcome = subject.detect("NSE_INDEX|Nifty 50")
    assert candidate is None
    assert outcome == NOT_AN_OPTION


def test_a_premium_above_the_cutoff_refuses():
    subject = a_detector(maximum_premium=5.0)
    subject.observe_listing(a_listing(CALL_KEY, TODAY_EXPIRY_MS))
    subject.observe_ltp(LtpUpdate(
        instrument_key=CALL_KEY, last_traded_price=50.0, last_traded_quantity=50.0,
        last_traded_time_ms=1, close_price=None, broker_time_ns=TODAY_NS,
    ))
    subject.observe_greeks(BrokerOptionGreeks(
        instrument_key=CALL_KEY, delta=0.04, theta=-1.0, gamma=0.001,
        vega=0.5, rho=0.1, implied_volatility=0.3, broker_time_ns=TODAY_NS,
    ))
    candidate, outcome = subject.detect(CALL_KEY)
    assert candidate is None
    assert outcome == PREMIUM_TOO_HIGH


def test_a_delta_too_close_to_the_money_refuses():
    subject = a_detector(maximum_abs_delta=0.10)
    subject.observe_listing(a_listing(CALL_KEY, TODAY_EXPIRY_MS))
    subject.observe_ltp(LtpUpdate(
        instrument_key=CALL_KEY, last_traded_price=2.5, last_traded_quantity=50.0,
        last_traded_time_ms=1, close_price=None, broker_time_ns=TODAY_NS,
    ))
    subject.observe_greeks(BrokerOptionGreeks(
        instrument_key=CALL_KEY, delta=0.45, theta=-1.0, gamma=0.001,
        vega=0.5, rho=0.1, implied_volatility=0.3, broker_time_ns=TODAY_NS,
    ))
    candidate, outcome = subject.detect(CALL_KEY)
    assert candidate is None
    assert outcome == NOT_FAR_ENOUGH_OTM


def test_an_unknown_instrument_refuses_by_name():
    subject = a_detector()
    candidate, outcome = subject.detect(CALL_KEY)
    assert candidate is None
    assert outcome == NO_LISTING


def test_a_listing_with_no_premium_yet_refuses_by_name():
    subject = a_detector()
    subject.observe_listing(a_listing(CALL_KEY, TODAY_EXPIRY_MS))
    subject.observe_greeks(BrokerOptionGreeks(
        instrument_key=CALL_KEY, delta=0.04, theta=-1.0, gamma=0.001,
        vega=0.5, rho=0.1, implied_volatility=0.3, broker_time_ns=TODAY_NS,
    ))
    candidate, outcome = subject.detect(CALL_KEY)
    assert candidate is None
    assert outcome == NO_PREMIUM


def test_a_listing_with_no_delta_yet_refuses_by_name():
    subject = a_detector()
    subject.observe_listing(a_listing(CALL_KEY, TODAY_EXPIRY_MS))
    subject.observe_ltp(LtpUpdate(
        instrument_key=CALL_KEY, last_traded_price=2.5, last_traded_quantity=50.0,
        last_traded_time_ms=1, close_price=None, broker_time_ns=TODAY_NS,
    ))
    candidate, outcome = subject.detect(CALL_KEY)
    assert candidate is None
    assert outcome == NO_DELTA


def test_cheaper_and_further_otm_scores_higher_signal_strength():
    subject = a_detector(maximum_premium=5.0, maximum_abs_delta=0.10)
    subject.observe_listing(a_listing(CALL_KEY, TODAY_EXPIRY_MS))
    subject.observe_ltp(LtpUpdate(
        instrument_key=CALL_KEY, last_traded_price=0.5, last_traded_quantity=50.0,
        last_traded_time_ms=1, close_price=None, broker_time_ns=TODAY_NS,
    ))
    subject.observe_greeks(BrokerOptionGreeks(
        instrument_key=CALL_KEY, delta=0.01, theta=-1.0, gamma=0.001,
        vega=0.5, rho=0.1, implied_volatility=0.3, broker_time_ns=TODAY_NS,
    ))
    cheap_candidate, _ = subject.detect(CALL_KEY)

    subject.observe_ltp(LtpUpdate(
        instrument_key=CALL_KEY, last_traded_price=4.5, last_traded_quantity=50.0,
        last_traded_time_ms=1, close_price=None, broker_time_ns=TODAY_NS,
    ))
    subject.observe_greeks(BrokerOptionGreeks(
        instrument_key=CALL_KEY, delta=0.09, theta=-1.0, gamma=0.001,
        vega=0.5, rho=0.1, implied_volatility=0.3, broker_time_ns=TODAY_NS,
    ))
    expensive_candidate, _ = subject.detect(CALL_KEY)

    assert cheap_candidate.signal_strength > expensive_candidate.signal_strength


def test_maximum_premium_must_be_positive():
    with pytest.raises(ValueError):
        ZeroToHeroDetector(
            maximum_premium=0.0, maximum_abs_delta=0.1, horizon_seconds=3600.0,
            calibrator=calibrator(),
        )


def test_maximum_abs_delta_must_be_between_zero_and_one():
    with pytest.raises(ValueError):
        ZeroToHeroDetector(
            maximum_premium=5.0, maximum_abs_delta=1.5, horizon_seconds=3600.0,
            calibrator=calibrator(),
        )
