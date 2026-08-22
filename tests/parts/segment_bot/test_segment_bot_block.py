"""instrument-selector: how an intent gets expressed, and what it says when it cannot.

The tests that matter here are the ones about honesty. This part sits where a
build-order decision becomes a trading decision, and the failure that would be
invisible is falling through to the instrument the system happens to have built
and recording it as a choice.
"""

import importlib

import pytest

from parts.segment_bot.instrument_selector import (
    BEST_IS_IN_AN_UNBUILT_SEGMENT, CHOSEN, DATED_FUTURE, NONE_CAN_CARRY_THE_INTENT,
    NONE_LIQUID_ENOUGH, NOTHING_AVAILABLE, OPTION, PERPETUAL_FUTURE, SPOT,
    InstrumentSelector, ListedInstrument,
)
from runtime.part_declaration import load_declaration_from_blueprint

VENUE = "binance-usdm"
SYMBOL = "BTCUSDT"


class Intent:
    """A trade intent, as this part reads it."""

    def __init__(
        self, is_long=True, notional=10_000.0, horizon_seconds=3600.0,
        needs_convexity=False, fill_within_seconds=None,
    ):
        self.venue_id, self.symbol = VENUE, SYMBOL
        self.is_long = is_long
        self.notional_quote = notional
        self.horizon_seconds = horizon_seconds
        self.needs_convexity = needs_convexity
        self.fill_within_seconds = fill_within_seconds


def a_perpetual(funding=0.0001, cost=0.0005, absorbable=1_000_000.0, seconds_to_fill=1.0):
    return ListedInstrument(
        venue_id=VENUE, symbol=SYMBOL, instrument_kind=PERPETUAL_FUTURE,
        contract_symbol="BTCUSDT-PERP", funding_rate_per_settlement=funding,
        settlements_per_day=3.0, basis_fraction=None, seconds_to_expiry=None,
        supports_short=True, supports_convexity=False, round_trip_cost_fraction=cost,
        absorbable_quote=absorbable, seconds_to_fill=seconds_to_fill, premium_fraction=None,
    )


def a_dated_future(basis=0.01, seconds_to_expiry=30 * 86400.0, cost=0.0006):
    return ListedInstrument(
        venue_id=VENUE, symbol=SYMBOL, instrument_kind=DATED_FUTURE,
        contract_symbol="BTCUSDT-0926", funding_rate_per_settlement=None,
        settlements_per_day=None, basis_fraction=basis, premium_fraction=None,
        seconds_to_expiry=seconds_to_expiry, supports_short=True, supports_convexity=False,
        round_trip_cost_fraction=cost, absorbable_quote=1_000_000.0, seconds_to_fill=2.0,
    )


def a_spot(cost=0.001, supports_short=False):
    return ListedInstrument(
        venue_id=VENUE, symbol=SYMBOL, instrument_kind=SPOT, contract_symbol="BTCUSDT-SPOT",
        funding_rate_per_settlement=None, settlements_per_day=None, basis_fraction=None,
        premium_fraction=None, seconds_to_expiry=None, supports_short=supports_short,
        supports_convexity=False, round_trip_cost_fraction=cost,
        absorbable_quote=1_000_000.0, seconds_to_fill=1.0,
    )


def an_option(cost=0.002, premium=0.02, seconds_to_expiry=7 * 86400.0):
    return ListedInstrument(
        venue_id=VENUE, symbol=SYMBOL, instrument_kind=OPTION, contract_symbol="BTC-C-80000",
        funding_rate_per_settlement=None, settlements_per_day=None, basis_fraction=None,
        premium_fraction=premium, seconds_to_expiry=seconds_to_expiry,
        supports_short=True, supports_convexity=True, round_trip_cost_fraction=cost,
        absorbable_quote=500_000.0, seconds_to_fill=5.0,
    )


def a_selector(built=("futures",), maximum_cost=0.05):
    return InstrumentSelector(built_segments=built, maximum_cost_fraction=maximum_cost)


def test_the_built_declaration_equals_the_blueprint():
    module = importlib.import_module("parts.segment_bot.instrument_selector")
    assert module.PART_DECLARATION == load_declaration_from_blueprint("instrument-selector")


def test_a_symbol_with_nothing_listed_expresses_nothing():
    choice = a_selector().select(Intent())
    assert choice.state == NOTHING_AVAILABLE
    assert choice.is_actionable is False


def test_the_horizon_decides_between_a_perpetual_and_a_dated_future():
    """An hour and a month have opposite answers, and neither is a preference."""
    subject = a_selector()
    subject.observe_listed_instrument(a_perpetual(funding=0.0005))
    subject.observe_listed_instrument(a_dated_future(basis=0.01, seconds_to_expiry=30 * 86400.0))

    brief = subject.select(Intent(horizon_seconds=3600.0))
    assert brief.chosen.instrument_kind == PERPETUAL_FUTURE

    extended = subject.select(Intent(horizon_seconds=20 * 86400.0))
    assert extended.chosen.instrument_kind == DATED_FUTURE


def test_carry_is_priced_over_the_intents_own_horizon():
    subject = a_selector()
    perpetual = a_perpetual(funding=0.001)
    assert subject.carry_over(perpetual, horizon_seconds=86400.0) == pytest.approx(0.003)
    assert subject.carry_over(perpetual, horizon_seconds=86400.0 / 3) == pytest.approx(0.001)


def test_a_dated_futures_basis_is_pro_rated_over_the_life_it_is_held_for():
    subject = a_selector()
    dated = a_dated_future(basis=0.012, seconds_to_expiry=30 * 86400.0)
    assert subject.carry_over(dated, horizon_seconds=15 * 86400.0) == pytest.approx(0.006)
    assert subject.carry_over(dated, horizon_seconds=60 * 86400.0) == pytest.approx(0.012)


def test_carry_is_a_credit_to_a_short_and_a_cost_to_a_long():
    subject = a_selector()
    subject.observe_listed_instrument(a_perpetual(funding=0.002, cost=0.0005))
    long_side = subject.select(Intent(is_long=True, horizon_seconds=86400.0))
    short_side = subject.select(Intent(is_long=False, horizon_seconds=86400.0))
    assert long_side.carry_cost_fraction > 0 > short_side.carry_cost_fraction
    assert short_side.total_cost_fraction < long_side.total_cost_fraction


def test_an_instrument_that_cannot_express_the_intent_is_not_a_worse_choice():
    """A short cannot be expressed in spot without borrow. That is not a price."""
    subject = a_selector(built=("futures", "spot"))
    subject.observe_listed_instrument(a_spot(cost=0.0001, supports_short=False))
    choice = subject.select(Intent(is_long=False))
    assert choice.state == NONE_CAN_CARRY_THE_INTENT
    assert "cannot be sold short" in choice.rejected["BTCUSDT-SPOT"]


def test_a_convexity_view_needs_an_option():
    subject = a_selector(built=("futures", "options"))
    subject.observe_listed_instrument(a_perpetual())
    subject.observe_listed_instrument(an_option())
    choice = subject.select(Intent(needs_convexity=True))
    assert choice.chosen.instrument_kind == OPTION


def test_the_best_instrument_being_in_an_unbuilt_segment_is_reported_not_skipped():
    """Falling through would record a futures decision nobody made."""
    subject = a_selector(built=("futures",))
    subject.observe_listed_instrument(a_perpetual(funding=0.0001, cost=0.0005))
    subject.observe_listed_instrument(a_spot(cost=0.0001))
    choice = subject.select(Intent(horizon_seconds=30 * 86400.0))
    assert choice.chosen.instrument_kind == PERPETUAL_FUTURE
    assert choice.unbuilt_segment_would_have_won == "spot"
    assert "unbuilt spot segment" in choice.reason
    assert subject.standing.unbuilt_segment_wins["spot"] == 1


def test_an_options_carry_is_its_time_value_and_decays_with_the_square_root_of_time():
    """A week out of a month costs far less than a quarter of the premium."""
    subject = a_selector(built=("futures", "options"))
    option = an_option(premium=0.02, seconds_to_expiry=30 * 86400.0)
    week = subject.carry_over(option, horizon_seconds=7 * 86400.0)
    assert 0 < week < 0.02 * 0.25
    assert subject.carry_over(option, horizon_seconds=30 * 86400.0) == pytest.approx(0.02)


def test_an_option_with_no_premium_from_the_surface_cannot_be_priced():
    subject = a_selector(built=("futures", "options"))
    subject.observe_listed_instrument(an_option(premium=None))
    choice = subject.select(Intent(needs_convexity=True))
    assert "implied-vol surface" in choice.rejected["BTC-C-80000"]


def test_an_options_premium_is_paid_whichever_way_the_intent_points():
    """Unlike funding, it is not a credit to the other side."""
    subject = a_selector(built=("futures", "options"))
    subject.observe_listed_instrument(an_option())
    long_side = subject.select(Intent(needs_convexity=True, is_long=True))
    short_side = subject.select(Intent(needs_convexity=True, is_long=False))
    assert long_side.carry_cost_fraction > 0
    assert short_side.carry_cost_fraction > 0


def test_an_intent_only_expressible_in_an_unbuilt_segment_chooses_nothing():
    subject = a_selector(built=("futures",))
    subject.observe_listed_instrument(an_option())
    choice = subject.select(Intent(needs_convexity=True))
    assert choice.state == BEST_IS_IN_AN_UNBUILT_SEGMENT
    assert choice.is_actionable is False
    assert "not built" in choice.reason


def test_liquidity_is_judged_at_the_size_actually_asked_for():
    """The tightest instrument at one size is not the tightest at fifty times it."""
    subject = a_selector()
    subject.observe_listed_instrument(a_perpetual(cost=0.0005, absorbable=20_000.0))
    subject.observe_listed_instrument(a_dated_future(cost=0.002, basis=0.0))
    small = subject.select(Intent(notional=10_000.0, horizon_seconds=3600.0))
    large = subject.select(Intent(notional=500_000.0, horizon_seconds=3600.0))
    assert small.chosen.instrument_kind == PERPETUAL_FUTURE
    assert large.chosen.instrument_kind == DATED_FUTURE


def test_a_timed_intent_narrows_the_field_rather_than_changing_it():
    subject = a_selector()
    subject.observe_listed_instrument(a_perpetual(cost=0.0005, seconds_to_fill=30.0))
    subject.observe_listed_instrument(a_dated_future(cost=0.002, basis=0.0))
    unhurried = subject.select(Intent(horizon_seconds=3600.0))
    urgent = subject.select(Intent(horizon_seconds=3600.0, fill_within_seconds=5.0))
    assert unhurried.chosen.instrument_kind == PERPETUAL_FUTURE
    assert urgent.chosen.instrument_kind == DATED_FUTURE
    assert "30s to fill" in urgent.rejected["BTCUSDT-PERP"]


def test_a_dated_future_expiring_inside_the_horizon_cannot_carry_the_intent():
    subject = a_selector()
    subject.observe_listed_instrument(a_dated_future(seconds_to_expiry=3600.0))
    choice = subject.select(Intent(horizon_seconds=30 * 86400.0))
    assert choice.state == NONE_CAN_CARRY_THE_INTENT
    assert "expires in" in choice.rejected["BTCUSDT-0926"]


def test_an_intent_too_expensive_to_express_is_refused():
    subject = a_selector(maximum_cost=0.001)
    subject.observe_listed_instrument(a_perpetual(funding=0.05, cost=0.01))
    choice = subject.select(Intent(horizon_seconds=86400.0))
    assert choice.state == NONE_LIQUID_ENOUGH
    assert choice.is_actionable is False


def test_an_instrument_whose_carry_cannot_be_priced_is_rejected_by_name():
    subject = a_selector()
    unpriceable = ListedInstrument(
        venue_id=VENUE, symbol=SYMBOL, instrument_kind=PERPETUAL_FUTURE,
        contract_symbol="BTCUSDT-PERP", funding_rate_per_settlement=None,
        settlements_per_day=None, basis_fraction=None, premium_fraction=None,
        seconds_to_expiry=None, supports_short=True, supports_convexity=False,
        round_trip_cost_fraction=0.0005, absorbable_quote=1_000_000.0, seconds_to_fill=1.0,
    )
    subject.observe_listed_instrument(unpriceable)
    choice = subject.select(Intent())
    assert choice.state == NONE_CAN_CARRY_THE_INTENT
    assert "carry could not be priced" in choice.rejected["BTCUSDT-PERP"]


def test_relisting_an_instrument_replaces_it_rather_than_duplicating_it():
    subject = a_selector()
    subject.observe_listed_instrument(a_perpetual(funding=0.01))
    subject.observe_listed_instrument(a_perpetual(funding=0.0001))
    choice = subject.select(Intent(horizon_seconds=86400.0))
    assert choice.considered == 1
    assert choice.state == CHOSEN


def test_a_selector_with_no_built_segment_is_refused_at_construction():
    with pytest.raises(ValueError):
        InstrumentSelector(built_segments=(), maximum_cost_fraction=0.05)
