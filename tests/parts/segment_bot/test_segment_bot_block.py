"""instrument-selector: how an intent gets expressed, and what it says when it cannot.

The tests that matter here are the ones about honesty. This part sits where a
build-order decision becomes a trading decision, and the failure that would be
invisible is falling through to the instrument the system happens to have built
and recording it as a choice.
"""

import importlib
import time

import pytest

from parts.segment_bot.instrument_selector import (
    BEST_IS_IN_AN_UNBUILT_SEGMENT, CHOSEN, DATED_FUTURE,
    NO_REFERENCE_PRICE_HAS_EVER_ARRIVED, NONE_CAN_CARRY_THE_INTENT,
    NONE_LIQUID_ENOUGH, NOTHING_AVAILABLE, OPTION, PERPETUAL_FUTURE,
    REFERENCE_PRICE_IS_TOO_OLD, SPOT, UPSTOX_VENUE_ID, InstrumentSelector,
    ListedInstrument,
)
from runtime.part_declaration import load_declaration_from_blueprint
from runtime.trading_types import BUY
from runtime.price_staleness import PriceStalenessEstimator
from runtime.symbol_universe import CapturableSymbol

VENUE = "binance-usdm"
SYMBOL = "BTCUSDT"
SECOND_NS = 1_000_000_000


class Intent:
    """A trade intent, as this part reads it."""

    def __init__(
        self, is_long=True, notional=10_000.0, horizon_seconds=3600.0,
        needs_convexity=False, fill_within_seconds=None, venue_id=VENUE, symbol=SYMBOL,
    ):
        self.venue_id, self.symbol = venue_id, symbol
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
    subject = a_selector(built=("futures", "index-options"))
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
    subject = a_selector(built=("futures", "index-options"))
    option = an_option(premium=0.02, seconds_to_expiry=30 * 86400.0)
    week = subject.carry_over(option, horizon_seconds=7 * 86400.0)
    assert 0 < week < 0.02 * 0.25
    assert subject.carry_over(option, horizon_seconds=30 * 86400.0) == pytest.approx(0.02)


def test_an_option_with_no_premium_from_the_surface_cannot_be_priced():
    subject = a_selector(built=("futures", "index-options"))
    subject.observe_listed_instrument(an_option(premium=None))
    choice = subject.select(Intent(needs_convexity=True))
    assert "implied-vol surface" in choice.rejected["BTC-C-80000"]


def test_an_options_premium_is_paid_whichever_way_the_intent_points():
    """Unlike funding, it is not a credit to the other side."""
    subject = a_selector(built=("futures", "index-options"))
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


# --- what the venue lists, read from the venue's own catalogue -----------------
#
# These run on the responses binance-usdm actually returned on 2026-08-22
# (RL-063). The universe entry is built the way symbol-catalogue-reader builds it,
# from the same three responses, so a change to either end fails here rather than
# at the part that would have refused every trade.

FUNDING_RATE_FIXTURE = "2026-08-22-funding-premiumIndex-subset.json"
FUNDING_INTERVAL_FIXTURE = "2026-08-22-funding-fundingInfo-subset.json"


def a_real_universe(read_captured_json):
    """`symbol-universe` for one venue, as its catalogue reader would publish it."""
    from parts.market_data_feed.symbol_catalogue_reader import select_capturable_symbols
    from runtime.venues.adapter_registry import load_venue_adapter

    adapter = load_venue_adapter(VENUE)
    catalogue = read_captured_json(VENUE, "2026-08-22-catalogue-subset.json")
    tickers = read_captured_json(VENUE, "2026-08-22-ticker-24h-subset.json")
    listings = adapter.read_symbol_listings(catalogue)
    return select_capturable_symbols(
        adapter=adapter,
        listings=listings,
        quote_volumes=dict(adapter.read_quote_volumes(tickers)),
        captured_symbol_count=0,
        selection_metric="quote-volume-24h",
        funding=adapter.read_funding_facts(
            listings,
            tickers,
            [
                read_captured_json(VENUE, FUNDING_RATE_FIXTURE),
                read_captured_json(VENUE, FUNDING_INTERVAL_FIXTURE),
            ],
        ),
    )


def a_selector_that_pays_fees(taker_fee_rate=0.0005, believable_age_seconds=None):
    """A selector, optionally told how old a price may be.

    The bound is pinned rather than learned in these tests -- floor and ceiling set
    to the same second -- because what is under test here is what the selector does
    with a bound, not how the bound is arrived at. That is
    tests/runtime/test_price_staleness.py, against the tape.
    """
    staleness = None
    if believable_age_seconds is not None:
        staleness = PriceStalenessEstimator(
            materiality_fraction=2 * taker_fee_rate,
            anchor_seconds=1.0,
            quantile=0.95,
            window=3_600,
            observations_needed=300,
            prior_one_second_move=0.000898,
            minimum_age_seconds=believable_age_seconds,
            maximum_age_seconds=believable_age_seconds,
        )
    return InstrumentSelector(
        built_segments=("futures",),
        maximum_cost_fraction=0.05,
        round_trip_cost_fraction=2 * taker_fee_rate,
        price_staleness=staleness,
    )


def test_a_perpetual_the_venue_declared_carries_an_intent(read_captured_json):
    """The whole of the carry, from the venue's own numbers.

    This is the case the first paper fill was blocked on: nothing published a
    funding rate, so the only listed instrument was rejected as unpriceable and
    every intent came back `no-listed-instrument-can-express-this-intent`.
    """
    subject = a_selector_that_pays_fees()
    for listed in a_real_universe(read_captured_json):
        subject.observe_listed_symbol(listed)

    choice = subject.select(Intent(horizon_seconds=3600.0))
    assert choice.state == CHOSEN, choice.reason
    assert choice.chosen.instrument_kind == PERPETUAL_FUTURE
    assert choice.chosen.contract_symbol == SYMBOL

    # An hour of an eight-hourly rate is an eighth of one settlement, at the rate
    # the venue last charged. Nothing here is a preference or a default.
    settlements = 3600.0 / 86400.0 * choice.chosen.settlements_per_day
    assert choice.carry_cost_fraction == pytest.approx(
        choice.chosen.funding_rate_per_settlement * settlements
    )
    assert choice.total_cost_fraction == pytest.approx(
        0.001 + max(0.0, choice.carry_cost_fraction)
    )


def test_the_horizon_still_decides_when_the_numbers_are_the_venue_s_own(read_captured_json):
    """A day of funding costs 24 times an hour of it, on the same declared rate."""
    subject = a_selector_that_pays_fees()
    for listed in a_real_universe(read_captured_json):
        subject.observe_listed_symbol(listed)

    brief = subject.select(Intent(horizon_seconds=3600.0))
    long_held = subject.select(Intent(horizon_seconds=86400.0))
    assert long_held.carry_cost_fraction == pytest.approx(brief.carry_cost_fraction * 24.0)
    assert long_held.total_cost_fraction > brief.total_cost_fraction


def test_a_dated_future_is_skipped_by_name_rather_than_listed_unpriceably(read_captured_json):
    """Every listing is either registered or counted under why it was not.

    The quarterlies are the case in this fixture: their kind is known and their
    carry is their basis, which nothing publishes to this part. Registering one
    with no basis would add a listing that could only ever be rejected, and the
    count is what says it was seen at all.
    """
    subject = a_selector_that_pays_fees()
    universe = a_real_universe(read_captured_json)
    for listed in universe:
        subject.observe_listed_symbol(listed)

    skipped = subject.standing.listings_skipped
    assert subject.standing.listings_registered + sum(skipped.values()) == len(universe)
    assert any("dated-future is not priced by this part yet" in reason for reason in skipped), (
        skipped
    )
    dated = [entry.symbol for entry in universe if entry.instrument_kind == DATED_FUTURE]
    assert dated, "the fixture holds no dated contract, so this case is absent"
    for symbol in dated:
        assert not any(
            instrument.symbol == symbol
            for listed in subject._listed.values()
            for instrument in listed
        ), f"{symbol} was registered although its basis is not published to this part"


def test_a_perpetual_whose_interval_the_venue_never_declared_is_skipped(read_captured_json):
    """A rate without an interval prices nothing, and is refused rather than halved.

    OMGUSDT is the real case: on 2026-08-22 this venue listed it as a perpetual on
    its way out, quoted `lastFundingRate` for it on premiumIndex, and declared no
    `fundingIntervalHours` for it anywhere. Measured the same day, all 740 of its
    TRADING perpetuals do declare one -- so this branch guards a state the venue
    genuinely produces while no symbol the capture selects is currently in it,
    which is exactly when an assumed eight hours would go unnoticed.
    """
    from runtime.symbol_universe import CapturableSymbol
    from runtime.venues.adapter_registry import load_venue_adapter

    adapter = load_venue_adapter(VENUE)
    catalogue = read_captured_json(VENUE, "2026-08-22-catalogue-subset.json")
    tickers = read_captured_json(VENUE, "2026-08-22-ticker-24h-subset.json")
    listings = adapter.read_symbol_listings(catalogue)
    facts = adapter.read_funding_facts(
        listings,
        tickers,
        [
            read_captured_json(VENUE, FUNDING_RATE_FIXTURE),
            read_captured_json(VENUE, FUNDING_INTERVAL_FIXTURE),
        ],
    )
    unpriceable = [
        listing
        for listing in listings
        if listing.instrument_kind == PERPETUAL_FUTURE
        and facts[listing.symbol].settlements_per_day is None
    ]
    assert unpriceable, "the fixture declares an interval for every perpetual it holds"

    subject = a_selector_that_pays_fees()
    for listing in unpriceable:
        fact = facts[listing.symbol]
        assert fact.rate_per_settlement is not None, "the venue did quote it a rate"
        subject.observe_listed_symbol(
            CapturableSymbol(
                venue_id=VENUE,
                symbol=listing.symbol,
                contract_type=listing.contract_type,
                quote_volume_24h=None,
                price_increment=listing.price_increment,
                instrument_kind=listing.instrument_kind,
                funding_rate_per_settlement=fact.rate_per_settlement,
                funding_settlements_per_day=fact.settlements_per_day,
                funding_source=fact.source,
            )
        )

    assert subject.standing.listings_registered == 0
    assert subject.standing.listings_skipped[
        "the venue declared no funding rate or no settlement interval"
    ] == len(unpriceable)


def test_a_selector_that_was_given_no_fee_registers_nothing(read_captured_json):
    """What trading costs is the operator's to state, and unstated is not free."""
    subject = a_selector()
    for listed in a_real_universe(read_captured_json):
        subject.observe_listed_symbol(listed)

    assert subject.standing.listings_registered == 0
    assert subject.select(Intent()).state == NOTHING_AVAILABLE


def test_a_trade_registers_a_price_and_never_an_instrument():
    """A symbol printing trades is evidence a contract exists, not its terms.

    Inferring one here is what left every perpetual with an unpriceable carry: the
    inferred listing carried no funding, so it was rejected the moment it was
    priced, while looking from outside exactly like a venue that listed nothing.
    """
    subject = a_selector_that_pays_fees()
    subject.observe_price(VENUE, SYMBOL, 77_000.0, observed_at_ns=1_000 * SECOND_NS)

    choice = subject.select(Intent())
    assert choice.state == NOTHING_AVAILABLE
    assert choice.reference_price == 77_000.0, (
        "the price a choice was made at must still travel with the choice"
    )


def test_a_reference_price_carries_when_it_was_observed():
    """The fact the sizer needed and the choice never carried.

    On the live run of 2026-08-23 an ENAUSDT order was priced at 0.17019, the real
    market of 09:29:08, fifty-six minutes earlier. The choice that carried that
    price was stamped `chosen_at_ns` = the moment it was made, so every reader
    downstream saw a message that was genuinely fresh with a price inside it that
    was not. A price and the time it was seen are one fact and travel together.
    """
    subject = a_selector_that_pays_fees()
    subject.observe_price(VENUE, SYMBOL, 77_000.0, observed_at_ns=1_000 * SECOND_NS)

    choice = subject.select(Intent())
    assert choice.reference_price == 77_000.0
    assert choice.reference_price_observed_at_ns == 1_000 * SECOND_NS


def test_a_choice_is_refused_when_its_price_is_older_than_the_bound(read_captured_json):
    """A symbol whose prints stopped cannot be sized against, and says so.

    Not a null price with a chosen instrument: the sizer would then fall back to
    the stop plan's entry and price the order off a different stale number. The
    refusal is the answer, and it is one of this part's countable states (T-5).
    """
    at = 1_000 * SECOND_NS
    subject = a_selector_that_pays_fees(believable_age_seconds=60.0)
    for listed in a_real_universe(read_captured_json):
        subject.observe_listed_symbol(listed)
    subject.observe_price(VENUE, SYMBOL, 77_000.0, observed_at_ns=at)

    fresh = subject.select(Intent(), now_ns=at + 30 * SECOND_NS)
    assert fresh.state == CHOSEN

    stale = subject.select(Intent(), now_ns=at + 3_360 * SECOND_NS)
    assert stale.state == REFERENCE_PRICE_IS_TOO_OLD
    assert stale.is_actionable is False
    assert stale.chosen is None
    assert "3360" in stale.reason or "3,360" in stale.reason
    assert subject.standing.by_refusal[REFERENCE_PRICE_IS_TOO_OLD] == 1


def test_a_symbol_that_never_printed_is_refused_for_that_and_not_for_age(read_captured_json):
    """Never seen and too old are different facts and must not share a reason."""
    subject = a_selector_that_pays_fees(believable_age_seconds=60.0)
    for listed in a_real_universe(read_captured_json):
        subject.observe_listed_symbol(listed)

    choice = subject.select(Intent(), now_ns=1_000 * SECOND_NS)
    assert choice.state == NO_REFERENCE_PRICE_HAS_EVER_ARRIVED
    assert choice.reference_price is None
    assert choice.reference_price_observed_at_ns is None


def test_a_price_that_comes_back_makes_the_symbol_tradable_again(read_captured_json):
    """The symbol went quiet; it did not cease to exist."""
    at = 1_000 * SECOND_NS
    later = at + 3_360 * SECOND_NS
    subject = a_selector_that_pays_fees(believable_age_seconds=60.0)
    for listed in a_real_universe(read_captured_json):
        subject.observe_listed_symbol(listed)
    subject.observe_price(VENUE, SYMBOL, 77_000.0, observed_at_ns=at)
    assert subject.select(Intent(), now_ns=later).state == REFERENCE_PRICE_IS_TOO_OLD

    subject.observe_price(VENUE, SYMBOL, 78_500.0, observed_at_ns=later)
    back = subject.select(Intent(), now_ns=later)
    assert back.state == CHOSEN
    assert back.reference_price == 78_500.0


def test_without_a_bound_a_price_never_expires(read_captured_json):
    """RL-061: the bound is a named setting. A selector given none does not invent
    one, and does not silently expire anything either."""
    at = 1_000 * SECOND_NS
    subject = a_selector_that_pays_fees()
    for listed in a_real_universe(read_captured_json):
        subject.observe_listed_symbol(listed)
    subject.observe_price(VENUE, SYMBOL, 77_000.0, observed_at_ns=at)

    choice = subject.select(Intent(), now_ns=at + 3_360 * SECOND_NS)
    assert choice.state == CHOSEN
    assert choice.reference_price == 77_000.0


def test_the_price_a_real_tape_last_printed_is_what_a_choice_carries(read_captured_trades):
    """Against the tape rather than an invented number (RL-063)."""
    trades = read_captured_trades(limit=200)
    subject = a_selector_that_pays_fees(believable_age_seconds=60.0)
    for trade in trades:
        subject.observe_price(trade.venue_id, trade.symbol, trade.price, trade.venue_time_ns)

    last = trades[-1]
    choice = subject.select(
        Intent(venue_id=last.venue_id, symbol=last.symbol), now_ns=last.venue_time_ns
    )
    assert choice.reference_price == last.price
    assert choice.reference_price_observed_at_ns == last.venue_time_ns


def test_a_republished_universe_replaces_a_contract_s_terms(read_captured_json):
    """Funding changes at every settlement, and the universe is republished whole.

    A second read must overwrite the instrument rather than list it twice --
    otherwise the cheapest of the two is chosen, which is the stale one exactly
    when the rate has risen.
    """
    import dataclasses

    subject = a_selector_that_pays_fees()
    universe = [
        entry
        for entry in a_real_universe(read_captured_json)
        if entry.symbol == SYMBOL and entry.funding_settlements_per_day is not None
    ]
    assert universe
    for listed in universe:
        subject.observe_listed_symbol(listed)
    first = subject.select(Intent(horizon_seconds=3600.0))

    for listed in universe:
        subject.observe_listed_symbol(
            dataclasses.replace(
                listed,
                funding_rate_per_settlement=listed.funding_rate_per_settlement * 10.0,
            )
        )
    after = subject.select(Intent(horizon_seconds=3600.0))

    assert after.considered == first.considered, "the same contract was listed twice"
    assert after.carry_cost_fraction == pytest.approx(first.carry_cost_fraction * 10.0)

# ---- instrument-selector: options reach it through symbol-universe -----------

NIFTY = "NIFTY"
NIFTY_KEY = "NSE_INDEX|Nifty 50"
A_CALL_KEY = "NSE_FO|48001"
A_PUT_KEY = "NSE_FO|48002"


def a_universe_underlying():
    return CapturableSymbol(
        venue_id=UPSTOX_VENUE_ID, symbol=NIFTY, contract_type="INDEX",
        quote_volume_24h=None, price_increment=None, instrument_kind=None,
        venue_instrument_id=NIFTY_KEY,
    )


def a_universe_contract(key, symbol, contract_type, strike, expiry_ms):
    return CapturableSymbol(
        venue_id=UPSTOX_VENUE_ID, symbol=symbol, contract_type=contract_type,
        quote_volume_24h=None, price_increment=0.05, instrument_kind=OPTION,
        strike_price=strike, expiry_ms=expiry_ms, lot_size=75,
        venue_instrument_id=key, underlying_symbol=NIFTY,
        underlying_venue_instrument_id=NIFTY_KEY,
    )


class Greeks:
    def __init__(self, instrument_key, delta):
        self.instrument_key, self.delta = instrument_key, delta


def a_selector_fed_from_the_universe(spot=24_500.0, premium=120.0):
    """Everything the live part sees, in the order it sees it.

    `broker-instrument-listing` is deliberately absent: measured on the live
    spine 2026-09-04, `instrument-selector` received **zero** of them -- the
    102,940-row catalogue is published as one burst and this part never
    absorbed any of it -- while receiving 58,308 option greeks and 70,115
    option prices it could do nothing with, because nothing could resolve a
    contract to its underlying. `symbol-universe` is the bounded set it does
    receive (6,735), and it carries the same facts.
    """
    expiry_ms = (time.time_ns() // 1_000_000) + 7 * 24 * 3_600_000
    subject = InstrumentSelector(
        built_segments=("index-options",), maximum_cost_fraction=0.05,
        round_trip_cost_fraction=0.001,
    )
    subject.observe_listed_symbol(a_universe_underlying())
    subject.observe_listed_symbol(
        a_universe_contract(A_CALL_KEY, "NIFTY24500CE", "CE", 24_500.0, expiry_ms))
    subject.observe_listed_symbol(
        a_universe_contract(A_PUT_KEY, "NIFTY24500PE", "PE", 24_500.0, expiry_ms))
    subject.observe_price(UPSTOX_VENUE_ID, NIFTY, spot, time.time_ns())
    subject.observe_option_greeks(Greeks(A_CALL_KEY, 0.5))
    subject.observe_option_greeks(Greeks(A_PUT_KEY, -0.5))
    subject.observe_option_price(A_CALL_KEY, premium, time.time_ns())
    subject.observe_option_price(A_PUT_KEY, premium, time.time_ns())
    return subject


def test_an_option_from_the_universe_can_carry_an_intent_on_its_underlying():
    """The gap that refused the first real intent this system ever produced.

    Measured 2026-09-04: an intent reached this part and came back
    `no-instrument-is-listed-for-this-symbol`, while 6,600 option listings sat
    counted as "kind option is not priced by this part yet". The ATM path that
    prices options was already built and already correct -- it was simply never
    fed, because it was fed only from a catalogue this part does not receive.
    """
    subject = a_selector_fed_from_the_universe()

    choice = subject.select(Intent(venue_id=UPSTOX_VENUE_ID, symbol=NIFTY))

    assert choice.chosen is not None, choice.reason
    assert choice.chosen.instrument_kind == OPTION
    assert choice.chosen.contract_symbol == "NIFTY24500CE"


def test_a_bearish_intent_is_carried_by_the_put_and_a_bullish_one_by_the_call():
    """Buy-only options: the delta's sign is what says which view a contract
    can express, and neither contract can express the other's."""
    subject = a_selector_fed_from_the_universe()

    bullish = subject.select(Intent(venue_id=UPSTOX_VENUE_ID, symbol=NIFTY, is_long=True))
    bearish = subject.select(Intent(venue_id=UPSTOX_VENUE_ID, symbol=NIFTY, is_long=False))

    assert bullish.chosen.contract_symbol == "NIFTY24500CE"
    assert bearish.chosen.contract_symbol == "NIFTY24500PE"


def test_an_intent_naming_a_contract_is_resolved_to_its_underlying():
    """63 of the 69 intents formed on 2026-09-04 named a contract, none named an
    underlying, and the registry is keyed by underlying -- so every lookup missed
    and every intent came back `no-instrument-is-listed-for-this-symbol`."""
    subject = a_selector_fed_from_the_universe()

    choice = subject.select(
        Intent(venue_id=UPSTOX_VENUE_ID, symbol="NIFTY24500CE", is_long=True)
    )

    assert choice.chosen is not None, choice.reason
    assert choice.resolved_underlying == NIFTY
    assert choice.chosen.contract_symbol == "NIFTY24500CE"
    assert choice.view_was_converted is False


def test_a_sell_of_a_call_becomes_a_buy_of_the_put():
    """While the segment is buy-only, a bearish view is carried by BUYING a put.

    Selling the call is the same direction and a different trade: it writes an
    option, whose loss is unbounded and whose margin this account does not have.
    settings/segments/index-options.toml: "Phase A is buy-only index/stock
    options". Until 2026-09-04 this intent became a sell-to-open on the call.
    """
    subject = a_selector_fed_from_the_universe()

    choice = subject.select(
        Intent(venue_id=UPSTOX_VENUE_ID, symbol="NIFTY24500CE", is_long=False)
    )

    assert choice.chosen.contract_symbol == "NIFTY24500PE"
    assert choice.order_side == BUY
    assert choice.view_was_converted is True
    assert subject.standing.views_converted_to_a_buy == 1


def test_a_sell_of_a_put_becomes_a_buy_of_the_call():
    """The other half of the same rule: short a put is a bullish view."""
    subject = a_selector_fed_from_the_universe()

    choice = subject.select(
        Intent(venue_id=UPSTOX_VENUE_ID, symbol="NIFTY24500PE", is_long=False)
    )

    assert choice.chosen.contract_symbol == "NIFTY24500CE"
    assert choice.order_side == BUY
    assert choice.view_was_converted is True


def test_every_option_choice_is_a_buy_whichever_way_the_view_points():
    """There is no sell-to-open in this segment. The direction is carried by
    which contract is bought, never by the side."""
    subject = a_selector_fed_from_the_universe()

    for symbol in ("NIFTY24500CE", "NIFTY24500PE"):
        for is_long in (True, False):
            choice = subject.select(
                Intent(venue_id=UPSTOX_VENUE_ID, symbol=symbol, is_long=is_long)
            )
            assert choice.chosen is not None, choice.reason
            assert choice.order_side == BUY, f"{symbol} is_long={is_long}"


def test_a_view_carried_by_the_contract_it_names_is_not_counted_as_converted():
    """The counter has to separate the trades that were turned around from the
    ones that were not, or the cost of converting cannot be read later."""
    subject = a_selector_fed_from_the_universe()

    subject.select(Intent(venue_id=UPSTOX_VENUE_ID, symbol="NIFTY24500CE", is_long=True))
    subject.select(Intent(venue_id=UPSTOX_VENUE_ID, symbol="NIFTY24500PE", is_long=True))

    assert subject.standing.views_converted_to_a_buy == 0


def test_the_universe_alone_is_not_enough_without_a_greek_to_say_which_is_atm():
    """A contract with no delta cannot be placed on the chain, and is not
    guessed onto it -- the tracker resolves ATM from real greeks."""
    subject = InstrumentSelector(
        built_segments=("index-options",), maximum_cost_fraction=0.05,
        round_trip_cost_fraction=0.001,
    )
    expiry_ms = (time.time_ns() // 1_000_000) + 7 * 24 * 3_600_000
    subject.observe_listed_symbol(a_universe_underlying())
    subject.observe_listed_symbol(
        a_universe_contract(A_CALL_KEY, "NIFTY24500CE", "CE", 24_500.0, expiry_ms))
    subject.observe_price(UPSTOX_VENUE_ID, NIFTY, 24_500.0, time.time_ns())

    assert subject.select(Intent(venue_id=UPSTOX_VENUE_ID, symbol=NIFTY)).chosen is None


def test_an_option_universe_entry_is_not_counted_as_an_unpriced_kind():
    """The counter that named this gap must stop naming it once it is closed."""
    subject = a_selector_fed_from_the_universe()

    assert not any(
        "kind option" in reason for reason in subject.standing.listings_skipped
    ), dict(subject.standing.listings_skipped)


# ---- instrument-selector: the price a choice carries is the contract's -------


def test_a_choice_carries_the_chosen_contracts_price_and_not_the_underlyings():
    """The defect that refused every options order on 2026-09-07.

    `reference_price` is the only price the parts downstream have -- neither
    position-sizer nor stop-target-placer consumes market-data -- and both put it
    on an order whose symbol is `chosen.contract_symbol`. It was read off
    `intent.symbol` instead, so an order for a contract left carrying the
    underlying's price. Measured live that morning against this project's own
    tape: RELIANCE 1320 PE went out priced at 1311.05 with the contract printing
    22.85, ITC 265 CE at 263.30 against 3.70, TCS 2280 CE at 2264.65 against 3.70.
    paper-fill-simulator refuses past `maximum_decision_price_drift` (6%), so
    6,806 of the 8,049 orders that reached a verdict were refused
    `decision-price-stale` and no options position could open.

    The numbers here are that session's: NIFTY at 23,773.6 with the contract at
    118.0. The ratio is what matters -- a premium is not on its underlying's scale
    and no bound over a fraction of price can absorb the difference.
    """
    spot, premium = 23_773.6, 118.0
    subject = a_selector_fed_from_the_universe(spot=spot, premium=premium)
    subject.observe_price(UPSTOX_VENUE_ID, "NIFTY24500CE", premium, time.time_ns())

    choice = subject.select(Intent(venue_id=UPSTOX_VENUE_ID, symbol=NIFTY))

    assert choice.chosen.contract_symbol == "NIFTY24500CE"
    assert choice.reference_price == premium, (
        "an order is sent on the contract, so the price it is sized against is the "
        "contract's -- the underlying's price is a different scale entirely"
    )
    assert choice.underlying_reference_price == spot, (
        "the underlying's price is still carried, under its own name, for the "
        "readers that genuinely want it"
    )


def test_the_underlyings_price_alone_does_not_price_a_contract():
    """A contract neither source can price is left unpriced, never priced off its
    underlying.

    This is the state the old field could not reach: the underlying always has a
    price, so a contract with none was invisible and went out carrying a number
    from the wrong instrument. Withholding it is what makes position-sizer count
    `missing_entry_price` instead of sizing against 23,773.6 an order that fills
    at 118 -- an unsized order is a trade not taken, and a wrongly-sized one is a
    position in a place nobody looked.

    The contract here carries a premium fraction (so it can be chosen) and no
    venue instrument id and no frame price (so it cannot be priced), which is a
    listing that reached this part from a source naming neither.
    """
    subject = InstrumentSelector(
        built_segments=("index-options",), maximum_cost_fraction=0.05,
        round_trip_cost_fraction=0.001,
    )
    subject.observe_price(UPSTOX_VENUE_ID, NIFTY, 23_773.6, observed_at_ns=1_000 * SECOND_NS)
    subject.observe_listed_instrument(
        ListedInstrument(
            venue_id=UPSTOX_VENUE_ID, symbol=NIFTY, instrument_kind=OPTION,
            contract_symbol="NIFTY 23750 CE 08 SEP 26", venue_instrument_id=None,
            funding_rate_per_settlement=None, settlements_per_day=None,
            basis_fraction=None, premium_fraction=0.005, seconds_to_expiry=86_400.0,
            supports_short=False, supports_long=True, supports_convexity=True,
            round_trip_cost_fraction=0.001, absorbable_quote=None, seconds_to_fill=None,
        )
    )

    choice = subject.select(Intent(venue_id=UPSTOX_VENUE_ID, symbol=NIFTY))

    assert choice.chosen is not None, choice.reason
    assert choice.reference_price is None
    assert choice.reference_price_observed_at_ns is None
    assert choice.underlying_reference_price == 23_773.6
    assert subject.standing.chosen_without_a_price_for_the_contract == 1


def test_an_instrument_that_is_its_own_underlying_is_unchanged():
    """cash-equity-intraday and every perpetual: contract_symbol IS symbol.

    The fix must be the identity for them, or it would have moved the defect
    rather than removed it.
    """
    subject = a_selector_that_pays_fees()
    subject.observe_price(VENUE, SYMBOL, 77_000.0, observed_at_ns=1_000 * SECOND_NS)

    choice = subject.select(Intent())

    assert choice.reference_price == 77_000.0
    assert choice.underlying_reference_price == 77_000.0


def test_a_contract_is_priced_from_the_venues_own_market_data_when_the_frame_never_names_it():
    """The second half of the same defect, measured four minutes after the first
    fix went live on 2026-09-07.

    `symbol-price-frame` names a contract only where that contract is itself a
    subscribed symbol, and most are not: 7,013 of the 8,200 contracts chosen in
    those four minutes had no price on it. `broker-market-data` names every
    subscribed contract, by instrument key rather than by trading symbol, and this
    part was already holding those prices -- it used them for one ratio and threw
    the timestamp away. Asking it second is what turns a correctly-refused choice
    into a priced one.
    """
    subject = a_selector_fed_from_the_universe(spot=23_773.6, premium=118.0)

    choice = subject.select(Intent(venue_id=UPSTOX_VENUE_ID, symbol=NIFTY))

    assert choice.chosen.venue_instrument_id == A_CALL_KEY
    assert choice.reference_price == 118.0, (
        "the contract's own LTP is a price for the contract, and it is the only "
        "one most contracts have"
    )
    assert choice.reference_price_observed_at_ns is not None
    assert subject.standing.priced_from_the_venues_own_market_data == 1
    assert subject.standing.chosen_without_a_price_for_the_contract == 0
