"""news-text-structurer: read one item's text into its schema-checked fields.

**The single LLM read in this block**, and the block's own design says so: every
other judgement here is a learned scorer or a deterministic rule, and putting a
model anywhere else — in the fetch path, in the deduplicator — would put a model
between the broker and the tape.

What it asks for is narrow on purpose: what happened, to whom, which figures the
text states, and the tense of the claim. It does not ask which way the news
points (`news-sentiment-model`), whether the news is new (`news-novelty-scorer`),
which instruments it means (`news-symbol-resolver`, against the broker's own
listings) or which segments it belongs to (`news-segment-classifier`). A model
asked for one thing can be checked; a model asked for a verdict cannot.

## The answer is correlated by a key the model hands back

`validated-llm-output` carries the purpose and the parsed value, not the id of
the request that caused it. So `story_key` is one of the facts sent and a
required field of the output schema: the answer says which story it is about, and
an answer naming a story nobody asked about is counted
(`answers_for_a_story_not_awaited`) rather than guessed at. That is the same
device `prompt-evaluator` uses when it embeds the case and version it is scoring.

## When no model answers, the source's own fields still get through

`runtime/claim_verification.written_without_a_model` states the house rule: a
part whose output depends on a model being available goes silent exactly when the
model is down. Upstox gives a heading and a summary, and those are real text
whoever reads them — so after `wait_for_the_model_seconds` a story is structured
from what the source itself stated, and `read_by` says
`the-source-own-fields-only-no-model-answered`.

That is not a placeholder (RL-062). Nothing is invented: `event_phrase` becomes
the source's own headline, `figures_cited` the figures a plain scan finds in it,
`names_mentioned` empty rather than guessed, and `tense` `not-stated` rather than
`happened` — the reading that would cost money if wrong. A consumer can tell the
two apart on every single item, which is the whole point of `read_by` being on
the payload rather than in a log.

## What it costs, and why it is paced

Measured 2026-09-12: the broker's news endpoint carries a rolling backlog of 167
hours and `news-item-deduplicator` collapses it to about 31 distinct stories per
30 underlyings. One model call per story at the subscription transport's measured
$0.041 is real money, and `llm_subscription_calls_per_hour` is 20 — so this part
asks about the stories it has not asked about yet, oldest first, at
`news_stories_asked_about_per_tick` a tick, and lets the router refuse the rest.
Asking twice about one story is the waste worth avoiding; asking about a story
whose answer is already in is worse, because it pays to learn what it knows.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from runtime.claim_verification import make_request
from runtime.news_types import ReadBy, StructuredNewsItem, Tense
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "news-text-structurer"

PART_DECLARATION = PartDeclaration(
    part_id="news-text-structurer",
    consumes=("distinct-news-item", "validated-llm-output"),
    produces=("structured-news-item", "llm-request", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

NANOSECONDS_PER_SECOND = 1_000_000_000

# The purpose this part asks under. One string, declared here as a constant
# because `prompt-template-author` keys the templates it writes off the purposes
# parts actually ask for -- a purpose spelled differently in two places is a
# template written for a question nobody asks, which is the 2026-09-07 defect
# that part's own tick carries a note about.
PURPOSE = "structure-a-news-item"

# What the model is asked. Narrow, and every field checkable against the text it
# was given: `structured-output-enforcer` refuses an answer that is not this
# shape, and `claim_verification` refuses a number that is not in the facts.
OUTPUT_SCHEMA = {
    "story_key": {"type": "string", "required": True},
    "event_phrase": {"type": "string", "required": True},
    "names_mentioned": {"type": "string", "required": True},
    "figures_cited": {"type": "string", "required": True},
    "tense": {
        "type": "string",
        "required": True,
        "one_of": [tense.value for tense in Tense],
    },
}

INSTRUCTION = (
    "Read the news item in the facts. Answer with: story_key exactly as given; "
    "event_phrase, one short clause saying what happened, using the item's own "
    "words; names_mentioned, the company and index names the item names, "
    "semicolon separated, empty if none; figures_cited, the figures the item "
    "states with their units, semicolon separated, empty if none; and tense, "
    "one of happened, scheduled, anticipated, not-stated. Do not add anything "
    "the item does not say."
)

# How the model is asked to separate a list inside one string field. A schema
# field is a string because `structured-output-enforcer` checks strings, numbers
# and booleans, and a nested list is not one of them -- so the separator is part
# of the contract rather than a convention the prompt hopes for.
LIST_SEPARATOR = ";"

# A figure in text: a number with what follows it attached, so "12%" and
# "$110/barrel" survive as written. Used only for the no-model fallback, where a
# plain scan is honest and a guess is not.
FIGURE_IN_TEXT = re.compile(r"[₹$]?\d[\d,]*\.?\d*\s?(?:%|per cent|percent|crore|lakh|bps)?")


@dataclass
class StructurerStanding:
    stories_seen: int = 0
    requests_made: int = 0
    structured_by_a_model: int = 0
    structured_from_the_source_only: int = 0
    answers_read: int = 0
    # An answer naming a story this part never asked about. Counted rather than
    # guessed at: it means the correlation key is not doing its job.
    answers_for_a_story_not_awaited: int = 0
    answers_with_an_unusable_tense: int = 0
    stories_waiting_for_an_answer: int = 0
    # Stories whose model answer never came and were structured from the source
    # instead. A large number here is the LLM path being unavailable, which is a
    # fact about the system rather than about the news.
    gave_up_waiting: int = 0


@dataclass
class AwaitedStory:
    story: object
    asked_at_ns: int


class NewsTextStructurer:
    """Asks a model to read each story once, and structures it either way."""

    def __init__(
        self,
        wait_for_the_model_seconds: float,
        stories_asked_about_per_tick: int,
        maximum_sentences: int,
        now_ns=time.time_ns,
    ) -> None:
        if wait_for_the_model_seconds <= 0:
            raise ValueError(
                "the wait must be positive: it is how long a story is held for a model "
                "answer before the source's own fields are used instead, and zero would "
                "mean the model is never given a chance to answer"
            )
        if stories_asked_about_per_tick < 1:
            raise ValueError(
                "asking about no stories per tick is a part that never asks anything"
            )
        self._wait_ns = int(wait_for_the_model_seconds * NANOSECONDS_PER_SECOND)
        self._per_tick = stories_asked_about_per_tick
        self._maximum_sentences = maximum_sentences
        self._now_ns = now_ns
        self._to_ask: dict[str, object] = {}
        self._awaited: dict[str, AwaitedStory] = {}
        self.standing = StructurerStanding()

    def observe_story(self, story) -> None:
        """One distinct story. Queued to be asked about, once."""
        self.standing.stories_seen += 1
        story_key = str(getattr(story, "story_key", "") or "")
        if not story_key:
            return
        if story_key in self._awaited or story_key in self._to_ask:
            # A story restated -- another outlet, another instrument key -- is
            # the same text. Paying a second model call to read it again is the
            # waste this check exists to prevent.
            return
        self._to_ask[story_key] = story

    def requests_for_this_tick(self) -> tuple:
        """The next few stories to ask about, oldest first.

        Oldest first because a backlog read newest-first never reaches the
        bottom, and the bottom is where the stories this system has never
        structured are.
        """
        keys = sorted(
            self._to_ask,
            key=lambda key: int(
                getattr(self._to_ask[key], "earliest_published_at_ns", 0) or 0
            ),
        )[: self._per_tick]
        requests = []
        for key in keys:
            story = self._to_ask.pop(key)
            self._awaited[key] = AwaitedStory(story=story, asked_at_ns=self._now_ns())
            requests.append(self._request_for(story))
        self.standing.requests_made += len(requests)
        self.standing.stories_waiting_for_an_answer = len(self._awaited)
        return tuple(requests)

    def _request_for(self, story):
        """One request, carrying the story as the facts its answer is checked against."""
        return make_request(
            purpose=PURPOSE,
            # A story is not about a venue and not about one symbol; those are
            # what `news-symbol-resolver` works out from the names. Left empty
            # rather than filled with a symbol this part guessed.
            venue_id="",
            symbol="",
            instruction=INSTRUCTION,
            facts={
                "story_key": story.story_key,
                "headline": story.title,
                "body": story.body,
            },
            maximum_sentences=self._maximum_sentences,
            now_ns=self._now_ns,
            asked_by=PART_ID,
            # Stated, so `prompt-template-author` writes this purpose's template
            # from this part's own question. The shared venue/symbol/text shape
            # carries no `story_key`, and without it no answer can be matched.
            output_schema=OUTPUT_SCHEMA,
        )

    def observe_answer(self, output) -> StructuredNewsItem | None:
        """One validated model answer. The structured item it is about."""
        if str(getattr(output, "purpose", "")) != PURPOSE:
            return None
        self.standing.answers_read += 1
        value = getattr(output, "value", None)
        value = value if isinstance(value, dict) else {}
        story_key = str(value.get("story_key") or "")
        awaited = self._awaited.pop(story_key, None)
        self.standing.stories_waiting_for_an_answer = len(self._awaited)
        if awaited is None:
            self.standing.answers_for_a_story_not_awaited += 1
            return None

        tense = self._tense_of(value.get("tense"))
        self.standing.structured_by_a_model += 1
        return self._structured(
            awaited.story,
            event_phrase=str(value.get("event_phrase") or "").strip(),
            names_mentioned=_split(value.get("names_mentioned")),
            figures_cited=_split(value.get("figures_cited")),
            tense=tense,
            read_by=ReadBy.A_MODEL,
        )

    def _tense_of(self, stated) -> Tense:
        try:
            return Tense(str(stated))
        except ValueError:
            # The enforcer's `one_of` should have refused this already. Counted
            # rather than trusted, because a tense outside the vocabulary
            # arriving here means the schema and the enforcer disagree -- which
            # is exactly the class of defect this block has already been bitten
            # by twice.
            self.standing.answers_with_an_unusable_tense += 1
            return Tense.NOT_STATED

    def stories_the_model_did_not_answer(self) -> tuple[StructuredNewsItem, ...]:
        """Stories waited on long enough, structured from the source's own fields."""
        now = self._now_ns()
        gave_up = [
            key
            for key, awaited in self._awaited.items()
            if now - awaited.asked_at_ns >= self._wait_ns
        ]
        structured = []
        for key in gave_up:
            awaited = self._awaited.pop(key)
            self.standing.gave_up_waiting += 1
            self.standing.structured_from_the_source_only += 1
            structured.append(
                self._structured(
                    awaited.story,
                    # The source's own headline, not a summary of it: this part
                    # has not read the text and must not appear to have.
                    event_phrase=str(getattr(awaited.story, "title", "") or ""),
                    names_mentioned=(),
                    figures_cited=figures_in(
                        f"{getattr(awaited.story, 'title', '')} "
                        f"{getattr(awaited.story, 'body', '')}"
                    ),
                    tense=Tense.NOT_STATED,
                    read_by=ReadBy.THE_SOURCE_ITSELF,
                )
            )
        self.standing.stories_waiting_for_an_answer = len(self._awaited)
        return tuple(structured)

    def _structured(
        self, story, event_phrase, names_mentioned, figures_cited, tense, read_by
    ) -> StructuredNewsItem:
        return StructuredNewsItem(
            story_key=story.story_key,
            source_ids=tuple(story.source_ids),
            url=story.urls[0] if story.urls else "",
            headline=story.title,
            body=story.body,
            event_phrase=event_phrase,
            names_mentioned=names_mentioned,
            # What the source itself said this story is about, carried straight
            # through. Independent of whether a model ever read the text.
            source_instrument_keys=tuple(getattr(story, "instrument_keys", ()) or ()),
            figures_cited=figures_cited,
            tense=tense,
            read_by=read_by,
            published_at_ns=story.earliest_published_at_ns,
            first_observed_at_ns=story.first_observed_at_ns,
            structured_at_ns=self._now_ns(),
        )

    @property
    def stories_queued(self) -> int:
        return len(self._to_ask)

    @property
    def stories_awaited(self) -> int:
        return len(self._awaited)


def _split(stated) -> tuple[str, ...]:
    """A semicolon-separated field as a tuple, with the blanks dropped."""
    if not stated:
        return ()
    return tuple(
        part.strip() for part in str(stated).split(LIST_SEPARATOR) if part.strip()
    )


def figures_in(text: str) -> tuple[str, ...]:
    """Every figure a plain scan finds, as written.

    Only for the no-model path. A scan finds "1,298.20" and "12%"; it does not
    know which of them matters, and it does not pretend to -- which is precisely
    why an item structured this way is marked as such.
    """
    found = [match.group(0).strip() for match in FIGURE_IN_TEXT.finditer(text or "")]
    return tuple(dict.fromkeys(figure for figure in found if any(c.isdigit() for c in figure)))


def describe_structuring(structurer: NewsTextStructurer) -> dict:
    standing = structurer.standing
    return {
        "part_id": PART_ID,
        "stories_seen": standing.stories_seen,
        "requests_made": standing.requests_made,
        "structured_by_a_model": standing.structured_by_a_model,
        "structured_from_the_source_only": standing.structured_from_the_source_only,
        "answers_read": standing.answers_read,
        "answers_for_a_story_not_awaited": standing.answers_for_a_story_not_awaited,
        "answers_with_an_unusable_tense": standing.answers_with_an_unusable_tense,
        "stories_queued": structurer.stories_queued,
        "stories_waiting_for_an_answer": structurer.stories_awaited,
        "gave_up_waiting": standing.gave_up_waiting,
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch

    stories = Batch(read=context.bus.reader("distinct-news-item"))
    answers = Batch(read=context.bus.reader("validated-llm-output"))
    publish_structured = context.bus.publisher_for("structured-news-item")
    publish_requests = context.bus.publisher_for("llm-request")
    structurer = NewsTextStructurer(
        wait_for_the_model_seconds=context.number("news_wait_for_the_model_seconds"),
        stories_asked_about_per_tick=int(
            context.number("news_stories_asked_about_per_tick")
        ),
        maximum_sentences=int(context.number("news_structuring_maximum_sentences")),
    )

    def tick() -> None:
        for story in stories.payloads():
            structurer.observe_story(story)

        structured = []
        for output in answers.payloads():
            item = structurer.observe_answer(output)
            if item is not None:
                structured.append(item)
        structured.extend(structurer.stories_the_model_did_not_answer())
        if structured:
            publish_structured(tuple(structured))

        requests = structurer.requests_for_this_tick()
        if requests:
            publish_requests(requests)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_structuring(structurer),
    )


__all__ = [
    "AwaitedStory",
    "INSTRUCTION",
    "LIST_SEPARATOR",
    "NewsTextStructurer",
    "OUTPUT_SCHEMA",
    "PART_DECLARATION",
    "PART_ID",
    "PURPOSE",
    "StructurerStanding",
    "describe_structuring",
    "figures_in",
    "start_part",
]
