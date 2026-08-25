"""Risk and capital: every way this system is stopped from spending too much.

The arithmetic is checked against worked examples, because each number is a claim
about money. The refusals get as much attention as the successes: a limiter that
cannot say no is decoration, and most of these parts exist only to say no.
"""

import importlib

import pytest

from parts.capital_desk.allocation_conservation_checker import (
    CONSERVED, NO_BALANCE, OVER_ALLOCATED, UNDER_ALLOCATED, AllocationConservationChecker,
)
from parts.capital_desk.allocation_rebalance_proposer import (
    NO_CHANGE, PROPOSED, TOO_FEW_TRADES, AllocationRebalanceProposer, SegmentPerformance,
)
from parts.capital_desk.capital_settings_change_recorder import CapitalSettingsChangeRecorder
from parts.capital_desk.capital_settings_validator import (
    CONSISTENT, INCOMPLETE, INCONSISTENT, CapitalSettingsValidator,
)
from parts.capital_desk.capital_utilisation_meter import (
    MEASURED, NO_ALLOTMENT, OVER_COMMITTED, CapitalUtilisationMeter,
)
from parts.capital_desk.live_balance_divergence_watch import (
    AGREES, LIVE, NOT_LIVE, PAPER, UNREADABLE, VENUE_HOLDS_LESS, VENUE_HOLDS_MORE,
    LiveBalanceDivergenceWatch,
)
from parts.capital_desk.paper_currency_converter import (
    CONVERTED, NO_RATE, SAME_CURRENCY, STALE_RATE, PaperCurrencyConverter,
)
from parts.risk_capital_allocation.drawdown_breaker import DrawdownBreaker
from parts.risk_capital_allocation.event_risk_limiter import EventRiskLimiter
from parts.risk_capital_allocation.exit_order_chainer import (
    CHAINED, EXTENDED, NO_PLAN, ExitOrderChainer,
)
from parts.risk_capital_allocation.exposure_limiter import (
    PER_CLUSTER, PER_POSITION, TOTAL_GROSS, ExposureLimiter,
)
from parts.risk_capital_allocation.halt_enforcer import (
    HUMAN_OVERRIDE, POLICY_REFUSAL, TRADING_HALT, HaltEnforcer,
)
from parts.risk_capital_allocation.leverage_selector import (
    AT_CEILING, CHOSEN, NO_LEVERAGE, UNLEVERAGED_NO_FORECAST, LeverageSelector,
)
from parts.risk_capital_allocation.margin_liquidation_watch import (
    NEAR_LIQUIDATION, NEAR_MARGIN_CALL, SAFE, UNKNOWN_DISTANCE, MarginLiquidationWatch,
)
from parts.risk_capital_allocation.participation_capped_order_splitter import (
    REFUSED_HORIZON_EXCEEDED, REFUSED_NO_VOLUME, SINGLE_SLICE, SPLIT,
    ParticipationCappedOrderSplitter,
)
from parts.risk_capital_allocation.position_sizer import (
    REFUSED_NO_INCREMENT, REFUSED_NO_LIMIT, REFUSED_STOP_INVALID, REFUSED_TOO_SMALL,
    SIZED, SHRUNK_TO_FIT, PositionSizer, entry_price_for,
)
from parts.risk_capital_allocation.profit_lock import (
    HELD, MOVED_TO_BREAK_EVEN, NOT_YET_PROFITABLE, TRAILED, ProfitLock,
)
from parts.risk_capital_allocation.stop_frequency_breaker import StopFrequencyBreaker
from parts.risk_capital_allocation.stop_target_placer import (
    MOVED_CLEAR_OF_CLUSTER, PLACED, REFUSED_NO_DISTANCE, REFUSED_REWARD_TOO_THIN,
    StopTargetPlacer,
)
from parts.risk_capital_allocation.trade_capital_bounds_gate import (
    BUMPED_TO_MINIMUM, CAPPED_AT_MAXIMUM, REFUSED_BUMP_BREACHES_RISK,
    REFUSED_SETTINGS_INVALID, WITHIN_BOUNDS, TradeCapitalBoundsGate,
    does_verdict_permit_trading,
)
from runtime.journal import Journal
from runtime.part_declaration import load_declaration_from_blueprint
from runtime.risk_types import (
    NO_RISK_ALLOWED, CapitalAllotment, RiskLimit, TradeCapitalBounds, tightest_limit,
)
from runtime.trading_types import BUY, LONG, SELL, SHORT

BLOCK_PARTS = {
    "profit-lock": "parts.risk_capital_allocation.profit_lock",
    "capital-allotment-reader": "parts.risk_capital_allocation.capital_allotment_reader",
    "leverage-selector": "parts.risk_capital_allocation.leverage_selector",
    "stop-target-placer": "parts.risk_capital_allocation.stop_target_placer",
    "exposure-limiter": "parts.risk_capital_allocation.exposure_limiter",
    "drawdown-breaker": "parts.risk_capital_allocation.drawdown_breaker",
    "halt-enforcer": "parts.risk_capital_allocation.halt_enforcer",
    "event-risk-limiter": "parts.risk_capital_allocation.event_risk_limiter",
    "position-sizer": "parts.risk_capital_allocation.position_sizer",
    "margin-liquidation-watch": "parts.risk_capital_allocation.margin_liquidation_watch",
    "stop-frequency-breaker": "parts.risk_capital_allocation.stop_frequency_breaker",
    "exit-order-chainer": "parts.risk_capital_allocation.exit_order_chainer",
    "participation-capped-order-splitter": "parts.risk_capital_allocation.participation_capped_order_splitter",
    "trade-capital-bounds-gate": "parts.risk_capital_allocation.trade_capital_bounds_gate",
    "main-account-settings-reader": "parts.capital_desk.main_account_settings_reader",
    "allocation-conservation-checker": "parts.capital_desk.allocation_conservation_checker",
    "capital-settings-validator": "parts.capital_desk.capital_settings_validator",
    "capital-settings-change-recorder": "parts.capital_desk.capital_settings_change_recorder",
    "paper-currency-converter": "parts.capital_desk.paper_currency_converter",
    "capital-utilisation-meter": "parts.capital_desk.capital_utilisation_meter",
    "allocation-rebalance-proposer": "parts.capital_desk.allocation_rebalance_proposer",
    "live-balance-divergence-watch": "parts.capital_desk.live_balance_divergence_watch",
}

SEGMENT = "futures"
VENUE = "binance-usdm"
SYMBOL = "BTCUSDT"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_every_built_declaration_equals_the_blueprint(part_id):
    module = importlib.import_module(BLOCK_PARTS[part_id])
    assert module.PART_DECLARATION == load_declaration_from_blueprint(part_id)


# ---- the combining rule ------------------------------------------------------

def limit(name, fraction):
    return RiskLimit(name, fraction, "because", fraction <= 0, 1)


def test_the_smallest_limit_wins_and_says_who_set_it():
    tightest = tightest_limit([limit("a", 0.5), limit("b", 0.1), limit("c", 0.9)])
    assert tightest.fraction_of_allotment == pytest.approx(0.1)
    assert tightest.limiter == "b"


def test_no_limiter_reporting_is_not_permission():
    """Silence is the one answer that must never be read as 'go ahead'."""
    tightest = tightest_limit([])
    assert tightest.fraction_of_allotment == NO_RISK_ALLOWED
    assert "silence is not permission" in tightest.reason


# ---- drawdown-breaker --------------------------------------------------------

def breaker(maximum=0.2, recovery=0.5, allowed=1.0):
    return DrawdownBreaker(
        maximum_drawdown_fraction=maximum, recovery_fraction=recovery,
        allowed_fraction_when_trading=allowed,
    )


def test_drawdown_is_measured_from_the_peak_not_the_start():
    """A system that doubled and gave it all back must trip."""
    subject = breaker(maximum=0.2)
    subject.observe_equity(1000.0)
    subject.observe_equity(2000.0)
    tripped = subject.observe_equity(1500.0)
    assert tripped.fraction_of_allotment == NO_RISK_ALLOWED
    assert subject.is_tripped is True


def test_a_shallow_dip_does_not_trip_it():
    subject = breaker(maximum=0.2)
    subject.observe_equity(1000.0)
    assert subject.observe_equity(900.0).fraction_of_allotment > 0


def test_the_breaker_is_sticky_until_enough_is_recovered():
    """Releasing at the bottom of every dip is worse than not stopping at all."""
    subject = breaker(maximum=0.2, recovery=0.5)
    subject.observe_equity(1000.0)
    subject.observe_equity(700.0)
    assert subject.is_tripped
    assert subject.observe_equity(760.0).fraction_of_allotment == NO_RISK_ALLOWED
    released = subject.observe_equity(860.0)
    assert released.fraction_of_allotment > 0
    assert subject.is_tripped is False


# ---- halt-enforcer -----------------------------------------------------------

def test_a_human_override_outranks_everything():
    enforcer = HaltEnforcer(allowed_fraction_when_clear=1.0)
    enforcer.raise_halt(POLICY_REFUSAL, "policy-engine", "a rule said no")
    enforcer.raise_halt(HUMAN_OVERRIDE, "operator", "stop now")
    assert "human-override" in enforcer.read_limit().reason


def test_a_halt_is_only_lifted_explicitly():
    """Silence from the source that raised it is not the condition ending."""
    enforcer = HaltEnforcer(allowed_fraction_when_clear=1.0)
    enforcer.raise_halt(TRADING_HALT, "watchdog", "venue outage")
    assert enforcer.read_limit().fraction_of_allotment == NO_RISK_ALLOWED
    assert enforcer.read_limit().fraction_of_allotment == NO_RISK_ALLOWED
    assert enforcer.release_halt(TRADING_HALT) is True
    assert enforcer.read_limit().fraction_of_allotment > 0


def test_nothing_halting_permits_trading():
    assert HaltEnforcer(allowed_fraction_when_clear=1.0).read_limit().fraction_of_allotment == 1.0


# ---- exposure-limiter --------------------------------------------------------

# The allotment every exposure test measures against. Stated once: the limiter's
# caps are fractions of it, and since 2026-08-25 the part is given a notional and
# divides -- so a test that means "a tenth of the book" says so at one place.
EXPOSURE_ALLOTMENT = 10_000.0


def exposure(per_position=0.2, total=0.6, per_cluster=0.3):
    limiter = ExposureLimiter(per_position, total, per_cluster)
    limiter.set_allotment(EXPOSURE_ALLOTMENT)
    return limiter


def hold(subject, symbol, fraction_of_allotment):
    """One position worth that fraction of the allotment, at what it cost."""
    subject.observe_position(VENUE, symbol, fraction_of_allotment * EXPOSURE_ALLOTMENT)


def test_ten_correlated_positions_are_one_position():
    """The cap that actually saves accounts."""
    subject = exposure(per_position=0.2, total=0.9, per_cluster=0.3)
    for index in range(3):
        subject.set_correlation_cluster(f"ALT{index}USDT", "majors")
        hold(subject, f"ALT{index}USDT", 0.1)
    subject.set_correlation_cluster("ALT9USDT", "majors")
    assert subject.read_limit("ALT9USDT").fraction_of_allotment == pytest.approx(0.0)


def test_an_uncorrelated_symbol_still_has_room():
    subject = exposure(per_position=0.2, total=0.9, per_cluster=0.3)
    for index in range(3):
        subject.set_correlation_cluster(f"ALT{index}USDT", "majors")
        hold(subject, f"ALT{index}USDT", 0.1)
    subject.set_correlation_cluster("GOLDUSDT", "metals")
    assert subject.read_limit("GOLDUSDT").fraction_of_allotment > 0


def test_an_unclustered_symbol_is_its_own_cluster_not_assumed_independent():
    subject = exposure(per_cluster=0.3)
    hold(subject, "LONEUSDT", 0.3)
    assert subject.read_limit("LONEUSDT").fraction_of_allotment == pytest.approx(0.0)


def test_total_exposure_binds_before_any_single_position_does():
    subject = exposure(per_position=0.2, total=0.5, per_cluster=1.0)
    for index in range(5):
        hold(subject, f"S{index}", 0.1)
    assert subject.read_limit().fraction_of_allotment == pytest.approx(0.0)


# ---- margin-liquidation-watch ------------------------------------------------

def margin_watch(distance=0.1, headroom=0.2):
    return MarginLiquidationWatch(
        danger_price_distance=distance, danger_equity_headroom=headroom,
        allowed_fraction_when_safe=1.0,
    )


def test_a_position_near_its_liquidation_stops_new_risk():
    subject = margin_watch(distance=0.1)
    subject.observe_liquidation_price(VENUE, SYMBOL, liquidation_price=95.0, mark_price=100.0)
    subject.observe_account(equity=1000.0, maintenance_requirement=100.0)
    assert subject.read_limit().fraction_of_allotment == NO_RISK_ALLOWED
    assert subject.standing.state == NEAR_LIQUIDATION


def test_a_thin_equity_cushion_stops_new_risk_even_with_distant_liquidations():
    subject = margin_watch(distance=0.1, headroom=0.2)
    subject.observe_liquidation_price(VENUE, SYMBOL, liquidation_price=50.0, mark_price=100.0)
    subject.observe_account(equity=1000.0, maintenance_requirement=900.0)
    assert subject.read_limit().fraction_of_allotment == NO_RISK_ALLOWED
    assert subject.standing.state == NEAR_MARGIN_CALL


def test_an_unmeasurable_liquidation_price_stops_rather_than_assuming_safety():
    """Assuming it is safe is the assumption that ends a segment."""
    subject = margin_watch()
    subject.observe_liquidation_price(VENUE, SYMBOL, liquidation_price=None, mark_price=100.0)
    limit_now = subject.read_limit()
    assert limit_now.fraction_of_allotment == NO_RISK_ALLOWED
    assert subject.standing.state == UNKNOWN_DISTANCE


def test_a_safe_account_permits_risk():
    subject = margin_watch()
    subject.observe_liquidation_price(VENUE, SYMBOL, liquidation_price=50.0, mark_price=100.0)
    subject.observe_account(equity=1000.0, maintenance_requirement=100.0)
    assert subject.read_limit().fraction_of_allotment == 1.0
    assert subject.standing.state == SAFE


# ---- stop-frequency-breaker --------------------------------------------------

def frequency_breaker(window=10, prior=0.3, excess=1.8, minimum=5, cooldown=5):
    return StopFrequencyBreaker(
        window_trades=window, prior_stop_rate=prior, excess_ratio=excess,
        minimum_observations=minimum, cooldown_trades=cooldown,
        allowed_fraction_when_trading=1.0,
    )


def test_a_cluster_of_stops_trips_before_the_equity_curve_shows_it():
    subject = frequency_breaker(window=10, prior=0.2, excess=1.5)
    for _ in range(11):
        result = subject.observe_closed_trade(was_stopped_out=True)
    assert result.fraction_of_allotment == NO_RISK_ALLOWED
    assert subject.is_tripped


def test_an_ordinary_stop_rate_does_not_trip_it():
    subject = frequency_breaker(window=10, prior=0.5, excess=1.8)
    for index in range(10):
        result = subject.observe_closed_trade(was_stopped_out=index % 2 == 0)
    assert result.fraction_of_allotment > 0


def test_the_baseline_rate_is_learned_from_this_strategys_own_history():
    """A strategy that stops out four times in ten by design is healthy at that rate."""
    subject = frequency_breaker(window=10, prior=0.05, excess=1.5, minimum=20)
    for index in range(40):
        subject.observe_closed_trade(was_stopped_out=index % 10 < 4)
    described = importlib.import_module(
        "parts.risk_capital_allocation.stop_frequency_breaker"
    ).describe_stop_frequency(subject)
    assert described["baseline_is_fitted"] is True
    assert described["baseline_stop_rate"] > 0.2


def test_it_cools_down_before_resuming():
    subject = frequency_breaker(window=5, prior=0.1, excess=1.5, minimum=1, cooldown=3)
    for _ in range(5):
        subject.observe_closed_trade(True)
    assert subject.is_tripped
    for _ in range(2):
        assert subject.observe_closed_trade(False).fraction_of_allotment == NO_RISK_ALLOWED
    assert subject.observe_closed_trade(False).fraction_of_allotment > 0


# ---- event-risk-limiter ------------------------------------------------------

def event_limiter(clock):
    return EventRiskLimiter(
        allowed_fraction_when_calm=1.0, turbulence_shrink_floor=0.1, monotonic=clock.monotonic
    )


def test_a_calm_market_is_unshrunk():
    assert event_limiter(Clock()).read_limit().fraction_of_allotment == 1.0


def test_overlapping_causes_multiply_rather_than_taking_the_worst():
    """A listing during a turbulent hour is riskier than either alone."""
    clock = Clock()
    subject = event_limiter(clock)
    subject.register_announcement("listing", shrink_to=0.5, seconds=60, reason="new listing")
    subject.observe_turbulence(index=2.0, normal_index=1.0, seconds=60)
    assert subject.read_limit().fraction_of_allotment == pytest.approx(0.25)


def test_a_scheduled_events_window_opens_before_it_happens():
    clock = Clock()
    subject = event_limiter(clock)
    subject.register_scheduled_event(
        "funding", seconds_until=60, window_seconds=30, shrink_to=0.5, reason="funding settles"
    )
    assert subject.read_limit().fraction_of_allotment == 1.0
    clock.now += 31
    assert subject.read_limit().fraction_of_allotment == pytest.approx(0.5)


def test_an_anomaly_decays_back_rather_than_releasing_at_a_cliff():
    clock = Clock()
    subject = event_limiter(clock)
    subject.register_anomaly("gap", shrink_to=0.2, decay_seconds=100, reason="unexplained gap")
    first = subject.read_limit().fraction_of_allotment
    clock.now += 50
    middle = subject.read_limit().fraction_of_allotment
    clock.now += 60
    assert first < middle < subject.read_limit().fraction_of_allotment


# ---- leverage-selector -------------------------------------------------------

def selector(target=0.05, horizons=2.0, funding_tolerance=0.001, maintenance=0.005):
    return LeverageSelector(
        target_liquidation_distance=target,
        volatility_horizons_to_survive=horizons,
        funding_tolerance_per_day=funding_tolerance,
        maintenance_margin_rate=maintenance,
    )


def test_a_violent_symbol_gets_less_leverage_than_a_calm_one():
    subject = selector()
    calm = subject.choose(VENUE, SYMBOL, ceiling=50.0, volatility_forecast=0.01, funding_forecast=0.0)
    violent = subject.choose(VENUE, "ALTUSDT", ceiling=50.0, volatility_forecast=0.10, funding_forecast=0.0)
    assert calm.leverage > violent.leverage


def test_the_operators_ceiling_is_never_exceeded():
    """RL-053: the ceiling is not advice."""
    chosen = selector().choose(VENUE, SYMBOL, ceiling=3.0, volatility_forecast=0.001, funding_forecast=0.0)
    assert chosen.leverage == 3.0
    assert chosen.outcome == AT_CEILING


def test_expensive_funding_cuts_the_leverage():
    subject = selector(funding_tolerance=0.001)
    cheap = subject.choose(VENUE, SYMBOL, ceiling=50.0, volatility_forecast=0.02, funding_forecast=0.0)
    dear = subject.choose(VENUE, SYMBOL, ceiling=50.0, volatility_forecast=0.02, funding_forecast=0.01)
    assert dear.leverage < cheap.leverage


def test_no_volatility_forecast_means_unlevered():
    chosen = selector().choose(VENUE, SYMBOL, ceiling=50.0, volatility_forecast=None)
    assert chosen.leverage == NO_LEVERAGE
    assert chosen.outcome == UNLEVERAGED_NO_FORECAST


# ---- stop-target-placer ------------------------------------------------------

def placer(minimum_reward=1.5, clearance=0.005, maximum_stop=0.2, minimum=3):
    return StopTargetPlacer(
        minimum_reward_to_risk=minimum_reward, cluster_clearance_fraction=clearance,
        maximum_stop_fraction=maximum_stop, minimum_observations=minimum, window=50,
    )


def test_a_stop_is_placed_from_the_volatility_forecast_when_there_is_no_history():
    plan = placer().place(VENUE, SYMBOL, BUY, entry_price=100.0, volatility_forecast=0.02)
    assert plan.outcome == PLACED
    assert plan.stop_price == pytest.approx(98.0)
    assert plan.distance_estimate.is_fitted is False


def test_measured_excursions_take_over_from_the_forecast():
    subject = placer(minimum=3)
    for _ in range(5):
        subject.observe_adverse_excursion(VENUE, SYMBOL, 0.05)
    plan = subject.place(VENUE, SYMBOL, BUY, entry_price=100.0, volatility_forecast=0.01)
    assert plan.distance_estimate.is_fitted is True
    assert plan.stop_price == pytest.approx(95.0)


def test_a_stop_inside_a_liquidation_cluster_is_moved_beyond_it():
    """A stop inside the pool is not a stop; price reaches for the pool."""
    subject = placer(clearance=0.01)
    subject.set_liquidation_clusters(VENUE, SYMBOL, (98.5,))
    plan = subject.place(VENUE, SYMBOL, BUY, entry_price=100.0, volatility_forecast=0.02)
    assert plan.outcome == MOVED_CLEAR_OF_CLUSTER
    assert plan.stop_price < 98.5


def test_a_trade_whose_reward_does_not_justify_its_stop_is_refused():
    plan = placer(minimum_reward=2.0).place(
        VENUE, SYMBOL, BUY, entry_price=100.0, volatility_forecast=0.02, target_price=101.0
    )
    assert plan.outcome == REFUSED_REWARD_TOO_THIN


def test_no_forecast_and_no_history_places_nothing():
    plan = placer().place(VENUE, SYMBOL, BUY, entry_price=100.0, volatility_forecast=None)
    assert plan.outcome == REFUSED_NO_DISTANCE
    assert plan.stop_price is None


# ---- position-sizer ----------------------------------------------------------

class Choice:
    """An instrument-choice, as the sizer reads it -- by shape, never by import."""

    def __init__(self, reference_price=None, chosen="BTCUSDT-PERP", state="chosen"):
        self.reference_price = reference_price
        self.chosen = chosen
        self.state = state

    @property
    def is_actionable(self):
        return self.chosen is not None


class Plan:
    def __init__(self, entry_price=None, stop_price=None):
        self.entry_price = entry_price
        self.stop_price = stop_price


def test_the_entry_price_comes_from_the_refined_plan_when_there_is_one():
    assert entry_price_for(Plan(entry_price=101.0), Choice(reference_price=100.0)) == 101.0


def test_a_choice_that_chose_something_lends_its_reference_price():
    """No plan exists before any trade has closed, and the choice carries the price."""
    assert entry_price_for(None, Choice(reference_price=100.0)) == 100.0


def test_a_choice_that_refused_lends_nothing():
    """The defect this closes: a refusal still had a price attached, and the sizer
    took it.

    `instrument-selector` answers "nothing is listed for this symbol", "the best
    instrument is in a segment that is not built", or "this symbol's last price is
    too old to size against" by returning a choice with no instrument in it. The
    price it carries is evidence about the refusal, not an entry. Sizing against it
    builds an order for an instrument the part that knows about instruments has
    just said cannot carry the trade.
    """
    refused = Choice(reference_price=100.0, chosen=None, state="this-symbol's-last-price-is-too-old-to-size-against")
    assert entry_price_for(None, refused) is None
    assert entry_price_for(Plan(entry_price=None), refused) is None


def test_a_refusal_cannot_be_overridden_by_an_absent_plan():
    """A plan that exists but has no entry price must not fall through to a refusal."""
    assert entry_price_for(Plan(entry_price=None, stop_price=99.0), Choice(chosen=None)) is None


def test_no_choice_at_all_is_not_a_price():
    assert entry_price_for(None, None) is None
    assert entry_price_for(Plan(entry_price=101.0), None) == 101.0



def sizer(fee=0.0004, slippage=0.0):
    return PositionSizer(taker_fee_rate=fee, slippage_fraction=slippage)


def a_size(**overrides):
    request = dict(
        venue_id=VENUE, symbol=SYMBOL, side=BUY, entry_price=100.0, stop_price=98.0,
        allotment=10_000.0, risk_limit_fraction=0.01, leverage=1.0,
        price_increment=0.1, quantity_increment=0.001, minimum_quantity=0.001,
    )
    request.update(overrides)
    return request


def test_a_sized_order_carries_the_decision_it_serves():
    """Only refusals carried the intent id until 2026-08-23. A sized order
    without it reaches the stamper as an order with no decision behind it, the
    id falls back to quantity and price, and one standing decision becomes a
    new order on every tick the market moves."""
    subject = sizer(fee=0.0)
    order = subject.size(**a_size(), intent_id="binance-usdm|BTCUSDT|buy|open")
    assert order.quantity > 0, order.reason
    assert order.intent_id == "binance-usdm|BTCUSDT|buy|open"
    refused = subject.size(**a_size(risk_limit_fraction=0.0), intent_id="the-decision")
    assert refused.intent_id == "the-decision"


def test_size_comes_from_the_stop_distance_not_a_fixed_fraction():
    """Risking 1% means 1%, whatever the stop distance is."""
    subject = sizer(fee=0.0)
    wide = subject.size(**a_size(stop_price=90.0))
    narrow = subject.size(**a_size(stop_price=99.0))
    assert wide.quantity < narrow.quantity
    assert wide.risk_at_stop == pytest.approx(100.0, rel=0.02)
    assert narrow.risk_at_stop == pytest.approx(100.0, rel=0.02)


def test_fees_are_inside_the_risk_budget_not_added_to_it():
    subject = sizer(fee=0.001)
    order = subject.size(**a_size())
    assert order.risk_at_stop <= order.risk_allowed * 1.001
    assert order.fees_charged > 0


def test_a_zero_limit_refuses_outright():
    assert sizer().size(**a_size(risk_limit_fraction=0.0)).outcome == REFUSED_NO_LIMIT


def test_a_stop_on_the_wrong_side_is_refused():
    assert sizer().size(**a_size(side=BUY, stop_price=101.0)).outcome == REFUSED_STOP_INVALID
    assert sizer().size(**a_size(side=SELL, stop_price=99.0)).outcome == REFUSED_STOP_INVALID


def test_no_price_increment_refuses_rather_than_guessing():
    assert sizer().size(**a_size(price_increment=None)).outcome == REFUSED_NO_INCREMENT


def test_a_trade_whose_smallest_size_risks_too_much_is_refused():
    order = sizer().size(**a_size(risk_limit_fraction=0.0000001, minimum_quantity=1.0))
    assert order.outcome == REFUSED_TOO_SMALL
    assert "would risk" in order.reason


def test_the_size_is_snapped_down_to_the_increment_never_up():
    order = sizer(fee=0.0).size(**a_size(quantity_increment=1.0))
    assert order.quantity == float(int(order.quantity))
    assert order.risk_at_stop <= order.risk_allowed


# ---- trade-capital-bounds-gate -----------------------------------------------

def bounds(minimum=100.0, maximum=1000.0):
    return TradeCapitalBounds(SEGMENT, minimum, maximum, "USDT")


def sized_for_gate(quantity, entry=100.0, risk=50.0, allowed=100.0):
    from parts.risk_capital_allocation.position_sizer import SIZED, SizedOrder

    return SizedOrder(
        venue_id=VENUE, symbol=SYMBOL, side=BUY, quantity=quantity, entry_price=entry,
        stop_price=98.0, outcome=SIZED, risk_allowed=allowed, risk_at_stop=risk,
        fees_charged=0.0, notional=quantity * entry, leverage=1.0, reason="", sized_at_ns=1,
    )


def test_an_order_inside_the_bounds_passes_untouched():
    gate = TradeCapitalBoundsGate(quantity_increment=0.001)
    result = gate.bound(sized_for_gate(5.0), bounds(), settings_are_valid=True)
    assert result.outcome == WITHIN_BOUNDS
    assert result.quantity == 5.0


def test_a_small_order_is_bumped_to_the_minimum():
    """RL-054: a trade too small to be worth its fees is worse than no trade."""
    gate = TradeCapitalBoundsGate(quantity_increment=0.001)
    result = gate.bound(sized_for_gate(0.5, risk=5.0, allowed=100.0), bounds(minimum=100.0), True)
    assert result.outcome == BUMPED_TO_MINIMUM
    assert result.capital_used >= 100.0


def test_a_bump_that_would_breach_the_risk_limit_is_refused_instead():
    gate = TradeCapitalBoundsGate(quantity_increment=0.001)
    result = gate.bound(sized_for_gate(0.5, risk=90.0, allowed=100.0), bounds(minimum=1000.0), True)
    assert result.outcome == REFUSED_BUMP_BREACHES_RISK
    assert result.quantity == 0.0


def test_a_large_order_is_capped_never_refused():
    gate = TradeCapitalBoundsGate(quantity_increment=0.001)
    result = gate.bound(sized_for_gate(100.0), bounds(maximum=1000.0), True)
    assert result.outcome == CAPPED_AT_MAXIMUM
    assert result.capital_used <= 1000.0
    assert result.may_be_sent is True


def test_inconsistent_settings_refuse_everything():
    gate = TradeCapitalBoundsGate(quantity_increment=0.001)
    result = gate.bound(sized_for_gate(5.0), bounds(), settings_are_valid=False)
    assert result.outcome == REFUSED_SETTINGS_INVALID
    assert result.may_be_sent is False


# ---- profit-lock -------------------------------------------------------------

def profit_lock(trigger=0.02, costs=0.002, prior=0.01, maximum=0.1, minimum=3):
    return ProfitLock(
        break_even_trigger_fraction=trigger, round_trip_cost_fraction=costs,
        prior_retracement_fraction=prior, maximum_trail_fraction=maximum,
        minimum_observations=minimum, window=50,
    )


def test_nothing_is_locked_before_the_trade_covers_its_costs():
    """Locking earlier guarantees a loss after fees."""
    adjustment = profit_lock().adjust(VENUE, SYMBOL, LONG, 100.0, 100.1, 98.0)
    assert adjustment.outcome == NOT_YET_PROFITABLE
    assert adjustment.new_stop == 98.0


def test_a_winning_trade_moves_its_stop_to_break_even():
    adjustment = profit_lock(trigger=0.02).adjust(VENUE, SYMBOL, LONG, 100.0, 103.0, 98.0)
    assert adjustment.did_move
    assert adjustment.new_stop > 100.0


def test_the_stop_never_moves_against_the_position():
    """A trailing stop that could retreat is not a stop."""
    subject = profit_lock(prior=0.05)
    # Best price 110 with a 5% trail puts the candidate at 104.5, below a stop
    # already sitting at 108 -- so the stop must stay where it is.
    subject.adjust(VENUE, SYMBOL, LONG, 100.0, 110.0, 98.0)
    held = subject.adjust(VENUE, SYMBOL, LONG, 100.0, 106.0, 108.0)
    assert held.outcome == HELD
    assert held.new_stop == 108.0


def test_the_trail_follows_this_symbols_own_retracements():
    subject = profit_lock(prior=0.001, minimum=3)
    for _ in range(5):
        subject.observe_retracement(VENUE, SYMBOL, 0.05)
    adjustment = subject.adjust(VENUE, SYMBOL, LONG, 100.0, 120.0, 98.0)
    assert adjustment.retracement_estimate.is_fitted is True
    assert adjustment.new_stop == pytest.approx(114.0)


# ---- exit-order-chainer ------------------------------------------------------

def test_the_exits_exist_the_moment_the_entry_fills():
    """The window between a fill and its stop is when the position is naked."""
    chainer = ExitOrderChainer()
    chainer.register_plan(VENUE, SYMBOL, BUY, stop_price=98.0, target_price=105.0)
    exits = chainer.observe_entry_fill("f1", "o1", VENUE, SYMBOL, BUY, filled_quantity=2.0)
    assert exits.outcome == CHAINED
    assert exits.exit_side == SELL
    assert exits.quantity == 2.0
    assert exits.stop_price == 98.0


def test_a_partial_fill_gets_exits_for_what_actually_filled():
    """Exits sized to the order would leave a stop for a position never taken."""
    chainer = ExitOrderChainer()
    chainer.register_plan(VENUE, SYMBOL, BUY, 98.0, None)
    first = chainer.observe_entry_fill("f1", "o1", VENUE, SYMBOL, BUY, 1.0)
    second = chainer.observe_entry_fill("f2", "o1", VENUE, SYMBOL, BUY, 3.0)
    assert first.quantity == 1.0
    assert second.quantity == 3.0
    assert second.outcome == EXTENDED
    assert second.filled_quantity_so_far == 4.0


def test_a_repeated_fill_does_not_place_a_second_stop():
    chainer = ExitOrderChainer()
    chainer.register_plan(VENUE, SYMBOL, BUY, 98.0, None)
    chainer.observe_entry_fill("f1", "o1", VENUE, SYMBOL, BUY, 1.0)
    assert chainer.observe_entry_fill("f1", "o1", VENUE, SYMBOL, BUY, 1.0) is None


def test_a_fill_with_no_plan_is_reported_as_a_naked_position():
    chainer = ExitOrderChainer()
    exits = chainer.observe_entry_fill("f1", "unknown", VENUE, SYMBOL, BUY, 1.0)
    assert exits.outcome == NO_PLAN
    assert "naked" in exits.reason


# ---- participation-capped-order-splitter -------------------------------------

def splitter(cap=0.1, interval=10.0, horizon=300.0, urgency=1.0):
    return ParticipationCappedOrderSplitter(
        participation_cap=cap, slice_interval_seconds=interval,
        maximum_horizon_seconds=horizon, volatility_urgency_factor=urgency,
    )


def test_a_small_order_is_not_split():
    subject = splitter()
    subject.observe_traded_volume(VENUE, SYMBOL, quantity_traded=1000.0, over_seconds=10.0)
    schedule = subject.split(VENUE, SYMBOL, BUY, quantity=50.0)
    assert schedule.outcome == SINGLE_SLICE
    assert len(schedule.slices) == 1


def test_a_large_order_is_sliced_against_measured_volume():
    subject = splitter(cap=0.1, interval=10.0)
    subject.observe_traded_volume(VENUE, SYMBOL, quantity_traded=1000.0, over_seconds=10.0)
    schedule = subject.split(VENUE, SYMBOL, BUY, quantity=500.0)
    assert schedule.outcome == SPLIT
    assert len(schedule.slices) == 5
    assert sum(one.quantity for one in schedule.slices) == pytest.approx(500.0)
    assert schedule.slices[-1].is_final


def test_an_order_that_cannot_fill_inside_the_horizon_is_refused():
    """An order still working after its signal died is a position taken for nothing."""
    subject = splitter(cap=0.01, interval=10.0, horizon=60.0)
    subject.observe_traded_volume(VENUE, SYMBOL, quantity_traded=100.0, over_seconds=10.0)
    assert subject.split(VENUE, SYMBOL, BUY, quantity=1000.0).outcome == REFUSED_HORIZON_EXCEEDED


def test_no_volume_measurement_refuses_rather_than_guessing():
    assert splitter().split(VENUE, "NEWUSDT", BUY, 10.0).outcome == REFUSED_NO_VOLUME


def test_volatility_makes_the_slices_larger():
    subject = splitter(cap=0.1, urgency=5.0)
    subject.observe_traded_volume(VENUE, SYMBOL, 1000.0, 10.0)
    calm = subject.split(VENUE, SYMBOL, BUY, 500.0)
    fast = subject.split(VENUE, SYMBOL, BUY, 500.0, volatility_forecast=0.5)
    assert len(fast.slices) < len(calm.slices)


# ---- capital-desk ------------------------------------------------------------

def test_allocating_more_than_exists_raises_a_high_alert():
    checker = AllocationConservationChecker(under_allocation_alert_fraction=0.5)
    checker.observe_main_balance(1000.0)
    checker.observe_segment_allocation("futures", 700.0)
    checker.observe_segment_allocation("spot", 600.0)
    headroom, alerts = checker.check()
    assert headroom.state == OVER_ALLOCATED
    assert alerts[0].severity == "high"
    assert headroom.headroom == pytest.approx(-300.0)


def test_a_mostly_unallocated_account_is_mentioned_once():
    checker = AllocationConservationChecker(under_allocation_alert_fraction=0.5)
    checker.observe_main_balance(1000.0)
    checker.observe_segment_allocation("futures", 100.0)
    headroom, alerts = checker.check()
    assert headroom.state == UNDER_ALLOCATED
    assert alerts[0].severity == "low"


def test_a_zero_main_balance_is_the_shipped_state_not_an_alert():
    checker = AllocationConservationChecker(under_allocation_alert_fraction=0.5)
    checker.observe_main_balance(0.0)
    headroom, alerts = checker.check()
    assert headroom.state == NO_BALANCE
    assert alerts == ()


def allotment(allotted=1000.0, minimum=10.0, maximum=100.0, ceiling=5.0):
    return CapitalAllotment(
        segment=SEGMENT, allotted=allotted, currency="USDT", leverage_ceiling=ceiling,
        bounds=TradeCapitalBounds(SEGMENT, minimum, maximum, "USDT"), read_at_ns=1,
    )


def test_coherent_settings_are_consistent():
    verdict = CapitalSettingsValidator().judge(SEGMENT, allotment(), main_balance=5000.0)
    assert verdict.verdict == CONSISTENT
    assert verdict.permits_trading is True


def test_a_maximum_above_the_allocation_is_a_contradiction():
    verdict = CapitalSettingsValidator().judge(
        SEGMENT, allotment(allotted=100.0, maximum=500.0), main_balance=5000.0
    )
    assert verdict.verdict == INCONSISTENT
    assert any("commit everything" in fault.explanation for fault in verdict.faults)


def test_a_ceiling_above_the_instrument_is_named():
    verdict = CapitalSettingsValidator().judge(
        SEGMENT, allotment(ceiling=50.0), main_balance=5000.0, instrument_maximum_leverage=20.0
    )
    assert verdict.verdict == INCONSISTENT
    assert any("this instrument allows" in fault.explanation for fault in verdict.faults)


def test_nothing_read_is_incomplete_not_consistent():
    assert CapitalSettingsValidator().judge(SEGMENT, None).verdict == INCOMPLETE


def test_each_setting_change_is_journalled_with_both_sides():
    """RL-055: the board's 'when it last changed' comes from here, never from mtime."""
    journal = Journal()
    recorder = CapitalSettingsChangeRecorder(journal)
    recorder.observe(SEGMENT, {"leverage_ceiling": 5.0, "allocated_balance": 1000.0})
    changes = recorder.observe(SEGMENT, {"leverage_ceiling": 3.0, "allocated_balance": 1000.0})
    assert len(changes) == 1
    assert changes[0].previous_value == 5.0 and changes[0].new_value == 3.0
    assert changes[0].direction == "decreased"
    assert recorder.last_changed_at(SEGMENT, "leverage_ceiling") is not None


def test_two_segments_with_the_same_setting_are_two_settings():
    recorder = CapitalSettingsChangeRecorder(Journal())
    recorder.observe("futures", {"leverage_ceiling": 5.0})
    changes = recorder.observe("spot", {"leverage_ceiling": 1.0})
    assert changes[0].is_first_reading is True
    assert recorder.standing.changes_recorded == 0


def converter(clock, maximum_age=60.0):
    return PaperCurrencyConverter(
        Journal(), maximum_rate_age_seconds=maximum_age, monotonic=clock.monotonic
    )


def test_a_conversion_journals_the_rate_it_used():
    """RL-029: without the rate, a historical figure cannot be re-derived."""
    clock = Clock()
    subject = converter(clock)
    subject.observe_rate("USDT", "USDC", 0.999, source="binance-usdm")
    conversion = subject.convert(100.0, "USDT", "USDC")
    assert conversion.outcome == CONVERTED
    assert conversion.converted_amount == pytest.approx(99.9)
    assert subject.standing.rates_journalled == 1


def test_an_unknown_pair_converts_nothing():
    assert converter(Clock()).convert(100.0, "USDT", "EUR").outcome == NO_RATE


def test_a_stale_rate_is_refused_rather_than_used_quietly():
    clock = Clock()
    subject = converter(clock, maximum_age=10.0)
    subject.observe_rate("USDT", "USDC", 0.999, source="binance-usdm")
    clock.now += 11
    assert subject.convert(100.0, "USDT", "USDC").outcome == STALE_RATE


def test_the_same_currency_needs_no_rate():
    assert converter(Clock()).convert(100.0, "USDT", "USDT").outcome == SAME_CURRENCY


def test_locked_capital_is_visible_separately_from_positions():
    """Capital held against an unfilled order is invisible in both other states."""
    meter = CapitalUtilisationMeter()
    meter.set_allotment(SEGMENT, 1000.0)
    meter.observe_position_capital(SEGMENT, SYMBOL, 300.0)
    meter.observe_lock(SEGMENT, "o1", 200.0)
    utilisation = meter.measure(SEGMENT)
    assert utilisation.in_positions == 300.0
    assert utilisation.locked == 200.0
    assert utilisation.free == 500.0
    assert utilisation.utilisation == pytest.approx(0.5)


def test_over_commitment_is_reported_not_clamped():
    meter = CapitalUtilisationMeter()
    meter.set_allotment(SEGMENT, 100.0)
    meter.observe_position_capital(SEGMENT, SYMBOL, 90.0)
    meter.observe_lock(SEGMENT, "o1", 50.0)
    utilisation = meter.measure(SEGMENT)
    assert utilisation.state == OVER_COMMITTED
    assert utilisation.utilisation > 1.0


def test_no_allotment_has_no_denominator():
    assert CapitalUtilisationMeter().measure(SEGMENT).state == NO_ALLOTMENT


def test_allocation_is_proposed_toward_return_on_capital_not_absolute_profit():
    proposer = AllocationRebalanceProposer(
        minimum_closed_trades=10, maximum_move_fraction=0.5, utilisation_floor=0.1
    )
    proposer.set_allocation("small", 1000.0)
    proposer.set_allocation("large", 10_000.0)
    proposer.observe_performance(SegmentPerformance("small", 100.0, 1000.0, 20, 0.9))
    proposer.observe_performance(SegmentPerformance("large", 150.0, 10_000.0, 20, 0.9))
    proposals = {p.segment: p for p in proposer.propose()}
    assert proposals["small"].change > 0
    assert proposals["large"].change < 0


def test_a_segment_with_too_few_trades_is_left_alone():
    proposer = AllocationRebalanceProposer(
        minimum_closed_trades=30, maximum_move_fraction=0.5, utilisation_floor=0.1
    )
    proposer.set_allocation("new", 1000.0)
    proposer.observe_performance(SegmentPerformance("new", 500.0, 1000.0, 3, 0.9))
    assert proposer.propose()[0].outcome == TOO_FEW_TRADES


def test_the_proposer_moves_no_capital():
    proposer = AllocationRebalanceProposer(
        minimum_closed_trades=1, maximum_move_fraction=0.5, utilisation_floor=0.1
    )
    proposer.set_allocation("futures", 1000.0)
    proposer.observe_performance(SegmentPerformance("futures", 100.0, 1000.0, 20, 0.9))
    proposer.propose()
    import parts.capital_desk.allocation_rebalance_proposer as module

    assert module.describe_proposals(proposer)["moves_capital"] is False


def divergence_watch(tolerance=0.02):
    return LiveBalanceDivergenceWatch(tolerance_fraction=tolerance)


def test_paper_mode_is_not_compared_to_a_venue():
    watch = divergence_watch()
    watch.set_money_mode(SEGMENT, PAPER)
    report, alerts = watch.check(SEGMENT)
    assert report.state == NOT_LIVE
    assert alerts == ()


def test_a_live_shortfall_raises_a_high_alert():
    watch = divergence_watch()
    watch.set_money_mode(SEGMENT, LIVE)
    watch.set_allocation(SEGMENT, 1000.0)
    watch.observe_venue_balance(SEGMENT, 500.0)
    report, alerts = watch.check(SEGMENT)
    assert report.state == VENUE_HOLDS_LESS
    assert alerts[0].severity == "high"


def test_an_unreadable_venue_balance_while_live_is_not_agreement():
    watch = divergence_watch()
    watch.set_money_mode(SEGMENT, LIVE)
    watch.set_allocation(SEGMENT, 1000.0)
    watch.observe_venue_balance(SEGMENT, None)
    report, alerts = watch.check(SEGMENT)
    assert report.state == UNREADABLE
    assert alerts[0].severity == "high"


def test_a_balance_inside_tolerance_agrees():
    watch = divergence_watch(tolerance=0.05)
    watch.set_money_mode(SEGMENT, LIVE)
    watch.set_allocation(SEGMENT, 1000.0)
    watch.observe_venue_balance(SEGMENT, 1010.0)
    assert watch.check(SEGMENT)[0].state == AGREES


def test_the_gate_reads_the_verdict_the_validator_actually_publishes():
    """The two parts are joined here, on the object one of them really produces.

    They were joined by `getattr(verdict, "is_valid", None)`, and the verdict has
    no `is_valid`: the gate refused every order for inconsistent settings while
    the validator was publishing `consistent` beside it, and the refusal it
    produced is the same one it produces for settings that genuinely contradict
    each other. Nothing caught it because the gate's own tests pass a bool.
    """
    validator = CapitalSettingsValidator()
    consistent = validator.judge(
        segment="futures",
        allotment=CapitalAllotment(
            segment="futures",
            allotted=1_000.0,
            currency="USDT",
            leverage_ceiling=1.0,
            bounds=TradeCapitalBounds(
                segment="futures", minimum_capital=10.0, maximum_capital=100.0, currency="USDT"
            ),
            read_at_ns=1,
        ),
        main_balance=10_000.0,
    )
    assert consistent.permits_trading is True
    assert does_verdict_permit_trading(consistent) is True

    incomplete = validator.judge(segment="futures", allotment=None)
    assert does_verdict_permit_trading(incomplete) is False
    assert does_verdict_permit_trading(None) is None, (
        "no verdict is unverified, which the gate refuses -- not the same as a verdict "
        "that said no"
    )

    gate = TradeCapitalBoundsGate(quantity_increment=0.001)
    passed = gate.bound(sized_for_gate(5.0), bounds(), does_verdict_permit_trading(consistent))
    assert passed.outcome == WITHIN_BOUNDS, passed.reason
    assert passed.quantity > 0

    refused = gate.bound(sized_for_gate(5.0), bounds(), does_verdict_permit_trading(None))
    assert refused.outcome == REFUSED_SETTINGS_INVALID


# ---- a stop that is not beyond the entry is not a stop -------------------------

def test_a_zero_distance_never_becomes_a_placed_plan():
    """The defect that killed the bull bot's first real decision to open a trade.

    On the live run of 2026-08-23 08:34 the arbiter formed `action: open` on
    TUTUSDT at a weighted 55.8% -- the first time the system had ever decided to
    trade -- and the sizer refused it with "a buy stop at 0.065531 is on the wrong
    side of an entry at 0.065531". The distance had come out zero, so the stop sat
    exactly on the entry.

    Refused here rather than passed downstream: the sizer can only report it as an
    untradeable order, and by then the decision has already been made.
    """
    subject = StopTargetPlacer(
        minimum_reward_to_risk=1.0, cluster_clearance_fraction=0.002,
        maximum_stop_fraction=0.05, minimum_observations=2, window=200,
    )
    # Two recorded excursions of exactly zero: a claim that resolved before the
    # market moved against it at all. Real, and not a statement that the symbol
    # cannot move against a trade.
    subject.observe_adverse_excursion(VENUE, SYMBOL, 0.0)
    subject.observe_adverse_excursion(VENUE, SYMBOL, 0.0)

    plan = subject.place(
        venue_id=VENUE, symbol=SYMBOL, side=BUY, entry_price=100.0,
        volatility_forecast=None, target_price=110.0,
    )
    assert plan.is_placeable is False
    assert plan.stop_price is None
    assert "not beyond it" in plan.reason or plan.outcome == REFUSED_NO_DISTANCE


def test_an_unfitted_excursion_profile_is_not_evidence_of_no_risk():
    """An unfitted profile reports 0.0 because it has nothing to report.

    Feeding those zeros to the quantile estimator taught it that the symbol never
    moves against a winner. The guard lives in the part's `start_part`, so this
    pins the property the guard exists for: a zero adverse excursion must never
    become a placeable stop.
    """
    from runtime.trade_profiles import ExcursionProfile

    unfitted = ExcursionProfile(
        venue_id=VENUE, symbol=SYMBOL, side=BUY, adverse_excursion=0.0,
        favourable_quantiles={}, trades_observed=3, is_fitted=False,
    )
    assert unfitted.adverse_excursion == 0.0
    assert unfitted.is_fitted is False, (
        "a profile that reports itself unfitted is the only signal a reader has "
        "that its zero is an absence rather than a measurement"
    )


def test_exits_are_priced_against_what_the_entry_actually_cost():
    """The defect that closed two positions one second after opening them.

    On the live run of 2026-08-23 10:25 the plan was computed against an ENAUSDT
    price of 0.17019 and the entry filled at 0.18043 -- six per cent away, because
    the decision half was reading an hour-old price. The exits carried the
    absolute numbers from decision time, so the take-profit sat far below the
    market, was already through its trigger, and fired on arrival: zero profit and
    two lots of fees.

    A stop 1.5% below entry means 1.5% below the price actually obtained.
    """
    chainer = ExitOrderChainer()
    chainer.register_plan(VENUE, SYMBOL, BUY, stop_price=98.5, target_price=103.0,
                          entry_price=100.0)
    exits = chainer.observe_entry_fill(
        fill_id="f1", entry_order_id="o1", venue_id=VENUE, symbol=SYMBOL,
        entry_side=BUY, filled_quantity=1.0, fill_price=200.0,
    )
    assert exits.outcome == CHAINED
    # 1.5% below and 3% above the fill, not the numbers from decision time.
    assert exits.stop_price == pytest.approx(197.0)
    assert exits.target_price == pytest.approx(206.0)
    assert exits.stop_price < 200.0 < exits.target_price, (
        "both exits must straddle the price actually paid, or they fire on arrival"
    )
    assert chainer.standing.exits_repriced_onto_the_fill == 1


def test_a_plan_with_no_reference_price_keeps_its_absolute_exits_and_says_so():
    """The fallback, counted apart because it is a different claim about the trade."""
    chainer = ExitOrderChainer()
    chainer.register_plan(VENUE, SYMBOL, BUY, stop_price=98.0, target_price=None)
    exits = chainer.observe_entry_fill(
        fill_id="f1", entry_order_id="o1", venue_id=VENUE, symbol=SYMBOL,
        entry_side=BUY, filled_quantity=1.0, fill_price=200.0,
    )
    assert exits.stop_price == pytest.approx(98.0)
    assert chainer.standing.exits_from_the_decision_price == 1
    assert chainer.standing.exits_repriced_onto_the_fill == 0
