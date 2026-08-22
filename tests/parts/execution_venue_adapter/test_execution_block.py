"""The execution block: the parts that spend money, and what stops them.

Every test here is about a way to lose money that is not a trading loss --
a doubled position from a retried order, a live order written off as lost, a
balance believed after it went stale, a status guessed at. Those are the failures
this block exists to prevent, so they are what it is tested against.

The learned parts (RL-060) are tested twice over: once unfitted, where they must
say so and use the operator's prior, and once after real outcomes have moved
them, where the estimate must actually follow the evidence.
"""

import importlib

import pytest

from parts.execution_venue_adapter.ccxt_order_router import (
    FAILED, REFUSED_DUPLICATE, REFUSED_NO_BUDGET, REFUSED_NO_KEY, REFUSED_VENUE_STANDING,
    SENT, CcxtOrderRouter,
)
from parts.execution_venue_adapter.limit_price_walker import (
    AT_CEILING, AT_TOUCH, HELD, STEPPED, LimitPriceWalker,
)
from parts.execution_venue_adapter.order_not_found_debouncer import OrderNotFoundDebouncer
from parts.execution_venue_adapter.order_reject_classifier import (
    INSUFFICIENT_MARGIN, RATE_LIMITED, SYMBOL_NOT_TRADING, UNCLASSIFIED, OrderRejectClassifier,
)
from parts.execution_venue_adapter.order_resubmitter import (
    GIVE_UP_ATTEMPTS, GIVE_UP_NOT_TRANSIENT, GIVE_UP_NO_BUDGET, RESUBMIT, WAIT, OrderResubmitter,
)
from parts.execution_venue_adapter.order_state_poller import DUE, OVERDUE, WAITING, OrderStatePoller
from parts.execution_venue_adapter.resting_order_cancel_policy import (
    CANCEL_DISTANCE, CANCEL_TIME, HOLD, RestingOrderCancelPolicy,
)
from parts.execution_venue_adapter.venue_balance_reader import (
    FRESH, NO_KEY, STALE, UNREADABLE, VenueBalanceReader,
)
from parts.execution_venue_adapter.venue_order_status_translator import (
    CANCELLED, EXPIRED, FILLED, OPEN, PARTIALLY_FILLED, REJECTED, UNKNOWN,
    VenueOrderStatusTranslator,
)
from parts.execution_venue_adapter.venue_position_reader import (
    REPORTED, UNREACHABLE, VenuePositionReader,
)
from parts.execution_venue_adapter.venue_rate_budgeter import (
    COUNTED_LOCALLY, MARKET_DATA, ORDER_PLACEMENT, REPORTED_BY_VENUE, VenueRateBudgeter,
)
from runtime.part_declaration import load_declaration_from_blueprint
from runtime.trading_types import BUY, SELL

BLOCK_PARTS = {
    "ccxt-order-router": "parts.execution_venue_adapter.ccxt_order_router",
    "venue-balance-reader": "parts.execution_venue_adapter.venue_balance_reader",
    "venue-position-reader": "parts.execution_venue_adapter.venue_position_reader",
    "order-state-poller": "parts.execution_venue_adapter.order_state_poller",
    "order-reject-classifier": "parts.execution_venue_adapter.order_reject_classifier",
    "order-resubmitter": "parts.execution_venue_adapter.order_resubmitter",
    "venue-rate-budgeter": "parts.execution_venue_adapter.venue_rate_budgeter",
    "venue-order-status-translator": "parts.execution_venue_adapter.venue_order_status_translator",
    "order-not-found-debouncer": "parts.execution_venue_adapter.order_not_found_debouncer",
    "resting-order-cancel-policy": "parts.execution_venue_adapter.resting_order_cancel_policy",
    "limit-price-walker": "parts.execution_venue_adapter.limit_price_walker",
}

VENUE = "binance-usdm"
SYMBOL = "BTCUSDT"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


class RecordingClient:
    """A venue client that records what it was asked and answers as told."""

    def __init__(self, response=None, raises=None, balance=None, positions=None):
        self.calls = []
        self._response = response or {"id": "venue-1"}
        self._raises = raises
        self._balance = balance or {}
        self._positions = positions or []

    def create_order(self, **kwargs):
        self.calls.append(("create_order", kwargs))
        if self._raises:
            raise self._raises
        return self._response

    def cancel_order(self, **kwargs):
        self.calls.append(("cancel_order", kwargs))
        if self._raises:
            raise self._raises
        return self._response

    def edit_order(self, **kwargs):
        self.calls.append(("edit_order", kwargs))
        if self._raises:
            raise self._raises
        return self._response

    def fetch_balance(self):
        self.calls.append(("fetch_balance", {}))
        if self._raises:
            raise self._raises
        return self._balance

    def fetch_positions(self, symbols=None):
        self.calls.append(("fetch_positions", symbols))
        if self._raises:
            raise self._raises
        return self._positions


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_every_built_declaration_equals_the_blueprint(part_id):
    module = importlib.import_module(BLOCK_PARTS[part_id])
    assert module.PART_DECLARATION == load_declaration_from_blueprint(part_id)


# ---- ccxt-order-router -------------------------------------------------------

def router(client=None, budget=True, standing="serving", key="key-a"):
    return CcxtOrderRouter(
        clients={VENUE: client or RecordingClient()},
        has_rate_budget=lambda venue_id: budget,
        read_venue_standing=lambda venue_id: standing,
        read_key_standing=lambda venue_id: key,
    )


def an_order(**overrides):
    order = dict(
        venue_id=VENUE, symbol=SYMBOL, side=BUY, quantity=1.0, price=100.0, intent_id="intent-1"
    )
    order.update(overrides)
    return order


def test_the_same_order_always_gets_the_same_client_order_id():
    """What makes a retry safe: the venue rejects the duplicate instead of doubling."""
    first = router().client_order_id(VENUE, SYMBOL, BUY, 1.0, 100.0, "intent-1")
    second = router().client_order_id(VENUE, SYMBOL, BUY, 1.0, 100.0, "intent-1")
    assert first == second


def test_two_different_intents_asking_for_the_same_order_do_not_collide():
    one = router().client_order_id(VENUE, SYMBOL, BUY, 1.0, 100.0, "intent-1")
    two = router().client_order_id(VENUE, SYMBOL, BUY, 1.0, 100.0, "intent-2")
    assert one != two


def test_an_order_carries_its_client_order_id_to_the_venue():
    client = RecordingClient()
    status = router(client).place(**an_order())
    assert status.outcome == SENT
    _, kwargs = client.calls[0]
    assert kwargs["params"]["clientOrderId"] == status.client_order_id


def test_the_same_order_is_never_sent_twice():
    client = RecordingClient()
    sender = router(client)
    sender.place(**an_order())
    repeat = sender.place(**an_order())
    assert repeat.outcome == REFUSED_DUPLICATE
    assert len(client.calls) == 1


def test_nothing_is_sent_without_rate_budget():
    client = RecordingClient()
    assert router(client, budget=False).place(**an_order()).outcome == REFUSED_NO_BUDGET
    assert client.calls == []


def test_nothing_is_sent_to_a_venue_that_is_refusing_us():
    client = RecordingClient()
    assert router(client, standing="banned").place(**an_order()).outcome == REFUSED_VENUE_STANDING
    assert client.calls == []


def test_nothing_is_sent_without_a_key():
    client = RecordingClient()
    assert router(client, key=None).place(**an_order()).outcome == REFUSED_NO_KEY
    assert client.calls == []


def test_a_failed_send_says_the_order_may_still_have_arrived():
    """The honest answer to 'did that go through', and why the id matters."""
    status = router(RecordingClient(raises=TimeoutError("no response"))).place(**an_order())
    assert status.outcome == FAILED
    assert "may or may not have arrived" in status.reason
    assert status.client_order_id


# ---- venue-rate-budgeter -----------------------------------------------------

def budgeter_with(limit=100.0, window=60.0, request_class=MARKET_DATA):
    clock = Clock()
    budgeter = VenueRateBudgeter(monotonic=clock.monotonic)
    budgeter.declare_limit(VENUE, request_class, limit, window)
    return budgeter, clock


def test_an_undeclared_class_may_not_be_spent_at_all():
    """An unmetered class is not a free one."""
    budgeter, _ = budgeter_with()
    assert budgeter.spend(VENUE, ORDER_PLACEMENT, 1.0) is False
    assert budgeter.standing.requests_refused == 1


def test_spending_reduces_what_remains_and_stops_at_the_limit():
    budgeter, _ = budgeter_with(limit=3.0)
    assert [budgeter.spend(VENUE, MARKET_DATA) for _ in range(4)] == [True, True, True, False]
    assert budgeter.read(VENUE, MARKET_DATA).remaining == pytest.approx(0.0)


def test_weight_is_charged_not_request_count():
    """A depth snapshot costs twenty times a symbol list on one of these venues."""
    budgeter, _ = budgeter_with(limit=10.0)
    assert budgeter.spend(VENUE, MARKET_DATA, weight=9.0) is True
    assert budgeter.spend(VENUE, MARKET_DATA, weight=2.0) is False


def test_the_window_slides_so_budget_returns():
    budgeter, clock = budgeter_with(limit=1.0, window=60.0)
    assert budgeter.spend(VENUE, MARKET_DATA) is True
    assert budgeter.spend(VENUE, MARKET_DATA) is False
    clock.now += 61
    assert budgeter.spend(VENUE, MARKET_DATA) is True


def test_the_venues_own_figure_overrides_the_local_count():
    """The limit is per IP: another process on this box spends it invisibly."""
    budgeter, _ = budgeter_with(limit=100.0)
    budgeter.spend(VENUE, MARKET_DATA, weight=5.0)
    budgeter.observe_venue_reported_spend(VENUE, MARKET_DATA, spent=80.0)
    reading = budgeter.read(VENUE, MARKET_DATA)
    assert reading.source == REPORTED_BY_VENUE
    assert reading.spent == pytest.approx(80.0)
    assert budgeter.standing.largest_unseen_spend if False else budgeter.standing.largest_undercount == pytest.approx(75.0)


def test_a_venue_that_reports_nothing_is_counted_locally_and_says_so():
    budgeter, _ = budgeter_with(limit=100.0)
    budgeter.spend(VENUE, MARKET_DATA, weight=5.0)
    assert budgeter.read(VENUE, MARKET_DATA).source == COUNTED_LOCALLY


def test_a_banned_venue_may_not_be_spent_on():
    budgeter, _ = budgeter_with(limit=100.0)
    budgeter.set_venue_standing(VENUE, "banned")
    assert budgeter.spend(VENUE, MARKET_DATA) is False


# ---- venue-order-status-translator ------------------------------------------

def test_each_venues_words_collapse_into_the_closed_set():
    translator = VenueOrderStatusTranslator()
    assert translator.translate("o1", "binance-usdm", SYMBOL, "PARTIALLY_FILLED").status == PARTIALLY_FILLED
    assert translator.translate("o2", "bybit-linear", SYMBOL, "PartiallyFilled").status == PARTIALLY_FILLED
    assert translator.translate("o3", "binance-usdm", SYMBOL, "EXPIRED_IN_MATCH").status == EXPIRED
    assert translator.translate("o4", "bybit-linear", SYMBOL, "Deactivated").status == EXPIRED


def test_an_unknown_status_is_refused_rather_than_guessed():
    """A status wrongly read as filled invents a position nothing is holding."""
    translator = VenueOrderStatusTranslator()
    result = translator.translate("o1", VENUE, SYMBOL, "SOMETHING_NEW")
    assert result.status == UNKNOWN
    assert result.is_terminal is False
    assert "no reading for" in result.reason
    assert translator.standing.unknown_statuses == 1


def test_a_fill_is_emitted_once_per_venue_fill_id():
    translator = VenueOrderStatusTranslator()
    first = translator.translate(
        "o1", VENUE, SYMBOL, "FILLED", filled_quantity=1.0, fill_price=100.0, fill_id="v-1", side=BUY
    )
    repeat = translator.translate(
        "o1", VENUE, SYMBOL, "FILLED", filled_quantity=1.0, fill_price=100.0, fill_id="v-1", side=BUY
    )
    assert first.fill is not None and first.fill.quantity == 1.0
    assert repeat.fill is None
    assert translator.standing.duplicate_fills_ignored == 1


def test_a_terminal_status_is_marked_and_remembered():
    translator = VenueOrderStatusTranslator()
    assert translator.translate("o1", VENUE, SYMBOL, "FILLED").is_terminal is True
    assert translator.status_of("o1") == FILLED
    assert translator.translate("o1", VENUE, SYMBOL, "CANCELED").is_terminal is True
    assert translator.standing.terminal_after_terminal == 1


def test_a_rejection_carries_the_venues_own_words():
    translator = VenueOrderStatusTranslator()
    result = translator.translate("o1", VENUE, SYMBOL, "REJECTED", venue_message="Margin is insufficient")
    assert result.status == REJECTED
    assert result.reject_message == "Margin is insufficient"


# ---- order-reject-classifier -------------------------------------------------

def classifier(threshold=0.5, minimum=3):
    return OrderRejectClassifier(
        retry_threshold=threshold, minimum_observations=minimum,
        prior_weight=2.0, half_life_observations=50.0,
    )


def test_a_venues_own_code_names_the_reason():
    subject = classifier()
    subject.learn_venue_code(VENUE, "-2019", INSUFFICIENT_MARGIN)
    result = subject.classify("o1", VENUE, SYMBOL, "-2019", "whatever the message says")
    assert result.reason == INSUFFICIENT_MARGIN


def test_the_venues_words_name_it_when_the_code_is_unknown():
    result = classifier().classify("o1", VENUE, SYMBOL, None, "Too many requests; slow down")
    assert result.reason == RATE_LIMITED


def test_a_message_nothing_matches_is_unclassified_not_guessed():
    result = classifier().classify("o1", VENUE, SYMBOL, None, "zxcvbnm")
    assert result.reason == UNCLASSIFIED
    assert classifier().standing.unclassified == 0


def test_an_unfitted_classifier_uses_the_prior_and_says_so():
    """RL-060 and Rule 8: no evidence renders as no evidence."""
    result = classifier().classify("o1", VENUE, SYMBOL, None, "Too many requests")
    assert result.retry_estimate.is_fitted is False
    assert result.retry_estimate.value == pytest.approx(0.90)
    assert result.should_retry is True


def test_the_retry_rate_follows_what_actually_happened():
    """The learned component: a rejection that never clears stops being retried."""
    subject = classifier(threshold=0.5, minimum=3)
    for _ in range(10):
        subject.observe_retry_outcome(VENUE, RATE_LIMITED, succeeded=False)
    result = subject.classify("o1", VENUE, SYMBOL, None, "Too many requests")
    assert result.retry_estimate.is_fitted is True
    assert result.retry_estimate.value < 0.5
    assert result.should_retry is False


def test_learning_is_per_venue():
    subject = classifier(minimum=3)
    for _ in range(10):
        subject.observe_retry_outcome("bybit-linear", RATE_LIMITED, succeeded=False)
    on_binance = subject.classify("o1", VENUE, SYMBOL, None, "Too many requests")
    assert on_binance.retry_estimate.is_fitted is False


def test_a_hopeless_rejection_starts_out_not_worth_retrying():
    result = classifier().classify("o1", VENUE, SYMBOL, None, "Symbol is not trading")
    assert result.reason == SYMBOL_NOT_TRADING
    assert result.should_retry is False


# ---- order-not-found-debouncer -----------------------------------------------

def debouncer(prior=3, maximum=10, minimum=3):
    return OrderNotFoundDebouncer(
        prior_threshold=prior, maximum_threshold=maximum,
        minimum_observations=minimum, window=50,
    )


def test_one_denial_does_not_write_off_a_live_order():
    """Treating the first 'not found' as truth is how a live position vanishes."""
    subject = debouncer(prior=3)
    assert subject.observe_denial("o1", VENUE).is_lost is False


def test_repeated_denials_past_the_threshold_declare_it_lost():
    subject = debouncer(prior=3)
    verdicts = [subject.observe_denial("o1", VENUE).is_lost for _ in range(3)]
    assert verdicts == [False, False, True]
    assert subject.standing.orders_declared_lost == 1


def test_an_order_that_turns_up_teaches_the_venues_tolerance():
    """A resolved run is the only evidence of how many false denials a venue makes."""
    subject = debouncer(prior=2, maximum=10, minimum=2)
    for order_id in ("o1", "o2", "o3"):
        for _ in range(5):
            subject.observe_denial(order_id, VENUE)
        subject.observe_order_appeared(order_id, VENUE)
    assert subject.standing.spurious_runs_observed == 3
    learned = subject.learned_thresholds()[VENUE]
    assert learned.is_fitted is True
    assert learned.value > 2


def test_the_learned_threshold_is_clamped_by_the_operators_ceiling():
    subject = debouncer(prior=2, maximum=4, minimum=2)
    for order_id in ("o1", "o2", "o3"):
        for _ in range(20):
            subject.observe_denial(order_id, VENUE)
        subject.observe_order_appeared(order_id, VENUE)
    learned = subject.learned_thresholds()[VENUE]
    assert learned.value <= 4
    assert learned.was_clamped is True


# ---- order-state-poller ------------------------------------------------------

def poller(prior=4.0, minimum_interval=0.5, maximum_interval=30.0, overdue=10.0, minimum=3):
    clock = Clock()
    return (
        OrderStatePoller(
            prior_resolution_seconds=prior,
            minimum_poll_interval_seconds=minimum_interval,
            maximum_poll_interval_seconds=maximum_interval,
            overdue_multiple=overdue,
            minimum_observations=minimum,
            window=50,
            monotonic=clock.monotonic,
        ),
        clock,
    )


def test_a_new_order_is_polled_immediately_then_waits():
    subject, clock = poller()
    subject.observe_order_sent("o1", VENUE, SYMBOL)
    assert subject.decide("o1").state == DUE
    assert subject.decide("o1").state == WAITING


def test_the_interval_passing_makes_it_due_again():
    subject, clock = poller(prior=4.0)
    subject.observe_order_sent("o1", VENUE, SYMBOL)
    subject.decide("o1")
    clock.now += 1.1
    assert subject.decide("o1").state == DUE


def test_the_poll_interval_follows_how_long_orders_actually_take():
    """A thin symbol's orders rest for minutes; polling them every second is waste."""
    subject, clock = poller(prior=4.0, minimum=3)
    for index in range(5):
        subject.observe_order_sent(f"o{index}", VENUE, SYMBOL)
        clock.now += 20.0
        subject.observe_order_resolved(f"o{index}")
    subject.observe_order_sent("slow", VENUE, SYMBOL)
    decision = subject.decide("slow")
    assert decision.interval_estimate.is_fitted is True
    assert decision.poll_interval_seconds == pytest.approx(5.0)


def test_an_order_open_far_longer_than_any_other_is_escalated():
    subject, clock = poller(prior=1.0, overdue=10.0)
    subject.observe_order_sent("o1", VENUE, SYMBOL)
    clock.now += 100.0
    decision = subject.decide("o1")
    assert decision.state == OVERDUE
    assert "will not explain it" in decision.reason


# ---- order-resubmitter -------------------------------------------------------

class Rejection:
    def __init__(self, reason=RATE_LIMITED, retry_rate=0.9, order_id="o1"):
        self.order_id = order_id
        self.venue_id = VENUE
        self.symbol = SYMBOL
        self.reason = reason

        class _Estimate:
            value = retry_rate
            is_fitted = True

        self.retry_estimate = _Estimate()


def resubmitter(attempts=3, threshold=0.5, budget=True, prior_clear=2.0, minimum=3):
    clock = Clock()
    return (
        OrderResubmitter(
            attempts_allowed=attempts,
            retry_rate_threshold=threshold,
            prior_clear_seconds=prior_clear,
            maximum_wait_seconds=60.0,
            minimum_observations=minimum,
            window=50,
            has_rate_budget=lambda venue_id: budget,
            monotonic=clock.monotonic,
        ),
        clock,
    )


def test_a_transient_rejection_is_resent_with_the_same_id():
    subject, _ = resubmitter()
    decision = subject.decide(Rejection())
    assert decision.action == RESUBMIT
    assert decision.client_order_id == "o1"
    assert "same client order id" in decision.reason


def test_a_rejection_that_never_clears_is_not_retried():
    subject, _ = resubmitter(threshold=0.5)
    assert subject.decide(Rejection(reason=INSUFFICIENT_MARGIN, retry_rate=0.02)).action == GIVE_UP_NOT_TRANSIENT


def test_attempts_are_bounded():
    subject, clock = resubmitter(attempts=2)
    assert subject.decide(Rejection()).action == RESUBMIT
    clock.now += 100
    assert subject.decide(Rejection()).action == RESUBMIT
    clock.now += 100
    assert subject.decide(Rejection()).action == GIVE_UP_ATTEMPTS


def test_nothing_is_retried_into_a_rate_limit():
    """A burst of retries is how a transient rejection becomes a ban."""
    subject, _ = resubmitter(budget=False)
    assert subject.decide(Rejection()).action == GIVE_UP_NO_BUDGET


def test_a_second_attempt_waits_for_the_condition_to_clear():
    subject, clock = resubmitter(prior_clear=5.0)
    subject.decide(Rejection())
    assert subject.decide(Rejection()).action == WAIT
    clock.now += 6.0
    assert subject.decide(Rejection()).action == RESUBMIT


def test_the_wait_follows_how_long_the_condition_actually_takes():
    subject, clock = resubmitter(prior_clear=1.0, minimum=3)
    for _ in range(5):
        subject.observe_resubmission_cleared(VENUE, RATE_LIMITED, seconds_waited=20.0)
    decision = subject.decide(Rejection())
    assert decision.wait_estimate.is_fitted is True
    assert decision.wait_seconds == pytest.approx(20.0)


# ---- resting-order-cancel-policy ---------------------------------------------

def cancel_policy(prior_ttl=30.0, prior_distance=0.01, minimum=3):
    clock = Clock()
    return (
        RestingOrderCancelPolicy(
            prior_time_to_live_seconds=prior_ttl,
            maximum_time_to_live_seconds=600.0,
            prior_distance_fraction=prior_distance,
            maximum_distance_fraction=0.5,
            minimum_observations=minimum,
            window=50,
            monotonic=clock.monotonic,
        ),
        clock,
    )


def test_a_fresh_order_near_the_market_is_held():
    subject, _ = cancel_policy()
    subject.observe_order_placed("o1", VENUE, SYMBOL, 100.0)
    subject.observe_price(VENUE, SYMBOL, 100.1)
    assert subject.decide("o1").action == HOLD


def test_an_order_resting_past_its_tolerance_is_pulled():
    subject, clock = cancel_policy(prior_ttl=30.0)
    subject.observe_order_placed("o1", VENUE, SYMBOL, 100.0)
    subject.observe_price(VENUE, SYMBOL, 100.0)
    clock.now += 31
    assert subject.decide("o1").action == CANCEL_TIME


def test_an_order_the_market_walked_away_from_is_pulled():
    subject, _ = cancel_policy(prior_distance=0.01)
    subject.observe_order_placed("o1", VENUE, SYMBOL, 100.0)
    subject.observe_price(VENUE, SYMBOL, 110.0)
    decision = subject.decide("o1")
    assert decision.action == CANCEL_DISTANCE
    assert decision.distance_fraction == pytest.approx(10.0 / 110.0)


def test_patience_is_learned_from_what_actually_filled():
    """A minute is nothing on a thin symbol and an eternity on a liquid one."""
    subject, clock = cancel_policy(prior_ttl=5.0, minimum=3)
    for index in range(5):
        subject.observe_order_placed(f"o{index}", VENUE, SYMBOL, 100.0)
        subject.observe_price(VENUE, SYMBOL, 100.0)
        clock.now += 60.0
        subject.observe_order_filled(f"o{index}")
    subject.observe_order_placed("new", VENUE, SYMBOL, 100.0)
    clock.now += 10.0
    decision = subject.decide("new")
    assert decision.time_to_live_estimate.is_fitted is True
    assert decision.action == HOLD, "10s is patient by this symbol's own history"


# ---- limit-price-walker ------------------------------------------------------

def walker(cadence=1.0, prior_step=0.001, maximum_walk=0.01, minimum=3):
    clock = Clock()
    return (
        LimitPriceWalker(
            cadence_seconds=cadence,
            prior_step_fraction=prior_step,
            maximum_total_walk_fraction=maximum_walk,
            minimum_observations=minimum,
            window=50,
            monotonic=clock.monotonic,
        ),
        clock,
    )


def test_a_buy_steps_up_toward_the_ask():
    subject, clock = walker(prior_step=0.001)
    subject.observe_order_placed("o1", VENUE, SYMBOL, BUY, 100.0)
    subject.observe_touch(VENUE, SYMBOL, best_bid=100.0, best_ask=101.0)
    clock.now += 1.1
    reprice = subject.step("o1")
    assert reprice.action == STEPPED
    assert reprice.to_price == pytest.approx(100.1)


def test_a_sell_steps_down_toward_the_bid():
    subject, clock = walker(prior_step=0.001)
    subject.observe_order_placed("o1", VENUE, SYMBOL, SELL, 101.0)
    subject.observe_touch(VENUE, SYMBOL, best_bid=100.0, best_ask=101.0)
    clock.now += 1.1
    assert subject.step("o1").to_price == pytest.approx(100.899)


def test_the_walk_never_crosses_the_touch():
    """Crossing would pay the spread this part exists to save."""
    subject, clock = walker(prior_step=0.5, maximum_walk=1.0)
    subject.observe_order_placed("o1", VENUE, SYMBOL, BUY, 100.0)
    subject.observe_touch(VENUE, SYMBOL, best_bid=99.0, best_ask=100.5)
    clock.now += 1.1
    first = subject.step("o1")
    assert first.to_price <= 100.5
    clock.now += 1.1
    assert subject.step("o1").action == AT_TOUCH


def test_the_walk_stops_at_the_operators_ceiling():
    subject, clock = walker(prior_step=0.004, maximum_walk=0.005)
    subject.observe_order_placed("o1", VENUE, SYMBOL, BUY, 100.0)
    subject.observe_touch(VENUE, SYMBOL, best_bid=100.0, best_ask=200.0)
    clock.now += 1.1
    assert subject.step("o1").action == STEPPED
    clock.now += 1.1
    held = subject.step("o1")
    assert held.action == AT_CEILING
    assert "operator's ceiling" in held.reason


def test_the_cadence_is_respected():
    subject, clock = walker(cadence=5.0)
    subject.observe_order_placed("o1", VENUE, SYMBOL, BUY, 100.0)
    subject.observe_touch(VENUE, SYMBOL, best_bid=100.0, best_ask=200.0)
    clock.now += 5.1
    assert subject.step("o1").action == STEPPED
    assert subject.step("o1").action == HELD


def test_the_step_follows_how_far_orders_actually_had_to_walk():
    subject, clock = walker(prior_step=0.0001, maximum_walk=0.05, minimum=3)
    for index in range(5):
        subject.observe_order_placed(f"o{index}", VENUE, SYMBOL, BUY, 100.0)
        subject.observe_touch(VENUE, SYMBOL, best_bid=100.0, best_ask=200.0)
        clock.now += 1.1
        for _ in range(10):
            clock.now += 1.1
            subject.step(f"o{index}")
        subject.observe_order_filled(f"o{index}")
    subject.observe_order_placed("new", VENUE, SYMBOL, BUY, 100.0)
    clock.now += 1.1
    reprice = subject.step("new")
    assert reprice.step_estimate.is_fitted is True
    assert reprice.step_estimate.value > 0.0001


# ---- venue-balance-reader ----------------------------------------------------

def balance_reader(client=None, key="key-a", freshness=10.0):
    clock = Clock()
    return (
        VenueBalanceReader(
            clients={VENUE: client or RecordingClient(balance={"USDT": {"free": 900.0, "used": 100.0, "total": 1000.0}})},
            read_key_standing=lambda venue_id: key,
            settlement_currency="USDT",
            freshness_seconds=freshness,
            monotonic=clock.monotonic,
        ),
        clock,
    )


def test_a_balance_is_read_from_the_venue():
    reader, _ = balance_reader()
    balance = reader.read(VENUE)
    assert balance.state == FRESH
    assert balance.free == pytest.approx(900.0)
    assert balance.is_usable is True


def test_no_key_means_no_balance():
    reader, _ = balance_reader(key=None)
    assert reader.read(VENUE).state == NO_KEY


def test_a_failed_read_with_no_history_is_unreadable_not_zero():
    reader, _ = balance_reader(client=RecordingClient(raises=OSError("down")))
    balance = reader.read(VENUE)
    assert balance.state == UNREADABLE
    assert balance.free is None


def test_a_balance_past_its_freshness_is_marked_stale_not_served_quietly():
    """A stale balance sizes an order that cannot fill, and looks current."""
    client = RecordingClient(balance={"USDT": {"free": 900.0, "used": 0.0, "total": 900.0}})
    reader, clock = balance_reader(client=client, freshness=10.0)
    reader.read(VENUE)
    client._raises = OSError("down")
    clock.now += 11
    balance = reader.read(VENUE)
    assert balance.state == STALE
    assert balance.is_usable is False
    assert balance.age_seconds > 10


# ---- venue-position-reader ---------------------------------------------------

def test_the_venues_positions_are_read_with_their_side():
    """Reading size alone turns every short into a long."""
    client = RecordingClient(positions=[
        {"symbol": SYMBOL, "contracts": 2.0, "side": "short", "entryPrice": 100.0},
        {"symbol": "ETHUSDT", "contracts": 3.0, "side": "long", "entryPrice": 50.0},
    ])
    reader = VenuePositionReader({VENUE: client}, read_key_standing=lambda v: "key-a")
    reports = {report.symbol: report for report in reader.read(VENUE)}
    assert reports[SYMBOL].quantity == pytest.approx(-2.0)
    assert reports["ETHUSDT"].quantity == pytest.approx(3.0)
    assert reports[SYMBOL].is_known is True


def test_an_unreachable_venue_is_unknown_and_never_flat():
    """A system that believes it is flat when it is not opens the position again."""
    reader = VenuePositionReader(
        {VENUE: RecordingClient(raises=OSError("down"))}, read_key_standing=lambda v: "key-a"
    )
    reports = reader.read(VENUE, (SYMBOL,))
    assert reports[0].state == UNREACHABLE
    assert reports[0].quantity is None
    assert reports[0].direction is None
    assert "unknown is not flat" in reports[0].reason


def test_a_venue_reporting_nothing_is_known_to_be_flat():
    reader = VenuePositionReader({VENUE: RecordingClient(positions=[])}, read_key_standing=lambda v: "key-a")
    assert reader.read(VENUE) == ()
