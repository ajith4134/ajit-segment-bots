"""broker-news-reader, against a real Upstox news response (RL-063).

The fixture is one authenticated call this project really made on 2026-09-12 --
17 items for RELIANCE and HDFCBANK, kept verbatim. No test here reaches the
broker: the transport is injected, and the URL builder is checked separately so
the shape of the request is asserted without issuing one.

**This part is the head of the whole news feature.** 29 parts are declared in
`stock-market-news-data`, five ran, and nothing produced `raw-news-item` at all,
so every part below a source was starved at the top of the chain. That is why
the source was built before any of the fourteen parts that read from it.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from parts.stock_market_news_data.broker_news_reader import (
    MAXIMUM_INSTRUMENT_KEYS_PER_REQUEST, PART_DECLARATION, SOURCE_ID,
    BrokerNewsReader, describe_reading, request_url_for,
)
from runtime.news_types import RawNewsItem, published_at_ns_from_milliseconds
from runtime.part_declaration import load_declaration_from_blueprint

CAPTURE = (
    pathlib.Path(__file__).resolve().parents[3]
    / "tests" / "captured" / "upstox" / "2026-09-12-news-for-two-underlyings.json"
)


@pytest.fixture(scope="module")
def real_response():
    assert CAPTURE.exists(), (
        f"{CAPTURE} is missing. This runs on a response the broker really sent "
        f"(RL-063); there is no hand-written fallback."
    )
    return json.loads(CAPTURE.read_text())


def a_listing(instrument_key, instrument_type="EQ"):
    import types

    return types.SimpleNamespace(
        instrument_key=instrument_key, instrument_type=instrument_type
    )


def a_reader(response=None, keys_per_request=30, now_ns=lambda: 1_800_000_000_000_000_000):
    asked = []

    def fetch(url, token):
        asked.append({"url": url, "token": token})
        return response if response is not None else {"status": "success", "data": {}}

    reader = BrokerNewsReader(
        fetch=fetch, keys_per_request=keys_per_request,
        seconds_between_requests=0.0, now_ns=now_ns,
    )
    reader.asked = asked
    return reader


# ---- the declaration ---------------------------------------------------------

def test_the_built_declaration_equals_the_blueprint():
    assert PART_DECLARATION == load_declaration_from_blueprint("broker-news-reader")


def test_it_declares_the_token_its_endpoint_requires():
    """The blueprint said only broker-instrument-listing until 2026-09-12.

    Upstox's News API is authenticated, so a reader with the listings and no
    token can name what to ask about and cannot ask. The gap was invisible while
    the part had no source file: a declaration nothing implements is never wrong
    about what it needs.
    """
    assert "broker-token-standing" in PART_DECLARATION.consumes
    assert "broker-instrument-listing" in PART_DECLARATION.consumes


# ---- the real response -------------------------------------------------------

def test_every_item_in_a_real_response_becomes_a_raw_news_item(real_response):
    reader = a_reader()
    items = reader.items_in(real_response)

    expected = sum(len(rows) for rows in real_response["data"].values())
    assert len(items) == expected == 17
    assert all(isinstance(item, RawNewsItem) for item in items)
    assert all(item.source_id == SOURCE_ID for item in items)
    assert reader.standing.items_read == 17


def test_a_real_item_carries_the_brokers_own_fields(real_response):
    reader = a_reader()
    items = reader.items_in(real_response)

    first_key = next(iter(real_response["data"]))
    row = real_response["data"][first_key][0]
    item = next(i for i in items if i.returned_under_instrument_key == first_key)

    assert item.title == row["heading"]
    assert item.body == row["summary"]
    assert item.url == row["article_link"]
    assert item.title and item.url, "a real item must not be blank"


def test_published_time_is_read_as_milliseconds_not_nanoseconds(real_response):
    """Upstox stamps epoch MILLISECONDS; everything else here is nanoseconds.

    Read as nanoseconds a 2026 item lands in 1970, which would make the newest
    news in the system look like the oldest -- precisely backwards for a feature
    whose downstream asks what is new.
    """
    reader = a_reader()
    items = reader.items_in(real_response)

    first_key = next(iter(real_response["data"]))
    row = real_response["data"][first_key][0]
    item = next(i for i in items if i.returned_under_instrument_key == first_key)

    assert item.published_at_ns == row["published_time"] * 1_000_000
    # A sanity bound rather than an exact date: the stamp must land in this
    # decade, which is the check that catches the unit being wrong at all.
    assert 1_700_000_000_000_000_000 < item.published_at_ns < 2_000_000_000_000_000_000


def test_both_timestamps_are_present_on_every_item(real_response):
    """A replay on publish time alone hands a backtest what the live bot lacked."""
    reader = a_reader()
    for item in reader.items_in(real_response):
        assert item.published_at_ns > 0
        assert item.observed_at_ns > 0


def test_an_item_with_no_published_time_is_counted_not_dropped():
    reader = a_reader()
    items = reader.items_in({"data": {"NSE_EQ|X": [
        {"heading": "h", "summary": "s", "article_link": "u", "published_time": None},
    ]}})

    assert len(items) == 1, "an undated item is still news"
    assert reader.standing.items_with_no_published_time == 1


def test_an_undated_item_is_not_stamped_at_the_epoch():
    """Zero is 1970 and would make an undated item the oldest news in the system."""
    assert published_at_ns_from_milliseconds(None) is None
    assert published_at_ns_from_milliseconds("") is None
    assert published_at_ns_from_milliseconds("not a number") is None

    reader = a_reader()
    item = reader.items_in({"data": {"NSE_EQ|X": [
        {"heading": "h", "summary": "s", "article_link": "u"},
    ]}})[0]
    assert item.published_at_ns == item.observed_at_ns


def test_the_same_story_under_two_keys_is_reported_twice():
    """Deduplication is news-item-deduplicator's job, not this part's.

    Collapsing here would hide that the source said it twice, which is itself
    the signal that part is built to read.
    """
    row = {"heading": "one story", "summary": "s", "article_link": "u",
           "published_time": 1_776_251_261_821}
    reader = a_reader()
    items = reader.items_in({"data": {"NSE_EQ|A": [row], "NSE_EQ|B": [row]}})

    assert len(items) == 2
    assert {i.returned_under_instrument_key for i in items} == {"NSE_EQ|A", "NSE_EQ|B"}


# ---- the request -------------------------------------------------------------

def test_instrument_keys_are_percent_encoded_in_the_url():
    """The keys carry a pipe; unencoded, the query is not the query asked for."""
    url = request_url_for(["NSE_EQ|INE002A01018", "NSE_FO|51420"])

    assert "NSE_EQ%7CINE002A01018" in url
    assert "|" not in url
    assert url.startswith("https://api.upstox.com/v2/news?")
    assert "category=instrument_keys" in url


def test_more_keys_than_the_broker_accepts_is_refused_not_truncated():
    """Truncating would hide which instruments were never asked about."""
    keys = [f"NSE_EQ|K{n}" for n in range(MAXIMUM_INSTRUMENT_KEYS_PER_REQUEST + 1)]
    with pytest.raises(ValueError, match="refused rather than truncated"):
        request_url_for(keys)


def test_a_request_with_no_keys_asks_about_nothing_and_says_so():
    with pytest.raises(ValueError, match="asks about nothing"):
        request_url_for([])


def test_a_reader_cannot_be_built_above_the_brokers_cap():
    with pytest.raises(ValueError, match="not a request it would answer"):
        BrokerNewsReader(fetch=lambda url, token: {}, keys_per_request=31)


# ---- rotation ----------------------------------------------------------------

def test_it_rotates_through_every_instrument_rather_than_asking_the_first_thirty():
    """At 220 underlyings and 30 a request, the other 190 would never be read.

    And nothing would report it: items_read would climb happily on the same
    thirty instruments.
    """
    reader = a_reader(keys_per_request=3)
    for n in range(7):
        reader.observe_listing(a_listing(f"NSE_EQ|K{n}"))

    seen = set()
    for _ in range(3):
        seen.update(reader.keys_for_this_tick())

    assert seen == {f"NSE_EQ|K{n}" for n in range(7)}, (
        f"only {sorted(seen)} were ever asked about"
    )


def test_a_rotation_that_runs_off_the_end_wraps_rather_than_shrinking():
    reader = a_reader(keys_per_request=3)
    for n in range(4):
        reader.observe_listing(a_listing(f"NSE_EQ|K{n}"))

    reader.keys_for_this_tick()
    second = reader.keys_for_this_tick()

    assert len(second) == 3, "a short request forever would starve the tail"


# ---- refusals ----------------------------------------------------------------

def test_no_token_means_no_request():
    reader = a_reader()
    reader.observe_listing(a_listing("NSE_EQ|K"))

    assert reader.read(None) == ()
    assert reader.asked == []
    assert reader.standing.refused_no_token == 1


def test_no_listings_yet_is_its_own_state_not_an_empty_answer():
    """"Nothing to ask about" and "asked and got nothing" both read as zero items."""
    reader = a_reader()
    standing = describe_reading(reader)

    assert standing["waiting_for_the_first_instrument_listing"] is True
    assert reader.read("a-token") == ()
    assert reader.asked == []

    reader.observe_listing(a_listing("NSE_EQ|K"))
    assert describe_reading(reader)["waiting_for_the_first_instrument_listing"] is False


def test_a_failed_read_is_recorded_and_not_raised():
    def fails(url, token):
        raise TimeoutError("no response")

    reader = BrokerNewsReader(fetch=fails)
    reader.observe_listing(a_listing("NSE_EQ|K"))

    assert reader.read("a-token") == ()
    assert reader.standing.failures == 1
    assert "TimeoutError" in reader.standing.last_failure


def test_a_real_read_end_to_end(real_response):
    reader = a_reader(response=real_response)
    reader.observe_listing(a_listing("NSE_EQ|INE002A01018"))
    reader.observe_listing(a_listing("NSE_EQ|INE040A01034"))

    items = reader.read("a-token")

    assert len(items) == 17
    assert reader.standing.requests_made == 1
    assert reader.standing.keys_asked_about == 2
    assert reader.asked[0]["token"] == "a-token"


# ---- the two defects the first live run exposed (2026-09-12) -----------------

def test_a_contract_is_not_something_news_can_be_about():
    """95,496 of the master's 118,388 rows are CE, PE or futures contracts.

    There is no story about "NIFTY 24550 CE 08 SEP 26" -- only about NIFTY. A
    reader that asked about every listing spent 97% of its requests on
    instruments no story can name, and took hours to come back round to the
    ones that matter. Measured live before this filter: keys_known climbing
    through 7,782 toward the whole master.
    """
    import types

    reader = a_reader()
    for instrument_type in ("CE", "PE", "FUT", "F", "SG"):
        reader.observe_listing(types.SimpleNamespace(
            instrument_key=f"NSE_FO|{instrument_type}", instrument_type=instrument_type,
        ))
    assert reader.standing.keys_known == 0
    assert reader.standing.listings_that_news_cannot_be_about == 5
    assert reader.standing.waiting_for_the_first_instrument_listing is True

    reader.observe_listing(types.SimpleNamespace(
        instrument_key="NSE_EQ|INE002A01018", instrument_type="EQ"))
    reader.observe_listing(types.SimpleNamespace(
        instrument_key="NSE_INDEX|Nifty 50", instrument_type="INDEX"))
    assert reader.standing.keys_known == 2


def test_requests_are_paced_rather_than_issued_every_tick():
    """Measured live before this: 371 requests in 110 seconds, 3.37 a second.

    About 291,000 calls a day at a rate-limited endpoint. Nothing had failed
    yet, which is the shape of a problem that arrives as a ban rather than an
    error.
    """
    import types

    clock = {"now": 1_800_000_000_000_000_000}
    reader = a_reader(now_ns=lambda: clock["now"])
    reader._seconds_between_requests = 10.0
    reader.observe_listing(types.SimpleNamespace(
        instrument_key="NSE_EQ|K", instrument_type="EQ"))

    reader.read("a-token")
    assert len(reader.asked) == 1

    # Same second, and every tick in between: no second request.
    for _ in range(50):
        reader.read("a-token")
    assert len(reader.asked) == 1, "the pace must hold across ticks"
    assert reader.standing.ticks_inside_the_pace == 50

    # Ten seconds later it asks again.
    clock["now"] += 10 * 1_000_000_000
    reader.read("a-token")
    assert len(reader.asked) == 2


def test_a_tick_inside_the_pace_does_nothing_and_says_so():
    """Idle-because-paced must not read the same as idle-because-broken."""
    import types

    clock = {"now": 1_800_000_000_000_000_000}
    reader = a_reader(now_ns=lambda: clock["now"])
    reader._seconds_between_requests = 60.0
    reader.observe_listing(types.SimpleNamespace(
        instrument_key="NSE_EQ|K", instrument_type="EQ"))

    reader.read("a-token")
    reader.read("a-token")

    standing = describe_reading(reader)
    assert standing["ticks_inside_the_pace"] == 1
    assert standing["failures"] == 0


def test_a_negative_pace_is_refused():
    with pytest.raises(ValueError, match="not a pace"):
        BrokerNewsReader(fetch=lambda url, token: {}, seconds_between_requests=-1.0)
