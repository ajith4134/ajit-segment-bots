"""llm-model-picker explores what is unmeasured, and learns from the enforcer's verdict.

Measured on the live spine 2026-09-13: 526 requests, 52 chosen, 474 refused as
`no-model-has-shown-it-can-do-this-purpose-well-enough`, with one model declared
and nothing ever measured. These tests replay that shape on the live settings
and judge real answers -- the two `haiku` wrote for real Upstox stories, in
`tests/captured/upstox/2026-09-12-two-real-stories-read-by-haiku.json` --
through the real enforcer configured as `start_part` configures it.
"""

from __future__ import annotations

import json
import pathlib
from collections import Counter
from types import SimpleNamespace

import pytest

from parts.llm_foundation.structured_output_enforcer import (
    StructuredOutputEnforcer,
    verdict_of,
)
from parts.llm_services.llm_model_picker import (
    CHOSEN,
    EXPLORING,
    NO_MODEL_CLEARS_THE_BAR,
    LlmModelPicker,
    observe_outcomes,
)
from runtime.llm_types import SUBSCRIPTION, LlmResponse, PromptVersion

ANSWERS = (
    pathlib.Path(__file__).resolve().parents[3]
    / "tests/captured/upstox/2026-09-12-two-real-stories-read-by-haiku.json"
)

# The live settings, as `settings/runtime.example.toml` states them.
EXPLORATION_SHARE = 0.1
MINIMUM_VERDICTS = 19
PRIOR_QUALITY = 0.5
PRIOR_WEIGHT = 4.0
HALF_LIFE = 500.0
QUALITY_BAR = 0.6
CLAIM_TOLERANCE = 0.02
MAXIMUM_REPAIRS_AS_A_FIRST_ANSWER_SEES_THEM = 1

LIVE_REQUESTS_SEEN = 526
PURPOSE = "structure-a-news-item"


def a_picker() -> LlmModelPicker:
    picker = LlmModelPicker(
        exploration_share=EXPLORATION_SHARE,
        minimum_observations=MINIMUM_VERDICTS,
        prior_quality=PRIOR_QUALITY,
        prior_weight=PRIOR_WEIGHT,
        half_life_observations=HALF_LIFE,
        now_ns=lambda: 1_000,
    )
    picker.declare_model("haiku", SUBSCRIPTION, 0.041, 8.0)
    return picker


@pytest.fixture(scope="module")
def captured() -> dict:
    return json.loads(ANSWERS.read_text())


def a_judged_answer(answer: dict, schema: dict, facts: dict):
    """One captured answer through the enforcer exactly as the spine configures it."""
    version = PromptVersion(
        version_id="v1", template_id="template:" + PURPOSE, purpose=PURPOSE,
        instruction="", output_schema=schema, required_context_kinds=(),
        is_active=True, promoted_at_ns=1, created_at_ns=1,
    )
    response = LlmResponse(
        response_id="r-" + answer["story_key"][-8:], rendered_id="p-" + answer["story_key"][-8:],
        version_id="v1", model_id="haiku", text=answer["answer_text"],
        finish_reason=answer["finish_reason"], input_tokens=answer["input_tokens"],
        output_tokens=answer["output_tokens"], latency_seconds=0.0,
        payment_kind=SUBSCRIPTION, was_cached=False, responded_at_ns=1,
    )
    enforcer = StructuredOutputEnforcer(
        maximum_repairs=MAXIMUM_REPAIRS_AS_A_FIRST_ANSWER_SEES_THEM,
        relative_tolerance=CLAIM_TOLERANCE,
        require_a_citation=True,
        now_ns=lambda: 1,
    )
    outcome = enforcer.enforce(response, version, facts)
    return verdict_of(outcome, response, version, now_ns=lambda: 1)


def facts_of(answer: dict) -> dict:
    """The facts the rendered request carried, read back out of the prompt it rendered."""
    block = answer["prompt"].split("MEASURED FACTS (the only numbers you may use)\n", 1)[1]
    block = block.split("\n\nANSWER WITH", 1)[0]
    facts: dict[str, str] = {}
    name = None
    for line in block.splitlines():
        head, separator, value = line.partition(" = ")
        if separator and head in ("body", "headline", "story_key"):
            name = head
            facts[name] = value
        elif name is not None:
            # A body that runs over several lines is still one fact.
            facts[name] += "\n" + line
    assert set(facts) == {"body", "headline", "story_key"}
    return facts


def test_the_live_refusals_are_reproduced_by_the_old_rule_and_gone_under_the_new():
    picker = a_picker()
    states = Counter(
        picker.pick(f"r{index}", PURPOSE, QUALITY_BAR).state
        for index in range(LIVE_REQUESTS_SEEN)
    )
    assert states[NO_MODEL_CLEARS_THE_BAR] == 0
    assert states[EXPLORING] == LIVE_REQUESTS_SEEN
    assert picker.standing.choices_for_a_purpose_nothing_is_measured_on == LIVE_REQUESTS_SEEN


def test_a_model_measured_below_the_bar_is_still_refused():
    picker = a_picker()
    for _ in range(MINIMUM_VERDICTS):
        picker.observe_outcome("haiku", PURPOSE, was_good=False)
    choice = picker.pick("r", PURPOSE, QUALITY_BAR)
    assert choice.state == NO_MODEL_CLEARS_THE_BAR
    # A different purpose has no record, and is not refused on another's evidence.
    assert picker.pick("r2", "argue-against-this-trade", QUALITY_BAR).state == EXPLORING


def test_a_model_measured_above_the_bar_is_chosen_not_explored():
    picker = a_picker()
    for _ in range(MINIMUM_VERDICTS):
        picker.observe_outcome("haiku", PURPOSE, was_good=True)
    assert picker.pick("r", PURPOSE, QUALITY_BAR).state == CHOSEN


def test_a_real_answer_judged_against_its_real_facts_is_usable(captured):
    for answer in captured["answers"]:
        verdict = a_judged_answer(answer, captured["output_schema"], facts_of(answer))
        assert verdict.was_usable, verdict.state
        assert verdict.model_id == "haiku"
        assert verdict.purpose == PURPOSE


def test_the_same_answer_judged_against_no_facts_was_rejected(captured):
    """What the spine's enforcer did until 2026-09-13: every answer against `{}`."""
    for answer in captured["answers"]:
        verdict = a_judged_answer(answer, captured["output_schema"], {})
        assert not verdict.was_usable


def test_an_answered_call_is_counted_once_by_its_verdict(captured):
    """A succeeded record plus a rejecting verdict is one bad outcome, not a good and a bad."""
    picker = a_picker()
    answer = captured["answers"][0]
    rejected = a_judged_answer(answer, captured["output_schema"], {})
    succeeded = SimpleNamespace(model_id="haiku", purpose=PURPOSE, succeeded=True)
    failed = SimpleNamespace(model_id="haiku", purpose=PURPOSE, succeeded=False)

    observe_outcomes(picker, (succeeded, failed), (rejected,))

    assert picker.standing.outcomes_observed == 2
    estimator = picker._quality[("haiku", PURPOSE)]
    assert estimator.observations == 2
