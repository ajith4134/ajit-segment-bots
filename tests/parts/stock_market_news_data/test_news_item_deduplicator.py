"""news-item-deduplicator against the real Upstox news capture.

RL-063: real data, never invented fixtures. Every item fed to this part here is
a row this project's own broker token fetched from Upstox on 2026-09-12, kept
verbatim in `tests/captured/upstox/2026-09-12-news-for-two-underlyings.json` —
17 rows whose own `metadata.total_records` says 14, because three stories were
returned under both instrument keys.

The number 14 is what makes this suite worth something: the broker itself
states how many distinct articles it sent, so the deduplicator's answer can be
checked against the source's own count rather than against a count this test
made up.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from parts.stock_market_news_data.broker_news_reader import (
    BrokerNewsReader,
    SOURCE_ID,
)
from parts.stock_market_news_data.news_item_deduplicator import (
    NewsItemDeduplicator,
    describe_deduplication,
    headline_overlap,
    story_tokens,
)
from runtime.news_types import Collapse, RawNewsItem

CAPTURE = (
    pathlib.Path(__file__).resolve().parents[3]
    / "tests/captured/upstox/2026-09-12-news-for-two-underlyings.json"
)

# The threshold as `settings/runtime.example.toml` derives it, so this suite
# exercises the value the running part is actually given.
OVERLAP_TO_COLLAPSE = 0.8
REMEMBER_FOR_SECONDS = 691200.0


@pytest.fixture(scope="module")
def captured_response() -> dict:
    return json.loads(CAPTURE.read_text())


@pytest.fixture
def captured_items(captured_response) -> tuple[RawNewsItem, ...]:
    """The capture as `raw-news-item`s, decoded by the reader that produces them.

    Decoded through `BrokerNewsReader.items_in` rather than by re-reading the
    JSON here: a test that parsed the fields itself would be checking its own
    parser and would keep passing if the reader's changed.
    """
    reader = BrokerNewsReader(fetch=lambda url, token: {}, now_ns=lambda: 1_000)
    return reader.items_in(captured_response)


def a_deduplicator(now_ns=lambda: 1_000_000_000) -> NewsItemDeduplicator:
    return NewsItemDeduplicator(
        headline_overlap_to_collapse=OVERLAP_TO_COLLAPSE,
        remember_for_seconds=REMEMBER_FOR_SECONDS,
        now_ns=now_ns,
    )


def test_the_capture_is_the_shape_this_suite_claims(captured_response, captured_items):
    assert captured_response["metadata"]["page"]["total_records"] == 14
    assert len(captured_items) == 17


def test_it_finds_exactly_the_number_of_stories_the_broker_says_it_sent(captured_items):
    """The broker's own total_records is the answer key."""
    deduplicator = a_deduplicator()
    published = [
        story for item in captured_items if (story := deduplicator.observe(item))
    ]
    assert deduplicator.stories_held == 14
    # 17 statements of 14 stories: 14 first sightings, and 3 restatements of a
    # story already known, because each of those three arrived under a SECOND
    # instrument key and which instruments a story arrived under is part of the
    # story. A restatement that added nothing would return None instead.
    assert len(published) == 17
    assert len({story.story_key for story in published}) == 14
    assert deduplicator.standing.collapsed_by_url == 3
    assert deduplicator.standing.collapsed_by_headline == 0


def test_a_second_sweep_of_the_same_backlog_publishes_nothing_new(captured_items):
    """The endpoint carries a rolling backlog; the reader re-delivers it.

    Measured 2026-09-12: 153.3 hours of backlog and a sweep that comes back
    round every sixteen minutes. If a second sweep produced stories again,
    every part below this one would treat a six-day-old story as breaking news
    four times an hour.
    """
    deduplicator = a_deduplicator()
    for item in captured_items:
        deduplicator.observe(item)
    first_pass = deduplicator.standing.stories_published

    published_again = [
        story for item in captured_items if (story := deduplicator.observe(item))
    ]
    assert published_again == []
    assert deduplicator.standing.stories_published == first_pass
    assert deduplicator.stories_held == 14
    assert deduplicator.standing.items_read == 34


def test_the_same_story_under_two_instrument_keys_collapses_and_keeps_both_keys(
    captured_items,
):
    deduplicator = a_deduplicator()
    stories = [story for item in captured_items if (story := deduplicator.observe(item))]
    with_two_keys = [story for story in stories if len(story.instrument_keys) == 2]
    assert len(with_two_keys) == 3
    for story in with_two_keys:
        assert story.times_seen == 2
        assert story.collapsed_by is Collapse.SAME_STORY_DIFFERENT_OUTLET
        assert set(story.instrument_keys) == {
            "NSE_EQ|INE002A01018",
            "NSE_EQ|INE040A01034",
        }


def test_every_story_carries_the_source_that_delivered_it(captured_items):
    deduplicator = a_deduplicator()
    stories = [story for item in captured_items if (story := deduplicator.observe(item))]
    assert stories
    for story in stories:
        assert story.source_ids == (SOURCE_ID,)
        assert story.outlets == 1


def test_the_closest_two_distinct_real_headlines_do_not_collapse(captured_items):
    """The 0.600 pair, which is why the threshold cannot sit lower.

    'SENSEX, NIFTY50 resume decline after a day's pause dragged by Reliance
    Industries, Larsen & Toubro' against '... dragged by IT stocks': one
    outlet's market wrap on two different days. Collapsing them deletes a day
    of news.
    """
    wraps = [
        item
        for item in captured_items
        if item.title.startswith("SENSEX, NIFTY50 resume decline")
    ]
    assert len(wraps) == 2
    overlap = headline_overlap(
        story_tokens(wraps[0].title), story_tokens(wraps[1].title)
    )
    assert overlap == pytest.approx(0.6, abs=0.001)
    assert overlap < OVERLAP_TO_COLLAPSE

    deduplicator = a_deduplicator()
    first = deduplicator.observe(wraps[0])
    second = deduplicator.observe(wraps[1])
    assert first is not None and second is not None
    assert first.story_key != second.story_key
    assert deduplicator.standing.collapsed_by_headline == 0
    assert deduplicator.standing.highest_overlap_not_collapsed == pytest.approx(0.6)


def test_the_same_story_from_a_second_outlet_collapses_into_one(captured_items):
    """The mechanism one outlet cannot evidence, exercised on a real headline.

    A second outlet is simulated by changing only the source and the url — the
    headline is a real one from the capture, not a string written to pass.
    """
    original = captured_items[0]
    from_another_outlet = RawNewsItem(
        source_id="some-other-outlet",
        url="https://example.invalid/a-different-link-to-the-same-story",
        title=original.title,
        body=original.body,
        published_at_ns=original.published_at_ns - 60 * 1_000_000_000,
        observed_at_ns=original.observed_at_ns + 1,
        returned_under_instrument_key=original.returned_under_instrument_key,
    )
    deduplicator = a_deduplicator()
    first = deduplicator.observe(original)
    second = deduplicator.observe(from_another_outlet)

    assert first is not None and second is not None
    assert deduplicator.stories_held == 1
    assert deduplicator.standing.collapsed_by_headline == 1
    assert second.story_key == first.story_key
    assert second.outlets == 2
    assert set(second.source_ids) == {SOURCE_ID, "some-other-outlet"}
    assert len(second.urls) == 2


def test_the_earliest_publish_time_wins_because_it_is_the_tradable_one(captured_items):
    original = captured_items[0]
    a_minute_earlier = original.published_at_ns - 60 * 1_000_000_000
    earlier_outlet = RawNewsItem(
        source_id="some-other-outlet",
        url="https://example.invalid/earlier",
        title=original.title,
        body=original.body,
        published_at_ns=a_minute_earlier,
        observed_at_ns=original.observed_at_ns + 1,
    )
    deduplicator = a_deduplicator()
    deduplicator.observe(original)
    restated = deduplicator.observe(earlier_outlet)

    assert restated is not None
    assert restated.earliest_published_at_ns == a_minute_earlier
    assert deduplicator.standing.earliest_publish_time_moved_earlier == 1
    # First observation is when THIS system first had it, and does not move.
    assert restated.first_observed_at_ns == original.observed_at_ns


def test_a_later_duplicate_does_not_move_the_publish_time_or_republish(captured_items):
    original = captured_items[0]
    later_outlet = RawNewsItem(
        source_id=original.source_id,
        url=original.url,
        title=original.title,
        body=original.body,
        published_at_ns=original.published_at_ns + 3600 * 1_000_000_000,
        observed_at_ns=original.observed_at_ns + 1,
        returned_under_instrument_key=original.returned_under_instrument_key,
    )
    deduplicator = a_deduplicator()
    first = deduplicator.observe(original)
    assert deduplicator.observe(later_outlet) is None
    assert deduplicator.standing.collapsed_by_url == 1
    assert (
        deduplicator._stories[first.story_key].earliest_published_at_ns
        == original.published_at_ns
    )


def test_a_story_is_forgotten_only_after_the_window_past_its_last_sighting(
    captured_items,
):
    """Keyed on last seen, not published: the backlog re-delivers old stories."""
    clock = {"now": captured_items[0].observed_at_ns}
    deduplicator = a_deduplicator(now_ns=lambda: clock["now"])
    for item in captured_items:
        deduplicator.observe(item)
    assert deduplicator.stories_held == 14

    a_week_and_a_day_later = RawNewsItem(
        source_id=SOURCE_ID,
        url="https://example.invalid/much-later",
        title="A headline sharing nothing with the capture",
        body="",
        published_at_ns=captured_items[0].observed_at_ns,
        observed_at_ns=captured_items[0].observed_at_ns
        + int((REMEMBER_FOR_SECONDS + 86400) * 1_000_000_000),
    )
    deduplicator.observe(a_week_and_a_day_later)
    assert deduplicator.standing.stories_forgotten_past_the_window == 14
    assert deduplicator.stories_held == 1


def test_an_item_with_neither_a_url_nor_a_headline_is_counted_not_admitted():
    deduplicator = a_deduplicator()
    empty = RawNewsItem(
        source_id=SOURCE_ID,
        url="",
        title="",
        body="a body with no headline and no link",
        published_at_ns=1,
        observed_at_ns=2,
    )
    assert deduplicator.observe(empty) is None
    assert deduplicator.standing.items_with_no_headline_and_no_url == 1
    assert deduplicator.stories_held == 0


def test_a_source_with_no_url_still_gets_a_stable_story_key():
    """A key that changed across a restart would republish every story."""
    item = RawNewsItem(
        source_id="a-feed-with-no-links",
        url="",
        title="RBI holds the repo rate at 5.50%",
        body="",
        published_at_ns=1,
        observed_at_ns=2,
    )
    first = a_deduplicator().observe(item)
    second = a_deduplicator().observe(item)
    assert first.story_key == second.story_key
    assert first.story_key.startswith("headline:")


def test_the_token_index_does_not_hold_a_forgotten_story(captured_items):
    """A structure that only grows is the shape this project has been bitten by."""
    clock = {"now": captured_items[0].observed_at_ns}
    deduplicator = a_deduplicator(now_ns=lambda: clock["now"])
    for item in captured_items:
        deduplicator.observe(item)
    assert deduplicator._story_keys_by_token

    far_later = RawNewsItem(
        source_id=SOURCE_ID,
        url="https://example.invalid/far-later",
        title="Unrelated wording entirely",
        body="",
        published_at_ns=captured_items[0].observed_at_ns,
        observed_at_ns=captured_items[0].observed_at_ns
        + int((REMEMBER_FOR_SECONDS + 1) * 1_000_000_000),
    )
    deduplicator.observe(far_later)
    held = set(deduplicator._stories)
    for token, keys in deduplicator._story_keys_by_token.items():
        assert keys <= held, token
    assert deduplicator._by_url.keys() == {far_later.url}


def test_the_standing_reports_what_it_measured(captured_items):
    deduplicator = a_deduplicator()
    for item in captured_items:
        deduplicator.observe(item)
    standing = describe_deduplication(deduplicator)
    assert standing["part_id"] == "news-item-deduplicator"
    assert standing["items_read"] == 17
    assert standing["stories_held"] == 14
    assert standing["collapsed_by_url"] == 3
    assert standing["collapsed_by_headline"] == 0
    assert standing["highest_overlap_not_collapsed"] == pytest.approx(0.6)


@pytest.mark.parametrize(
    "overlap, remember",
    [(0.0, 60.0), (1.5, 60.0), (0.8, 0.0), (0.8, -1.0)],
)
def test_a_setting_that_cannot_work_is_refused_at_construction(overlap, remember):
    with pytest.raises(ValueError):
        NewsItemDeduplicator(
            headline_overlap_to_collapse=overlap, remember_for_seconds=remember
        )


def test_function_words_do_not_make_two_headlines_look_alike():
    """Every removal makes headlines look MORE alike, so the list stays minimal."""
    assert story_tokens("The bank is in the news") == frozenset({"bank", "news"})
    assert headline_overlap(story_tokens("and the of"), story_tokens("in on at")) == 0.0


def test_headline_overlap_of_nothing_is_zero_not_one():
    assert headline_overlap(frozenset(), frozenset()) == 0.0
    assert headline_overlap(frozenset({"nifty"}), frozenset()) == 0.0


# --- the wider capture: 30 real NSE option underlyings, 42 rows, 31 stories ---

CAPTURE_OF_THIRTY_UNDERLYINGS = (
    pathlib.Path(__file__).resolve().parents[3]
    / "tests/captured/upstox/2026-09-12-news-for-thirty-underlyings.json"
)


@pytest.fixture(scope="module")
def wider_capture() -> dict:
    return json.loads(CAPTURE_OF_THIRTY_UNDERLYINGS.read_text())


@pytest.fixture
def wider_items(wider_capture) -> tuple[RawNewsItem, ...]:
    reader = BrokerNewsReader(fetch=lambda url, token: {}, now_ns=lambda: 1_000)
    return reader.items_in(wider_capture)


def test_the_wider_capture_also_finds_exactly_what_the_broker_says_it_sent(
    wider_capture, wider_items
):
    """Three times the rows, the same answer key: the broker's own count."""
    assert wider_capture["metadata"]["page"]["total_records"] == 31
    assert len(wider_items) == 42

    deduplicator = a_deduplicator()
    for item in wider_items:
        deduplicator.observe(item)
    assert deduplicator.stories_held == 31
    assert deduplicator.standing.collapsed_by_url == 11
    assert deduplicator.standing.collapsed_by_headline == 0


def test_the_threshold_still_clears_every_distinct_story_at_three_times_the_size(
    wider_items,
):
    """The evidence for 0.8 that a wider sample could have broken, and did not."""
    deduplicator = a_deduplicator()
    for item in wider_items:
        deduplicator.observe(item)
    assert deduplicator.standing.highest_overlap_not_collapsed == pytest.approx(0.6)
    assert deduplicator.standing.highest_overlap_not_collapsed < OVERLAP_TO_COLLAPSE


def test_the_measured_backlog_still_fits_inside_the_remembered_window(wider_items):
    """The window is derived from this span, so the two must not cross.

    167.08 hours measured against a 192-hour (8 day) memory. The first
    derivation of that setting was 7 days off the narrower capture and left
    0.92 hours of margin, which is why this assertion exists rather than a note.
    """
    stamps = sorted(item.published_at_ns for item in wider_items)
    backlog_seconds = (stamps[-1] - stamps[0]) / 1_000_000_000
    assert backlog_seconds == pytest.approx(167.08 * 3600, rel=0.001)
    assert backlog_seconds < REMEMBER_FOR_SECONDS
