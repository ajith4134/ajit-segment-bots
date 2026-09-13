"""What a news item is, before and after this system has understood it.

The `stock-market-news-data` feature is 29 parts and, until 2026-09-12, five of
them ran and **nothing produced `raw-news-item` at all** — so the whole chain
below the sources was starved at its head. Building any downstream part before a
source would have achieved nothing, which is why the source came first and why
this type exists now rather than earlier.

`raw-news-item` is the blueprint's own description, made real:

> One item exactly as its source delivered it: source id, url, title, body,
> published_at_ns as the source stamped it, observed_at_ns as this system first
> saw it. Both times, always -- replaying on publish time alone hands a backtest
> information the live bot did not have.

That last sentence is the whole reason two timestamps are mandatory rather than
convenient, and it is enforced here rather than left to each reader.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

# Epoch milliseconds to nanoseconds. Upstox stamps `published_time` in
# milliseconds; this project reasons in nanoseconds everywhere else, and mixing
# the two is the timestamp trap `clock-skew-monitor` exists to catch.
NANOSECONDS_PER_MILLISECOND = 1_000_000


@dataclass(frozen=True)
class RawNewsItem:
    """One news item exactly as its source delivered it.

    **Both timestamps, always.** `published_at_ns` is when the source says the
    item was published; `observed_at_ns` is when this system first saw it. A
    replay driven on publish time alone hands a backtest information the live
    bot did not have — the item is dated 09:15 and did not reach this machine
    until 09:47, and a strategy that traded it at 09:15 never existed.

    Nothing here is interpreted. The title and body are the source's own words,
    unparsed and unsummarised: `news-text-structurer` is the part that reads
    them, and doing it here would put an LLM in the fetch path.
    """

    source_id: str
    url: str
    title: str
    body: str
    published_at_ns: int
    observed_at_ns: int
    # The instrument key the source returned this item under, where it returned
    # one. Carried rather than resolved: `news-symbol-resolver` resolves the
    # instruments an item NAMES, against the broker's own listings, and that is
    # a different question from which key it arrived under. A broker returning
    # an item under RELIANCE does not make the item about RELIANCE only.
    returned_under_instrument_key: str | None = None

    @property
    def latency_ns(self) -> int:
        """How long this item took to reach this system.

        Negative is possible and is not corrected: a source whose clock runs
        ahead of this machine's is a fact `news-latency-meter` should see, and
        clamping it to zero would hide exactly the skew that matters.
        """
        return self.observed_at_ns - self.published_at_ns


def published_at_ns_from_milliseconds(published_time_ms) -> int | None:
    """A source's millisecond stamp as nanoseconds, or None if it said nothing.

    None rather than zero, and the difference matters: zero is 1970 and would
    make every undated item look like the oldest news in the system, which is
    precisely backwards for a part whose job is to find what is new.
    """
    if published_time_ms in (None, ""):
        return None
    try:
        return int(published_time_ms) * NANOSECONDS_PER_MILLISECOND
    except (TypeError, ValueError):
        return None


class Delivery(enum.StrEnum):
    """Whether a news source is still delivering. T-5: countable, from a
    vocabulary, so a board can render every value and none of them is a
    default.

    `NOT_MEASURED` exists because a source that has never delivered is a
    different fact from one that has gone quiet, and Rule 8 refuses to render
    the first as the second. A dead RSS feed reads exactly like a quiet news
    day unless something separates them, and that separation is this enum.
    """

    NOT_MEASURED = "not-measured"
    DELIVERING = "delivering"
    # Quiet for longer than this source's own measured gap between items --
    # a finding, not yet a fault. Indian market news genuinely stops
    # overnight, and a monitor that called that a dead feed would fire every
    # night.
    QUIETER_THAN_USUAL = "quieter-than-usual"
    NOT_DELIVERING = "not-delivering"


class Collapse(enum.StrEnum):
    """Why a raw item did not become a new distinct story.

    Two mechanisms, counted apart on purpose. Collapsing by url is
    arithmetic -- the same article reached this system twice. Collapsing by
    headline is a judgement, and a judgement that fires on two genuinely
    different stories destroys news rather than deduplicating it, so it must
    be countable separately from the one that cannot be wrong.
    """

    FIRST_SIGHTING = "first-sighting"
    SAME_URL = "same-url"
    SAME_STORY_DIFFERENT_OUTLET = "same-story-different-outlet"


@dataclass(frozen=True)
class DistinctNewsItem:
    """One story, with the same story from every other outlet collapsed in.

    **`earliest_published_at_ns` is the tradable time.** Eight outlets carry
    one story and the eighth is not news by the time it arrives; what the
    system can act on is dated by whoever published first, so that is the
    stamp this type carries and the later ones are dropped rather than
    averaged.

    `first_observed_at_ns` is kept beside it and is deliberately *not*
    updated as the story is seen again: it is when THIS system first had the
    information, which is the only honest input to a replay
    (`lookahead-auditor` refuses the other reading).
    """

    story_key: str
    title: str
    body: str
    earliest_published_at_ns: int
    first_observed_at_ns: int
    source_ids: tuple[str, ...]
    urls: tuple[str, ...]
    instrument_keys: tuple[str, ...]
    times_seen: int
    collapsed_by: Collapse

    @property
    def outlets(self) -> int:
        """How many sources carried this one story."""
        return len(self.source_ids)


@dataclass(frozen=True)
class NewsLatencyReading:
    """How late this system saw one item, and how late is normal for its source.

    Not only telemetry: `news-impact-forecaster` discounts a late item,
    because the move is already gone. "Late" only means anything against a
    source's own habit -- twenty seconds is late for an exchange filing feed
    and early for a daily news digest -- so the source's measured typical
    latency travels with the reading rather than living in a threshold
    somewhere downstream.

    `typical_latency_ns` is None until the source has been observed enough
    times to state one (Rule 8: an unmeasured typical is not zero).
    """

    source_id: str
    story_key: str
    published_at_ns: int
    observed_at_ns: int
    latency_ns: int
    typical_latency_ns: int | None
    observations: int
    # True when the item was published before this meter started looking. Its
    # latency then measures when this system was switched on, not how late the
    # source is, and averaging the two together would make every restart look
    # like a news outage.
    was_already_published_when_the_meter_started: bool


@dataclass(frozen=True)
class NewsSourceStanding:
    """Whether one news source is still delivering, measured.

    `typical_seconds_between_items` is learned from that source's own
    arrivals rather than set: a broker news API that carries two stories a
    day per name and an exchange filing feed that carries none for hours and
    then forty cannot share a silence bound, and a bound that suited one
    would fire permanently on the other.
    """

    source_id: str
    delivery: Delivery
    items_seen: int
    last_item_observed_at_ns: int | None
    seconds_since_last_item: float | None
    typical_seconds_between_items: float | None
    observed_at_ns: int


class Tense(enum.StrEnum):
    """When the thing an item describes happened. T-5: countable, from a vocabulary.

    The blueprint asks a structured item for "the claim's tense", and it decides
    whether an item is tradable at all: a results announcement that HAPPENED is a
    fact the market is pricing now, the same announcement SCHEDULED for Thursday
    is a date, and an ANTICIPATED one is somebody's opinion about Thursday.
    Collapsing the three is how a bot trades a rumour as though it were a filing.
    """

    HAPPENED = "happened"
    SCHEDULED = "scheduled"
    ANTICIPATED = "anticipated"
    # The text does not say. Its own state rather than a guess at `happened`,
    # which is the reading that costs money (Rule 8).
    NOT_STATED = "not-stated"


class ReadBy(enum.StrEnum):
    """What turned an item's prose into fields.

    A field a model read and a field a fallback filled are not the same
    evidence, and a consumer that cannot tell them apart is weighing a guess as
    though it were a reading. `news-text-structurer` is the block's single LLM
    read; when no model answers it still structures what the source itself
    stated, and says which happened.
    """

    A_MODEL = "a-model-read-the-text"
    THE_SOURCE_ITSELF = "the-source-own-fields-only-no-model-answered"


class NewsCategory(enum.StrEnum):
    """What kind of news an item is. Routing depends on it: a halt is not an
    analyst note, and the blueprint names these eight."""

    RESULTS = "results"
    CORPORATE_ACTION = "corporate-action"
    REGULATORY = "regulatory"
    MACRO = "macro"
    HALT = "halt"
    ANALYST = "analyst"
    RUMOUR = "rumour"
    BLOCK_DEAL = "block-deal"
    # Nothing in the text places it in any of the eight. Kept rather than forced
    # into the nearest one: a misrouted halt is worse than an unrouted item.
    UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class StructuredNewsItem:
    """One item read into fields: what happened, to whom, the figures, the tense.

    `names_mentioned` is deliberately unresolved text — the names as the article
    wrote them. Turning "Reliance Industries" into `NSE_EQ|INE002A01018` is
    `news-symbol-resolver`'s job, against the broker's own listings, because an
    alias table written here would be the hardcoded table the blueprint refuses.
    """

    story_key: str
    source_ids: tuple[str, ...]
    url: str
    headline: str
    body: str
    # What happened, in the words the reader selected from the item itself.
    event_phrase: str
    # The names the text mentions, as written. Unresolved on purpose.
    names_mentioned: tuple[str, ...]
    # The figures the text states, as written -- "$110/barrel", "12%". Strings
    # rather than floats: a figure's unit is part of it, and parsing one into a
    # number here would throw away the difference between 12% and 12 rupees.
    figures_cited: tuple[str, ...]
    tense: Tense
    read_by: ReadBy
    published_at_ns: int
    first_observed_at_ns: int
    structured_at_ns: int

    @property
    def was_read_by_a_model(self) -> bool:
        return self.read_by is ReadBy.A_MODEL


@dataclass(frozen=True)
class NewsSymbolTagging:
    """Which instruments an item names, resolved against the broker's own master.

    `underlying_symbols` are trading symbols from the instrument listing —
    never an alias table written by hand, which is the one thing the blueprint
    says about this part. `names_not_resolved` is carried beside them because a
    name nothing matched is the evidence that the resolver is missing something,
    and an empty tagging with no explanation is indistinguishable from an item
    about nothing.
    """

    story_key: str
    underlying_symbols: tuple[str, ...]
    instrument_keys: tuple[str, ...]
    names_not_resolved: tuple[str, ...]
    # True when the item names an index rather than a company. The segment
    # classifier routes on it, and the two are different markets.
    names_an_index: bool
    resolved_at_ns: int

    @property
    def names_anything_tradable(self) -> bool:
        return bool(self.underlying_symbols)


@dataclass(frozen=True)
class NewsCategoryTagging:
    """What kind of news an item is, and how sure that is.

    `confidence` is the share of the evidence that pointed at the winning
    category, measured from the item's own words rather than declared. An
    unclassified item carries 0.0 and says so: a category picked at random routes
    a halt to an analyst-note reader.
    """

    story_key: str
    category: NewsCategory
    confidence: float
    # The words in the item that put it in that category. Provenance, so a
    # classification can be argued with rather than only believed (Rule 8).
    matched_words: tuple[str, ...]
    classified_at_ns: int


@dataclass(frozen=True)
class NewsCredibilityRating:
    """How far a source has earned trust, learned from later confirmation.

    `is_fitted` is False until the source has been observed enough times to say
    anything, and then `rating` is the prior rather than a measurement — which is
    exactly the distinction a consumer weighting by credibility has to see.
    """

    source_id: str
    rating: float
    observations: int
    is_fitted: bool
    confirmed: int
    contradicted: int
    rated_at_ns: int


@dataclass(frozen=True)
class NewsItem:
    """The published news fact. What every consumer outside the news block reads.

    Assembled rather than decided: the structured item, the instruments it names,
    the segments it belongs to, its category and its source's credibility, each
    from the part that owns that question. `news-segment-classifier` puts them
    together because the segment tagging is the last thing added and the one that
    makes "divided by segments" real.
    """

    story_key: str
    structured: StructuredNewsItem
    symbols: NewsSymbolTagging
    category: NewsCategoryTagging
    # None until `news-credibility-scorer` has anything to say about the source.
    # None rather than a default number: a consumer weighting by credibility must
    # be able to tell "unrated" from "rated badly".
    credibility: NewsCredibilityRating | None
    segments: tuple[str, ...]
    published_at_ns: int
    first_observed_at_ns: int
    assembled_at_ns: int

    @property
    def belongs_to_a_built_segment(self) -> bool:
        return bool(self.segments)


__all__ = [
    "Collapse",
    "Delivery",
    "NewsCategory",
    "NewsCategoryTagging",
    "NewsCredibilityRating",
    "NewsItem",
    "NewsSymbolTagging",
    "ReadBy",
    "StructuredNewsItem",
    "Tense",
    "DistinctNewsItem",
    "NANOSECONDS_PER_MILLISECOND",
    "NewsLatencyReading",
    "NewsSourceStanding",
    "RawNewsItem",
    "published_at_ns_from_milliseconds",
]
