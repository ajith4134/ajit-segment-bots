"""The llm-services block: who pays, what it cost, and what the model was given.

Three pockets that are not interchangeable, two callers whose failure modes are
opposite, one ledger for the money that does not come back, and the snapshot that is
the boundary between what is known and what is generated.
"""

import importlib

import pytest

from parts.llm_services.ground_truth_snapshot_builder import (
    ASK, BID, BUILT, GroundTruthSnapshotBuilder, INCOMPLETE, INCONSISTENT, MID,
    NOTHING_MEASURED, SPREAD, TOO_STALE,
)
from parts.llm_services.llm_backpressure_gauge import (
    LlmBackpressureGauge, NOT_MEASURED, OPEN, STOPPED_BY_SURVIVAL, THROTTLED_BY_PACE,
    THROTTLED_BY_SPEND, TIERS_THAT_STOP_REASONING,
)
from parts.llm_services.llm_model_picker import (
    CHOSEN, EXPLORING, LlmModelPicker, NO_MODELS, NO_MODEL_CLEARS_THE_BAR,
)
from parts.llm_services.llm_request_router import (
    LlmRequestRouter, NOTHING_CAN_PAY, NO_MODEL_CHOSEN, PART_OUT_OF_BUDGET, ROUTED,
    SHED_BY_BACKPRESSURE, WOULD_NEED_A_WEAKER_MODEL,
)
from parts.llm_services.llm_response_cache import (
    EXPIRED, HIT, LlmResponseCache, MISS, NOT_CACHEABLE, STORED,
)
from parts.llm_services.local_model_caller import (
    ANSWERED as LOCAL_ANSWERED, LocalModelCaller, MACHINE_IS_BUSY, NO_MODEL_LOADED,
    TOOK_TOO_LONG,
)
from parts.llm_services.metered_api_caller import (
    ABOVE_THE_PER_CALL_CEILING, ANSWERED as METERED_ANSWERED, MeteredApiCaller,
    NO_PRICE, WOULD_EXCEED_THE_BUDGET,
)
from parts.llm_services.paid_spend_ledger import (
    ALREADY_RECORDED, CEILING_REACHED, NOT_METERED, PaidSpendLedger, QUOTE_CURRENCY,
    RECORDED,
)
from parts.llm_services.subscription_quota_watch import (
    DIVERGED_FROM_THE_PROVIDER, EXHAUSTED, MEASURED, SubscriptionQuotaWatch,
)
from parts.llm_services.subscription_session_caller import (
    ANSWERED as SUBSCRIPTION_ANSWERED, CALL_FAILED, RATE_LIMITED, RETRIES_EXHAUSTED,
    SESSION_EXPIRED, SubscriptionSessionCaller,
)
from runtime.llm_types import (
    LlmBackpressure, LlmCallRecord, LlmModelChoice, LlmPartBudget, LlmQuotaState,
    LlmResponse, LlmSpendState, LOCAL, METERED, RenderedLlmRequest, RoutedLlmRequest,
    SUBSCRIPTION,
)
from runtime.part_declaration import load_declaration_from_blueprint

BLOCK_PARTS = {
    "llm-request-router": "parts.llm_services.llm_request_router",
    "subscription-session-caller": "parts.llm_services.subscription_session_caller",
    "metered-api-caller": "parts.llm_services.metered_api_caller",
    "subscription-quota-watch": "parts.llm_services.subscription_quota_watch",
    "paid-spend-ledger": "parts.llm_services.paid_spend_ledger",
    "llm-model-picker": "parts.llm_services.llm_model_picker",
    "llm-response-cache": "parts.llm_services.llm_response_cache",
    "local-model-caller": "parts.llm_services.local_model_caller",
    "llm-backpressure-gauge": "parts.llm_services.llm_backpressure_gauge",
    "ground-truth-snapshot-builder": "parts.llm_services.ground_truth_snapshot_builder",
}


class Clock:
    def __init__(self, now_ns=1_700_000_000_000_000_000):
        self.now_ns = now_ns

    def __call__(self):
        return self.now_ns


class TickingClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_every_built_declaration_equals_the_blueprint(part_id):
    module = importlib.import_module(BLOCK_PARTS[part_id])
    assert module.PART_DECLARATION == load_declaration_from_blueprint(part_id)


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_no_part_in_this_block_imports_another_part(part_id):
    with open(importlib.import_module(BLOCK_PARTS[part_id]).__file__, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith(("from parts.", "import parts.")):
                raise AssertionError(f"{part_id} imports another part: {line.strip()}")


def a_rendered(fingerprint="fp-1", purpose="explain-a-trade", text="INSTRUCTION\nx"):
    return RenderedLlmRequest(
        rendered_id="r-1", request_id="req-1", version_id="t-1:v1", purpose=purpose,
        text=text, output_schema={"verdict": {"type": "string"}}, facts={"mid": 70_000.0},
        context_id="c-1", characters=len(text), fingerprint=fingerprint,
        rendered_at_ns=0,
    )


def a_routed(payment_kind=SUBSCRIPTION, model_id="model-x", part_id="ai-brain",
             routed_id="routed-1", text="INSTRUCTION"):
    return RoutedLlmRequest(
        routed_id=routed_id, rendered_id="r-1", request_id="req-1", version_id="t-1:v1",
        purpose="explain-a-trade", part_id=part_id, payment_kind=payment_kind,
        model_id=model_id, text=text, output_schema={}, facts={"mid": 70_000.0},
        fingerprint="fp-1", admitted_fraction=1.0, reason="", routed_at_ns=0,
    )


def a_budget(part_id="ai-brain", used=0, allowed=10):
    return LlmPartBudget(
        part_id=part_id, calls_allowed=allowed, tokens_allowed=10_000,
        character_budget=40_000, money_allowed=1.0, window_seconds=3600.0,
        calls_used=used, tokens_used=0, money_used=0.0, issued_at_ns=0,
    )


def a_quota(used=0, allowed=100, tokens_used=0, tokens_allowed=100_000, resets_at_ns=0):
    return LlmQuotaState(
        window_seconds=3600.0, calls_used=used, calls_allowed=allowed,
        tokens_used=tokens_used, tokens_allowed=tokens_allowed,
        resets_at_ns=resets_at_ns, measured_at_ns=0,
    )


def a_spend(spent=0.0, ceiling=10.0):
    return LlmSpendState(
        period_seconds=86_400.0, spent=spent, ceiling=ceiling, calls=0,
        period_started_at_ns=0, measured_at_ns=0,
    )


def a_model_choice(model_id="model-x", payment_kind=SUBSCRIPTION):
    return LlmModelChoice(
        request_id="req-1", purpose="explain-a-trade", model_id=model_id,
        payment_kind=payment_kind, expected_cost=0.001,
        expected_latency_seconds=1.0, quality_on_this_purpose=0.8, is_fitted=True,
        reason="", chosen_at_ns=0,
    )


# ---- llm-request-router -----------------------------------------------------

def a_router():
    router = LlmRequestRouter(now_ns=Clock())
    router.observe_budget(a_budget())
    router.observe_model_choice(a_model_choice())
    return router


def test_quota_is_spent_before_money():
    """Unspent quota at the end of a window is wasted."""
    subject = a_router()
    subject.observe_quota(a_quota())
    subject.observe_spend(a_spend())
    decision = subject.route(a_rendered(), "ai-brain")
    assert decision.state == ROUTED
    assert decision.payment_kind == SUBSCRIPTION


def test_money_is_last():
    subject = a_router()
    subject.observe_quota(a_quota(used=100, allowed=100))
    subject.observe_spend(a_spend())
    subject.observe_local_availability(False)
    decision = subject.route(a_rendered(), "ai-brain")
    assert decision.payment_kind == METERED


def test_a_part_out_of_budget_is_refused_at_the_router():
    subject = a_router()
    subject.observe_budget(a_budget(used=10, allowed=10))
    subject.observe_quota(a_quota())
    assert subject.route(a_rendered(), "ai-brain").state == PART_OUT_OF_BUDGET


def test_shedding_is_deterministic_so_a_retry_is_not_a_second_chance():
    subject = a_router()
    subject.observe_quota(a_quota())
    subject.observe_backpressure(
        LlmBackpressure(
            admit_fraction=0.5, reason="draining", quota_fraction_used=0.9,
            spend_fraction_used=0.0, survival_tier=None, measured_at_ns=0,
        )
    )
    first = subject.route(a_rendered(fingerprint="fp-a"), "ai-brain").state
    second = subject.route(a_rendered(fingerprint="fp-a"), "ai-brain").state
    assert first == second


def test_a_request_nothing_can_pay_for_is_refused_not_queued():
    subject = a_router()
    subject.observe_quota(a_quota(used=100, allowed=100))
    subject.observe_spend(a_spend(spent=10.0, ceiling=10.0))
    subject.observe_local_availability(False)
    decision = subject.route(a_rendered(), "ai-brain")
    assert decision.state == NOTHING_CAN_PAY
    assert "instead of a decision" in decision.reason
    assert importlib.import_module(
        BLOCK_PARTS["llm-request-router"]
    ).describe_routing(subject)["queues_a_refused_request"] is False


def test_a_purpose_is_deferred_rather_than_downgraded():
    subject = a_router()
    subject.observe_quota(a_quota(used=100, allowed=100))
    subject.observe_local_availability(True)
    decision = subject.route(a_rendered(), "ai-brain", acceptable_payment_kinds=(SUBSCRIPTION,))
    assert decision.state == WOULD_NEED_A_WEAKER_MODEL
    assert "the failure nobody sees" in decision.reason


def test_routing_without_a_chosen_model_is_refused():
    subject = LlmRequestRouter(now_ns=Clock())
    subject.observe_budget(a_budget())
    subject.observe_quota(a_quota())
    assert subject.route(a_rendered(), "ai-brain").state == NO_MODEL_CHOSEN


def test_the_preference_order_must_cover_every_pocket():
    with pytest.raises(ValueError):
        LlmRequestRouter(preference_order=(SUBSCRIPTION, METERED))


# ---- subscription-session-caller --------------------------------------------

def a_subscription_caller(retries=2):
    return SubscriptionSessionCaller(
        maximum_retries=retries, initial_backoff_seconds=1.0, backoff_multiplier=2.0,
        quota_cost_per_call=1.0, now_ns=Clock(),
    )


def test_a_failed_call_still_spent_quota():
    """Treating failures as free is how a retry loop consumes a window invisibly."""
    subject = a_subscription_caller()

    def failing(routed):
        raise RuntimeError("upstream")

    subject.install_session(failing)
    outcome = subject.call(a_routed())
    assert outcome.state == CALL_FAILED
    assert outcome.record.quota_spent == 1.0
    assert subject.standing.quota_spent_on_failures == 1.0


def test_backoff_grows_rather_than_retrying_immediately():
    subject = a_subscription_caller()

    def limited(routed):
        raise TimeoutError()

    subject.install_session(limited)
    first = subject.call(a_routed()).backoff_seconds
    second = subject.call(a_routed()).backoff_seconds
    assert second > first


def test_retries_are_bounded():
    subject = a_subscription_caller(retries=1)

    def failing(routed):
        raise RuntimeError("upstream")

    subject.install_session(failing)
    subject.call(a_routed())
    subject.call(a_routed())
    assert subject.call(a_routed()).state == RETRIES_EXHAUSTED


def test_an_expired_session_is_re_established_rather_than_retried_through():
    subject = a_subscription_caller()
    calls = {"count": 0}

    def sometimes(routed):
        calls["count"] += 1
        if calls["count"] == 1:
            raise PermissionError("session expired")
        return "answer", "stop", 10, 5, 0.5

    subject.install_session(sometimes, establish_session=lambda: None)
    assert subject.call(a_routed()).state == SESSION_EXPIRED
    outcome = subject.call(a_routed(routed_id="routed-2"))
    assert outcome.state == SUBSCRIPTION_ANSWERED
    assert subject.standing.sessions_re_established == 1


def test_a_successful_call_produces_a_record_and_a_response():
    subject = a_subscription_caller()
    subject.install_session(lambda routed: ("answer", "stop", 10, 5, 0.5))
    outcome = subject.call(a_routed())
    assert outcome.response.payment_kind == SUBSCRIPTION
    assert outcome.record.part_id == "ai-brain"


def test_the_caller_does_not_re_decide_whether_to_call():
    assert importlib.import_module(
        BLOCK_PARTS["subscription-session-caller"]
    ).describe_subscription_calling(
        a_subscription_caller()
    )["decides_whether_a_call_should_be_made"] is False


# ---- metered-api-caller -----------------------------------------------------

def a_metered_caller(ceiling=1.0, output_tokens=500, safety=1.2):
    caller = MeteredApiCaller(
        per_call_ceiling=ceiling, characters_per_token=4.0,
        estimated_output_tokens=output_tokens, estimate_safety_multiplier=safety,
        now_ns=Clock(),
    )
    caller.install_endpoint(
        lambda routed: ("answer", "stop", 100, 200, 1.0, None)
    )
    return caller


def test_input_and_output_tokens_are_priced_separately():
    """One blended rate misprices exactly the calls that produce long answers."""
    subject = a_metered_caller()
    subject.observe_price("model-x", input_price_per_token=0.000001, output_price_per_token=0.000010)
    estimate = subject.estimate_cost(a_routed(payment_kind=METERED, text="x" * 4000))
    input_only = 1.2 * 1000 * 0.000001
    assert estimate > input_only


def test_a_call_is_priced_before_it_is_made():
    subject = a_metered_caller()
    assert subject.call(a_routed(payment_kind=METERED), money_left=10.0).state == NO_PRICE


def test_one_call_may_not_exceed_the_per_call_ceiling():
    subject = a_metered_caller(ceiling=0.0001)
    subject.observe_price("model-x", 0.001, 0.002)
    outcome = subject.call(a_routed(payment_kind=METERED), money_left=1_000.0)
    assert outcome.state == ABOVE_THE_PER_CALL_CEILING
    assert "prompt that grew" in outcome.reason


def test_a_call_that_would_exceed_what_is_left_is_refused():
    subject = a_metered_caller(ceiling=100.0)
    subject.observe_price("model-x", 0.001, 0.002)
    assert subject.call(
        a_routed(payment_kind=METERED), money_left=0.0000001
    ).state == WOULD_EXCEED_THE_BUDGET


def test_the_record_uses_the_responses_own_token_counts_not_the_estimate():
    subject = a_metered_caller(ceiling=100.0)
    subject.observe_price("model-x", 0.001, 0.002)
    outcome = subject.call(a_routed(payment_kind=METERED), money_left=100.0)
    assert outcome.state == METERED_ANSWERED
    assert outcome.actual_cost == pytest.approx(100 * 0.001 + 200 * 0.002)
    assert outcome.actual_cost != outcome.estimated_cost


def test_a_provider_reported_cost_is_used_when_it_exists():
    subject = a_metered_caller(ceiling=100.0)
    subject.observe_price("model-x", 0.001, 0.002)
    subject.install_endpoint(lambda routed: ("answer", "stop", 100, 200, 1.0, 0.42))
    outcome = subject.call(a_routed(payment_kind=METERED), money_left=100.0)
    assert outcome.actual_cost == 0.42
    assert outcome.cost_was_reported


# ---- subscription-quota-watch -----------------------------------------------

def a_quota_watch(window=3600.0, calls=100, tokens=100_000, tolerance=0, clock=None):
    return SubscriptionQuotaWatch(
        window_seconds=window, calls_allowed=calls, tokens_allowed=tokens,
        divergence_tolerance=tolerance, now_ns=clock or Clock(),
    )


def a_record(part_id="ai-brain", kind=SUBSCRIPTION, succeeded=True, tokens=100, money=0.0,
             call_id="c-1"):
    return LlmCallRecord(
        call_id=call_id, part_id=part_id, purpose="explain-a-trade", version_id="t-1:v1",
        model_id="model-x", payment_kind=kind, input_tokens=tokens, output_tokens=tokens,
        money_spent=money, quota_spent=1.0, latency_seconds=1.0, was_cached=False,
        succeeded=succeeded, called_at_ns=0,
    )


def test_failed_calls_count_against_the_window():
    subject = a_quota_watch()
    subject.observe_record(a_record(succeeded=False))
    subject.observe_record(a_record(succeeded=True))
    reading = subject.measure()
    assert reading.quota.calls_used == 2
    assert subject.standing.failed_calls_counted == 1


def test_the_local_count_is_not_overwritten_by_the_provider():
    subject = a_quota_watch(tolerance=0)
    for _ in range(5):
        subject.observe_record(a_record())
    subject.observe_provider_header(calls_used=3)
    reading = subject.measure()
    assert reading.state == DIVERGED_FROM_THE_PROVIDER
    assert reading.quota.calls_used == 5
    assert reading.divergence == 2


def test_the_window_resets_rather_than_refilling_gradually():
    clock = Clock()
    subject = a_quota_watch(window=60.0, clock=clock)
    for _ in range(5):
        subject.observe_record(a_record())
    clock.now_ns += 61_000_000_000
    reading = subject.measure()
    assert reading.quota.calls_used == 0
    assert subject.standing.windows_reset == 1


def test_time_to_reset_travels_with_every_reading():
    clock = Clock()
    subject = a_quota_watch(window=3600.0, clock=clock)
    clock.now_ns += 600_000_000_000
    assert subject.measure().seconds_to_reset == pytest.approx(3000.0, abs=1.0)


def test_an_exhausted_window_says_nothing_more_is_available_until_the_reset():
    subject = a_quota_watch(calls=2)
    for index in range(2):
        subject.observe_record(a_record(call_id=f"c-{index}"))
    reading = subject.measure()
    assert reading.state == EXHAUSTED
    assert "does not refill gradually" in reading.reason


# ---- paid-spend-ledger ------------------------------------------------------

def a_ledger(period=86_400.0, ceiling=10.0, clock=None):
    return PaidSpendLedger(
        period_seconds=period, ceiling=ceiling, period_started_at_ns=(clock or Clock())(),
        now_ns=clock or Clock(),
    )


def test_every_entry_names_the_part_that_spent_it():
    subject = a_ledger()
    subject.record(a_record("ai-brain", METERED, money=1.0, call_id="c-1"))
    subject.record(a_record("bull-bot", METERED, money=2.0, call_id="c-2"))
    assert subject.spent_by_part() == {"bull-bot": 2.0, "ai-brain": 1.0}


def test_the_same_call_is_not_recorded_twice():
    subject = a_ledger()
    subject.record(a_record(kind=METERED, money=1.0))
    assert subject.record(a_record(kind=METERED, money=1.0)).state == ALREADY_RECORDED
    assert subject.spend_state().spent == 1.0


def test_quota_calls_are_not_money():
    subject = a_ledger()
    assert subject.record(a_record(kind=SUBSCRIPTION)).state == NOT_METERED
    assert subject.spend_state().spent == 0.0


def test_the_ceiling_cannot_be_raised_from_inside():
    subject = a_ledger(ceiling=1.0)
    outcome = subject.record(a_record(kind=METERED, money=1.5))
    assert outcome.state == CEILING_REACHED
    assert not hasattr(subject, "raise_ceiling")
    described = importlib.import_module(
        BLOCK_PARTS["paid-spend-ledger"]
    ).describe_spend_ledger(subject)
    assert described["can_raise_its_own_ceiling"] is False
    assert described["currency"] == QUOTE_CURRENCY


def test_the_period_rolls_on_its_boundary():
    clock = Clock()
    subject = a_ledger(period=60.0, clock=clock)
    subject.record(a_record(kind=METERED, money=1.0))
    clock.now_ns += 61_000_000_000
    assert subject.spend_state().spent == 0.0
    assert subject.standing.periods_rolled == 1
    assert subject.standing.total_spent_all_time == 1.0


# ---- llm-model-picker -------------------------------------------------------

def a_picker(exploration=0.0, minimum=3):
    picker = LlmModelPicker(
        exploration_share=exploration, minimum_observations=minimum,
        prior_quality=0.5, prior_weight=4.0, half_life_observations=200, now_ns=Clock(),
    )
    picker.declare_model("cheap", SUBSCRIPTION, cost_per_call=0.0, typical_latency_seconds=1.0)
    picker.declare_model("dear", METERED, cost_per_call=0.05, typical_latency_seconds=2.0)
    return picker


def test_the_cheapest_model_that_clears_the_bar_wins():
    """Headroom nobody uses is money spent for nothing."""
    subject = a_picker()
    for _ in range(20):
        subject.observe_outcome("cheap", "explain-a-trade", True)
        subject.observe_outcome("dear", "explain-a-trade", True)
    choice = subject.pick("req-1", "explain-a-trade", quality_bar=0.6)
    assert choice.state == CHOSEN
    assert choice.choice.model_id == "cheap"


def test_quality_is_measured_per_purpose_not_globally():
    subject = a_picker()
    for _ in range(20):
        subject.observe_outcome("cheap", "explain-a-trade", True)
        subject.observe_outcome("cheap", "write-a-premortem", False)
    good, fitted, _ = subject.quality_of("cheap", "explain-a-trade")
    bad, _, _ = subject.quality_of("cheap", "write-a-premortem")
    assert fitted and good > bad


def test_no_model_clearing_the_bar_is_refused_rather_than_downgraded():
    subject = a_picker()
    for _ in range(20):
        subject.observe_outcome("cheap", "explain-a-trade", False)
        subject.observe_outcome("dear", "explain-a-trade", False)
    choice = subject.pick("req-1", "explain-a-trade", quality_bar=0.9)
    assert choice.state == NO_MODEL_CLEARS_THE_BAR
    assert "a downgrade nobody chose" in choice.reason


def test_exploration_is_a_named_share_not_an_accident():
    subject = a_picker(exploration=0.5)
    for _ in range(20):
        subject.observe_outcome("cheap", "explain-a-trade", True)
    states = [
        subject.pick(f"req-{index}", "explain-a-trade", 0.6).state for index in range(6)
    ]
    assert EXPLORING in states
    assert subject.standing.exploratory_choices > 0


def test_picking_from_an_empty_set_is_refused():
    subject = LlmModelPicker(
        exploration_share=0.0, minimum_observations=3, prior_quality=0.5,
        prior_weight=4.0, half_life_observations=200, now_ns=Clock(),
    )
    assert subject.pick("req-1", "p", 0.5).state == NO_MODELS


def test_the_picker_makes_no_calls():
    assert importlib.import_module(
        BLOCK_PARTS["llm-model-picker"]
    ).describe_model_picking(a_picker())["makes_a_call"] is False


# ---- llm-response-cache -----------------------------------------------------

def a_cache(entries=10, ttl=None, default_ttl=60.0, clock=None):
    return LlmResponseCache(
        maximum_entries=entries, time_to_live_by_purpose=ttl or {},
        default_time_to_live_seconds=default_ttl, now_ns=clock or Clock(),
    )


def a_response(text="answer", finish_reason="stop"):
    return LlmResponse(
        response_id="resp-1", rendered_id="r-1", version_id="t-1:v1", model_id="model-x",
        text=text, finish_reason=finish_reason, input_tokens=10, output_tokens=5,
        latency_seconds=1.0, payment_kind=SUBSCRIPTION, was_cached=False,
        responded_at_ns=0,
    )


def test_a_call_differing_in_one_fact_is_a_different_call():
    clock = Clock()
    subject = a_cache(clock=clock)
    subject.store(a_rendered(fingerprint="fp-a"), a_response(), clock.now_ns)
    assert subject.look_up(a_rendered(fingerprint="fp-a"), clock.now_ns).state == HIT
    assert subject.look_up(a_rendered(fingerprint="fp-b"), clock.now_ns).state == MISS


def test_entries_expire_on_the_age_of_the_facts_not_their_own():
    clock = Clock()
    subject = a_cache(default_ttl=30.0, clock=clock)
    subject.store(a_rendered(), a_response(), clock.now_ns)
    stale_facts = clock.now_ns - 60_000_000_000
    lookup = subject.look_up(a_rendered(), stale_facts)
    assert lookup.state == EXPIRED
    assert "not their own" in lookup.reason


def test_the_time_to_live_is_per_purpose():
    subject = a_cache(ttl={"explain-a-trade": 5.0}, default_ttl=3600.0)
    assert subject.time_to_live_for("explain-a-trade") == 5.0
    assert subject.time_to_live_for("something-else") == 3600.0


def test_a_cached_response_is_marked_as_cached():
    clock = Clock()
    subject = a_cache(clock=clock)
    subject.store(a_rendered(), a_response(), clock.now_ns)
    lookup = subject.look_up(a_rendered(), clock.now_ns)
    assert lookup.response.was_cached
    assert lookup.response.latency_seconds == 0.0


def test_a_truncated_response_is_not_cached():
    """A failure served instantly for as long as the entry lives."""
    subject = a_cache()
    assert subject.store(
        a_rendered(), a_response(finish_reason="length"), 0
    ) == NOT_CACHEABLE


def test_the_cache_is_bounded():
    clock = Clock()
    subject = a_cache(entries=2, clock=clock)
    for index in range(5):
        subject.store(a_rendered(fingerprint=f"fp-{index}"), a_response(), clock.now_ns)
    assert subject.size() == 2
    assert subject.standing.evictions == 3


# ---- local-model-caller -----------------------------------------------------

def a_local_caller(concurrent=1, seconds=10.0, monotonic=None):
    return LocalModelCaller(
        maximum_concurrent_generations=concurrent, maximum_seconds_per_call=seconds,
        tokens_per_second=20.0, monotonic=monotonic or TickingClock(), now_ns=Clock(),
    )


def test_the_machine_being_busy_is_a_refusal_not_a_queue():
    subject = a_local_caller(concurrent=1)
    seen = {}

    def generate(text, deadline):
        seen["inner"] = subject.call(a_routed(payment_kind=LOCAL, routed_id="routed-2"))
        return "answer", "stop", 10

    subject.load_model("local-x", generate)
    subject.call(a_routed(payment_kind=LOCAL))
    assert seen["inner"].state == MACHINE_IS_BUSY
    assert "rather than queued" in seen["inner"].reason


def test_a_generation_has_a_hard_wall_clock_limit():
    subject = a_local_caller(seconds=2.0)

    def slow(text, deadline):
        raise TimeoutError()

    subject.load_model("local-x", slow)
    outcome = subject.call(a_routed(payment_kind=LOCAL))
    assert outcome.state == TOOK_TOO_LONG
    assert "released rather than held" in outcome.reason


def test_availability_is_reported_so_the_router_can_stop_offering_work():
    subject = a_local_caller()
    assert subject.is_available() is False
    subject.load_model("local-x", lambda text, deadline: ("answer", "stop", 10))
    assert subject.is_available() is True


def test_no_model_loaded_is_a_fact_about_the_machine():
    assert a_local_caller().call(a_routed(payment_kind=LOCAL)).state == NO_MODEL_LOADED


def test_a_local_answer_costs_no_money_and_no_quota():
    subject = a_local_caller()
    subject.load_model("local-x", lambda text, deadline: ("answer", "stop", 42))
    outcome = subject.call(a_routed(payment_kind=LOCAL))
    assert outcome.state == LOCAL_ANSWERED
    assert outcome.record.money_spent == 0.0
    assert outcome.record.quota_spent == 0.0


def test_the_caller_never_sets_its_own_thread_count():
    described = importlib.import_module(
        BLOCK_PARTS["local-model-caller"]
    ).describe_local_calling(a_local_caller())
    assert described["sets_its_own_thread_count"] is False
    assert described["queues_when_busy"] is False


# ---- llm-backpressure-gauge -------------------------------------------------

def a_gauge(throttle_at=0.6, pace=1.2, floor=0.05, clock=None):
    return LlmBackpressureGauge(
        throttle_begins_at=throttle_at, pace_tolerance=pace,
        minimum_admit_fraction=floor, now_ns=clock or Clock(),
    )


def test_nothing_measured_means_nothing_admitted():
    """Admitting freely on no measurement is exactly what Rule 8 forbids."""
    reading = a_gauge().measure()
    assert reading.state == NOT_MEASURED
    assert reading.backpressure.admit_fraction == 0.0


def test_admission_falls_smoothly_rather_than_off_a_cliff():
    subject = a_gauge(throttle_at=0.5, floor=0.1)
    assert subject.admit_fraction_for(0.4) == 1.0
    middling = subject.admit_fraction_for(0.75)
    assert 0.1 < middling < 1.0
    assert subject.admit_fraction_for(1.0) == 0.0


def test_the_tightest_constraint_wins_rather_than_an_average():
    """A healthy quota must not mask an exhausted budget."""
    subject = a_gauge(throttle_at=0.5)
    subject.observe_quota(a_quota(used=0, allowed=100))
    subject.observe_spend(a_spend(spent=9.5, ceiling=10.0))
    reading = subject.measure()
    assert reading.binding_constraint == "spend"
    assert reading.backpressure.admit_fraction < 1.0
    assert reading.state == THROTTLED_BY_SPEND


def test_a_burn_ahead_of_the_clock_is_throttled_before_the_window_empties():
    clock = Clock()
    subject = a_gauge(throttle_at=0.9, pace=1.2, clock=clock)
    # Half the window elapsed, most of the quota already spent.
    subject.observe_quota(
        a_quota(used=80, allowed=100, resets_at_ns=clock.now_ns + 1_800_000_000_000)
    )
    reading = subject.measure()
    assert reading.pace_ratio > 1.2
    assert reading.state == THROTTLED_BY_PACE


def test_a_degraded_survival_tier_stops_reasoning_outright():
    subject = a_gauge()
    subject.observe_quota(a_quota())
    subject.observe_survival_tier(TIERS_THAT_STOP_REASONING[0])
    reading = subject.measure()
    assert reading.state == STOPPED_BY_SURVIVAL
    assert reading.backpressure.is_stopped


def test_a_healthy_system_admits_everything():
    clock = Clock()
    subject = a_gauge(throttle_at=0.8, clock=clock)
    subject.observe_quota(
        a_quota(used=1, allowed=100, resets_at_ns=clock.now_ns + 3_600_000_000_000)
    )
    subject.observe_spend(a_spend(spent=0.0))
    reading = subject.measure()
    assert reading.state == OPEN
    assert reading.admits_everything


# ---- ground-truth-snapshot-builder ------------------------------------------

def a_builder(staleness=30.0, clock=None):
    return GroundTruthSnapshotBuilder(
        maximum_staleness_seconds=staleness, now_ns=clock or Clock(),
    )


def test_a_missing_fact_makes_the_whole_snapshot_unusable():
    clock = Clock()
    subject = a_builder(clock=clock)
    subject.observe_fact("binance-usdm", "BTCUSDT", BID, 69_999.0, clock.now_ns)
    outcome = subject.build("binance-usdm", "BTCUSDT", (BID, ASK))
    assert outcome.state == INCOMPLETE
    assert outcome.missing == (ASK,)


def test_derived_facts_are_computed_here_and_cannot_be_supplied():
    clock = Clock()
    subject = a_builder(clock=clock)
    with pytest.raises(ValueError):
        subject.observe_fact("binance-usdm", "BTCUSDT", MID, 70_000.0, clock.now_ns)
    subject.observe_fact("binance-usdm", "BTCUSDT", BID, 69_999.0, clock.now_ns)
    subject.observe_fact("binance-usdm", "BTCUSDT", ASK, 70_001.0, clock.now_ns)
    outcome = subject.build("binance-usdm", "BTCUSDT", (MID, SPREAD))
    assert outcome.snapshot.facts[MID] == 70_000.0
    assert outcome.snapshot.facts[SPREAD] == 2.0


def test_a_snapshot_is_only_as_fresh_as_its_stalest_fact():
    clock = Clock()
    subject = a_builder(staleness=60.0, clock=clock)
    subject.observe_fact("binance-usdm", "BTCUSDT", BID, 69_999.0, clock.now_ns - 30_000_000_000)
    subject.observe_fact("binance-usdm", "BTCUSDT", ASK, 70_001.0, clock.now_ns)
    outcome = subject.build("binance-usdm", "BTCUSDT", (BID, ASK))
    assert outcome.state == BUILT
    assert outcome.oldest_fact == BID
    assert outcome.snapshot.staleness_seconds == pytest.approx(30.0, abs=0.1)


def test_a_stale_snapshot_is_refused():
    clock = Clock()
    subject = a_builder(staleness=10.0, clock=clock)
    subject.observe_fact("binance-usdm", "BTCUSDT", BID, 1.0, clock.now_ns - 60_000_000_000)
    subject.observe_fact("binance-usdm", "BTCUSDT", ASK, 2.0, clock.now_ns)
    assert subject.build("binance-usdm", "BTCUSDT", (BID, ASK)).state == TOO_STALE


def test_contradictory_facts_are_caught_rather_than_reasoned_over():
    clock = Clock()
    subject = a_builder(clock=clock)
    subject.observe_fact("binance-usdm", "BTCUSDT", BID, 70_010.0, clock.now_ns)
    subject.observe_fact("binance-usdm", "BTCUSDT", ASK, 69_990.0, clock.now_ns)
    outcome = subject.build("binance-usdm", "BTCUSDT", (MID,))
    assert outcome.state == INCONSISTENT


def test_nothing_measured_is_not_filled_in():
    outcome = a_builder().build("binance-usdm", "NEWUSDT", (BID,))
    assert outcome.state == NOTHING_MEASURED
    described = importlib.import_module(
        BLOCK_PARTS["ground-truth-snapshot-builder"]
    ).describe_snapshot_building(a_builder())
    assert described["fills_a_missing_fact"] is False
    assert described["facts_filled_in"] == 0
