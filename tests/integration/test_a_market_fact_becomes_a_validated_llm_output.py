"""A real market fact, through the whole LLM chain, to a validated output.

**Nothing in this project had ever made an LLM call.** Sixteen parts publish
`llm-request` and the count of calls across all three callers was zero, not
because anything was broken but because four things in a row could not complete:
nothing could promote a purpose's first prompt version, no part could get its
first budget, `llm-request-router` looked a budget up by `str(context_id)`, and
the budget window rolled on every tick so no allowance could ever bind.

Those are fixed, and the live spine now shows two purposes with an active version
and 1,580 budgets issued where it showed zero. What it still cannot show is a
call *through the parts*, and for a correct reason: `context-assembler` needs a
verified snapshot, and `ground-truth-snapshot-builder` refuses to build one while
the market is shut — 122 `refused_stale` on a Saturday evening. Waiting for
Monday to find out whether eight parts fit together is exactly the babysitting
this project's rules exist to avoid.

So this test drives the chain end to end, today, on real captured prints:

    real bid/ask/last     ground-truth-snapshot-builder  -> verified-snapshot
                          part-token-budgeter            -> llm-part-budget
                          context-assembler              -> prompt-context
    prompt-registry + seed-prompt-promoter               -> an active version
                          prompt-renderer                -> rendered-llm-request
                          llm-model-picker               -> llm-model-choice
                          llm-request-router             -> subscription-llm-request
                          subscription-session-caller    -> llm-response   (REAL CALL)
                          structured-output-enforcer     -> validated-llm-output

**The prices are real and they are of one moment.** `tests/captured/upstox/
2026-09-08-reliance-book-and-trade-at-one-moment.json` was taken from this
project's own tape — the book's top level and the trade print that landed in the
*same nanosecond*, bid 1298.1, ask 1298.2, last 1298.2. One moment matters: the
snapshot builder refuses two facts that contradict each other, and a bid scraped
from one second with an ask from another is how a test would fake its way past
that check.

**One real LLM call is made, and it is marked.** It goes through the operator's
own Claude Code subscription (no API key exists on this box) at the measured
~$0.041 for a stripped haiku call, and it is the only test here that spends
anything, so it carries its own marker and can be deselected with
`-m "not spends_real_money"`.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from parts.llm_foundation.context_assembler import ContextAssembler
from parts.llm_foundation.part_token_budgeter import PartTokenBudgeter
from parts.llm_foundation.prompt_registry import PromptRegistry
from parts.llm_foundation.prompt_renderer import (
    OUTPUT_HEADING,
    SUBJECT_HEADING,
    PromptRenderer,
)
from parts.llm_foundation.seed_prompt_promoter import SeedPromptPromoter
from parts.llm_foundation.structured_output_enforcer import StructuredOutputEnforcer
from parts.llm_services.ground_truth_snapshot_builder import (
    ASK,
    BID,
    LAST_TRADE,
    MID,
    SPREAD,
    TICK_SIZE,
    GroundTruthSnapshotBuilder,
)
from parts.llm_services.llm_model_picker import LlmModelPicker
from parts.llm_services.llm_request_router import (
    ROUTED,
    SUBSCRIPTION,
    LlmRequestRouter,
)
from parts.llm_services.subscription_session_caller import SubscriptionSessionCaller
from runtime.claim_verification import make_request
from runtime.llm_providers import claude_code_session_call
from runtime.llm_types import LlmQuotaState, LlmSpendState, PromptTemplate

CAPTURE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "tests/captured/upstox/2026-09-08-reliance-book-and-trade-at-one-moment.json"
)

# The part standing in for whatever asks. Named rather than "a-part": the budget,
# the routing and the refusals are all per part, and a test that used a
# placeholder would not show that they line up.
ASKING_PART = "market-thesis-reasoner"
PURPOSE = "state-the-touch-for-one-symbol"
VENUE = "upstox"
SYMBOL = "RELIANCE"

# The live pool and window, read off the operator's own settings 2026-09-12.
CALLS_ALLOWED = 200
TOKENS_ALLOWED = 2_000_000
SPEND_CEILING = 1.0
WINDOW_SECONDS = 18_000.0
STARTING_SHARE = 0.05
MAXIMUM_STALENESS_SECONDS = 60.0

# Mid and spread are asked for as well as bid and ask: the builder computes those
# two rather than accepting them, so a snapshot that carries them proves the
# derivation ran instead of a second fetch having been trusted.
REQUIRED_FACTS = (BID, ASK, MID, SPREAD, LAST_TRADE, TICK_SIZE)


@pytest.fixture(scope="module")
def captured_moment() -> dict:
    return json.loads(CAPTURE.read_text())


@pytest.fixture
def clock(captured_moment) -> dict:
    """A clock a second after the prints, so real 2026-09-08 facts are fresh.

    The prices are real; only the clock is the test's. Without this every fact
    would be days stale and the snapshot builder would refuse it — correctly,
    which is the whole reason the live spine cannot do this while shut.
    """
    return {"now": int(captured_moment["trade"]["venue_time_ns"]) + 1_000_000_000}


def test_the_capture_is_one_moment_of_real_prints(captured_moment):
    """A bid from one second and an ask from another would fake the consistency check."""
    assert captured_moment["milliseconds_between_the_two_prints"] == 0.0
    top = captured_moment["book"]["payload"]["levels"][0]
    assert top["bid_price"] == 1298.1
    assert top["ask_price"] == 1298.2
    assert captured_moment["trade"]["payload"]["last_traded_price"] == 1298.2
    assert top["bid_price"] < top["ask_price"]


def a_verified_snapshot(captured_moment, clock):
    """The first link: real prints become a snapshot, or are refused."""
    builder = GroundTruthSnapshotBuilder(
        maximum_staleness_seconds=MAXIMUM_STALENESS_SECONDS,
        now_ns=lambda: clock["now"],
    )
    top = captured_moment["book"]["payload"]["levels"][0]
    measured_at_ns = int(captured_moment["book"]["venue_time_ns"])
    builder.observe_fact(VENUE, SYMBOL, BID, top["bid_price"], measured_at_ns)
    builder.observe_fact(VENUE, SYMBOL, ASK, top["ask_price"], measured_at_ns)
    builder.observe_fact(
        VENUE,
        SYMBOL,
        LAST_TRADE,
        captured_moment["trade"]["payload"]["last_traded_price"],
        int(captured_moment["trade"]["venue_time_ns"]),
    )
    builder.observe_fact(
        VENUE, SYMBOL, TICK_SIZE, captured_moment["tick_size_rupees"], measured_at_ns
    )
    outcome = builder.build(VENUE, SYMBOL, REQUIRED_FACTS)
    assert outcome.is_usable, outcome.reason
    return outcome.snapshot


def test_real_prints_become_a_verified_snapshot(captured_moment, clock):
    snapshot = a_verified_snapshot(captured_moment, clock)
    assert snapshot.is_complete
    assert snapshot.missing_facts == ()
    assert snapshot.staleness_seconds == pytest.approx(1.0, abs=0.01)
    # The derived facts are computed here rather than fetched, so they cannot
    # contradict the two they summarise.
    assert snapshot.facts["mid"] == pytest.approx(1298.15)
    assert snapshot.facts["spread"] == pytest.approx(0.1, abs=1e-9)


def a_budget(clock):
    """The second link, and the one that had never happened once."""
    budgeter = PartTokenBudgeter(
        window_seconds=WINDOW_SECONDS,
        starting_share=STARTING_SHARE,
        prior_usefulness=0.5,
        prior_weight=1.0,
        half_life_observations=20.0,
        minimum_usefulness_observations=10,
        characters_per_token=4.0,
        now_ns=lambda: clock["now"],
    )
    budgeter.observe_quota(a_quota(clock))
    budgeter.observe_spend(a_spend(clock))
    issue = budgeter.issue(ASKING_PART)
    assert issue.budget is not None, issue.reason
    return issue.budget


def a_quota(clock) -> LlmQuotaState:
    return LlmQuotaState(
        window_seconds=WINDOW_SECONDS,
        calls_used=0,
        calls_allowed=CALLS_ALLOWED,
        tokens_used=0,
        tokens_allowed=TOKENS_ALLOWED,
        resets_at_ns=clock["now"] + int(WINDOW_SECONDS * 1_000_000_000),
        measured_at_ns=clock["now"],
    )


def a_spend(clock) -> LlmSpendState:
    return LlmSpendState(
        period_seconds=86_400.0,
        spent=0.0,
        ceiling=SPEND_CEILING,
        calls=0,
        period_started_at_ns=clock["now"],
        measured_at_ns=clock["now"],
    )


def an_active_version(clock):
    """The third link: a template registered, then seeded into being active.

    Through the real registry and the real seeder, because "the registry would
    have accepted it" is the assumption that made this chain look fine for weeks.
    """
    registry = PromptRegistry(now_ns=lambda: clock["now"])
    template = PromptTemplate(
        template_id="state-the-touch",
        purpose=PURPOSE,
        instruction=(
            "State the bid, the ask and the last traded price from the facts given. "
            "Use only those numbers."
        ),
        required_context_kinds=("verified-facts",),
        # In `structured-output-enforcer`'s own vocabulary -- a rule per field.
        # `prompt-template-author` wrote `{"venue_id": "str"}` until 2026-09-12,
        # which raises `AttributeError: 'str' object has no attribute 'get'`
        # inside the enforcer: every template it had ever written would have
        # crash-looped the enforcer on the first real answer.
        output_schema={
            "venue_id": {"type": "string", "required": True},
            "symbol": {"type": "string", "required": True},
            "text": {"type": "string", "required": True},
        },
        written_at_ns=clock["now"],
        written_by="a-test",
        derived_from=None,
    )
    registry.observe_template(template)
    registered = registry.register(template.template_id)
    assert registered.is_usable, registered.reason
    assert not registered.version.is_active, "registration is not activation"

    seeder = SeedPromptPromoter(seeding_is_allowed=True, now_ns=lambda: clock["now"])
    seeder.observe_version(registered.version)
    promotion = seeder.seed(PURPOSE).promotion
    assert promotion is not None
    activated = registry.apply_promotion(promotion)
    assert activated.is_usable, activated.reason
    assert activated.version.is_active
    return activated.version


def a_rendered_request(captured_moment, clock):
    """Snapshot plus budget plus active version, rendered into what will be sent."""
    snapshot = a_verified_snapshot(captured_moment, clock)
    budget = a_budget(clock)
    version = an_active_version(clock)

    assembler = ContextAssembler(
        maximum_staleness_seconds=MAXIMUM_STALENESS_SECONDS,
        now_ns=lambda: clock["now"],
    )
    assembled = assembler.assemble(
        request_id="q-1", snapshot=snapshot, hits=(), budget=budget
    )
    assert assembled.is_usable, assembled.reason

    request = make_request(
        purpose=PURPOSE,
        venue_id=VENUE,
        symbol=SYMBOL,
        instruction=version.instruction,
        facts=dict(snapshot.facts),
        maximum_sentences=2,
        now_ns=lambda: clock["now"],
        asked_by=ASKING_PART,
    )
    renderer = PromptRenderer(now_ns=lambda: clock["now"])
    outcome = renderer.render(request, version, assembled.context)
    assert outcome.is_usable, outcome.reason
    return outcome.rendered, version, request, budget


def test_a_snapshot_and_a_budget_become_a_rendered_prompt(captured_moment, clock):
    rendered, version, request, _budget = a_rendered_request(captured_moment, clock)
    assert rendered.purpose == PURPOSE
    assert rendered.version_id == version.version_id
    # The asking part travels, which is what the router needs and never had.
    assert rendered.asked_by == ASKING_PART
    # The real numbers are in the text that will be sent, not a placeholder.
    assert "1298.1" in rendered.text
    assert "1298.2" in rendered.text
    # And the shape the enforcer will demand is stated in the prompt. Until
    # 2026-09-12 it was not, so every answer would have been refused as "not the
    # declared structure" -- a model cannot guess a schema nobody showed it.
    assert OUTPUT_HEADING in rendered.text
    assert '"venue_id": string' in rendered.text
    # And what the question is about. The schema requires venue_id and symbol in
    # the answer; the first real call returned both as null because the prompt
    # had never said what they were.
    assert SUBJECT_HEADING in rendered.text
    assert f"symbol = {SYMBOL}" in rendered.text


def a_routed_request(captured_moment, clock):
    """The link that could not have worked: routing on the asking part."""
    rendered, version, request, budget = a_rendered_request(captured_moment, clock)

    picker = LlmModelPicker(
        # Every call explores, because nothing is measured about any model yet:
        # int(1 / 0.9) is 1, so the picker's own exploration path fires on the
        # first pick. Live it is a fraction and about one request in five gets a
        # choice, which is the cold start resolving itself rather than a deadlock.
        exploration_share=0.9,
        minimum_observations=10,
        prior_quality=0.5,
        prior_weight=1.0,
        half_life_observations=20.0,
        now_ns=lambda: clock["now"],
    )
    # The one model this box has: the operator's own Claude Code subscription.
    picker.declare_model(
        model_id="haiku",
        payment_kind=SUBSCRIPTION,
        cost_per_call=0.041,
        typical_latency_seconds=6.0,
    )
    choice = picker.pick(rendered.request_id, PURPOSE, quality_bar=0.5)
    assert choice.choice is not None, choice.reason

    router = LlmRequestRouter()
    router.observe_local_availability(False)
    router.observe_quota(a_quota(clock))
    router.observe_spend(a_spend(clock))
    router.observe_budget(budget)
    router.observe_model_choice(choice.choice)

    decision = router.route(rendered, rendered.asked_by)
    assert decision.state == ROUTED, decision.reason
    assert decision.payment_kind == SUBSCRIPTION
    return decision.routed, version, request


def test_a_rendered_prompt_is_routed_to_the_subscription(captured_moment, clock):
    routed, _version, _request = a_routed_request(captured_moment, clock)
    assert routed.model_id == "haiku"
    assert "1298.1" in routed.text


@pytest.mark.spends_real_money
def test_the_whole_chain_reaches_a_validated_output_through_one_real_call(
    captured_moment, clock
):
    """The end of it: a real model answers, and the answer is checked against the facts.

    This is the only test in the suite that spends anything — one stripped haiku
    call through the operator's own subscription, measured at about $0.041. Run
    the rest with `-m "not spends_real_money"`.
    """
    routed, version, request = a_routed_request(captured_moment, clock)

    caller = SubscriptionSessionCaller(
        maximum_retries=1,
        initial_backoff_seconds=1.0,
        backoff_multiplier=2.0,
        quota_cost_per_call=1.0,
    )
    caller.install_session(
        call=claude_code_session_call(model="haiku", timeout_seconds=180.0),
        establish_session=lambda: True,
    )
    outcome = caller.call(routed)
    assert outcome.response is not None, outcome.reason
    response = outcome.response
    assert response.finish_reason == "end_turn", response.finish_reason
    assert response.output_tokens > 0
    assert response.input_tokens > 0

    enforcer = StructuredOutputEnforcer(
        maximum_repairs=1,
        relative_tolerance=0.001,
        require_a_citation=False,
        now_ns=lambda: clock["now"],
    )
    validated = enforcer.enforce(response, version, dict(request.facts))
    assert validated.output is not None, validated.reason
    assert validated.output.purpose == PURPOSE

    # The model was given four real numbers and asked for three of them back. At
    # least one must survive the check that every number is one of the facts --
    # which is the thing that makes this output usable rather than plausible.
    answered = validated.output.text or str(validated.output.value)
    assert answered.strip(), "a validated output with no text is not an answer"
    assert validated.output.unsupported_claims == (), validated.output.unsupported_claims
