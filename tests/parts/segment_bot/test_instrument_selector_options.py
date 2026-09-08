"""Tests for instrument-selector's option-registration path. Real Upstox
shapes, matching tests/runtime/test_atm_strike_tracker.py's fixtures
(RL-063)."""

import pytest

from parts.segment_bot.instrument_selector import CHOSEN, OPTION, InstrumentSelector
from runtime.brokers.broker_adapter import BrokerOptionGreeks, InstrumentListing
from runtime.learned_estimator import Estimate
from runtime.price_staleness import PriceStalenessEstimator
from runtime.trade_intent import LONG, OPEN, SHORT, SOLE_OPINION, TradeIntent

VENUE = "upstox"
NIFTY_UNDERLYING = InstrumentListing(
    instrument_key="NSE_INDEX|Nifty 50", exchange="NSE", segment="NSE_INDEX",
    instrument_type="INDEX", trading_symbol="NIFTY", lot_size=None, tick_size=None,
    freeze_quantity=None, expiry_ms=None, strike_price=None, underlying_key=None,
    intraday_margin_percent=None, intraday_leverage=None,
)


def _call(instrument_key, strike, expiry_ms):
    return InstrumentListing(
        instrument_key=instrument_key, exchange="NSE", segment="NSE_FO",
        instrument_type="CE", trading_symbol=f"NIFTY {strike:g} CE",
        lot_size=75, tick_size=0.05, freeze_quantity=1800.0, expiry_ms=expiry_ms,
        strike_price=strike, underlying_key="NSE_INDEX|Nifty 50",
        intraday_margin_percent=None, intraday_leverage=None,
    )


def _put(instrument_key, strike, expiry_ms):
    return InstrumentListing(
        instrument_key=instrument_key, exchange="NSE", segment="NSE_FO",
        instrument_type="PE", trading_symbol=f"NIFTY {strike:g} PE",
        lot_size=75, tick_size=0.05, freeze_quantity=1800.0, expiry_ms=expiry_ms,
        strike_price=strike, underlying_key="NSE_INDEX|Nifty 50",
        intraday_margin_percent=None, intraday_leverage=None,
    )


def _greeks(instrument_key, delta):
    return BrokerOptionGreeks(
        instrument_key=instrument_key, delta=delta, theta=-2.0, gamma=0.001,
        vega=5.0, rho=0.5, implied_volatility=0.15, broker_time_ns=1_740_000_000_000_000_000,
    )


def a_selector():
    return InstrumentSelector(
        built_segments=("index-options",),
        maximum_cost_fraction=0.5,
        round_trip_cost_fraction=0.001,
        price_staleness=PriceStalenessEstimator(
            materiality_fraction=0.001, anchor_seconds=1.0, quantile=0.95,
            window=50, observations_needed=5, prior_one_second_move=0.0005,
            minimum_age_seconds=0.5, maximum_age_seconds=120.0,
        ),
        now_ns=lambda: 1_740_000_000_000_000_000,
    )


def an_intent(side, symbol="NIFTY", horizon=3600.0):
    """A real TradeIntent (runtime/trade_intent.py) -- venue_id, symbol, side,
    action, conviction, horizon_seconds, stop_price, agreement,
    contributing_bots, dissenting_bots, opinion_weights, evidence, reason,
    formed_at_ns are its actual fields; is_long/is_short are properties
    derived from side, not constructor arguments."""
    return TradeIntent(
        venue_id=VENUE, symbol=symbol, side=side, action=OPEN,
        conviction=Estimate(
            value=0.7, is_fitted=True, observations=50, prior=0.5,
            was_clamped=False, bound_low=None, bound_high=None, reason="test",
        ),
        horizon_seconds=horizon, stop_price=None, agreement=SOLE_OPINION,
        contributing_bots=("bull-bot",), dissenting_bots=(), opinion_weights={},
        evidence={}, reason="test intent", formed_at_ns=1_740_000_000_000_000_000,
    )


def a_long_intent(symbol="NIFTY", horizon=3600.0):
    return an_intent(LONG, symbol=symbol, horizon=horizon)


def test_a_long_intent_is_carried_by_the_atm_call():
    selector = a_selector()
    selector.observe_option_listing(NIFTY_UNDERLYING)
    selector.observe_option_listing(_call("NSE_FO|1001", 24500.0, 1_740_100_000_000))
    selector.observe_option_greeks(_greeks("NSE_FO|1001", 0.51))
    selector.observe_price(VENUE, "NIFTY", 24500.0, 1_740_000_000_000_000_000)
    selector.observe_option_price("NSE_FO|1001", 220.0, 1_740_000_000_000_000_000)

    choice = selector.select(a_long_intent(), now_ns=1_740_000_000_000_000_000)
    assert choice.state == CHOSEN
    assert choice.chosen.instrument_kind == OPTION
    assert choice.chosen.contract_symbol == "NIFTY 24500 CE"
    assert choice.chosen.premium_fraction == pytest.approx(220.0 / 24500.0)


def test_a_short_intent_is_never_carried_by_a_call():
    """The gap this plan closes: buying a call cannot express a bearish view."""
    selector = a_selector()
    selector.observe_option_listing(NIFTY_UNDERLYING)
    selector.observe_option_listing(_call("NSE_FO|1001", 24500.0, 1_740_100_000_000))
    selector.observe_option_greeks(_greeks("NSE_FO|1001", 0.51))
    selector.observe_price(VENUE, "NIFTY", 24500.0, 1_740_000_000_000_000_000)
    selector.observe_option_price("NSE_FO|1001", 220.0, 1_740_000_000_000_000_000)

    choice = selector.select(an_intent(SHORT), now_ns=1_740_000_000_000_000_000)
    assert choice.chosen is None
    assert "NIFTY 24500 CE" in choice.rejected
    assert choice.rejected["NIFTY 24500 CE"] == "it cannot be sold short"


def test_a_strike_that_stops_being_atm_is_evicted_not_accumulated():
    """observe_listed_instrument only replaces a same-contract_symbol entry;
    a strike that stops being ATM needs its own eviction, or every strike
    the underlying ever passed through stays a candidate forever."""
    selector = a_selector()
    selector.observe_option_listing(NIFTY_UNDERLYING)
    selector.observe_option_listing(_call("NSE_FO|1001", 24500.0, 1_740_100_000_000))
    selector.observe_option_listing(_call("NSE_FO|1002", 24600.0, 1_740_100_000_000))
    selector.observe_option_greeks(_greeks("NSE_FO|1001", 0.51))
    selector.observe_option_greeks(_greeks("NSE_FO|1002", 0.20))
    assert [i.contract_symbol for i in selector._listed[(VENUE, "NIFTY")]] == ["NIFTY 24500 CE"]

    # The underlying rallies; 24600 is now closer to 0.5 delta than 24500.
    selector.observe_option_greeks(_greeks("NSE_FO|1001", 0.85))
    selector.observe_option_greeks(_greeks("NSE_FO|1002", 0.51))
    symbols = [i.contract_symbol for i in selector._listed[(VENUE, "NIFTY")]]
    assert symbols == ["NIFTY 24600 CE"], symbols


def test_a_short_intent_is_carried_by_the_atm_put():
    selector = a_selector()
    selector.observe_option_listing(NIFTY_UNDERLYING)
    selector.observe_option_listing(_put("NSE_FO|2001", 24500.0, 1_740_100_000_000))
    selector.observe_option_greeks(_greeks("NSE_FO|2001", -0.51))
    selector.observe_price(VENUE, "NIFTY", 24500.0, 1_740_000_000_000_000_000)
    selector.observe_option_price("NSE_FO|2001", 210.0, 1_740_000_000_000_000_000)

    choice = selector.select(an_intent(SHORT), now_ns=1_740_000_000_000_000_000)
    assert choice.state == CHOSEN
    assert choice.chosen.contract_symbol == "NIFTY 24500 PE"


def test_a_long_intent_is_never_carried_by_a_put():
    selector = a_selector()
    selector.observe_option_listing(NIFTY_UNDERLYING)
    selector.observe_option_listing(_put("NSE_FO|2001", 24500.0, 1_740_100_000_000))
    selector.observe_option_greeks(_greeks("NSE_FO|2001", -0.51))
    selector.observe_price(VENUE, "NIFTY", 24500.0, 1_740_000_000_000_000_000)
    selector.observe_option_price("NSE_FO|2001", 210.0, 1_740_000_000_000_000_000)

    choice = selector.select(a_long_intent(), now_ns=1_740_000_000_000_000_000)
    assert choice.chosen is None
    assert choice.rejected["NIFTY 24500 PE"] == "it cannot express a long view"


def test_an_intent_naming_a_far_otm_contract_is_carried_by_that_contract_not_the_atm_pair():
    """expiry-day-zero-to-hero-detector names a strike *because* it is cheap
    and far from 0.5 delta -- the exact shape atm_call_for could never
    return. Before this, select() resolved a contract-named intent to its
    underlying and picked only from the registered ATM pair, silently
    substituting the opposite bet (highest premium, ~0.5 delta) for the one
    the intent actually named."""
    selector = a_selector()
    selector.observe_option_listing(NIFTY_UNDERLYING)
    selector.observe_option_listing(_call("NSE_FO|1001", 24500.0, 1_740_100_000_000))
    selector.observe_option_listing(_call("NSE_FO|9001", 26000.0, 1_740_100_000_000))
    selector.observe_option_greeks(_greeks("NSE_FO|1001", 0.51))
    selector.observe_option_greeks(_greeks("NSE_FO|9001", 0.03))
    selector.observe_price(VENUE, "NIFTY", 24500.0, 1_740_000_000_000_000_000)
    selector.observe_option_price("NSE_FO|1001", 220.0, 1_740_000_000_000_000_000)
    selector.observe_option_price("NSE_FO|9001", 8.0, 1_740_000_000_000_000_000)

    choice = selector.select(
        a_long_intent(symbol="NIFTY 26000 CE"), now_ns=1_740_000_000_000_000_000
    )
    assert choice.state == CHOSEN
    assert choice.chosen.contract_symbol == "NIFTY 26000 CE"
    assert choice.chosen.premium_fraction == pytest.approx(8.0 / 24500.0)


def test_the_atm_pair_stays_selectable_alongside_a_named_far_otm_contract():
    """Registering the named contract does not evict the ATM entry -- both
    stay candidates, and the existing cheapest-cost rule (unchanged by this
    fix) picks between them same as it always has between the ATM call and
    put."""
    selector = a_selector()
    selector.observe_option_listing(NIFTY_UNDERLYING)
    selector.observe_option_listing(_call("NSE_FO|1001", 24500.0, 1_740_100_000_000))
    selector.observe_option_listing(_call("NSE_FO|9001", 26000.0, 1_740_100_000_000))
    selector.observe_option_greeks(_greeks("NSE_FO|1001", 0.51))
    selector.observe_option_greeks(_greeks("NSE_FO|9001", 0.03))
    selector.observe_price(VENUE, "NIFTY", 24500.0, 1_740_000_000_000_000_000)
    selector.observe_option_price("NSE_FO|1001", 220.0, 1_740_000_000_000_000_000)
    selector.observe_option_price("NSE_FO|9001", 8.0, 1_740_000_000_000_000_000)

    selector.select(a_long_intent(symbol="NIFTY 26000 CE"), now_ns=1_740_000_000_000_000_000)
    symbols = {i.contract_symbol for i in selector._listed[(VENUE, "NIFTY")]}
    assert symbols == {"NIFTY 24500 CE", "NIFTY 26000 CE"}, symbols


def test_a_named_contract_with_no_delta_yet_falls_back_to_whatever_is_listed():
    """Mirrors contract_by_symbol's own refusal: a contract this part has not
    yet seen greeks for cannot be registered, so the ATM entries -- if any --
    are what select() has to work with, same as before this fix."""
    selector = a_selector()
    selector.observe_option_listing(NIFTY_UNDERLYING)
    selector.observe_option_listing(_call("NSE_FO|1001", 24500.0, 1_740_100_000_000))
    selector.observe_option_listing(_call("NSE_FO|9001", 26000.0, 1_740_100_000_000))
    selector.observe_option_greeks(_greeks("NSE_FO|1001", 0.51))
    # No greeks observed yet for NSE_FO|9001.
    selector.observe_price(VENUE, "NIFTY", 24500.0, 1_740_000_000_000_000_000)
    selector.observe_option_price("NSE_FO|1001", 220.0, 1_740_000_000_000_000_000)

    choice = selector.select(
        a_long_intent(symbol="NIFTY 26000 CE"), now_ns=1_740_000_000_000_000_000
    )
    assert choice.state == CHOSEN
    assert choice.chosen.contract_symbol == "NIFTY 24500 CE"


def test_no_price_yet_leaves_the_option_honestly_unpriceable():
    selector = a_selector()
    selector.observe_option_listing(NIFTY_UNDERLYING)
    selector.observe_option_listing(_call("NSE_FO|1001", 24500.0, 1_740_100_000_000))
    selector.observe_option_greeks(_greeks("NSE_FO|1001", 0.51))
    selector.observe_price(VENUE, "NIFTY", 24500.0, 1_740_000_000_000_000_000)
    # No observe_option_price call -- premium_fraction stays None.

    choice = selector.select(a_long_intent(), now_ns=1_740_000_000_000_000_000)
    assert choice.chosen is None
    assert "carry could not be priced" in choice.rejected["NIFTY 24500 CE"]
