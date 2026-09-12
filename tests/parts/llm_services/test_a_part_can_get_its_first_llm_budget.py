"""A part's first LLM call: the budget cold start, and the key the router used.

Two defects measured on the live spine 2026-09-12, both invisible until
`seed-prompt-promoter` let a prompt version become active for the first time:

- `part-token-budgeter` had issued **0** budgets ever, with a full pool in front
  of it (200 calls, 2,000,000 tokens, $1). It learned a part existed only from an
  `llm-call-record`; a record needs a call; a call needs this budget.
  `llm_budget_starting_share` — a setting that exists for precisely the part
  nothing has been measured about — could never be applied to anybody.
- `llm-request-router` looked a per-part budget up by `str(rendered.context_id)`,
  which is the id of an assembled prompt context and never a part id. Even with
  budgets in hand, every lookup would have missed and
  `refused_part_out_of_budget` was the only outcome this router could reach.

Both are exercised here through the real parts and the real payload types.
"""

from __future__ import annotations

import pytest

from parts.llm_foundation.part_token_budgeter import PartTokenBudgeter
from parts.llm_services.llm_request_router import (
    NO_ASKING_PART,
    PART_OUT_OF_BUDGET,
    ROUTED,
    SUBSCRIPTION,
    LlmRequestRouter,
    describe_routing,
)
from runtime.claim_verification import make_request
from runtime.llm_types import (
    LlmModelChoice,
    LlmQuotaState,
    LlmSpendState,
    RenderedLlmRequest,
)

A_MOMENT = 1_789_223_000_000_000_000
ASKING_PART = "news-text-structurer"

# The live pool, read off the operator's own settings on 2026-09-12.
CALLS_ALLOWED = 200
TOKENS_ALLOWED = 2_000_000
SPEND_CEILING = 1.0
WINDOW_SECONDS = 18_000.0
STARTING_SHARE = 0.05


def a_budgeter() -> PartTokenBudgeter:
    return PartTokenBudgeter(
        window_seconds=WINDOW_SECONDS,
        starting_share=STARTING_SHARE,
        prior_usefulness=0.5,
        prior_weight=1.0,
        half_life_observations=20.0,
        minimum_usefulness_observations=10,
        characters_per_token=4.0,
        now_ns=lambda: A_MOMENT,
    )


def a_quota() -> LlmQuotaState:
    return LlmQuotaState(
        window_seconds=WINDOW_SECONDS,
        calls_used=0,
        calls_allowed=CALLS_ALLOWED,
        tokens_used=0,
        tokens_allowed=TOKENS_ALLOWED,
        resets_at_ns=A_MOMENT + int(WINDOW_SECONDS * 1_000_000_000),
        measured_at_ns=A_MOMENT,
    )


def a_spend() -> LlmSpendState:
    return LlmSpendState(
        period_seconds=86_400.0,
        spent=0.0,
        ceiling=SPEND_CEILING,
        calls=0,
        period_started_at_ns=A_MOMENT,
        measured_at_ns=A_MOMENT,
    )


def a_rendered_request(asked_by: str = ASKING_PART) -> RenderedLlmRequest:
    return RenderedLlmRequest(
        rendered_id="r-1",
        request_id="q-1",
        version_id="a-template:1",
        purpose="structure-a-news-item",
        text="State what the facts show, briefly.",
        output_schema={"venue_id": "str", "symbol": "str", "text": "str"},
        facts={"venue_id": "upstox", "symbol": "RELIANCE"},
        context_id="ctx-1",
        characters=40,
        fingerprint="f-1",
        rendered_at_ns=A_MOMENT,
        asked_by=asked_by,
    )


def test_a_request_names_the_part_that_asked():
    """Nothing on this wire named one, which is why no budget could be found."""
    request = make_request(
        purpose="structure-a-news-item",
        venue_id="upstox",
        symbol="RELIANCE",
        instruction="State what the facts show.",
        facts={"heading": "a real headline"},
        maximum_sentences=2,
        asked_by=ASKING_PART,
    )
    assert request.asked_by == ASKING_PART
    assert request.names_the_asking_part

    unnamed = make_request(
        purpose="structure-a-news-item",
        venue_id="upstox",
        symbol="RELIANCE",
        instruction="State what the facts show.",
        facts={"heading": "a real headline"},
        maximum_sentences=2,
    )
    assert unnamed.asked_by == ""
    assert not unnamed.names_the_asking_part


def test_a_part_that_has_never_called_gets_the_starting_share():
    """The whole point of `llm_budget_starting_share`, never once applied."""
    budgeter = a_budgeter()
    budgeter.observe_quota(a_quota())
    budgeter.observe_spend(a_spend())

    issue = budgeter.issue(ASKING_PART)
    assert issue.budget is not None
    assert issue.is_fitted is False, "nothing has been measured about this part yet"
    assert issue.share == pytest.approx(STARTING_SHARE)
    assert issue.budget.calls_allowed == int(CALLS_ALLOWED * STARTING_SHARE) == 10
    assert issue.budget.tokens_allowed == int(TOKENS_ALLOWED * STARTING_SHARE)
    assert issue.budget.is_spent is False
    assert budgeter.standing.budgets_issued == 1


def test_the_router_finds_the_budget_by_the_asking_part():
    """The fix: `asked_by`, not `context_id`."""
    budgeter = a_budgeter()
    budgeter.observe_quota(a_quota())
    budgeter.observe_spend(a_spend())
    budget = budgeter.issue(ASKING_PART).budget

    router = LlmRequestRouter()
    router.observe_local_availability(False)
    router.observe_quota(a_quota())
    router.observe_spend(a_spend())
    router.observe_budget(budget)
    router.observe_model_choice(
        LlmModelChoice(
            request_id="q-1",
            purpose="structure-a-news-item",
            model_id="haiku",
            payment_kind=SUBSCRIPTION,
            expected_cost=0.041,
            expected_latency_seconds=6.0,
            quality_on_this_purpose=0.5,
            is_fitted=False,
            reason="the only model declared on this box",
            chosen_at_ns=A_MOMENT,
        )
    )

    decision = router.route(a_rendered_request(), ASKING_PART)
    assert decision.state == ROUTED, decision.reason
    assert decision.is_usable
    assert decision.payment_kind == SUBSCRIPTION


def test_the_context_id_is_not_a_part_id_and_never_finds_a_budget():
    """The defect itself, as it behaved: a guaranteed miss."""
    budgeter = a_budgeter()
    budgeter.observe_quota(a_quota())
    budgeter.observe_spend(a_spend())
    budget = budgeter.issue(ASKING_PART).budget

    router = LlmRequestRouter()
    router.observe_local_availability(False)
    router.observe_quota(a_quota())
    router.observe_spend(a_spend())
    router.observe_budget(budget)

    rendered = a_rendered_request()
    decision = router.route(rendered, str(rendered.context_id))
    assert decision.state == PART_OUT_OF_BUDGET
    assert not decision.is_usable


def test_a_request_that_names_no_part_is_refused_under_its_own_name():
    """A producer that was not updated must be findable, not blamed on a budget."""
    router = LlmRequestRouter()
    router.observe_local_availability(False)
    router.observe_quota(a_quota())
    router.observe_spend(a_spend())

    decision = router.route(a_rendered_request(asked_by=""), "")
    assert decision.state == NO_ASKING_PART
    assert not decision.is_usable
    standing = describe_routing(router)
    assert standing["refused_no_asking_part"] == 1
    assert standing["refused_part_out_of_budget"] == 0


def test_every_part_that_asks_for_a_call_now_names_itself():
    """One missed producer is one part that can never make a call.

    Read off the source rather than asserted in prose: every module that builds a
    request must pass its own `PART_ID`.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3] / "parts"
    missing = []
    for path in root.rglob("*.py"):
        text = path.read_text()
        builds_a_request = "make_request(" in text or " LlmRequest(" in text
        if not builds_a_request:
            continue
        if "asked_by=" not in text:
            missing.append(path.relative_to(root).as_posix())
    assert missing == [], f"these build a request without naming the asking part: {missing}"


def test_usage_accumulates_inside_a_window_so_a_budget_can_actually_bind():
    """A third defect from the same live reading: `windows_rolled` 103 in 5 minutes.

    `read_state` rolled the window on every tick, which clears `calls_used`,
    `tokens_used` and `money_used`. So a part's usage was zero every time anybody
    looked, `budget.is_spent` could never be true, and
    `refused_part_out_of_budget` was a refusal the system could not reach. A guard
    that cannot fire is the same defect as a board that cannot render red.
    """
    from runtime.llm_types import LlmCallRecord

    clock = {"now": A_MOMENT}
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
    budgeter.observe_quota(a_quota())
    budgeter.observe_spend(a_spend())
    allowed = budgeter.issue(ASKING_PART).budget.calls_allowed
    assert allowed == 10

    # Spend the whole allowance inside the window, a second apart.
    for index in range(allowed):
        clock["now"] += 1_000_000_000
        budgeter.observe_call(
            LlmCallRecord(
                call_id=f"c-{index}",
                part_id=ASKING_PART,
                purpose="structure-a-news-item",
                version_id="a-template:1",
                model_id="haiku",
                payment_kind=SUBSCRIPTION,
                input_tokens=100,
                output_tokens=50,
                money_spent=0.0,
                quota_spent=1.0,
                latency_seconds=6.0,
                was_cached=False,
                succeeded=True,
                called_at_ns=clock["now"],
            )
        )
    issue = budgeter.issue(ASKING_PART)
    assert issue.budget.calls_used == allowed
    assert issue.budget.is_spent, "the allowance must be able to run out"
    assert budgeter.standing.windows_rolled == 0

    # And the window does end when the window's own length has passed.
    clock["now"] += int(WINDOW_SECONDS * 1_000_000_000)
    after = budgeter.issue(ASKING_PART)
    assert budgeter.standing.windows_rolled == 1
    assert after.budget.calls_used == 0
    assert not after.budget.is_spent
