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
from parts.risk_capital_allocation.capital_allotment_reader import (
    AllotmentUnreadable, CapitalAllotmentReader,
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
    HUMAN_OVERRIDE, POLICY_REFUSAL, TRADING_HALT, HaltEnforcer, wants_halt,
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
    SIZED, SHRUNK_TO_FIT, PositionSizer, entry_price_for, opening_order_target,
    quantity_increment_for, stop_price_for,
)
from parts.risk_capital_allocation.profit_lock import (
    HELD, MOVED_TO_BREAK_EVEN, NOT_YET_PROFITABLE, TRAILED, ProfitLock,
    decide_every_stop,
)
from parts.risk_capital_allocation.stop_frequency_breaker import StopFrequencyBreaker
from parts.risk_capital_allocation.stop_target_placer import (
    MOVED_CLEAR_OF_CLUSTER, PLACED, REFUSED_NO_DISTANCE, REFUSED_REWARD_TOO_THIN,
    StopTargetPlacer, order_side_for_intent, priced_entry_for, priced_target_for,
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
    "position-flattener": "parts.risk_capital_allocation.position_flattener",
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


# ---- capital-allotment-reader -------------------------------------------------

def a_segment_settings_file(tmp_path, maximum_capital_per_trade=500.0, minimum_capital_per_trade=50.0):
    path = tmp_path / "futures.toml"
    path.write_text(
        f"""
[allocated_balance]
value = 1000000.0
unit = "USDT"
note = "test"

[minimum_capital_per_trade]
value = {minimum_capital_per_trade}
unit = "USDT"
note = "test"

[maximum_capital_per_trade]
value = {maximum_capital_per_trade}
unit = "USDT"
note = "test"

[leverage_ceiling]
value = 20.0
unit = "multiple"
note = "test"

[quote_currency]
value = "USDT"
unit = "currency"
note = "test"
"""
    )
    return path


def test_the_segments_own_maximum_binds_when_no_main_account_setting_has_arrived(tmp_path):
    reader = CapitalAllotmentReader("futures", settings_path=a_segment_settings_file(tmp_path))
    allotment = reader.read(main_account_maximum_capital_per_trade=None)
    assert allotment.bounds.maximum_capital == pytest.approx(500.0)


def test_the_tighter_of_the_two_maximums_binds(tmp_path):
    """The live bug (2026-08-30): main-account.toml's own maximum_capital_per_trade
    was read and published but nothing ever enforced it -- only the segment's own,
    independently-editable ceiling actually bound an order."""
    path = a_segment_settings_file(tmp_path, maximum_capital_per_trade=500.0)
    reader = CapitalAllotmentReader("futures", settings_path=path)
    allotment = reader.read(main_account_maximum_capital_per_trade=200.0)
    assert allotment.bounds.maximum_capital == pytest.approx(200.0)


def test_the_segments_own_maximum_binds_when_it_is_the_tighter_one(tmp_path):
    path = a_segment_settings_file(tmp_path, maximum_capital_per_trade=150.0)
    reader = CapitalAllotmentReader("futures", settings_path=path)
    allotment = reader.read(main_account_maximum_capital_per_trade=200.0)
    assert allotment.bounds.maximum_capital == pytest.approx(150.0)


def test_a_main_account_maximum_below_the_segments_minimum_is_refused(tmp_path):
    path = a_segment_settings_file(
        tmp_path, maximum_capital_per_trade=500.0, minimum_capital_per_trade=50.0,
    )
    reader = CapitalAllotmentReader("futures", settings_path=path)
    with pytest.raises(AllotmentUnreadable):
        reader.read(main_account_maximum_capital_per_trade=10.0)


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


# A limit is a level, and these two limiters decide one only when their input
# arrives -- an equity reading, a trade closing. Publishing only then leaves a
# limiter that is holding a brake on indistinguishable from one that has stopped,
# and the sizer stops believing a limiter that has gone quiet (it holds each
# limiter's word for `risk_limit_maximum_age_seconds`, because that is the only
# way to tell a stopped limiter from a silent one). Trades close hours apart, so
# stop-frequency-breaker was silent for essentially all of the time it was
# braking.


def test_a_breaker_restates_its_word_without_a_new_reading():
    subject = breaker(maximum=0.2)
    subject.observe_equity(1000.0)
    subject.observe_equity(700.0)
    assert subject.is_tripped

    restated = subject.read_limit()
    assert restated.fraction_of_allotment == NO_RISK_ALLOWED
    assert subject.read_limit() is restated, "a restatement is the same word, not a new decision"


def test_a_breaker_that_has_never_decided_says_nothing():
    """Never decided is not all-clear, and a limiter with nothing to say says nothing."""
    assert breaker().read_limit() is None


def test_a_stop_frequency_breaker_restates_its_word_between_trades():
    subject = frequency_breaker(window=10, prior=0.2, excess=1.5)
    for _ in range(11):
        subject.observe_closed_trade(was_stopped_out=True)
    assert subject.is_tripped

    assert subject.read_limit().fraction_of_allotment == NO_RISK_ALLOWED


def test_a_stop_frequency_breaker_that_has_seen_no_trade_says_nothing():
    assert frequency_breaker().read_limit() is None


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


def test_every_real_human_override_instruction_that_should_halt_does():
    """Until 2026-08-30 this checked `"halt" in instruction.lower()`, which
    matches none of human-override-reader's five real instructions -- an
    operator's stop-trading was published, read, and enforced nothing.
    """
    assert wants_halt("stop-everything") is True
    assert wants_halt("stop-trading") is True
    assert wants_halt("close-positions") is True


def test_instructions_that_are_not_about_stopping_trading_do_not_halt():
    assert wants_halt("stop-self-modification") is False
    assert wants_halt("resume") is False


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


def hold_with_a_stop(subject, symbol, entry, quantity, stop):
    """A position, and where its stop is -- what the caps are actually about."""
    subject.observe_position(
        VENUE, symbol, abs(quantity) * entry, entry_price=entry, quantity=quantity,
    )
    subject.observe_stop(VENUE, symbol, stop)


def test_a_position_is_measured_by_what_it_risks_not_by_what_it_is_worth():
    """The operator's own example, from `risk_maximum_per_position_fraction`.

    "at the 10000 USDT paper balance that is 100 USDT at risk per trade, and a
    stop 0.5% away therefore buys about 20000 USDT of notional". Summed as
    notional that one position is 200% of the allotment; as risk it is 1%.

    Measured on the live spine at 15:16 on 2026-08-26 with the notional
    arithmetic: 12 open positions read as 199% against a 5% total cap and
    position-sizer refused 62,935 of 71,233 actionable intents.
    """
    subject = exposure(per_position=0.01, total=0.05, per_cluster=0.02)
    entry = 100.0
    # 20,000 USDT of notional at a stop 0.5% away: 100 USDT at risk.
    hold_with_a_stop(subject, "BTCUSDT", entry=entry, quantity=200.0, stop=entry * 0.995)

    assert subject.total_exposure == pytest.approx(0.01)
    assert subject.read_limit("ETHUSDT").fraction_of_allotment > 0.0, (
        "one position risking 1% of the allotment used up a 5% book"
    )


def test_five_positions_at_full_risk_fill_the_book():
    """What the operator said the total cap is for: five trades at full size."""
    subject = exposure(per_position=0.01, total=0.05, per_cluster=1.0)
    for index in range(5):
        hold_with_a_stop(
            subject, f"S{index}USDT", entry=100.0, quantity=200.0, stop=99.5,
        )
    assert subject.total_exposure == pytest.approx(0.05)
    assert subject.read_limit("NEWUSDT").fraction_of_allotment == pytest.approx(0.0)


def test_a_position_with_no_stop_is_counted_at_everything_it_could_lose():
    """A position with no stop resting can lose all of it, so its notional is its risk.

    Measured 2026-08-26: stop-order-manager had placed 0 stops against 12 open
    positions, so this is the state of the book rather than a defensive default.
    """
    subject = exposure(per_position=0.01, total=0.05, per_cluster=1.0)
    hold(subject, "BTCUSDT", 0.2)

    assert subject.total_exposure == pytest.approx(0.2)
    assert subject.standing.positions_without_a_stop == 1
    assert subject.read_limit("ETHUSDT").fraction_of_allotment == pytest.approx(0.0)


def test_a_stop_moved_up_gives_the_book_its_room_back():
    """What profit-lock trailing a stop is worth to the rest of the book."""
    # The total cap is the one under test, so it is the tightest of the three.
    subject = exposure(per_position=0.05, total=0.05, per_cluster=1.0)
    hold_with_a_stop(subject, "BTCUSDT", entry=100.0, quantity=200.0, stop=99.0)
    assert subject.total_exposure == pytest.approx(0.02)
    assert subject.read_limit().fraction_of_allotment == pytest.approx(0.03)

    # profit-lock trails the stop three quarters of the way to the entry.
    subject.observe_stop(VENUE, "BTCUSDT", 99.75)

    assert subject.total_exposure == pytest.approx(0.005)
    assert subject.read_limit().fraction_of_allotment == pytest.approx(0.045), (
        "a position that now risks a quarter of what it did left the book no roomier"
    )


def test_a_stop_beyond_the_position_cannot_risk_more_than_the_position():
    """A stop the wrong side of entry is a fault in the stop, not a bigger position."""
    subject = exposure(per_position=1.0, total=1.0, per_cluster=1.0)
    # Short 10 at 100 -- 1000 USDT of position -- with its stop 200 above, which
    # is a loss of 2000 if it is ever reached.
    hold_with_a_stop(subject, "BTCUSDT", entry=100.0, quantity=-10.0, stop=300.0)

    assert subject.total_exposure == pytest.approx(0.1), (
        "a 1000 USDT position measured as risking more than 1000 USDT"
    )


def test_a_position_that_closes_forgets_its_stop():
    subject = exposure(per_position=0.01, total=0.05, per_cluster=1.0)
    hold_with_a_stop(subject, "BTCUSDT", entry=100.0, quantity=200.0, stop=99.5)
    subject.observe_position(VENUE, "BTCUSDT", 0.0)
    # Reopened at the same size, with no stop yet: it must not inherit the old one.
    hold(subject, "BTCUSDT", 0.2)

    assert subject.total_exposure == pytest.approx(0.2)
    assert subject.standing.positions_without_a_stop == 1


# ---- one decision, one order -------------------------------------------------
#
# The arbiter publishes a standing opinion every tick, so the sizer sized the same
# decision every tick and an order request went onto the bus each time. Measured on
# the live spine at 15:56 on 2026-08-26: `sized` 26,402 and paper-fill-simulator
# holding 26,911 in flight against 12 fills -- about 57 requests a second for
# perhaps thirty standing decisions. Nothing was double-filled; the whole execution
# path carried every duplicate to the point where it could be recognised as one.


def a_sized_order(intent_id, quantity, entry_price, outcome="sized", side=BUY):
    from parts.risk_capital_allocation.position_sizer import SizedOrder

    return SizedOrder(
        venue_id=VENUE, symbol=SYMBOL, side=side, quantity=quantity,
        entry_price=entry_price, stop_price=entry_price * 0.99, outcome=outcome,
        risk_allowed=100.0, risk_at_stop=100.0, fees_charged=0.1,
        notional=quantity * entry_price, leverage=1.0, reason="under test",
        sized_at_ns=1, intent_id=intent_id,
    )


def stated_orders(refresh_interval=5.0):
    """The sizer's own publishing seam, with a clock a test can move."""
    from runtime.level_publishing import LevelPublisherByKey

    published = []
    clock = Clock()
    stated = LevelPublisherByKey(
        publish=lambda items: published.extend(items),
        refresh_interval_seconds=refresh_interval,
        monotonic=lambda: clock.now,
        identity_of=lambda items: tuple(
            (item.intent_id, item.side, item.quantity, item.outcome) for item in items
        ),
    )

    def state(order):
        stated.publish_level(order.intent_id, (order,))

    return state, published, clock


def test_a_decision_sized_again_at_a_drifting_price_is_one_order():
    state, published, clock = stated_orders()
    decision = "binance-usdm|BTCUSDT|buy|open"

    for tick, price in enumerate((100.0, 100.02, 99.98, 100.05)):
        clock.now += 0.25
        state(a_sized_order(decision, quantity=2.0, entry_price=price))

    assert len(published) == 1, (
        f"one decision put {len(published)} order requests on the bus"
    )


def test_a_decision_whose_size_changes_is_a_new_order():
    """The dedupe must not swallow a decision the desk sized differently."""
    state, published, clock = stated_orders()
    decision = "binance-usdm|BTCUSDT|buy|open"

    state(a_sized_order(decision, quantity=2.0, entry_price=100.0))
    clock.now += 0.25
    state(a_sized_order(decision, quantity=3.0, entry_price=100.0))

    assert [order.quantity for order in published] == [2.0, 3.0]


def test_an_unchanged_decision_is_said_again_on_the_refresh():
    """The restatement is what stops a lost datagram meaning a trade never happens."""
    state, published, clock = stated_orders(refresh_interval=5.0)
    decision = "binance-usdm|BTCUSDT|buy|open"

    state(a_sized_order(decision, quantity=2.0, entry_price=100.0))
    clock.now += 4.0
    state(a_sized_order(decision, quantity=2.0, entry_price=100.0))
    assert len(published) == 1

    clock.now += 1.5
    state(a_sized_order(decision, quantity=2.0, entry_price=100.0))
    assert len(published) == 2


def test_two_decisions_do_not_share_a_clock():
    """One symbol being re-sized must not restate the rest of the book."""
    state, published, clock = stated_orders()

    state(a_sized_order("binance-usdm|BTCUSDT|buy|open", 2.0, 100.0))
    clock.now += 1.0
    state(a_sized_order("binance-usdm|ETHUSDT|buy|open", 5.0, 50.0))
    clock.now += 1.0
    state(a_sized_order("binance-usdm|BTCUSDT|buy|open", 2.0, 100.0))

    assert len(published) == 2, "an unchanged decision was restated by another's clock"


def test_a_refusal_is_deduplicated_too_and_a_changed_one_is_not():
    """A refusal repeated every tick is the same waste as an order repeated."""
    state, published, clock = stated_orders()
    decision = "binance-usdm|BTCUSDT|buy|open"

    state(a_sized_order(decision, 0.0, 100.0, outcome="refused-no-risk-allowed"))
    clock.now += 0.25
    state(a_sized_order(decision, 0.0, 100.0, outcome="refused-no-risk-allowed"))
    assert len(published) == 1

    clock.now += 0.25
    state(a_sized_order(decision, 2.0, 100.0, outcome="sized"))
    assert [order.outcome for order in published] == ["refused-no-risk-allowed", "sized"]


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
    limits = subject.read_limits()

    assert binding_for(limits, SYMBOL) == NO_RISK_ALLOWED
    assert subject.standing.state == NEAR_LIQUIDATION
    assert binding_for(limits, "SOMETHINGELSEUSDT") > 0.0, (
        "one position approaching its own liquidation stopped an unrelated symbol"
    )


def test_a_thin_equity_cushion_stops_new_risk_even_with_distant_liquidations():
    subject = margin_watch(distance=0.1, headroom=0.2)
    subject.observe_liquidation_price(VENUE, SYMBOL, liquidation_price=50.0, mark_price=100.0)
    subject.observe_account(equity=1000.0, maintenance_requirement=900.0)
    limits = subject.read_limits()

    assert subject.standing.state == NEAR_MARGIN_CALL
    # Account-wide, unlike the per-position stops: the maintenance requirement is
    # one number for the whole account and every symbol draws on it.
    assert binding_for(limits, "SOMETHINGELSEUSDT") == NO_RISK_ALLOWED


def test_an_unmeasurable_liquidation_price_stops_rather_than_assuming_safety():
    """Assuming it is safe is the assumption that ends a segment."""
    subject = margin_watch()
    subject.observe_liquidation_price(VENUE, SYMBOL, liquidation_price=None, mark_price=100.0)
    limits = subject.read_limits()

    assert binding_for(limits, SYMBOL) == NO_RISK_ALLOWED
    assert subject.standing.state == UNKNOWN_DISTANCE
    assert subject.standing.symbols_stopped_for_an_unmeasurable_position == 1


def test_an_unmeasurable_position_does_not_stop_the_rest_of_the_book():
    """The whole-segment stop this part used to issue, and what it cost.

    A liquidation price is computed from the leverage a position was opened at,
    leverage-selector answers only while an intent is being formed, and a position
    restored from a checkpoint has no leverage-choice behind it. Measured on the
    live spine at 15:01 on 2026-08-26: 12 open positions, liquidation-price-
    tracker refusing 7,041 of them for "no leverage-choice for this position",
    this watch issuing an unscoped zero every tick, and position-sizer refusing
    3,799 of 4,065 actionable intents -- with the nearest measurable liquidation
    99.5% away.
    """
    subject = margin_watch()
    subject.observe_liquidation_price(VENUE, SYMBOL, liquidation_price=None, mark_price=100.0)
    subject.observe_liquidation_price(VENUE, "ETHUSDT", liquidation_price=50.0, mark_price=100.0)
    subject.observe_account(equity=1000.0, maintenance_requirement=100.0)
    limits = subject.read_limits()

    assert binding_for(limits, SYMBOL) == NO_RISK_ALLOWED
    assert binding_for(limits, "ETHUSDT") > 0.0, (
        "a position nobody could measure stopped a position that was measured and safe"
    )
    assert binding_for(limits, "SOLUSDTNOTHELD") > 0.0, (
        "a position nobody could measure stopped a symbol with no position at all"
    )


def test_a_safe_account_permits_risk():
    subject = margin_watch()
    subject.observe_liquidation_price(VENUE, SYMBOL, liquidation_price=50.0, mark_price=100.0)
    subject.observe_account(equity=1000.0, maintenance_requirement=100.0)
    assert binding_for(subject.read_limits(), SYMBOL) == 1.0
    assert subject.standing.state == SAFE


def binding_for(limits, symbol: str) -> float:
    """What the sizer would allow on this symbol, given one limiter's statement."""
    applying = [
        limit.fraction_of_allotment for limit in limits if limit.applies_to(symbol)
    ]
    assert applying, f"no limit in this statement applies to {symbol}"
    return min(applying)


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
    limits = event_limiter(Clock()).read_limits()
    assert len(limits) == 1, "with nothing in force there is one limit, about everything"
    assert limits[0].fraction_of_allotment == 1.0
    assert limits[0].symbols == ()


def test_overlapping_causes_multiply_rather_than_taking_the_worst():
    """A listing during a turbulent hour is riskier than either alone."""
    clock = Clock()
    subject = event_limiter(clock)
    subject.register_announcement("listing", shrink_to=0.5, seconds=60, reason="new listing")
    subject.observe_turbulence(index=2.0, normal_index=1.0, seconds=60)
    assert subject.read_limits()[0].fraction_of_allotment == pytest.approx(0.25)


def test_a_scheduled_events_window_opens_before_it_happens():
    clock = Clock()
    subject = event_limiter(clock)
    subject.register_scheduled_event(
        "funding", seconds_until=60, window_seconds=30, shrink_to=0.5, reason="funding settles"
    )
    assert subject.read_limits()[0].fraction_of_allotment == 1.0
    clock.now += 31
    assert subject.read_limits()[0].fraction_of_allotment == pytest.approx(0.5)


def test_an_anomaly_decays_back_rather_than_releasing_at_a_cliff():
    clock = Clock()
    subject = event_limiter(clock)
    subject.register_anomaly("gap", shrink_to=0.2, decay_seconds=100, reason="unexplained gap")
    first = subject.read_limits()[0].fraction_of_allotment
    clock.now += 50
    middle = subject.read_limits()[0].fraction_of_allotment
    clock.now += 60
    assert first < middle < subject.read_limits()[0].fraction_of_allotment


# ---- leverage-selector -------------------------------------------------------

def selector(target=0.05, horizons=2.0, carry_tolerance=0.001, maintenance=0.005,
             daily_borrowing_rate=0.0):
    return LeverageSelector(
        target_liquidation_distance=target,
        volatility_horizons_to_survive=horizons,
        carry_tolerance_per_day=carry_tolerance,
        daily_borrowing_rate=daily_borrowing_rate,
        maintenance_margin_rate=maintenance,
    )


def test_a_violent_symbol_gets_less_leverage_than_a_calm_one():
    subject = selector()
    calm = subject.choose(VENUE, SYMBOL, ceiling=50.0, volatility_forecast=0.01)
    violent = subject.choose(VENUE, "ALTUSDT", ceiling=50.0, volatility_forecast=0.10)
    assert calm.leverage > violent.leverage


def test_the_operators_ceiling_is_never_exceeded():
    """RL-053: the ceiling is not advice."""
    chosen = selector().choose(VENUE, SYMBOL, ceiling=3.0, volatility_forecast=0.001)
    assert chosen.leverage == 3.0
    assert chosen.outcome == AT_CEILING


def test_expensive_borrowing_cuts_the_leverage():
    """The Indian analogue of the funding test this replaces: what is borrowed
    is the part of the position the broker's margin does not cover, and paying
    for it reduces how much is worth borrowing.

    At 5x the broker covers a fifth, so four fifths is borrowed; a rate of 1% a
    day on that is 0.8% of notional, eight times the 0.1% tolerance.
    """
    free = selector(daily_borrowing_rate=0.0)
    dear = selector(daily_borrowing_rate=0.01)

    cheap_choice = free.choose(
        VENUE, SYMBOL, ceiling=50.0, volatility_forecast=0.01,
        broker_available_leverage=5.0,
    )
    dear_choice = dear.choose(
        VENUE, SYMBOL, ceiling=50.0, volatility_forecast=0.01,
        broker_available_leverage=5.0,
    )

    assert dear_choice.leverage < cheap_choice.leverage
    assert dear.standing.carry_reduced == 1
    assert free.standing.carry_reduced == 0


def test_borrowing_nothing_costs_nothing():
    """Unlevered borrows none of the position, so no carry can apply -- the
    crypto version charged 0.5 of the leverage for an unknown funding rate,
    which on an Indian segment halved every choice for a cost nobody levies."""
    subject = selector(daily_borrowing_rate=0.01)

    assert subject.carry_cost_per_day(1.0) == 0.0
    assert subject.carry_cost_per_day(None) is None


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


class PlacerIntent:
    def __init__(self, side=SHORT, action="open"):
        self.side = side
        self.action = action


def test_a_bearish_open_on_an_option_is_a_buy_not_a_sell():
    """The defect this closes, live on 2026-09-07: this part derived BUY/SELL
    from the intent's own long/short, so a bearish view (SHORT) read as SELL
    -- but a buy-only options segment expresses a bearish view by *buying* a
    put. stop_from_refined_plan and refused_stop_invalid matched exactly,
    2,040 of 2,040: every stop this part placed for a short view sat above an
    entry the BUY order never traded above."""
    instrument = Choice(chosen=Contract("NIFTY24500PE"), order_side=BUY)
    assert order_side_for_intent(PlacerIntent(side=SHORT, action="open"), instrument) == BUY


def test_an_open_with_no_instrument_choice_has_no_side_to_guess():
    assert order_side_for_intent(PlacerIntent(action="open"), Choice(chosen=None)) is None
    assert order_side_for_intent(PlacerIntent(action="open"), None) is None


def test_a_close_keeps_its_own_translated_side():
    """Not a fresh selection -- reduce/close act on the contract already
    held, so they use the intent's own side, the same rule opening_order_target
    applies in position_sizer."""
    assert order_side_for_intent(PlacerIntent(side=SHORT, action="close"), None) == SELL
    assert order_side_for_intent(PlacerIntent(side=LONG, action="close"), None) == BUY


def test_the_chosen_instruments_own_price_is_what_gets_priced():
    """An option's premium (~220), not the underlying's spot the bot reasoned
    in (~24,500) -- the scale the order and the stop actually live on."""
    instrument = Choice(reference_price=220.0, chosen=Contract())
    assert priced_entry_for(instrument, underlying_entry=24_500.0) == 220.0


def test_an_unpriced_instrument_yields_no_entry_rather_than_the_underlyings():
    """The underlying's price is never an entry for a contract.

    It used to be, whenever the choice named a contract this part could not
    price. `position-sizer` sizes an open from exactly this number, so those
    orders went out on the underlying's scale -- INFY 1200 CE at 1,088.40
    against a real premium of 21.10 -- and `paper-fill-simulator` refused
    84.6% of the orders that reached a verdict on 2026-09-07 as
    `decision-price-stale`. An order on the wrong scale is worse than no
    order, so there is no entry and the caller skips.
    """
    assert priced_entry_for(Choice(chosen=None), underlying_entry=24_500.0) is None
    assert priced_entry_for(None, underlying_entry=24_500.0) is None
    # A contract that was chosen but carries no reference price is the same
    # refusal: chosen is not the same fact as priced.
    assert priced_entry_for(
        Choice(reference_price=None, chosen=Contract()), underlying_entry=24_500.0,
    ) is None


def test_a_target_is_translated_by_the_same_fraction_not_carried_over_raw():
    """A 2% target above a 24,500 underlying entry is a 2% target above
    whatever entry is actually being sized against -- not 24,990 sitting next
    to a 220 option premium."""
    target = priced_target_for(
        nearest_underlying_target=24_990.0, underlying_entry=24_500.0, priced_entry=220.0,
    )
    assert target == pytest.approx(220.0 * 1.02)


def test_no_underlying_entry_is_not_a_target():
    assert priced_target_for(24_990.0, underlying_entry=None, priced_entry=220.0) is None
    assert priced_target_for(None, underlying_entry=24_500.0, priced_entry=220.0) is None


# ---- position-sizer ----------------------------------------------------------

class Contract:
    """The chosen instrument, as the sizer reads it -- by shape, never by import."""

    def __init__(self, contract_symbol="BTCUSDT-PERP"):
        self.contract_symbol = contract_symbol


class Choice:
    """An instrument-choice, as the sizer reads it -- by shape, never by import."""

    def __init__(
        self, reference_price=None, chosen="BTCUSDT-PERP", state="chosen", order_side=None,
        quantity_increment=None,
    ):
        self.reference_price = reference_price
        self.chosen = chosen
        self.state = state
        self.order_side = order_side
        self.quantity_increment = quantity_increment

    @property
    def is_actionable(self):
        return self.chosen is not None


class OpeningIntent:
    def __init__(self, symbol="NIFTY24500CE", side=SHORT, action="open"):
        self.symbol = symbol
        self.side = side
        self.action = action


def test_an_open_is_placed_on_the_contract_the_selector_chose():
    """Until 2026-09-04 the order carried the symbol the intent named and the
    selector's decision reached nothing -- so a bearish view on a call became a
    sell-to-open on that call, writing a naked option in a buy-only segment."""
    target = opening_order_target(
        OpeningIntent(symbol="NIFTY24500CE", side=SHORT),
        Choice(chosen=Contract("NIFTY24500PE"), order_side=BUY),
    )

    assert target == ("NIFTY24500PE", BUY)


def test_an_open_with_no_instrument_choice_is_not_placed_at_all():
    """A stop-target-plan's entry price alone used to be enough to open a
    position in an instrument nothing had selected."""
    assert opening_order_target(OpeningIntent(), Choice(chosen=None)) is None
    assert opening_order_target(OpeningIntent(), Choice(chosen=Contract(), order_side=None)) is None


def test_a_close_keeps_the_contract_it_is_closing():
    """A close acts on the contract actually held; it is not a fresh selection,
    and re-selecting one would close a position nobody opened."""
    target = opening_order_target(
        OpeningIntent(symbol="NIFTY24500PE", side=SHORT, action="close"),
        Choice(chosen=None),
    )

    assert target == ("NIFTY24500PE", SELL)


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


class Intent:
    def __init__(self, stop_price=None, risk_fraction=None):
        self.stop_price = stop_price
        self.risk_fraction = risk_fraction


def test_a_fraction_is_reapplied_to_the_actual_entry_price():
    """The defect this closes, live on 2026-09-07: the exit-plan proposer sets
    both `stop_price` and `risk_fraction` against the *underlying's* price
    (NIFTY spot ~24,500), but entry_price_for may return the option contract's
    own premium (~220) once instrument-selector has chosen one. Reading the
    absolute stop_price against that premium put it on the wrong side of entry
    on 23,654 of 43,635 actionable intents and no options trade could open.
    The fraction is scale-free and gives a stop in the entry's own scale."""
    intent = Intent(stop_price=24_450.0, risk_fraction=0.02)
    assert stop_price_for(None, intent, entry_price=220.0, side=BUY) == pytest.approx(215.6)


def test_a_refined_plan_still_wins_over_the_fraction():
    assert stop_price_for(Plan(stop_price=210.0), Intent(risk_fraction=0.02), 220.0, BUY) == 210.0


def test_a_sell_stop_sits_above_entry_not_below():
    intent = Intent(risk_fraction=0.02)
    assert stop_price_for(None, intent, entry_price=220.0, side=SELL) == pytest.approx(224.4)


def test_no_fraction_falls_back_to_the_intents_own_stop_price():
    """What every intent without an exit plan behind it already carried."""
    intent = Intent(stop_price=99.0, risk_fraction=None)
    assert stop_price_for(None, intent, entry_price=100.0, side=BUY) == 99.0


def test_no_entry_price_cannot_be_reconstructed_from_a_fraction():
    intent = Intent(stop_price=99.0, risk_fraction=0.02)
    assert stop_price_for(None, intent, entry_price=None, side=BUY) == 99.0



def sizer(fee=0.0004, slippage=0.0):
    return PositionSizer(
        taker_fee_rate=fee, slippage_fraction=slippage,
        close_restated_after_seconds=30.0,
    )


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

def test_a_refusal_for_no_risk_names_the_limiter_that_bound_it():
    """Six parts publish `risk-limit` and the sizer takes the smallest.

    "No risk allowed" was one number with six possible authors, so a run that
    sized nothing said nothing about which part to go and look at -- measured on
    the live spine at 20:05 on 2026-08-26, 4,724 of 13,185 actionable intents.
    """
    sizer = PositionSizer(
        taker_fee_rate=0.0004, slippage_fraction=0.0, close_restated_after_seconds=30.0,
    )
    result = sizer.size(
        venue_id=VENUE, symbol=SYMBOL, side=BUY, entry_price=100.0, stop_price=98.0,
        allotment=10_000.0, risk_limit_fraction=0.0, leverage=1.0,
        price_increment=0.01, quantity_increment=0.001, minimum_quantity=0.001,
        bound_by="margin-liquidation-watch",
    )

    assert result.outcome == REFUSED_NO_LIMIT
    assert "margin-liquidation-watch" in result.reason
    assert sizer.standing.refused_by_limiter == {"margin-liquidation-watch": 1}


def test_a_limit_that_names_no_author_is_still_counted_as_one():
    """Silence about the author is its own state, not an absent refusal."""
    sizer = PositionSizer(
        taker_fee_rate=0.0004, slippage_fraction=0.0, close_restated_after_seconds=30.0,
    )
    sizer.size(
        venue_id=VENUE, symbol=SYMBOL, side=BUY, entry_price=100.0, stop_price=98.0,
        allotment=10_000.0, risk_limit_fraction=0.0, leverage=1.0,
        price_increment=0.01, quantity_increment=0.001, minimum_quantity=0.001,
    )

    assert sizer.standing.refused_by_limiter == {"a limiter that did not name itself": 1}


def bounds(minimum=100.0, maximum=1000.0):
    return TradeCapitalBounds(SEGMENT, minimum, maximum, "USDT")


def sized_for_gate(
    quantity, entry=100.0, risk=50.0, allowed=100.0, leverage=1.0, quantity_increment=0.0,
):
    from parts.risk_capital_allocation.position_sizer import SIZED, SizedOrder

    return SizedOrder(
        venue_id=VENUE, symbol=SYMBOL, side=BUY, quantity=quantity, entry_price=entry,
        stop_price=98.0, outcome=SIZED, risk_allowed=allowed, risk_at_stop=risk,
        fees_charged=0.0, notional=quantity * entry, leverage=leverage, reason="",
        sized_at_ns=1, quantity_increment=quantity_increment,
    )


# The operator's bound is on what a trade **commits**, not on what it controls.
# `maximum_capital_per_trade` says "the most one trade may commit" and
# `leverage_ceiling` says how far that commitment reaches; measured against the
# notional, the ceiling does nothing at all. It did nothing until 2026-08-26: the
# operator raised it from 1 to 10 at 12:42 and the three trades that opened after
# that committed 99.47, 100.17 and 100.09 USDT of notional against a 100 maximum,
# exactly as they had at 1x.


def test_leverage_decides_how_far_a_commitment_reaches():
    gate = TradeCapitalBoundsGate(quantity_increment=0.001)
    # 10 units at 100 is 1,000 of notional. At 10x that commits 100.
    result = gate.bound(
        sized_for_gate(10.0, leverage=10.0), bounds(minimum=50.0, maximum=100.0), True
    )

    assert result.outcome == WITHIN_BOUNDS, (
        "an order committing exactly the maximum was treated as ten times it"
    )
    assert result.capital_used == pytest.approx(100.0)
    assert result.quantity == 10.0


def test_the_same_order_unlevered_is_cut_to_the_maximum():
    """The same notional at 1x commits ten times as much, and the bound bites."""
    gate = TradeCapitalBoundsGate(quantity_increment=0.001)
    result = gate.bound(
        sized_for_gate(10.0, leverage=1.0), bounds(minimum=50.0, maximum=100.0), True
    )

    assert result.outcome == CAPPED_AT_MAXIMUM
    assert result.capital_used == pytest.approx(100.0)
    assert result.quantity == pytest.approx(1.0)


def test_a_cap_at_leverage_cuts_to_what_the_maximum_commitment_buys():
    gate = TradeCapitalBoundsGate(quantity_increment=0.001)
    # 100 units at 100 is 10,000 of notional; at 5x that commits 2,000, over the
    # 100 maximum, so it is cut to the 500 of notional 100 commits at 5x.
    result = gate.bound(
        sized_for_gate(100.0, leverage=5.0), bounds(minimum=50.0, maximum=100.0), True
    )

    assert result.outcome == CAPPED_AT_MAXIMUM
    assert result.capital_used == pytest.approx(100.0)
    assert result.quantity == pytest.approx(5.0)


def test_a_bump_at_leverage_buys_what_the_minimum_commitment_buys():
    gate = TradeCapitalBoundsGate(quantity_increment=0.001)
    result = gate.bound(
        sized_for_gate(0.1, risk=1.0, allowed=100.0, leverage=10.0),
        bounds(minimum=50.0, maximum=100.0), True,
    )

    assert result.outcome == BUMPED_TO_MINIMUM
    assert result.capital_used == pytest.approx(50.0)
    # 50 committed at 10x is 500 of notional, which is 5 units at 100.
    assert result.quantity == pytest.approx(5.0)


def test_an_order_with_no_leverage_behind_it_commits_its_whole_notional():
    """Missing is unlevered, which is the conservative reading and the old one."""
    from parts.risk_capital_allocation.trade_capital_bounds_gate import leverage_behind

    class Unlevered:
        leverage = 0.0

    assert leverage_behind(Unlevered()) == 1.0
    assert leverage_behind(object()) == 1.0


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


def a_position(symbol, entry, price, stop, direction=LONG):
    return {
        "venue_id": VENUE, "symbol": symbol, "direction": direction,
        "entry_price": entry, "current_price": price, "current_stop": stop,
    }


def test_a_stop_that_did_not_move_is_still_where_the_stop_is():
    """A stop is a level, and a position under water never moves its own.

    Measured on the live spine at 15:34 on 2026-08-26: 140,924 adjustments
    decided and none published, because publishing was gated on `did_move`.
    stop-order-manager held 0 stops against 12 open positions, and
    exposure-limiter -- which counts a position with no stop at its full
    notional -- read the book at 199% of a 5% cap and allowed nothing anywhere.
    """
    subject = profit_lock(trigger=0.02)
    stated = decide_every_stop(subject, (
        a_position("SINKINGUSDT", entry=100.0, price=97.0, stop=95.0),
        a_position("WINNINGUSDT", entry=100.0, price=103.0, stop=95.0),
    ))

    assert len(stated) == 2, "a position whose stop held was not stated at all"
    by_symbol = {adjustment.symbol: adjustment for adjustment in stated}
    assert by_symbol["SINKINGUSDT"].did_move is False
    assert by_symbol["SINKINGUSDT"].new_stop == 95.0, (
        "the stop that is actually resting on this position was not the one stated"
    )
    assert by_symbol["WINNINGUSDT"].did_move is True


def test_stating_every_stop_still_says_which_ones_moved():
    """`did_move` still means what it said; it just no longer decides who hears."""
    subject = profit_lock(trigger=0.02)
    held = decide_every_stop(subject, (a_position(SYMBOL, 100.0, 100.1, 98.0),))[0]

    assert held.outcome == NOT_YET_PROFITABLE
    assert held.did_move is False


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


# ---- a halt's scope reaches the sizer (2026-08-25) ---------------------------

def test_a_symbol_scoped_halt_zeroes_only_those_symbols():
    """Two anomalous symbols out of a hundred stopped every trade for an hour.

    trading-halt-decider narrows a halt to the symbols it was raised over, and
    halt-enforcer had nowhere to put that: it published one zero limit for the
    whole segment. The scope travels on the limit now, and position-sizer applies
    a limit only to the symbols it is about.
    """
    from parts.risk_capital_allocation.halt_enforcer import HaltEnforcer, TRADING_HALT

    enforcer = HaltEnforcer(allowed_fraction_when_clear=1.0)
    enforcer.raise_halt(TRADING_HALT, "ZECUSDT,ENAUSDT", "an anomaly on two symbols")
    limit = enforcer.read_limit()

    assert limit.fraction_of_allotment == 0.0
    assert limit.symbols == ("ZECUSDT", "ENAUSDT")
    assert limit.applies_to("ZECUSDT")
    assert not limit.applies_to("BTCUSDT")
    assert enforcer.standing.symbol_scoped_limits_issued == 1


def test_a_halt_over_everything_still_stops_every_symbol():
    from parts.risk_capital_allocation.halt_enforcer import HaltEnforcer, TRADING_HALT

    enforcer = HaltEnforcer(allowed_fraction_when_clear=1.0)
    enforcer.raise_halt(TRADING_HALT, "everything", "the exposure view is not reporting")
    limit = enforcer.read_limit()

    assert limit.symbols == ()
    assert limit.applies_to("BTCUSDT")
    assert enforcer.standing.symbol_scoped_limits_issued == 0


def test_a_scope_that_cannot_be_read_binds_everything():
    """Narrowing on a guess is how a halt stops protecting what it was raised over."""
    from parts.risk_capital_allocation.halt_enforcer import HaltEnforcer, TRADING_HALT

    enforcer = HaltEnforcer(allowed_fraction_when_clear=1.0)
    enforcer.raise_halt(TRADING_HALT, "", "a source that named no scope")
    assert enforcer.read_limit().applies_to("BTCUSDT")


# ---- an event on one symbol must not shrink the others ------------------------
#
# Measured on the live spine at 12:45 on 2026-08-26. market-anomaly-detector had
# raised 2,899 anomalies of the kind "one venue moved and the others did not"
# across 100 venue-symbols. 636 were active at once, and event-risk-limiter
# multiplied all 636 into a single unscoped limit: `smallest_shrink` 2.7e-237, a
# limit indistinguishable from zero on every symbol -- including the ninety-odd
# that had no event at all. position-sizer refused 1,284 intents with
# `refused_no_risk_allowed` while opinion-arbiter was forming 479 actionable ones
# at conviction 0.95.
#
# The multiplication is not the bug and is kept: a listing during a turbulent hour
# really is riskier than either alone. What was wrong is *what* was multiplied.
# That reasoning is about causes overlapping on one symbol, and applying it across
# symbols is a different operation wearing the same arithmetic -- one that does
# not converge. This is the defect halt-enforcer was fixed for on 2026-08-25, in a
# second part.


def test_an_anomaly_on_one_symbol_leaves_the_others_alone():
    clock = Clock()
    subject = event_limiter(clock)
    subject.register_anomaly(
        "binance-usdm:ETHUSDT", shrink_to=0.2, decay_seconds=100,
        reason="one venue moved and the others did not", symbols=("ETHUSDT",),
    )
    limits = subject.read_limits()

    for_eth = [limit for limit in limits if limit.applies_to("ETHUSDT")]
    for_btc = [limit for limit in limits if limit.applies_to("BTCUSDT")]
    assert min(limit.fraction_of_allotment for limit in for_eth) < 1.0
    assert min(limit.fraction_of_allotment for limit in for_btc) == 1.0, (
        "an anomaly on ETHUSDT shrank the risk allowed on BTCUSDT"
    )


def test_many_symbols_each_with_an_anomaly_do_not_compound_into_nothing():
    """636 active events multiplied to 2.7e-237. This is that, in miniature."""
    clock = Clock()
    subject = event_limiter(clock)
    for index in range(200):
        subject.register_anomaly(
            f"binance-usdm:SYM{index}USDT", shrink_to=0.2, decay_seconds=1000,
            reason="unexplained gap", symbols=(f"SYM{index}USDT",),
        )
    limits = subject.read_limits()

    for index in (0, 100, 199):
        symbol = f"SYM{index}USDT"
        binding = min(
            limit.fraction_of_allotment for limit in limits if limit.applies_to(symbol)
        )
        assert binding == pytest.approx(0.2), (
            f"{symbol} was shrunk to {binding} -- its own anomaly shrinks to 0.2, and the "
            f"other 199 symbols' anomalies are not about it"
        )
    untouched = min(
        limit.fraction_of_allotment for limit in limits if limit.applies_to("BTCUSDT")
    )
    assert untouched == 1.0, "a symbol with no event of its own was shrunk anyway"


def test_two_causes_on_one_symbol_still_multiply():
    """The design intent, preserved: overlapping causes on a symbol compound."""
    clock = Clock()
    subject = event_limiter(clock)
    subject.register_anomaly(
        "binance-usdm:ETHUSDT", shrink_to=0.5, decay_seconds=1000,
        reason="gap", symbols=("ETHUSDT",),
    )
    subject.register_announcement(
        "ETHUSDT", shrink_to=0.5, seconds=1000, reason="listing", symbols=("ETHUSDT",),
    )
    binding = min(
        limit.fraction_of_allotment
        for limit in subject.read_limits()
        if limit.applies_to("ETHUSDT")
    )
    assert binding == pytest.approx(0.25)


def test_a_market_wide_cause_still_reaches_every_symbol():
    """Turbulence is about the whole book and must keep binding everything."""
    clock = Clock()
    subject = event_limiter(clock)
    subject.observe_turbulence(index=2.0, normal_index=1.0, seconds=60)
    limits = subject.read_limits()
    for symbol in ("BTCUSDT", "ETHUSDT", "SOMETHINGELSEUSDT"):
        binding = min(
            limit.fraction_of_allotment for limit in limits if limit.applies_to(symbol)
        )
        assert binding == pytest.approx(0.5)


# ---- a condition restated is not a second condition ---------------------------
#
# Measured on the live spine at 13:28 on 2026-08-26, three minutes after a
# restart: 165 events registered and 165 still active, 35 of them scoped to a
# symbol and the rest the same market-wide turbulence over and over, and
# `smallest_shrink` 2.2e-53. A detector of an ongoing condition repeats itself --
# turbulence on every index reading, an anomaly on every trade that disagrees --
# and every repeat was being registered as another independent cause and
# multiplied in. Scoping the symbol-level causes (above) fixed one half of the
# non-convergence; this is the other half, and it bites the market-wide limit
# that binds every symbol.


def test_the_same_turbulence_restated_does_not_compound():
    clock = Clock()
    subject = event_limiter(clock)
    for _ in range(200):
        subject.observe_turbulence(index=2.0, normal_index=1.0, seconds=60)
    limits = subject.read_limits()

    assert len(subject.active_events) == 1, "one continuing condition became many"
    assert subject.standing.events_restated == 199
    assert min(
        limit.fraction_of_allotment for limit in limits if limit.applies_to("BTCUSDT")
    ) == pytest.approx(0.5)


def test_the_same_anomaly_seen_again_is_the_same_anomaly():
    clock = Clock()
    subject = event_limiter(clock)
    for _ in range(50):
        subject.register_anomaly(
            "binance-usdm:ETHUSDT:one venue moved and the others did not",
            shrink_to=0.2, decay_seconds=1000, reason="gap of 3.1%",
            symbols=("ETHUSDT",),
        )
    binding = min(
        limit.fraction_of_allotment
        for limit in subject.read_limits()
        if limit.applies_to("ETHUSDT")
    )
    assert binding == pytest.approx(0.2), (
        f"one anomaly seen fifty times shrank ETHUSDT to {binding}"
    )


def test_two_different_anomalies_on_one_symbol_are_two_conditions():
    """Restating must not merge causes that are genuinely distinct."""
    clock = Clock()
    subject = event_limiter(clock)
    subject.register_anomaly(
        "binance-usdm:ETHUSDT:one venue moved and the others did not",
        shrink_to=0.5, decay_seconds=1000, reason="gap", symbols=("ETHUSDT",),
    )
    subject.register_anomaly(
        "binance-usdm:ETHUSDT:the book crossed",
        shrink_to=0.5, decay_seconds=1000, reason="crossed book", symbols=("ETHUSDT",),
    )
    binding = min(
        limit.fraction_of_allotment
        for limit in subject.read_limits()
        if limit.applies_to("ETHUSDT")
    )
    assert binding == pytest.approx(0.25)
    assert len(subject.active_events) == 2


def test_a_restatement_carries_the_newest_window():
    """A condition that is still going on must not expire on its first window."""
    clock = Clock()
    subject = event_limiter(clock)
    subject.observe_turbulence(index=2.0, normal_index=1.0, seconds=60)
    clock.now += 50
    subject.observe_turbulence(index=2.0, normal_index=1.0, seconds=60)
    clock.now += 20

    assert len(subject.active_events) == 1, "the restated condition expired on the old window"


def test_a_symbols_own_cause_stacks_on_top_of_a_market_wide_one():
    """A symbol with an event is never treated as calmer than the market."""
    clock = Clock()
    subject = event_limiter(clock)
    subject.observe_turbulence(index=2.0, normal_index=1.0, seconds=60)
    subject.register_anomaly(
        "binance-usdm:ETHUSDT", shrink_to=0.5, decay_seconds=1000,
        reason="gap", symbols=("ETHUSDT",),
    )
    limits = subject.read_limits()

    eth = min(limit.fraction_of_allotment for limit in limits if limit.applies_to("ETHUSDT"))
    btc = min(limit.fraction_of_allotment for limit in limits if limit.applies_to("BTCUSDT"))
    assert eth == pytest.approx(0.25), "the symbol's own cause did not stack on the market's"
    assert btc == pytest.approx(0.5)


# ---- leverage-selector and the broker's own limit (2026-09-05) ---------------
#
# Bot 3's 5x had no source until broker-margin-quoter. The ceiling in the
# segment file and the volatility-implied figure are both this system's
# opinions; what the broker will lend is not, and an order above it is rejected
# rather than trimmed.

def test_the_broker_s_limit_binds_even_when_volatility_would_allow_more():
    from parts.risk_capital_allocation.leverage_selector import AT_BROKER_LIMIT

    subject = selector()
    chosen = subject.choose(
        venue_id="upstox", symbol="RELIANCE", ceiling=5.0,
        volatility_forecast=0.002,
        broker_available_leverage=3.2,
    )

    assert chosen.leverage == pytest.approx(3.2)
    assert chosen.outcome == AT_BROKER_LIMIT
    assert chosen.broker_available_leverage == pytest.approx(3.2)
    assert "the broker lends" in chosen.reason


def test_the_operator_ceiling_still_binds_when_it_is_the_smaller_of_the_two():
    from parts.risk_capital_allocation.leverage_selector import AT_CEILING

    subject = selector()
    chosen = subject.choose(
        venue_id="upstox", symbol="RELIANCE", ceiling=5.0,
        volatility_forecast=0.002,
        broker_available_leverage=20.0,
    )

    assert chosen.leverage == pytest.approx(5.0)
    assert chosen.outcome == AT_CEILING


def test_a_silent_broker_is_unlevered_and_never_the_ceiling():
    """A margin endpoint that is down must not silently become 5x."""
    from parts.risk_capital_allocation.leverage_selector import (
        NO_LEVERAGE, UNLEVERAGED_NO_BROKER_QUOTE,
    )

    subject = selector()
    chosen = subject.choose(
        venue_id="upstox", symbol="RELIANCE", ceiling=5.0,
        volatility_forecast=0.002,
        broker_available_leverage=None, a_broker_quote_is_required=True,
    )

    assert chosen.leverage == NO_LEVERAGE
    assert chosen.outcome == UNLEVERAGED_NO_BROKER_QUOTE
    assert subject.standing.unleveraged_for_want_of_a_broker_quote == 1


def test_a_segment_that_does_not_borrow_needs_no_broker_quote():
    """A bought option has no margin to quote, and its absence is correct
    rather than missing -- the same not-a-gap reading three audits reached."""
    subject = selector()
    chosen = subject.choose(
        venue_id="upstox", symbol="NIFTY 24150 CE", ceiling=1.0,
        volatility_forecast=0.002,
        broker_available_leverage=None, a_broker_quote_is_required=False,
    )

    assert chosen.leverage > 0
    assert subject.standing.unleveraged_for_want_of_a_broker_quote == 0


def test_a_quantity_is_snapped_to_the_venues_own_lot_not_a_global_step():
    """A NIFTY option trades in blocks of 65 and the global step is 0.001.

    Measured on the live spine 2026-09-07, before the choice carried a lot size:
    an order for 12,165.44092528724 units of NIFTY 23750 PE. No exchange accepts
    that quantity, so every fee, margin and fill figure computed from it was a
    fiction and the paper book filled a size the real book could never take.
    `order_quantity_increment`'s own note has asked for exactly this since
    2026-08-22 -- "the honest fix is a per-symbol venue fact carried on
    instrument-choice".
    """
    assert quantity_increment_for(Choice(quantity_increment=65.0), 0.001) == 65.0
    # A share trades in single units, and that is a real lot size, not an absence.
    assert quantity_increment_for(Choice(quantity_increment=1.0), 0.001) == 1.0
    # No lot named is not the same as a lot of one: it falls back to the global
    # step rather than silently trading a contract in single units.
    assert quantity_increment_for(Choice(quantity_increment=None), 0.001) == 0.001
    assert quantity_increment_for(None, 0.001) == 0.001


def test_the_gate_caps_to_the_step_the_sizer_used():
    """Capping with a different step than the order was sized to would hand the
    book a quantity neither part chose."""
    gate = TradeCapitalBoundsGate(quantity_increment=0.001)
    sized = sized_for_gate(quantity=300.0, entry=48.75, quantity_increment=65.0)

    bounded = gate.bound(
        sized, bounds(minimum=1_000.0, maximum=10_000.0), settings_are_valid=True
    )

    assert bounded.quantity % 65 == 0, bounded.quantity
    assert bounded.quantity * 48.75 <= 10_000.0


class PlanFor:
    """A stop-target-plan, as the sizer reads it -- by shape, never by import."""

    def __init__(self, entry_price=None, stop_price=None, priced_for_contract=None):
        self.entry_price = entry_price
        self.stop_price = stop_price
        self.priced_for_contract = priced_for_contract


class ChoiceOf:
    def __init__(self, contract_symbol, reference_price=None):
        self.chosen = type("C", (), {"contract_symbol": contract_symbol})()
        self.reference_price = reference_price
        self.state = "chosen"

    @property
    def is_actionable(self):
        return True


def test_a_plan_priced_for_another_contract_is_not_used():
    """The ATM strike moves during a session and the plan outlives it.

    `stop-target-plan` is keyed by the intent's symbol, which is the underlying,
    while every price on it is a contract's premium. Measured on the live spine
    2026-09-07: 477 orders for `NIFTY 23750 CE 08 SEP 26` carried a decided price
    of 1.30 while that contract traded 100-120 all session and was never near
    1.30 -- it was the premium of the strike that had been at the money before
    NIFTY moved from 23,781 to 23,750. None of the 477 filled.
    """
    plan = PlanFor(entry_price=1.30, stop_price=1.25,
                   priced_for_contract="NIFTY 24800 CE 08 SEP 26")
    choice = ChoiceOf("NIFTY 23750 CE 08 SEP 26", reference_price=104.75)

    # The plan's 1.30 is refused and the contract's own price is used instead.
    assert entry_price_for(plan, choice) == 104.75
    assert stop_price_for(plan, PlacerIntent(side=LONG, action="open"),
                          104.75, BUY, choice) != 1.25


def test_a_plan_priced_for_this_contract_is_still_preferred():
    """The refined plan is what should be used whenever it is about this
    contract -- the fix must not throw away the plan it was built for."""
    plan = PlanFor(entry_price=104.75, stop_price=99.0,
                   priced_for_contract="NIFTY 23750 CE 08 SEP 26")
    choice = ChoiceOf("NIFTY 23750 CE 08 SEP 26", reference_price=104.75)

    assert entry_price_for(plan, choice) == 104.75
    assert stop_price_for(plan, PlacerIntent(side=LONG, action="open"),
                          104.75, BUY, choice) == 99.0


def test_a_plan_naming_no_contract_is_trusted():
    """Every plan built before the field existed names none, and a choice that
    names no contract has nothing to disagree with."""
    plan = PlanFor(entry_price=22.5, stop_price=21.0, priced_for_contract=None)
    choice = ChoiceOf("RELIANCE 1320 PE 29 SEP 26", reference_price=22.85)
    assert entry_price_for(plan, choice) == 22.5


def test_the_ceiling_bounds_the_position_not_the_order():
    """`maximum_capital_per_trade` is "the most one trade may commit", and a
    trade is a position.

    This gate capped each order and never saw what it was adding to, while the
    bots re-decided the same contract every few seconds. Measured on the live
    spine 2026-09-07: 66 open positions above the ceiling, the largest
    Rs 1,277,667 against 200,000, and one contract that walked 4,149 -> 10,492
    units in a single round trip with every add passing this gate.
    """
    gate = TradeCapitalBoundsGate(quantity_increment=0.001)
    # 800 already committed of a 1,000 ceiling: only 200 of room is left.
    gate.observe_position(VENUE, SYMBOL, 800.0)

    bounded = gate.bound(
        sized_for_gate(quantity=5.0, entry=100.0), bounds(minimum=10.0, maximum=1_000.0),
        settings_are_valid=True,
    )

    assert bounded.quantity * 100.0 <= 200.0 + 1e-9, bounded.reason
    assert gate.standing.capped_to_the_room_left == 1


def test_a_position_already_at_the_ceiling_refuses_the_order_outright():
    """A bound is not a ban on adding, but there is nothing left to add."""
    gate = TradeCapitalBoundsGate(quantity_increment=0.001)
    gate.observe_position(VENUE, SYMBOL, 1_000.0)

    bounded = gate.bound(
        sized_for_gate(quantity=1.0, entry=100.0), bounds(minimum=10.0, maximum=1_000.0),
        settings_are_valid=True,
    )

    assert bounded.quantity == 0.0
    assert gate.standing.refused_position_already_at_the_ceiling == 1
    assert "already committed" in bounded.reason


def test_a_first_entry_is_bounded_exactly_as_before():
    """A symbol nothing is held in must behave identically -- this is the same
    rule applied to the right quantity, not a new rule for a first order."""
    gate = TradeCapitalBoundsGate(quantity_increment=0.001)

    bounded = gate.bound(
        sized_for_gate(quantity=50.0, entry=100.0), bounds(minimum=10.0, maximum=1_000.0),
        settings_are_valid=True,
    )

    assert bounded.quantity * 100.0 <= 1_000.0 + 1e-9
    assert gate.standing.refused_position_already_at_the_ceiling == 0
    assert gate.standing.capped_to_the_room_left == 0


def test_a_position_gone_flat_stops_counting_against_the_ceiling():
    """A position is a state: closed means the room is back."""
    gate = TradeCapitalBoundsGate(quantity_increment=0.001)
    gate.observe_position(VENUE, SYMBOL, 1_000.0)
    gate.observe_position(VENUE, SYMBOL, 0.0)

    assert gate.held_capital(sized_for_gate(quantity=1.0, entry=100.0)) == 0.0


# ---- a stock-options fill finds its plan (2026-09-13) --------------------------

# The first AXISBANK 1260 CE entry fill of 2026-09-07, verbatim from this project's
# own journal.trade-lifecycle-recorder: the intent named "AXISBANK", the fill names
# the contract.
AXISBANK_ENTRY_FILL = {
    "fee": 113.18405543749999, "fill_id": "paper-620c4753971836bef8d861e32933e136-324",
    "filled_at_ns": 1788773352612477922, "is_paper": True, "leverage": 1.0,
    "order_id": "620c4753971836bef8d861e32933e136", "price": 24.54230769230769,
    "quantity": 8125.0, "segment": "stock-options", "side": "buy",
    "symbol": "AXISBANK 1260 CE 29 SEP 26", "venue_id": "upstox",
}


def an_axisbank_plan():
    """The plan risk makes for the AXISBANK intent: underlying symbol, contract prices."""
    return placer().place(
        "upstox", "AXISBANK", BUY,
        entry_price=AXISBANK_ENTRY_FILL["price"], volatility_forecast=0.05,
        target_price=AXISBANK_ENTRY_FILL["price"] * 1.2,
        priced_for_contract=AXISBANK_ENTRY_FILL["symbol"],
    )


def test_a_stock_options_fill_finds_the_plan_priced_for_its_contract():
    """Registered under "AXISBANK", no contract fill ever found it: no target was ever placed."""
    from parts.risk_capital_allocation.exit_order_chainer import plan_registration_of

    plan = an_axisbank_plan()
    assert plan.is_placeable and plan.target_price is not None
    chainer = ExitOrderChainer()
    chainer.register_plan(**plan_registration_of(plan))

    fill = AXISBANK_ENTRY_FILL
    exits = chainer.observe_entry_fill(
        fill_id=fill["fill_id"], entry_order_id=fill["order_id"], venue_id=fill["venue_id"],
        symbol=fill["symbol"], entry_side=fill["side"], filled_quantity=fill["quantity"],
        fill_price=fill["price"], filled_at_ns=fill["filled_at_ns"],
    )
    assert exits.outcome == CHAINED
    assert exits.target_price is not None and exits.target_price > fill["price"]
    assert exits.stop_price < fill["price"]
    assert exits.entry_filled_at_ns == fill["filled_at_ns"]


def test_registered_under_the_underlying_the_same_fill_found_no_plan():
    """What the chainer did until 2026-09-13, kept as the evidence the fix is needed."""
    from parts.risk_capital_allocation.exit_order_chainer import plan_registration_of

    plan = an_axisbank_plan()
    chainer = ExitOrderChainer()
    chainer.register_plan(**(plan_registration_of(plan) | {"symbol": plan.symbol}))
    fill = AXISBANK_ENTRY_FILL
    exits = chainer.observe_entry_fill(
        fill_id=fill["fill_id"], entry_order_id=fill["order_id"], venue_id=fill["venue_id"],
        symbol=fill["symbol"], entry_side=fill["side"], filled_quantity=fill["quantity"],
        fill_price=fill["price"],
    )
    assert exits.outcome == NO_PLAN


def test_a_plan_naming_no_contract_is_registered_under_its_own_symbol():
    from parts.risk_capital_allocation.exit_order_chainer import plan_registration_of

    plan = placer().place(VENUE, SYMBOL, BUY, entry_price=100.0, volatility_forecast=0.02)
    assert plan_registration_of(plan)["symbol"] == SYMBOL
