"""news-tape-writer, reading back what it wrote.

RL-063: the items written here are the real rows Upstox delivered on 2026-09-12,
and every assertion reads them back off a real tape file through
`runtime/tape.py`'s own reader rather than trusting the writer's counters. A
writer that counts what it meant to write is exactly the shape of part this
project has been bitten by; the tape is the evidence.

`durable_tmp_path`, not pytest's `tmp_path`: `/tmp` is tmpfs on this box and
`runtime/storage_facts.require_durable_directory` refuses it outright, which is
the right refusal — a tape written to RAM proves nothing about a tape.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from parts.stock_market_news_data.broker_news_reader import (
    BrokerNewsReader,
    SOURCE_ID,
)
from parts.stock_market_news_data.news_tape_writer import (
    NEWS_TAPE_VENUE,
    NewsTapeWriter,
    SOURCE_UNKNOWN,
    describe_writing,
    payload_bytes_of,
    published_at_ns_of,
    source_id_of,
    source_published_at_ns_of,
)
from runtime.news_types import RawNewsItem
from runtime.tape import (
    StreamKind,
    day_of_timestamp_ns,
    read_payload,
    read_tape_index,
    tape_paths_for,
)

CAPTURE = (
    pathlib.Path(__file__).resolve().parents[3]
    / "tests/captured/upstox/2026-09-12-news-for-thirty-underlyings.json"
)
WRITEBACK_INTERVAL_BYTES = 1_048_576


@pytest.fixture
def captured_items() -> tuple[RawNewsItem, ...]:
    reader = BrokerNewsReader(
        fetch=lambda url, token: {}, now_ns=lambda: 1_789_223_143_338_711_172
    )
    return reader.items_in(json.loads(CAPTURE.read_text()))


def records_on(tape_root: pathlib.Path, source_id: str, day: str):
    index_path, blob_path = tape_paths_for(
        tape_root, NEWS_TAPE_VENUE, source_id, day, StreamKind.NEWS
    )
    index = read_tape_index(index_path)
    return [
        (record, json.loads(read_payload(blob_path, record))) for record in index
    ]


def test_every_real_item_is_readable_back_off_the_tape(durable_tmp_path, captured_items):
    writer = NewsTapeWriter(durable_tmp_path, WRITEBACK_INTERVAL_BYTES)
    for item in captured_items:
        writer.write(item, is_raw=True)
    writer.close()

    days = {day_of_timestamp_ns(item.observed_at_ns) for item in captured_items}
    written = [
        payload
        for day in days
        for _, payload in records_on(durable_tmp_path, SOURCE_ID, day)
    ]
    assert len(written) == len(captured_items) == 42
    assert {payload["url"] for payload in written} == {
        item.url for item in captured_items
    }


def test_both_timestamps_survive_the_round_trip(durable_tmp_path, captured_items):
    """One timestamp is not enough — the block's own design rule.

    The record's `venue_time_ns` is the source's publish time, the same slot the
    market tape gives the venue's clock; `observed_at_ns` rides inside the
    payload. A replay driven on publish time alone hands a backtest information
    the live bot did not have, which is what `lookahead-auditor` refuses.
    """
    item = captured_items[0]
    writer = NewsTapeWriter(durable_tmp_path, WRITEBACK_INTERVAL_BYTES)
    writer.write(item, is_raw=True)
    writer.close()

    day = day_of_timestamp_ns(item.observed_at_ns)
    (record, payload), = [
        row
        for row in records_on(durable_tmp_path, SOURCE_ID, day)
        if row[1]["url"] == item.url
    ]
    assert int(record["venue_time_ns"]) == item.published_at_ns
    assert int(payload["observed_at_ns"]) == item.observed_at_ns
    assert int(payload["published_at_ns"]) == item.published_at_ns
    assert record["stream_kind"] == StreamKind.NEWS


def test_a_story_is_filed_under_its_source_not_its_instrument(durable_tmp_path, captured_items):
    """One market wrap names a hundred shares; it is one story, written once."""
    writer = NewsTapeWriter(durable_tmp_path, WRITEBACK_INTERVAL_BYTES)
    for item in captured_items:
        writer.write(item, is_raw=True)
    writer.close()

    sources = sorted(path.name for path in (durable_tmp_path / NEWS_TAPE_VENUE).iterdir())
    assert sources == [SOURCE_ID]
    instrument_keys = {
        item.returned_under_instrument_key
        for item in captured_items
        if item.returned_under_instrument_key
    }
    assert len(instrument_keys) > 1, "the capture must span several instruments"
    for instrument_key in instrument_keys:
        assert not (durable_tmp_path / NEWS_TAPE_VENUE / instrument_key).exists()


def test_the_file_is_the_day_the_story_was_SEEN_not_the_day_it_was_published(
    durable_tmp_path, captured_items
):
    """The backlog carries a week of publish times, all first seen in one moment.

    Filing by publish date would scatter one fetch across eight files and claim
    this system held a story on 2026-09-05 when it first saw it on 2026-09-12 —
    which is the lookahead `lookahead-auditor` exists to refuse. The publish
    time is not lost: it is the record's `venue_time_ns`.
    """
    writer = NewsTapeWriter(durable_tmp_path, WRITEBACK_INTERVAL_BYTES)
    for item in captured_items:
        writer.write(item, is_raw=True)
    writer.close()

    files = sorted(
        path.name
        for path in (durable_tmp_path / NEWS_TAPE_VENUE / SOURCE_ID).iterdir()
        if path.name.endswith(".index")
    )
    publish_days = {day_of_timestamp_ns(item.published_at_ns) for item in captured_items}
    observation_days = {day_of_timestamp_ns(item.observed_at_ns) for item in captured_items}
    assert len(publish_days) == 8, "the real backlog spans eight days of publishing"
    assert len(observation_days) == 1, "and all of it was seen in one fetch"
    assert files == sorted(f"{day}.news.index" for day in observation_days)


def test_the_raw_and_structured_counts_are_kept_apart(durable_tmp_path, captured_items):
    """`news-item` has no producer yet; one total would read as a working chain."""
    writer = NewsTapeWriter(durable_tmp_path, WRITEBACK_INTERVAL_BYTES)
    for item in captured_items:
        writer.write(item, is_raw=True)
    writer.close()

    standing = describe_writing(writer)
    assert standing["raw_items_written"] == 42
    assert standing["news_items_written"] == 0
    assert standing["items_with_no_published_time"] == 0
    assert standing["last_failure"] is None


def test_a_news_item_whose_type_does_not_exist_yet_is_still_taped(durable_tmp_path):
    """The structurer is unbuilt, so its output is taped by shape, not by field.

    A writer that named `news-item`'s fields would be asserting a shape nobody
    has built. This one keeps whatever it is handed, which is what lets the tape
    be complete on the day the structurer lands.
    """
    writer = NewsTapeWriter(durable_tmp_path, WRITEBACK_INTERVAL_BYTES)
    a_future_shape = {
        "source_id": "some-structurer",
        "published_at_ns": 1_789_000_000_000_000_000,
        "observed_at_ns": 1_789_000_600_000_000_000,
        "segments": ["index-options", "stock-options"],
        "category": "results",
    }
    writer.write(a_future_shape, is_raw=False)
    writer.close()

    day = day_of_timestamp_ns(a_future_shape["observed_at_ns"])
    (record, payload), = records_on(durable_tmp_path, "some-structurer", day)
    assert payload == a_future_shape
    assert int(record["venue_time_ns"]) == a_future_shape["published_at_ns"]
    assert describe_writing(writer)["news_items_written"] == 1
    assert describe_writing(writer)["raw_items_written"] == 0


def test_an_item_naming_no_source_is_kept_under_a_name_that_is_not_a_source(durable_tmp_path):
    """Dropping it would lose real news; filing it as a source would count it."""
    writer = NewsTapeWriter(durable_tmp_path, WRITEBACK_INTERVAL_BYTES)
    writer.write(
        {
            "published_at_ns": 1_789_000_000_000_000_000,
            "observed_at_ns": 1_789_000_600_000_000_000,
            "url": "x",
        },
        is_raw=True,
    )
    writer.close()
    assert (durable_tmp_path / NEWS_TAPE_VENUE / SOURCE_UNKNOWN).is_dir()


def test_an_item_with_no_publish_time_is_counted_and_not_stamped_1970(durable_tmp_path):
    """A record stamped 1970 sorts before everything and replays first forever."""
    writer = NewsTapeWriter(durable_tmp_path, WRITEBACK_INTERVAL_BYTES)
    item = RawNewsItem(
        source_id="a-source",
        url="https://example.invalid/undated",
        title="t",
        body="",
        published_at_ns=0,
        observed_at_ns=1_789_000_000_000_000_000,
    )
    writer.write(item, is_raw=True)
    writer.close()
    assert describe_writing(writer)["items_with_no_published_time"] == 1
    # Falls back to the observation, so the record lands on the day it was seen.
    day = day_of_timestamp_ns(item.observed_at_ns)
    (record, _), = records_on(durable_tmp_path, "a-source", day)
    assert int(record["venue_time_ns"]) == item.observed_at_ns


def test_one_tape_per_source_and_they_are_all_closed(durable_tmp_path, captured_items):
    writer = NewsTapeWriter(durable_tmp_path, WRITEBACK_INTERVAL_BYTES)
    writer.write(captured_items[0], is_raw=True)
    writer.write({"source_id": "another-source", "published_at_ns": 1}, is_raw=False)
    assert writer.open_tapes == 2
    writer.close()


def test_the_publish_time_of_a_structured_item_prefers_the_earliest_seen():
    """`distinct-news-item` carries `earliest_published_at_ns`, the tradable one."""
    fields = {"earliest_published_at_ns": 42, "observed_at_ns": 99}
    assert published_at_ns_of(fields, fields) == 42
    assert source_published_at_ns_of(fields) == 42
    # No stamp from the source: the observation stands in for the record, but the
    # source is still counted as having dated nothing.
    assert published_at_ns_of({}, {"observed_at_ns": 99}) == 99
    assert source_published_at_ns_of({"observed_at_ns": 99}) == 0
    assert published_at_ns_of({}, {}) == 0


def test_a_payload_that_is_neither_a_dataclass_nor_a_mapping_is_still_kept():
    """The tape holds the only copy; an unexpected shape must not be dropped."""
    encoded = json.loads(payload_bytes_of(object()))
    assert "repr" in encoded
    assert source_id_of(object()) == SOURCE_UNKNOWN
