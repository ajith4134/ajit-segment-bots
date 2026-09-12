"""news-item-deduplicator: collapse one story arriving from many outlets.

The first part below the news sources, and the one that decides what counts as
a *story* rather than as a message. Everything downstream — the structurer's
LLM read, the sentiment model, the impact forecaster — is priced per story, so
a chain without this part pays eight times for one piece of news and scores it
eight times as strongly.

## Why this part is not optional for a source with a backlog

Measured on the real Upstox news endpoint, 2026-09-12: one response for two
underlyings carried **17 rows and 14 stories** — its own `metadata.total_records`
says 14 — and the three repeats were one story returned under both instrument
keys. `broker-news-reader` preserves that repetition deliberately (it reports
what the source said, including that it said it twice); collapsing it is this
part's job.

A second, wider capture the same day — the 30 NSE option underlyings with the
most contracts against them — says the same thing at three times the size:
**42 rows and 31 stories**, its own `total_records` agreeing at 31, and 11 rows
collapsing by url.

Worse, that endpoint carries a **rolling backlog, 167.1 hours of it** on the
wider capture, and a reader sweeping 2,871 instruments comes back round to any
given name about every sixteen minutes. So the same stories arrive again every
sixteen minutes, forever. Without a memory spanning that backlog,
`raw-news-item` is an endless restatement and every part below it treats a
week-old story as breaking news each time.

## The two mechanisms, counted apart

**Same url** is arithmetic. Two rows carrying one `article_link` are one
article, and this cannot be wrong. Measured: 3 of 17 rows on the narrow
capture, 11 of 42 on the wider one.

**Same story, different outlet** is a judgement, and judgements here are
derived from data, never chosen (RL-061). Headline token overlap (Jaccard) on
the real capture separates cleanly:

    same story (3 pairs, identical headings under two keys)   1.000
    different stories (133 pairs)      max 0.600, p95 0.250, median 0.044

so the collapse threshold is the midpoint of that gap, 0.80, derived in
`news_story_headline_overlap_to_collapse` rather than picked. The 0.600 pair is
the one worth naming, because it is the reason the threshold cannot sit lower:

    SENSEX, NIFTY50 resume decline after a day's pause dragged by Reliance
        Industries, Larsen & Toubro
    SENSEX, NIFTY50 resume decline after a day's pause dragged by IT stocks

— one outlet's market wrap on two different days, six words apart and two
different facts. Collapsing those would delete a day of news. The wider capture
does not beat it: across 42 real rows the closest pair this part refuses to
collapse still scores exactly 0.600, and it collapses nothing by headline.

**One outlet cannot evidence cross-outlet collapse.** Only
`broker-news-reader` runs today, so the second mechanism is bounded by
measurement from a single source and the honest thing is to say so and count
it: `collapsed_by_headline` is reported separately from `collapsed_by_url`, so
a threshold that starts destroying distinct stories when a second source lands
is visible in the standing rather than hidden inside one total.

## What this part does not do

It does not decide whether a story is *new information* — that is
`news-novelty-scorer`, and the distinction is in the block's own design: the
deduplicator kills the same story from eight outlets, the novelty scorer kills
the story rewritten tomorrow about a fact already priced. It does not read the
text either; the title is compared as tokens, never understood.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from runtime.news_types import Collapse, DistinctNewsItem
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "news-item-deduplicator"

PART_DECLARATION = PartDeclaration(
    # Written out rather than PART_ID: the part monitor reads this declaration
    # statically, without importing the module, and a part naming itself by
    # reference reads as having no verifiable wiring (2026-09-02).
    part_id="news-item-deduplicator",
    consumes=("raw-news-item",),
    produces=("distinct-news-item", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

NANOSECONDS_PER_SECOND = 1_000_000_000

# Words that carry no story. Kept deliberately short and structural -- these
# are English function words, not a judgement about finance -- because a stop
# list tuned on one capture would be a threshold hiding inside a word list.
# Every removal here makes two headlines look MORE alike, so the risk runs
# towards over-collapsing, and that is what keeps this list minimal.
WORDS_THAT_CARRY_NO_STORY = frozenset(
    "a an and are as at be by for from has have in is of on or that the to was were with".split()
)


def story_tokens(headline: str) -> frozenset[str]:
    """The comparable words of one headline.

    Lowercased and split on anything that is not a letter or digit, so
    `Larsen & Toubro` and `larsen and toubro` share their words, and `$110/barrel`
    contributes `110` and `barrel` rather than one token nothing else can match.
    """
    words = re.findall(r"[a-z0-9]+", (headline or "").lower())
    return frozenset(word for word in words if word not in WORDS_THAT_CARRY_NO_STORY)


def headline_overlap(left: frozenset[str], right: frozenset[str]) -> float:
    """How far two headlines are the same story, 0.0 to 1.0.

    Jaccard on the tokens: shared words over all words used. Chosen over a
    sequence ratio because outlets reorder a headline's clauses far more often
    than they reword them, and a sequence measure reads a reordering as a
    different story.
    """
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


@dataclass
class Story:
    """One story as this part currently understands it."""

    story_key: str
    title: str
    body: str
    tokens: frozenset[str]
    earliest_published_at_ns: int
    first_observed_at_ns: int
    last_observed_at_ns: int
    source_ids: tuple[str, ...]
    urls: tuple[str, ...]
    instrument_keys: tuple[str, ...]
    times_seen: int


@dataclass
class DeduplicatorStanding:
    items_read: int = 0
    stories_published: int = 0
    collapsed_by_url: int = 0
    collapsed_by_headline: int = 0
    # A story already known whose earliest publish time moved earlier because
    # another outlet turned out to have had it first. Republished when it does:
    # the earliest time is the tradable one and a consumer holding the later
    # one is holding a story it thinks it saw late.
    earliest_publish_time_moved_earlier: int = 0
    stories_held: int = 0
    stories_forgotten_past_the_window: int = 0
    items_with_no_headline_and_no_url: int = 0
    # Highest overlap this part has ever seen between two headlines it decided
    # were DIFFERENT stories. Real evidence about how close the threshold is
    # to firing, in a number, rather than a promise that it is well clear.
    highest_overlap_not_collapsed: float = 0.0


class NewsItemDeduplicator:
    """Every distinct story inside a window, and which raw items were it."""

    def __init__(
        self,
        headline_overlap_to_collapse: float,
        remember_for_seconds: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < headline_overlap_to_collapse <= 1.0:
            raise ValueError(
                "headline_overlap_to_collapse is a Jaccard overlap and must sit in "
                f"(0, 1]; {headline_overlap_to_collapse!r} would collapse either "
                "nothing or everything"
            )
        if not remember_for_seconds > 0:
            raise ValueError(
                "remember_for_seconds must be positive: it is how far back this part "
                "can recognise a story it has already seen, and a source with a "
                "rolling backlog re-delivers the same story until it falls out of "
                "that backlog"
            )
        self._overlap_to_collapse = headline_overlap_to_collapse
        self._remember_for_ns = int(remember_for_seconds * NANOSECONDS_PER_SECOND)
        self._now_ns = now_ns
        self._by_url: dict[str, str] = {}
        self._stories: dict[str, Story] = {}
        # token -> the story keys whose headline uses it. Candidate selection,
        # not an answer: comparing every new item against every story held is
        # quadratic in the number of stories, and at 2,871 instruments with a
        # six-day backlog that is tens of thousands of comparisons a tick on a
        # part whose whole block shares twelve cores.
        self._story_keys_by_token: dict[str, set[str]] = {}
        self.standing = DeduplicatorStanding()

    def observe(self, item) -> DistinctNewsItem | None:
        """One raw item. The story it is, if that story is newly stated.

        None when the item is another sighting of a story already published
        and nothing about it changed — a consumer that heard it once does not
        need to hear it again, and a restatement is exactly what this part
        exists to remove.
        """
        self.standing.items_read += 1
        observed_at_ns = int(getattr(item, "observed_at_ns", 0) or self._now_ns())
        self._forget_past_the_window(observed_at_ns)

        url = str(getattr(item, "url", "") or "")
        title = str(getattr(item, "title", "") or "")
        tokens = story_tokens(title)
        if not url and not tokens:
            # Neither an identity nor a headline: nothing here can be matched
            # to anything, and admitting it would create a new story for every
            # empty row a source sends. Counted so a source that starts
            # sending them is visible.
            self.standing.items_with_no_headline_and_no_url += 1
            return None

        known_key = self._by_url.get(url) if url else None
        collapse = Collapse.SAME_URL if known_key else Collapse.FIRST_SIGHTING
        if known_key is None:
            known_key, collapse = self._story_matching_headline(tokens)

        if known_key is None:
            return self._remember_new_story(item, url, title, tokens, observed_at_ns)

        if collapse is Collapse.SAME_URL:
            self.standing.collapsed_by_url += 1
        elif collapse is Collapse.SAME_STORY_DIFFERENT_OUTLET:
            self.standing.collapsed_by_headline += 1
        return self._add_sighting(self._stories[known_key], item, url, observed_at_ns)

    def _story_matching_headline(
        self, tokens: frozenset[str]
    ) -> tuple[str | None, Collapse]:
        """The story this headline is, where one is close enough to be it."""
        best_key: str | None = None
        best_overlap = 0.0
        runner_up = 0.0
        for candidate_key in self._candidate_story_keys(tokens):
            overlap = headline_overlap(tokens, self._stories[candidate_key].tokens)
            if overlap > best_overlap:
                runner_up = best_overlap
                best_key, best_overlap = candidate_key, overlap
            elif overlap > runner_up:
                runner_up = overlap
        if best_key is not None and best_overlap >= self._overlap_to_collapse:
            # The best match collapsed; the closest one that did NOT is the
            # evidence worth keeping about how near this threshold runs.
            self._note_overlap_not_collapsed(runner_up)
            return best_key, Collapse.SAME_STORY_DIFFERENT_OUTLET
        self._note_overlap_not_collapsed(best_overlap)
        return None, Collapse.FIRST_SIGHTING

    def _note_overlap_not_collapsed(self, overlap: float) -> None:
        if overlap > self.standing.highest_overlap_not_collapsed:
            self.standing.highest_overlap_not_collapsed = overlap

    def _candidate_story_keys(self, tokens: frozenset[str]) -> set[str]:
        """Stories sharing at least one word with this headline.

        Nothing is excluded that could have matched: Jaccard above zero
        requires a shared token, so a story this shortlist misses could not
        have cleared any threshold above zero anyway.
        """
        candidates: set[str] = set()
        for token in tokens:
            candidates.update(self._story_keys_by_token.get(token, ()))
        return candidates

    def _remember_new_story(
        self, item, url: str, title: str, tokens: frozenset[str], observed_at_ns: int
    ) -> DistinctNewsItem:
        published_at_ns = int(getattr(item, "published_at_ns", 0) or observed_at_ns)
        # The url where there is one, so the key is the same string a human
        # can follow; the headline's own words otherwise, so a source that
        # carries no link still gets a stable identity rather than a counter
        # that changes across a restart.
        story_key = url or "headline:" + " ".join(sorted(tokens))
        story = Story(
            story_key=story_key,
            title=title,
            body=str(getattr(item, "body", "") or ""),
            tokens=tokens,
            earliest_published_at_ns=published_at_ns,
            first_observed_at_ns=observed_at_ns,
            last_observed_at_ns=observed_at_ns,
            source_ids=(str(getattr(item, "source_id", "") or ""),),
            urls=(url,) if url else (),
            instrument_keys=_instrument_keys_of(item),
            times_seen=1,
        )
        self._stories[story_key] = story
        if url:
            self._by_url[url] = story_key
        for token in tokens:
            self._story_keys_by_token.setdefault(token, set()).add(story_key)
        self.standing.stories_held = len(self._stories)
        return self._published(story, Collapse.FIRST_SIGHTING)

    def _add_sighting(
        self, story: Story, item, url: str, observed_at_ns: int
    ) -> DistinctNewsItem | None:
        """Fold one more sighting into a story already known."""
        story.times_seen += 1
        story.last_observed_at_ns = observed_at_ns
        source_id = str(getattr(item, "source_id", "") or "")
        restated = True
        if source_id and source_id not in story.source_ids:
            story.source_ids += (source_id,)
            restated = False
        if url:
            if url not in story.urls:
                story.urls += (url,)
                restated = False
            self._by_url[url] = story.story_key
        for instrument_key in _instrument_keys_of(item):
            if instrument_key not in story.instrument_keys:
                story.instrument_keys += (instrument_key,)
                restated = False

        published_at_ns = int(getattr(item, "published_at_ns", 0) or observed_at_ns)
        if published_at_ns < story.earliest_published_at_ns:
            # Another outlet had it first. The earliest time is the tradable
            # one, so the story is restated rather than left holding a stamp
            # that makes this system look later to it than it was.
            story.earliest_published_at_ns = published_at_ns
            self.standing.earliest_publish_time_moved_earlier += 1
            restated = False

        if restated:
            return None
        return self._published(story, Collapse.SAME_STORY_DIFFERENT_OUTLET)

    def _published(self, story: Story, collapsed_by: Collapse) -> DistinctNewsItem:
        self.standing.stories_published += 1
        return DistinctNewsItem(
            story_key=story.story_key,
            title=story.title,
            body=story.body,
            earliest_published_at_ns=story.earliest_published_at_ns,
            first_observed_at_ns=story.first_observed_at_ns,
            source_ids=story.source_ids,
            urls=story.urls,
            instrument_keys=story.instrument_keys,
            times_seen=story.times_seen,
            collapsed_by=collapsed_by,
        )

    def _forget_past_the_window(self, now_ns: int) -> None:
        """Drop stories older than the window, so this part is bounded.

        Keyed on when the story was last *seen*, not when it was published: a
        source's backlog re-delivers a six-day-old story and forgetting it
        while it is still arriving would publish it again as new, which is the
        exact failure this part exists to prevent.
        """
        cutoff = now_ns - self._remember_for_ns
        expired = [
            key
            for key, story in self._stories.items()
            if story.last_observed_at_ns < cutoff
        ]
        for key in expired:
            story = self._stories.pop(key)
            for url in story.urls:
                if self._by_url.get(url) == key:
                    del self._by_url[url]
            for token in story.tokens:
                holders = self._story_keys_by_token.get(token)
                if holders is not None:
                    holders.discard(key)
                    if not holders:
                        del self._story_keys_by_token[token]
        if expired:
            self.standing.stories_forgotten_past_the_window += len(expired)
            self.standing.stories_held = len(self._stories)

    @property
    def stories_held(self) -> int:
        return len(self._stories)


def _instrument_keys_of(item) -> tuple[str, ...]:
    """The instrument key an item arrived under, where the source gave one."""
    key = getattr(item, "returned_under_instrument_key", None)
    return (str(key),) if key else ()


def describe_deduplication(deduplicator: NewsItemDeduplicator) -> dict:
    standing = deduplicator.standing
    return {
        "part_id": PART_ID,
        "items_read": standing.items_read,
        "stories_published": standing.stories_published,
        "collapsed_by_url": standing.collapsed_by_url,
        "collapsed_by_headline": standing.collapsed_by_headline,
        "earliest_publish_time_moved_earlier": (
            standing.earliest_publish_time_moved_earlier
        ),
        "stories_held": deduplicator.stories_held,
        "stories_forgotten_past_the_window": (
            standing.stories_forgotten_past_the_window
        ),
        "items_with_no_headline_and_no_url": standing.items_with_no_headline_and_no_url,
        "highest_overlap_not_collapsed": standing.highest_overlap_not_collapsed,
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch

    items = Batch(read=context.bus.reader("raw-news-item"))
    publish_stories = context.bus.publisher_for("distinct-news-item")
    deduplicator = NewsItemDeduplicator(
        headline_overlap_to_collapse=context.number(
            "news_story_headline_overlap_to_collapse"
        ),
        remember_for_seconds=context.number("news_story_remembered_for_seconds"),
    )

    def tick() -> None:
        stories = []
        for item in items.payloads():
            story = deduplicator.observe(item)
            if story is not None:
                stories.append(story)
        if stories:
            publish_stories(tuple(stories))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_deduplication(deduplicator),
    )


__all__ = [
    "DeduplicatorStanding",
    "NewsItemDeduplicator",
    "PART_DECLARATION",
    "PART_ID",
    "Story",
    "WORDS_THAT_CARRY_NO_STORY",
    "describe_deduplication",
    "headline_overlap",
    "start_part",
    "story_tokens",
]
