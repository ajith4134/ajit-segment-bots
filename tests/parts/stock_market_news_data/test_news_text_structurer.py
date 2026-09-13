"""news-text-structurer against real stories and a real model's real answers.

RL-063: real data, never invented fixtures. The stories are rows this project's
own broker token fetched from Upstox on 2026-09-12
(`tests/captured/upstox/2026-09-12-news-for-thirty-underlyings.json`), decoded by
`broker-news-reader` and collapsed by `news-item-deduplicator` -- the two parts
that produce `distinct-news-item` on the spine. The answers are what `haiku`
actually wrote when this part's own INSTRUCTION and OUTPUT_SCHEMA were rendered
for two of those stories, kept verbatim in
`tests/captured/upstox/2026-09-12-two-real-stories-read-by-haiku.json`, fence
and all.

Every answer goes through the real `structured-output-enforcer` before this part
sees it, because that is the only way an answer reaches it on the spine: a test
that handed the part a dict it built itself would be checking a shape the model
never produced.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from parts.llm_foundation.structured_output_enforcer import StructuredOutputEnforcer
from parts.stock_market_news_data.broker_news_reader import BrokerNewsReader
from parts.stock_market_news_data.news_item_deduplicator import NewsItemDeduplicator
from parts.stock_market_news_data.news_text_structurer import (
    INSTRUCTION,
    OUTPUT_SCHEMA,
    PURPOSE,
    NewsTextStructurer,
    describe_structuring,
    figures_in,
)
from runtime.llm_types import SUBSCRIPTION, LlmResponse, PromptVersion
from runtime.news_types import ReadBy, Tense

CAPTURED = pathlib.Path(__file__).resolve().parents[3] / "tests/captured/upstox"
NEWS = CAPTURED / "2026-09-12-news-for-thirty-underlyings.json"
ANSWERS = CAPTURED / "2026-09-12-two-real-stories-read-by-haiku.json"

# As `settings/runtime.example.toml` derives them, so the suite exercises the
# values the running part is given.
WAIT_FOR_THE_MODEL_SECONDS = 60.0
STORIES_ASKED_ABOUT_PER_TICK = 1
MAXIMUM_SENTENCES = 4
OVERLAP_TO_COLLAPSE = 0.8
REMEMBER_FOR_SECONDS = 691200.0
NANOSECONDS_PER_SECOND = 1_000_000_000


@pytest.fixture(scope="module")
def captured_answers() -> dict:
    return json.loads(ANSWERS.read_text())


@pytest.fixture(scope="module")
def distinct_stories() -> dict:
    """Every distinct story in the capture, keyed by story_key, as the spine makes them."""
    reader = BrokerNewsReader(fetch=lambda url, token: {}, now_ns=lambda: 1_000)
    deduplicator = NewsItemDeduplicator(
        headline_overlap_to_collapse=OVERLAP_TO_COLLAPSE,
        remember_for_seconds=REMEMBER_FOR_SECONDS,
        now_ns=lambda: 1_000_000_000,
    )
    stories = {}
    for item in reader.items_in(json.loads(NEWS.read_text())):
        story = deduplicator.observe(item)
        if story is not None:
            stories[story.story_key] = story
    return stories


class Clock:
    def __init__(self) -> None:
        self.now = 1_757_600_000 * NANOSECONDS_PER_SECOND

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += int(seconds * NANOSECONDS_PER_SECOND)


def a_structurer(clock) -> NewsTextStructurer:
    return NewsTextStructurer(
        wait_for_the_model_seconds=WAIT_FOR_THE_MODEL_SECONDS,
        stories_asked_about_per_tick=STORIES_ASKED_ABOUT_PER_TICK,
        maximum_sentences=MAXIMUM_SENTENCES,
        now_ns=clock,
    )


def the_validated_output(answer: dict, request, clock):
    """The captured answer, through the real enforcer, as the spine delivers it."""
    version = PromptVersion(
        version_id="structure-a-news-item-v1",
        template_id="structure-a-news-item",
        purpose=PURPOSE,
        instruction=INSTRUCTION,
        output_schema=OUTPUT_SCHEMA,
        required_context_kinds=(),
        is_active=True,
        promoted_at_ns=clock(),
        created_at_ns=clock(),
    )
    response = LlmResponse(
        response_id="r-" + answer["story_key"][-14:],
        rendered_id="p-" + answer["story_key"][-14:],
        version_id=version.version_id,
        model_id="haiku",
        text=answer["answer_text"],
        finish_reason=answer["finish_reason"],
        input_tokens=answer["input_tokens"],
        output_tokens=answer["output_tokens"],
        latency_seconds=0.0,
        payment_kind=SUBSCRIPTION,
        was_cached=False,
        responded_at_ns=clock(),
    )
    enforcer = StructuredOutputEnforcer(
        maximum_repairs=1,
        relative_tolerance=0.02,
        require_a_citation=False,
        now_ns=clock,
    )
    outcome = enforcer.enforce(response, version, dict(request.facts))
    assert outcome.output is not None, outcome.reason
    return outcome.output


def test_the_captured_answers_were_asked_with_this_parts_own_contract(captured_answers):
    """If the instruction or schema changes, the capture no longer tests this part."""
    assert captured_answers["purpose"] == PURPOSE
    assert captured_answers["output_schema"] == json.loads(json.dumps(OUTPUT_SCHEMA))
    for answer in captured_answers["answers"]:
        assert answer["prompt"].startswith("INSTRUCTION\n" + INSTRUCTION)


def test_both_captured_stories_are_real_distinct_stories(captured_answers, distinct_stories):
    for answer in captured_answers["answers"]:
        assert answer["story_key"] in distinct_stories


def test_a_story_is_asked_about_once_and_carries_its_own_text_as_the_facts(
    captured_answers, distinct_stories
):
    clock = Clock()
    structurer = a_structurer(clock)
    story = distinct_stories[captured_answers["answers"][0]["story_key"]]

    structurer.observe_story(story)
    structurer.observe_story(story)  # restated under another instrument key
    (request,) = structurer.requests_for_this_tick()

    assert request.purpose == PURPOSE
    assert request.asked_by == "news-text-structurer"
    assert request.facts == {
        "story_key": story.story_key,
        "headline": story.title,
        "body": story.body,
    }
    assert structurer.requests_for_this_tick() == ()
    structurer.observe_story(story)  # seen again while its answer is awaited
    assert structurer.requests_for_this_tick() == ()
    assert structurer.standing.requests_made == 1


def test_the_backlog_is_asked_about_oldest_first_at_the_paced_rate(distinct_stories):
    clock = Clock()
    structurer = a_structurer(clock)
    for story in distinct_stories.values():
        structurer.observe_story(story)

    oldest_first = sorted(
        distinct_stories.values(), key=lambda story: story.earliest_published_at_ns
    )
    asked = [structurer.requests_for_this_tick() for _ in range(3)]
    assert [len(batch) for batch in asked] == [1, 1, 1]
    assert [batch[0].facts["story_key"] for batch in asked] == [
        story.story_key for story in oldest_first[:3]
    ]
    assert structurer.stories_queued == len(distinct_stories) - 3


@pytest.mark.parametrize("which", [0, 1])
def test_a_real_model_answer_becomes_a_structured_item_that_matches_its_text(
    which, captured_answers, distinct_stories
):
    clock = Clock()
    structurer = a_structurer(clock)
    answer = captured_answers["answers"][which]
    story = distinct_stories[answer["story_key"]]
    structurer.observe_story(story)
    (request,) = structurer.requests_for_this_tick()

    item = structurer.observe_answer(the_validated_output(answer, request, clock))

    assert item is not None
    assert item.read_by is ReadBy.A_MODEL
    assert item.story_key == story.story_key
    assert item.headline == story.title
    assert item.tense is Tense.HAPPENED
    assert item.event_phrase
    # Every name the model returned is one the story's own text contains, up to
    # the spelling the article used -- the model added no company.
    text = f"{story.title} {story.body}".lower()
    for name in item.names_mentioned:
        assert name.lower().split()[0] in text, name
    assert item.published_at_ns == story.earliest_published_at_ns
    assert structurer.stories_awaited == 0


def test_the_oil_story_is_read_into_the_names_and_figures_it_states(
    captured_answers, distinct_stories
):
    clock = Clock()
    structurer = a_structurer(clock)
    answer = captured_answers["answers"][0]
    structurer.observe_story(distinct_stories[answer["story_key"]])
    (request,) = structurer.requests_for_this_tick()

    item = structurer.observe_answer(the_validated_output(answer, request, clock))

    assert item.names_mentioned == ("Reliance", "HPCL", "IndiGo", "BPCL")
    assert item.figures_cited == ("17-week high", "$110 per barrel")


def test_a_figure_the_story_never_stated_is_still_refused(captured_answers, distinct_stories):
    """Text facts now carry their numbers; that must not let an invented one through.

    The real oil answer with its one real figure changed from 110 to 120 --
    the smallest edit that turns a reading into a fabrication.
    """
    clock = Clock()
    structurer = a_structurer(clock)
    answer = dict(captured_answers["answers"][0])
    assert "$110 per barrel" in answer["answer_text"]
    answer["answer_text"] = answer["answer_text"].replace("$110", "$120")
    structurer.observe_story(distinct_stories[answer["story_key"]])
    (request,) = structurer.requests_for_this_tick()

    with pytest.raises(AssertionError, match="trace to no measurement"):
        the_validated_output(answer, request, clock)


def test_an_answer_for_a_story_nobody_asked_about_is_counted_not_used(
    captured_answers, distinct_stories
):
    clock = Clock()
    asker = a_structurer(clock)
    answer = captured_answers["answers"][1]
    asker.observe_story(distinct_stories[answer["story_key"]])
    (request,) = asker.requests_for_this_tick()
    output = the_validated_output(answer, request, clock)

    stranger = a_structurer(clock)
    assert stranger.observe_answer(output) is None
    assert stranger.standing.answers_for_a_story_not_awaited == 1


def test_without_a_model_the_story_is_structured_from_the_source_and_says_so(
    captured_answers, distinct_stories
):
    clock = Clock()
    structurer = a_structurer(clock)
    story = distinct_stories[captured_answers["answers"][0]["story_key"]]
    structurer.observe_story(story)
    structurer.requests_for_this_tick()

    clock.advance(WAIT_FOR_THE_MODEL_SECONDS - 1)
    assert structurer.stories_the_model_did_not_answer() == ()

    clock.advance(1)
    (item,) = structurer.stories_the_model_did_not_answer()
    assert item.read_by is ReadBy.THE_SOURCE_ITSELF
    assert item.tense is Tense.NOT_STATED
    assert item.names_mentioned == ()
    assert item.event_phrase == story.title
    assert "110" in " ".join(item.figures_cited)
    standing = describe_structuring(structurer)
    assert standing["gave_up_waiting"] == 1
    assert standing["structured_from_the_source_only"] == 1


def test_figures_are_scanned_as_written_across_every_real_story(distinct_stories):
    """The fallback scan finds only things with digits, and keeps their units."""
    scanned = 0
    for story in distinct_stories.values():
        for figure in figures_in(f"{story.title} {story.body}"):
            assert any(character.isdigit() for character in figure), figure
            assert figure in f"{story.title} {story.body}", figure
            scanned += 1
    assert scanned > 0
