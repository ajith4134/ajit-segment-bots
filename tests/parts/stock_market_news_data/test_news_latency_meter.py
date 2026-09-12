"""news-latency-meter against the real Upstox news capture.

RL-063: the items here are the 17 rows this project's own token fetched on
2026-09-12, decoded by the reader that produces them. Their latencies are
therefore real and they are enormous — the newest of the 14 stories was
published 2026-09-11 14:27 UTC and first seen 21.5 hours later, and the
response's own backlog spanned 153.3 hours.

That is the finding this part exists to state: **the broker's news API is a
digest, not a wire.** A suite built on an invented fixture with plausible
two-second latencies would have hidden it, which is precisely why fixtures are
refused here.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from parts.stock_market_news_data.broker_news_reader import (
    BrokerNewsReader,
    SOURCE_ID,
)
from parts.stock_market_news_data.news_latency_meter import (
    NewsLatencyMeter,
    describe_metering,
)
from runtime.news_types import RawNewsItem

CAPTURE = (
    pathlib.Path(__file__).resolve().parents[3]
    / "tests/captured/upstox/2026-09-12-news-for-two-underlyings.json"
)

NANOSECONDS_PER_SECOND = 1_000_000_000
# As `settings/runtime.example.toml` derives them.
WINDOW = 500
MINIMUM_OBSERVATIONS = 20


@pytest.fixture(scope="module")
def captured_items() -> tuple[RawNewsItem, ...]:
    response = json.loads(CAPTURE.read_text())
    # The moment the real call was made, so `observed_at_ns` is the real
    # observation time rather than a number this test chose: 2026-09-12
    # 11:58:53 UTC, the capture's own mtime and its manifest date.
    observed_at_ns = 1_789_214_333_000_000_000
    reader = BrokerNewsReader(fetch=lambda url, token: {}, now_ns=lambda: observed_at_ns)
    return reader.items_in(response)


def a_meter(started_at_ns: int, minimum_observations: int = MINIMUM_OBSERVATIONS):
    return NewsLatencyMeter(
        started_at_ns=started_at_ns,
        window=WINDOW,
        minimum_observations=minimum_observations,
        prior_latency_seconds=0.0,
    )


def test_the_real_capture_is_a_digest_and_the_meter_says_so(captured_items):
    """Every item in the real capture was published before it was fetched."""
    latencies_hours = sorted(
        item.latency_ns / NANOSECONDS_PER_SECOND / 3600 for item in captured_items
    )
    assert latencies_hours[0] > 21.0, "the freshest real story was still hours old"
    assert latencies_hours[-1] > 170.0, "the oldest reaches back beyond a week"


def test_a_backlog_item_is_counted_and_kept_out_of_the_habit(captured_items):
    """Its latency measures when this system started, not how late the source is."""
    # Started looking after every one of these was published, which is what a
    # part starting today actually does to a six-day backlog.
    meter = a_meter(started_at_ns=captured_items[0].observed_at_ns)
    readings = [r for item in captured_items if (r := meter.observe(item))]

    assert len(readings) == 17
    assert all(r.was_already_published_when_the_meter_started for r in readings)
    assert meter.standing.backlog_items == 17
    # Nothing was learned from them, so no typical latency is stated at all --
    # rather than a 21-hour "typical" that describes only the restart.
    assert all(r.typical_latency_ns is None for r in readings)
    assert meter.typical_latency_seconds(SOURCE_ID) is None
    assert meter.standing.slowest_latency_seconds == 0.0
    assert meter.standing.fastest_latency_seconds is None


def test_an_item_published_after_the_start_teaches_the_habit(captured_items):
    """A story that arrives while the meter is watching is the real measurement."""
    started_at_ns = captured_items[0].published_at_ns - NANOSECONDS_PER_SECOND
    meter = a_meter(started_at_ns=started_at_ns, minimum_observations=1)
    fresh = [
        item for item in captured_items if item.published_at_ns > started_at_ns
    ]
    assert fresh, "the capture must contain at least one item published after the start"
    readings = [r for item in fresh if (r := meter.observe(item))]

    assert readings
    assert not any(r.was_already_published_when_the_meter_started for r in readings)
    assert meter.standing.backlog_items == 0
    stated = [r for r in readings if r.typical_latency_ns is not None]
    assert stated, "one observation is enough when the minimum is one"
    assert meter.typical_latency_seconds(SOURCE_ID) > 0


def test_no_typical_is_stated_below_the_minimum_observations(captured_items):
    """Rule 8: not measured is its own answer, never a plausible default."""
    started_at_ns = captured_items[0].published_at_ns - NANOSECONDS_PER_SECOND
    meter = a_meter(started_at_ns=started_at_ns, minimum_observations=1000)
    readings = [
        r
        for item in captured_items
        if item.published_at_ns > started_at_ns and (r := meter.observe(item))
    ]
    assert readings
    assert all(r.typical_latency_ns is None for r in readings)
    assert all(r.observations > 0 for r in readings)


def test_the_typical_latency_is_the_median_of_what_was_observed():
    """Measured, not assumed: five real-shaped gaps, and the middle one wins."""
    started_at_ns = 0
    meter = a_meter(started_at_ns=started_at_ns, minimum_observations=5)
    for seconds in (2, 900, 60, 7200, 300):
        published_at_ns = 10 * NANOSECONDS_PER_SECOND
        meter.observe(
            RawNewsItem(
                source_id="a-source",
                url=f"https://example.invalid/{seconds}",
                title="t",
                body="",
                published_at_ns=published_at_ns,
                observed_at_ns=published_at_ns + seconds * NANOSECONDS_PER_SECOND,
            )
        )
    assert meter.typical_latency_seconds("a-source") == pytest.approx(300.0)


def test_each_source_is_measured_separately():
    """Twenty seconds is late for a filing feed and early for a digest."""
    meter = a_meter(started_at_ns=0, minimum_observations=1)
    for source_id, seconds in (("fast-wire", 2), ("slow-digest", 36_000)):
        published_at_ns = 10 * NANOSECONDS_PER_SECOND
        meter.observe(
            RawNewsItem(
                source_id=source_id,
                url=f"https://example.invalid/{source_id}",
                title="t",
                body="",
                published_at_ns=published_at_ns,
                observed_at_ns=published_at_ns + seconds * NANOSECONDS_PER_SECOND,
            )
        )
    assert meter.typical_latency_seconds("fast-wire") == pytest.approx(2.0)
    assert meter.typical_latency_seconds("slow-digest") == pytest.approx(36_000.0)
    assert meter.standing.sources_measured == 2


def test_an_item_published_in_the_future_is_reported_not_clamped():
    """Clamping would hide exactly the clock skew that matters."""
    meter = a_meter(started_at_ns=0, minimum_observations=1)
    published_at_ns = 100 * NANOSECONDS_PER_SECOND
    reading = meter.observe(
        RawNewsItem(
            source_id="a-source-whose-clock-runs-ahead",
            url="https://example.invalid/ahead",
            title="t",
            body="",
            published_at_ns=published_at_ns,
            observed_at_ns=published_at_ns - 5 * NANOSECONDS_PER_SECOND,
        )
    )
    assert reading.latency_ns == -5 * NANOSECONDS_PER_SECOND
    assert meter.standing.items_published_in_the_future == 1
    # Excluded from the habit: a negative number is not evidence about speed.
    assert meter.typical_latency_seconds("a-source-whose-clock-runs-ahead") is None


def test_an_item_that_names_no_source_cannot_be_attributed():
    meter = a_meter(started_at_ns=0)
    assert (
        meter.observe(
            RawNewsItem(
                source_id="",
                url="https://example.invalid/unattributed",
                title="t",
                body="",
                published_at_ns=1,
                observed_at_ns=2,
            )
        )
        is None
    )
    assert meter.standing.items_with_no_source == 1
    assert meter.standing.sources_measured == 0


def test_an_item_missing_either_timestamp_yields_no_reading():
    meter = a_meter(started_at_ns=0)
    for published, observed in ((0, 5), (5, 0)):
        assert (
            meter.observe(
                RawNewsItem(
                    source_id="a-source",
                    url="https://example.invalid/x",
                    title="t",
                    body="",
                    published_at_ns=published,
                    observed_at_ns=observed,
                )
            )
            is None
        )
    assert meter.standing.readings_published == 0


def test_the_standing_reports_what_it_measured(captured_items):
    meter = a_meter(started_at_ns=captured_items[0].observed_at_ns)
    for item in captured_items:
        meter.observe(item)
    standing = describe_metering(meter)
    assert standing["part_id"] == "news-latency-meter"
    assert standing["items_read"] == 17
    assert standing["readings_published"] == 17
    assert standing["backlog_items"] == 17
    assert standing["sources_measured"] == 1
    assert standing["items_published_in_the_future"] == 0


@pytest.mark.parametrize("window, minimum", [(0, 20), (500, 0), (-1, 20)])
def test_a_setting_that_cannot_work_is_refused_at_construction(window, minimum):
    with pytest.raises(ValueError):
        NewsLatencyMeter(
            started_at_ns=0,
            window=window,
            minimum_observations=minimum,
            prior_latency_seconds=0.0,
        )
